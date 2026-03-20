"""
services/gemini_client.py
Gemini 1.5 Pro 视频分析客户端

架构职责：
  · 接收视频字节流，上传到 Gemini File API，等待处理完成后调用分析
  · 指数退避重试（最多 3 次），处理网络/配额瞬时抖动
  · 分析完成后自动清理远端文件，避免占用配额
  · 返回结构化文本描述，供 VIRA Agent 流水线作为上下文输入

使用方法：
    from services.gemini_client import analyze_video, get_gemini_api_key
    result = analyze_video(video_bytes, filename="video.mp4")
"""

import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 默认视频分析 prompt：引导 Gemini 输出 VIRA Agent 流水线能直接消费的结构化描述
_DEFAULT_VIDEO_PROMPT = """你是一名专业的短视频内容分析师。请对这段视频进行深度竞品分析，输出以下结构化内容：

【视觉 Hook 分析】
- 前3秒钩子：描述开场的视觉/听觉 hook 类型和强度（0-10分）
- Hook 类型：悬念/痛点/颜值/反转/数字/权威/情绪 中的哪种
- 关键视觉元素：列举3-5个影响用户停留的核心画面元素

【内容结构】
- 情绪基调：（如：焦虑解决型 / 励志激励型 / 幽默娱乐型 等）
- 内容节奏：快切/慢剪/混合，平均镜头时长估算
- CTA 位置和类型

【转化力评估】
- 商业转化潜力（0-10分）及依据
- 产品/服务呈现方式
- 用户痛点切入角度

【合规风险】
- 是否有敏感词/违禁词
- 广告标注是否合规
- 风险等级：低/中/高

【爆款要素提炼】
- 最可复用的1个核心公式
- 与同类内容的差异化亮点
- 建议改进的最大弱点

请用中文回答，保持专业简洁。"""

# 等待 Gemini File API 处理的最长时间（秒）
_MAX_WAIT_SECONDS = 300
_POLL_INTERVAL    = 5

# 单次 generate_content 调用的 token 上限
_MAX_OUTPUT_TOKENS = 2048


def get_gemini_api_key() -> str:
    """
    优先级：
    1. Streamlit Cloud Secrets → GEMINI_API_KEY
    2. 本地环境变量 GEMINI_API_KEY
    """
    try:
        import streamlit as st
        if hasattr(st, "secrets"):
            key = st.secrets.get("GEMINI_API_KEY", "")
            if key:
                return key
    except Exception:
        pass
    return os.getenv("GEMINI_API_KEY", "")


def analyze_video(
    video_bytes: bytes,
    filename: str = "video.mp4",
    prompt: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """
    上传视频到 Gemini File API，等待处理完成后调用 Gemini 1.5 Pro 分析。

    Args:
        video_bytes:  视频文件字节流（mp4 / mov）
        filename:     原始文件名（仅用于日志和 MIME 推断）
        prompt:       分析 prompt；默认使用内置的竞品分析模板
        api_key:      Gemini API Key；None 时自动从 Secrets/环境变量读取

    Returns:
        Gemini 返回的文本分析结果

    Raises:
        ValueError:   API Key 未配置、文件处理失败或 API 响应异常
        RuntimeError: 重试 3 次后仍失败
    """
    try:
        import google.generativeai as genai
    except ImportError as e:
        raise ImportError(
            "请安装 google-generativeai：pip install google-generativeai>=0.5.0"
        ) from e

    _key = api_key or get_gemini_api_key()
    if not _key:
        raise ValueError(
            "未找到 GEMINI_API_KEY。"
            "请在 .streamlit/secrets.toml 或环境变量中配置 GEMINI_API_KEY。"
        )

    genai.configure(api_key=_key)

    _prompt = prompt or _DEFAULT_VIDEO_PROMPT

    # 推断 MIME 类型
    _suffix = Path(filename).suffix.lower()
    _mime   = "video/mp4" if _suffix in (".mp4", ".m4v") else "video/quicktime"

    uploaded_file = None
    tmp_path      = None

    for attempt in range(3):
        try:
            # ── Step 1：写入临时文件（File API 需要路径）────────────────────
            if tmp_path is None:
                with tempfile.NamedTemporaryFile(
                    suffix=_suffix or ".mp4", delete=False
                ) as _f:
                    _f.write(video_bytes)
                    tmp_path = _f.name
                logger.info(
                    "Video written to tmp: %s (%.1f MB)",
                    tmp_path, len(video_bytes) / 1024 / 1024,
                )

            # ── Step 2：上传到 Gemini File API ───────────────────────────────
            logger.info("[Gemini] Uploading video file (attempt %d)…", attempt + 1)
            uploaded_file = genai.upload_file(
                path=tmp_path,
                mime_type=_mime,
                display_name=filename,
            )
            logger.info(
                "[Gemini] Upload complete: %s (state=%s)",
                uploaded_file.name, uploaded_file.state.name,
            )

            # ── Step 3：轮询等待处理完成 ─────────────────────────────────────
            waited = 0
            while uploaded_file.state.name == "PROCESSING":
                if waited >= _MAX_WAIT_SECONDS:
                    raise TimeoutError(
                        f"Gemini 文件处理超时（>{_MAX_WAIT_SECONDS}s）"
                    )
                time.sleep(_POLL_INTERVAL)
                waited += _POLL_INTERVAL
                uploaded_file = genai.get_file(uploaded_file.name)
                logger.debug(
                    "[Gemini] Waiting for file processing: %ds elapsed, state=%s",
                    waited, uploaded_file.state.name,
                )

            if uploaded_file.state.name != "ACTIVE":
                raise ValueError(
                    f"Gemini 文件处理失败，最终状态：{uploaded_file.state.name}"
                )

            logger.info("[Gemini] File active, calling generate_content…")

            # ── Step 4：调用分析 ─────────────────────────────────────────────
            model    = genai.GenerativeModel("gemini-1.5-pro")
            response = model.generate_content(
                [uploaded_file, _prompt],
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=_MAX_OUTPUT_TOKENS,
                    temperature=0.3,
                ),
                request_options={"timeout": 120},
            )

            result_text = response.text
            logger.info(
                "[Gemini] Analysis complete: %d chars", len(result_text)
            )
            return result_text

        except Exception as e:
            logger.warning(
                "[Gemini] Attempt %d failed: %s", attempt + 1, e
            )
            # 清理已上传但失败的文件
            if uploaded_file is not None:
                try:
                    genai.delete_file(uploaded_file.name)
                except Exception:
                    pass
                uploaded_file = None

            if attempt < 2:
                _sleep = 2 ** (attempt + 1)   # 2s → 4s
                logger.info("[Gemini] Retrying in %ds…", _sleep)
                time.sleep(_sleep)
            else:
                raise RuntimeError(
                    f"Gemini 视频分析失败（已重试 3 次）：{e}"
                ) from e

        finally:
            # ── Step 5：清理远端文件（无论成功/失败）─────────────────────────
            if uploaded_file is not None and attempt == 2:
                pass  # 最后一次失败已在上面清理
            elif uploaded_file is not None:
                try:
                    genai.delete_file(uploaded_file.name)
                    logger.info(
                        "[Gemini] Remote file deleted: %s", uploaded_file.name
                    )
                except Exception as _de:
                    logger.debug("[Gemini] Failed to delete remote file: %s", _de)

    # ── 清理本地临时文件 ──────────────────────────────────────────────────────
    if tmp_path and Path(tmp_path).exists():
        try:
            Path(tmp_path).unlink()
        except Exception:
            pass

    raise RuntimeError("Gemini analyze_video: unexpected exit from retry loop")
