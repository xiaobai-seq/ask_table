from __future__ import annotations

"""轻量观测封装。

TraceRecorder 提供统一的 span 结构；配置 Langfuse 时会上报，不配置时仅在本地
记录事件，避免观测依赖影响核心链路。
"""

import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class TraceEvent:
    """一个节点或操作的观测事件。"""

    name: str
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    elapsed_ms: float = 0.0


class TraceRecorder:
    """可选 Langfuse 的本地 trace 记录器。"""

    def __init__(self) -> None:
        self.trace_id = uuid.uuid4().hex
        self.events: list[TraceEvent] = []
        self._langfuse = self._try_langfuse()

    @contextmanager
    def span(self, name: str, **input_payload: Any) -> Iterator[TraceEvent]:
        # 用 contextmanager 包住操作，可以统一记录耗时和异常。
        event = TraceEvent(name=name, input=input_payload)
        start = time.perf_counter()
        try:
            yield event
        except Exception as exc:
            event.error = str(exc)
            raise
        finally:
            event.elapsed_ms = (time.perf_counter() - start) * 1000
            self.events.append(event)
            self._flush_langfuse(event)

    def _try_langfuse(self):
        """只有密钥齐全且依赖可 import 时才启用 Langfuse。"""

        if not os.getenv("LANGFUSE_PUBLIC_KEY") or not os.getenv("LANGFUSE_SECRET_KEY"):
            return None
        try:  # pragma: no cover - optional dependency
            from langfuse import Langfuse
        except Exception:  # pragma: no cover
            return None
        return Langfuse()

    def _flush_langfuse(self, event: TraceEvent) -> None:
        """尽力上报观测数据；失败不影响主请求。"""

        if not self._langfuse:
            return
        try:  # pragma: no cover - optional service dependency
            self._langfuse.trace(id=self.trace_id, name="text2sql").span(
                name=event.name,
                input=event.input,
                output=event.output,
                metadata={"elapsed_ms": event.elapsed_ms, "error": event.error},
            )
        except Exception:
            pass
