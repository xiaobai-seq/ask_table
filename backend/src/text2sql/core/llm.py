from __future__ import annotations

"""LLM provider 抽象。

SQL 生成和结果总结都只依赖 complete(prompt)。默认不启用 LLM，
只有 TEXT2SQL_USE_LLM=1 且配置 DASHSCOPE_API_KEY 时才接入 DashScope。
"""

import asyncio
import os
from typing import Protocol


class LLMProvider(Protocol):
    async def complete(self, prompt: str) -> str:
        ...


class DashScopeLLMProvider:
    """DashScope/Qwen 同步 SDK 的 async 包装。"""

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or os.getenv("DASHSCOPE_LLM_MODEL", "qwen-plus")
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")

    async def complete(self, prompt: str) -> str:
        # DashScope SDK 是同步调用，放到线程里避免阻塞 async workflow。
        return await asyncio.to_thread(self._complete_sync, prompt)

    def _complete_sync(self, prompt: str) -> str:
        try:  # pragma: no cover - optional network dependency
            import dashscope
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("dashscope is not installed") from exc

        if self.api_key:
            dashscope.api_key = self.api_key
        response = dashscope.Generation.call(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "你是严谨的企业级 Text2SQL 与数据分析助手。",
                },
                {"role": "user", "content": prompt},
            ],
            result_format="message",
        )
        if getattr(response, "status_code", 200) != 200:
            raise RuntimeError(f"DashScope generation failed: {response}")
        output = response.get("output", {})
        choices = output.get("choices", [])
        if not choices:
            return ""
        return choices[0]["message"]["content"]


def default_llm_provider() -> LLMProvider | None:
    """默认关闭 LLM，让项目开箱即可跑测试；显式环境变量才启用。"""

    if os.getenv("TEXT2SQL_USE_LLM") == "1" and os.getenv("DASHSCOPE_API_KEY"):
        return DashScopeLLMProvider()
    return None
