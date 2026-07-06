from __future__ import annotations

"""LLM provider 抽象。

SQL 生成和结果总结都只依赖 complete(prompt)。默认不启用 LLM，
只有 TEXT2SQL_USE_LLM=1 且配置 DASHSCOPE_API_KEY 时才接入 DashScope。
"""

import asyncio
import os
from typing import Any, Protocol


class LLMProvider(Protocol):
    async def complete(self, prompt: str) -> str:
        ...


class DashScopeLLMProvider:
    """DashScope/Qwen 同步 SDK 的 async 包装。"""

    _SYSTEM_PROMPT = "你是严谨的企业级 Text2SQL 与数据分析助手。"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_http_api_url: str | None = None,
        request_timeout_seconds: int | None = None,
    ) -> None:
        self.model = model or os.getenv("DASHSCOPE_LLM_MODEL", "qwen3.7-plus")
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
        self.base_http_api_url = base_http_api_url or os.getenv("DASHSCOPE_HTTP_BASE_URL")
        self.request_timeout_seconds = request_timeout_seconds

    async def complete(self, prompt: str) -> str:
        # DashScope SDK 是同步调用，放到线程里避免阻塞 async workflow。
        task = asyncio.to_thread(self._complete_sync, prompt)
        if self.request_timeout_seconds is None or self.request_timeout_seconds <= 0:
            return await task
        return await asyncio.wait_for(task, timeout=self.request_timeout_seconds)

    def _complete_sync(self, prompt: str) -> str:
        try:  # pragma: no cover - optional network dependency
            import dashscope
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("dashscope is not installed") from exc

        if self.api_key:
            dashscope.api_key = self.api_key
        call_kwargs = {}
        if self.base_http_api_url:
            call_kwargs["base_address"] = self.base_http_api_url
        if self.request_timeout_seconds is not None:
            call_kwargs["timeout"] = self.request_timeout_seconds
        if _uses_multimodal_generation(self.model):
            response = dashscope.MultiModalConversation.call(
                model=self.model,
                messages=[
                    {"role": "system", "content": [{"text": self._SYSTEM_PROMPT}]},
                    {"role": "user", "content": [{"text": prompt}]},
                ],
                result_format="message",
                **call_kwargs,
            )
        else:
            response = dashscope.Generation.call(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": self._SYSTEM_PROMPT,
                    },
                    {"role": "user", "content": prompt},
                ],
                result_format="message",
                **call_kwargs,
            )
        if getattr(response, "status_code", 200) != 200:
            raise RuntimeError(f"DashScope generation failed: {response}")
        output = response.get("output", {})
        choices = output.get("choices", [])
        if not choices:
            return ""
        return _content_to_text(choices[0]["message"]["content"])


def _uses_multimodal_generation(model: str) -> bool:
    normalized = model.lower()
    return normalized.startswith("qwen3.7") or "-vl" in normalized


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


def default_llm_provider() -> LLMProvider | None:
    """默认关闭 LLM，让项目开箱即可跑测试；显式配置才启用。

    配置统一走 Settings（其底层仍读同名环境变量，兼容旧用法），
    缺 pydantic-settings 等异常时回退到 os.getenv，保证降级不报错。
    """

    try:
        from text2sql.config import Settings

        settings = Settings()
        if settings.use_llm and settings.dashscope_api_key:
            return DashScopeLLMProvider(
                model=settings.dashscope_llm_model,
                api_key=settings.dashscope_api_key,
                base_http_api_url=settings.dashscope_http_base_url,
                request_timeout_seconds=settings.llm_request_timeout_seconds,
            )
        return None
    except Exception:  # pragma: no cover - 配置异常时退回环境变量判断
        if os.getenv("TEXT2SQL_USE_LLM") == "1" and os.getenv("DASHSCOPE_API_KEY"):
            timeout = os.getenv("TEXT2SQL_LLM_REQUEST_TIMEOUT_SECONDS")
            return DashScopeLLMProvider(
                request_timeout_seconds=int(timeout) if timeout else None
            )
        return None
