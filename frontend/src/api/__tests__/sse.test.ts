import { describe, it, expect } from "vitest";

import { SSEParser, dispatchEvents } from "../sse";
import type { SSEEvent, NodeEventPayload, SchemaInspectorData } from "../types";

// 构造一条 SSE 文本块（含尾部空行分隔）。
function frame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

describe("SSEParser", () => {
  it("解析单条 task started 事件", () => {
    const parser = new SSEParser();
    const events = parser.push(frame("task", { task_id: "t1", status: "started" }));
    expect(events).toHaveLength(1);
    expect(events[0]).toEqual({
      type: "task",
      payload: { task_id: "t1", status: "started" },
    });
  });

  it("解析节点事件并保留 node 与 data 负载", () => {
    const parser = new SSEParser();
    const payload = {
      task_id: "t1",
      node: "sql_generator",
      data: { sql_plan: { sql: "SELECT 1" }, generated_sql: "SELECT 1", chart_type: "bar" },
    };
    const events = parser.push(frame("sql_generator", payload));
    expect(events).toHaveLength(1);
    const ev = events[0];
    expect(ev.type).toBe("sql_generator");
    const np = ev.payload as NodeEventPayload<"sql_generator">;
    expect(np.node).toBe("sql_generator");
    expect(np.data.generated_sql).toBe("SELECT 1");
  });

  it("跨 chunk 切分的事件能被正确缓冲拼接", () => {
    const parser = new SSEParser();
    const full = frame("task", { task_id: "t1", status: "started" });
    const mid = Math.floor(full.length / 2);
    const first = parser.push(full.slice(0, mid));
    expect(first).toHaveLength(0); // 半条事件，尚不可解析
    const second = parser.push(full.slice(mid));
    expect(second).toHaveLength(1);
    expect(second[0].type).toBe("task");
  });

  it("一次 push 含多条事件全部解析", () => {
    const parser = new SSEParser();
    const text =
      frame("task", { task_id: "t1", status: "started" }) +
      frame("schema_inspector", {
        task_id: "t1",
        node: "schema_inspector",
        data: { rewritten_query: "q", db_info: [], retrieval_hits: [], clarification: null, trace_id: "tr" },
      });
    const events = parser.push(text);
    expect(events.map((e) => e.type)).toEqual(["task", "schema_inspector"]);
  });

  it("解析 cancelled 与 error 特殊事件", () => {
    const parser = new SSEParser();
    const events = parser.push(
      frame("cancelled", { task_id: "t1", cancelled: true }) +
        frame("error", { task_id: "t1", code: "internal", message: "boom", trace_id: "tr" }),
    );
    expect(events[0]).toEqual({ type: "cancelled", payload: { task_id: "t1", cancelled: true } });
    expect(events[1].type).toBe("error");
  });

  it("schema_inspector 携带 clarification 时保留澄清字段", () => {
    const parser = new SSEParser();
    const events = parser.push(
      frame("schema_inspector", {
        task_id: "t1",
        node: "schema_inspector",
        data: {
          rewritten_query: "q",
          db_info: [],
          retrieval_hits: [],
          clarification: { question: "指哪个时间范围?", options: ["本月", "今年"], reason: "时间不明确" },
          trace_id: "tr",
        },
      }),
    );
    const data = (events[0].payload as NodeEventPayload<"schema_inspector">).data as SchemaInspectorData;
    expect(data.clarification?.question).toBe("指哪个时间范围?");
    expect(data.clarification?.options).toEqual(["本月", "今年"]);
  });

  it("忽略未知事件名，不抛异常", () => {
    const parser = new SSEParser();
    const events = parser.push(frame("heartbeat", { task_id: "t1" }));
    expect(events).toHaveLength(0);
  });

  it("兼容 \\r\\n 换行", () => {
    const parser = new SSEParser();
    const events = parser.push(
      `event: task\r\ndata: ${JSON.stringify({ task_id: "t1", status: "finished" })}\r\n\r\n`,
    );
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("task");
  });

  it("flush 解析无尾部空行的末条事件", () => {
    const parser = new SSEParser();
    // 末条事件缺少结尾的 \n\n：push 阶段无法切分而被缓冲。
    const noTrailing = `event: task\ndata: ${JSON.stringify({ task_id: "t1", status: "finished" })}`;
    expect(parser.push(noTrailing)).toHaveLength(0);
    // flush 收尾时应能解析出缓冲区里的最后一条事件。
    const flushed = parser.flush();
    expect(flushed).toHaveLength(1);
    expect(flushed[0]).toEqual({
      type: "task",
      payload: { task_id: "t1", status: "finished" },
    });
  });

  it("flush 空缓冲返回空数组", () => {
    expect(new SSEParser().flush()).toEqual([]);
  });

  it("非法 JSON 的事件被跳过且不影响后续事件", () => {
    const parser = new SSEParser();
    // data 不是合法 JSON：该事件被跳过而非抛异常。
    expect(parser.push(`event: task\ndata: {not valid json}\n\n`)).toHaveLength(0);
    // 后续合法事件仍能正常解析。
    const more = parser.push(frame("task", { task_id: "t1", status: "finished" }));
    expect(more).toHaveLength(1);
    expect(more[0].type).toBe("task");
  });

  it("flush 遇到非法 JSON 也安全返回空数组", () => {
    const parser = new SSEParser();
    parser.push(`event: task\ndata: {broken`);
    expect(parser.flush()).toEqual([]);
  });
});

describe("dispatchEvents", () => {
  it("按事件类型回调对应 handler", () => {
    const calls: string[] = [];
    const events: SSEEvent[] = [
      { type: "task", payload: { task_id: "t1", status: "started" } },
      {
        type: "sql_executor",
        payload: {
          task_id: "t1",
          node: "sql_executor",
          data: { execution_result: { columns: [], rows: [], row_count: 0, elapsed_ms: 1, error: null } },
        },
      },
      { type: "task", payload: { task_id: "t1", status: "finished" } },
    ];
    dispatchEvents(events, {
      onTask: (p) => calls.push(`task:${p.status}`),
      onNode: (p) => calls.push(`node:${p.node}`),
    });
    expect(calls).toEqual(["task:started", "node:sql_executor", "task:finished"]);
  });
});
