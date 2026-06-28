// SSE 客户端：POST /query 建立流式连接，按契约 §1 的 event 名分发事件。
// 设计要点：
// 1. 解析与网络分离 —— SSEParser 只做「文本块 → 强类型事件」的纯逻辑，便于单测；
//    streamQuery 负责 fetch / 流读取 / 取消等副作用。
// 2. 容错优先 —— 未知事件名、半截 JSON 不应导致整条链路崩溃。

import { API_BASE_URL } from "./config";
import {
  NODE_SEQUENCE,
  type SSEEvent,
  type NodeName,
  type TaskEventPayload,
  type NodeEventPayload,
  type CancelledEventPayload,
  type ErrorEventPayload,
} from "./types";

// 顶层已知事件名集合（task / 各节点 / cancelled / error）。
const NODE_NAMES = new Set<string>(NODE_SEQUENCE);
const TOP_LEVEL_EVENTS = new Set<string>(["task", "cancelled", "error", ...NODE_SEQUENCE]);

interface RawEvent {
  event: string;
  data: string;
}

// 把原始 {event, data(JSON 文本)} 转成强类型 SSEEvent；无法识别或解析失败时返回 null（忽略）。
function toSSEEvent(raw: RawEvent): SSEEvent | null {
  if (!TOP_LEVEL_EVENTS.has(raw.event)) {
    return null;
  }
  let payload: unknown;
  try {
    payload = raw.data ? JSON.parse(raw.data) : {};
  } catch {
    // 半截 / 非法 JSON：跳过该事件而非中断整流。
    return null;
  }

  if (raw.event === "task") {
    return { type: "task", payload: payload as TaskEventPayload };
  }
  if (raw.event === "cancelled") {
    return { type: "cancelled", payload: payload as CancelledEventPayload };
  }
  if (raw.event === "error") {
    return { type: "error", payload: payload as ErrorEventPayload };
  }
  if (NODE_NAMES.has(raw.event)) {
    return { type: raw.event as NodeName, payload: payload as NodeEventPayload };
  }
  return null;
}

// 增量 SSE 解析器：可多次 push 文本块，内部缓冲跨块的半截事件。
export class SSEParser {
  private buffer = "";

  push(chunk: string): SSEEvent[] {
    // 统一换行符，简化以空行（\n\n）切分事件块的逻辑。
    this.buffer += chunk.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    const events: SSEEvent[] = [];

    let sepIndex: number;
    while ((sepIndex = this.buffer.indexOf("\n\n")) !== -1) {
      const block = this.buffer.slice(0, sepIndex);
      this.buffer = this.buffer.slice(sepIndex + 2);
      const raw = this.parseBlock(block);
      if (!raw) continue;
      const ev = toSSEEvent(raw);
      if (ev) events.push(ev);
    }
    return events;
  }

  // 处理流结束时残留在缓冲区（无尾部空行）的最后一条事件。
  flush(): SSEEvent[] {
    const rest = this.buffer.trim();
    this.buffer = "";
    if (!rest) return [];
    const raw = this.parseBlock(rest);
    if (!raw) return [];
    const ev = toSSEEvent(raw);
    return ev ? [ev] : [];
  }

  // 解析单个事件块：收集 event: 行与（可能多行的）data: 行。
  private parseBlock(block: string): RawEvent | null {
    let event = "";
    const dataLines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith(":")) continue; // SSE 注释行
      if (line.startsWith("event:")) {
        event = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
    }
    if (!event && dataLines.length === 0) return null;
    return { event, data: dataLines.join("\n") };
  }
}

// 事件分发器：把强类型事件按类别派发到回调。组件层只需关心业务回调，无需关心 event 名匹配。
export interface SSEHandlers {
  onTask?: (payload: TaskEventPayload) => void;
  onNode?: (payload: NodeEventPayload) => void;
  onCancelled?: (payload: CancelledEventPayload) => void;
  onError?: (payload: ErrorEventPayload) => void;
}

export function dispatchEvents(events: SSEEvent[], handlers: SSEHandlers): void {
  for (const ev of events) {
    if (ev.type === "task") {
      handlers.onTask?.(ev.payload);
    } else if (ev.type === "cancelled") {
      handlers.onCancelled?.(ev.payload);
    } else if (ev.type === "error") {
      handlers.onError?.(ev.payload);
    } else {
      // 其余均为节点事件。
      handlers.onNode?.(ev.payload as NodeEventPayload);
    }
  }
}

export interface QueryRequest {
  query: string;
  session_id: string;
  task_id?: string;
}

export interface StreamQueryOptions extends SSEHandlers {
  signal?: AbortSignal;
  // 拿到响应头里的 X-Task-ID（用于后续取消）。
  onTaskId?: (taskId: string) => void;
}

// 发起 POST /query 并消费 SSE 流。返回 Promise 在流结束（finished / cancelled / error / 断流）时 resolve。
export async function streamQuery(req: QueryRequest, options: StreamQueryOptions): Promise<void> {
  const { signal, onTaskId, ...handlers } = options;

  const resp = await fetch(`${API_BASE_URL}/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(req),
    signal,
  });

  const headerTaskId = resp.headers.get("X-Task-ID");
  if (headerTaskId && onTaskId) onTaskId(headerTaskId);

  if (!resp.ok || !resp.body) {
    // 非 2xx：尝试读取结构化错误体并以 error 事件透出。
    let payload: ErrorEventPayload = {
      task_id: headerTaskId ?? req.task_id ?? "",
      code: `http_${resp.status}`,
      message: `请求失败 (HTTP ${resp.status})`,
      trace_id: "",
    };
    try {
      const body = (await resp.json()) as Partial<ErrorEventPayload>;
      payload = { ...payload, ...body };
    } catch {
      // 忽略 JSON 解析失败，使用默认错误体。
    }
    handlers.onError?.(payload);
    return;
  }

  const parser = new SSEParser();
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      const events = parser.push(decoder.decode(value, { stream: true }));
      if (events.length) dispatchEvents(events, handlers);
    }
    const tail = parser.flush();
    if (tail.length) dispatchEvents(tail, handlers);
  } catch (err) {
    // 主动取消（AbortError）属预期，不算错误。
    if (signal?.aborted) return;
    throw err;
  }
}
