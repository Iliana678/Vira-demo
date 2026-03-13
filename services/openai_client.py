"""
services/openai_client.py
封装 OpenAI API 调用：多模态支持、指数退避重试、Token 计数。
"""

import base64
import logging
import time
from typing import Optional

import tiktoken
from openai import OpenAI, RateLimitError, APIError, APIConnectionError

logger = logging.getLogger(__name__)


class OpenAIClient:
    """
    线程安全的 OpenAI 客户端封装。
    - 自动指数退避重试（最多 3 次）
    - 支持 GPT-4o Vision（base64 图片注入）
    - 记录每次调用的 Token 用量和耗时
    """

    MAX_RETRIES = 3
    BASE_DELAY = 1.5  # 首次重试等待秒数

    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self._last_usage: dict = {}

    @property
    def last_usage(self) -> dict:
        """返回最近一次 API 调用的用量统计"""
        return self._last_usage

    def chat(
        self,
        system_prompt: str,
        user_message: str,
        image_data: Optional[bytes] = None,
        max_tokens: int = 1500,
        temperature: float = 0.3,
    ) -> str:
        """
        发送 Chat Completion 请求。

        Args:
            system_prompt: Agent 的 System Prompt
            user_message:  用户消息文本
            image_data:    原始图片字节（JPEG/PNG），传入则启用 Vision
            max_tokens:    最大输出 Token 数
            temperature:   采样温度（JSON 输出任务建议 ≤0.3）

        Returns:
            模型输出的原始文本字符串

        Raises:
            RuntimeError: 超过最大重试次数后抛出
        """
        messages = [{"role": "system", "content": system_prompt}]

        # 构建用户消息内容（纯文本 or 多模态）
        if image_data:
            b64 = base64.b64encode(image_data).decode("utf-8")
            content = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{b64}",
                        "detail": "high",  # 高分辨率解析，Hook 细节更准确
                    },
                },
                {"type": "text", "text": user_message},
            ]
        else:
            content = user_message

        messages.append({"role": "user", "content": content})

        # 指数退避重试循环
        for attempt in range(self.MAX_RETRIES):
            try:
                t0 = time.perf_counter()
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                elapsed_ms = round((time.perf_counter() - t0) * 1000)

                self._last_usage = {
                    "prompt_tokens": resp.usage.prompt_tokens,
                    "completion_tokens": resp.usage.completion_tokens,
                    "total_tokens": resp.usage.total_tokens,
                    "elapsed_ms": elapsed_ms,
                    "model": self.model,
                }
                logger.info(
                    "OpenAI call OK | model=%s tokens=%d elapsed=%dms",
                    self.model,
                    self._last_usage["total_tokens"],
                    elapsed_ms,
                )
                return resp.choices[0].message.content

            except RateLimitError:
                wait = self.BASE_DELAY * (2**attempt)
                logger.warning("Rate limited. Retry %d/%d in %.1fs", attempt + 1, self.MAX_RETRIES, wait)
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(wait)
                else:
                    raise

            except (APIError, APIConnectionError) as e:
                logger.error("API error (attempt %d): %s", attempt + 1, e)
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.BASE_DELAY)
                else:
                    raise

        raise RuntimeError("OpenAI API: max retries exceeded")

    def count_tokens(self, text: str) -> int:
        """估算文本的 Token 数量（不发起 API 请求）"""
        try:
            enc = tiktoken.encoding_for_model(self.model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
