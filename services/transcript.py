"""
services/transcript.py
视频 / 音频口播提取服务

技术方案：
  · 视频  → ffmpeg-python 提取音频轨道（.mp3）→ OpenAI Whisper API 转录
  · 纯音频 → 直接送 Whisper
  · 无 ffmpeg → 降级：直接把视频二进制送 Whisper（whisper 支持 mp4）
  · 无 API Key → 返回空字符串 + 提示

输出：
  · transcript : str     完整口播文字
  · language   : str     检测到的语言
  · duration_s : float   音频时长（秒），-1 表示未知
"""

import io
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def extract_transcript(
    file_bytes: bytes,
    filename: str,
    api_key: str,
    language: Optional[str] = None,   # None = 自动检测；"zh" 强制中文
) -> dict:
    """
    从视频/音频文件中提取口播文字。

    Args:
        file_bytes : 文件原始字节
        filename   : 原始文件名（用于判断 MIME 类型）
        api_key    : OpenAI API Key
        language   : Whisper 语言代码（可选）

    Returns:
        {
            "transcript": str,
            "language":   str,
            "duration_s": float,
            "method":     "ffmpeg+whisper" | "direct+whisper" | "error",
            "error":      str  (空字符串表示成功)
        }
    """
    if not api_key:
        return _err("未提供 OpenAI API Key，无法调用 Whisper")

    ext = Path(filename).suffix.lower()  # .mp4 / .mov / .mp3 / .wav ...
    audio_bytes: Optional[bytes] = None
    audio_filename: str = filename
    method = "direct+whisper"

    # ── 尝试用 ffmpeg-python 提取音频 ─────────────────────────────────────
    if ext in (".mp4", ".mov", ".avi", ".webm", ".mkv", ".flv"):
        try:
            import ffmpeg  # type: ignore
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_in:
                tmp_in.write(file_bytes)
                tmp_in_path = tmp_in.name
            tmp_out_path = tmp_in_path.replace(ext, ".mp3")
            try:
                (
                    ffmpeg
                    .input(tmp_in_path)
                    .output(tmp_out_path, format="mp3", acodec="libmp3lame",
                            ar="16000", ac=1, audio_bitrate="64k")
                    .overwrite_output()
                    .run(quiet=True)
                )
                with open(tmp_out_path, "rb") as f:
                    audio_bytes    = f.read()
                    audio_filename = Path(filename).stem + ".mp3"
                    method         = "ffmpeg+whisper"
                logger.info("ffmpeg audio extraction OK: %s → %s", filename, audio_filename)
            finally:
                try:
                    os.unlink(tmp_in_path)
                    if os.path.exists(tmp_out_path):
                        os.unlink(tmp_out_path)
                except Exception:
                    pass
        except ImportError:
            logger.warning("ffmpeg-python not installed, sending raw video to Whisper")
        except Exception as fe:
            logger.warning("ffmpeg extraction failed (%s), falling back to raw", fe)

    # ── 调用 Whisper API ──────────────────────────────────────────────────
    payload = audio_bytes if audio_bytes is not None else file_bytes
    payload_filename = audio_filename if audio_bytes is not None else filename

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        kwargs: dict = dict(
            model="whisper-1",
            file=(payload_filename, io.BytesIO(payload), _guess_mime(payload_filename)),
            response_format="verbose_json",
        )
        if language:
            kwargs["language"] = language

        resp = client.audio.transcriptions.create(**kwargs)

        return {
            "transcript": resp.text.strip(),
            "language":   getattr(resp, "language", "auto"),
            "duration_s": round(getattr(resp, "duration", -1.0), 1),
            "method":     method,
            "error":      "",
        }
    except Exception as e:
        logger.error("Whisper API failed: %s", e)
        return _err(f"Whisper 转录失败：{e}")


def _err(msg: str) -> dict:
    return {
        "transcript": "",
        "language":   "",
        "duration_s": -1.0,
        "method":     "error",
        "error":      msg,
    }


def _guess_mime(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return {
        ".mp3": "audio/mpeg",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".webm": "video/webm",
        ".ogg": "audio/ogg",
    }.get(ext, "application/octet-stream")
