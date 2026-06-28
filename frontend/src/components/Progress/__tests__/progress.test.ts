import { describe, it, expect } from "vitest";

import {
  initialProgressState,
  reduceProgress,
  reduceProgressAll,
  NODE_LABELS,
} from "../progress";
import type { SSEEvent, NodeName } from "../../../api/types";

// 构造节点事件的辅助函数。
function nodeEvent<N extends NodeName>(node: N, data: unknown): SSEEvent {
  return { type: node, payload: { task_id: "t1", node, data } } as SSEEvent;
}

const started: SSEEvent = { type: "task", payload: { task_id: "t1", status: "started" } };
const finished: SSEEvent = { type: "task", payload: { task_id: "t1", status: "finished" } };

function status(state: ReturnType<typeof initialProgressState>, node: NodeName) {
  return state.nodes.find((n) => n.node === node)!.status;
}

describe("initialProgressState", () => {
  it("初始所有节点 pending，整体 idle", () => {
    const s = initialProgressState();
    expect(s.overall).toBe("idle");
    expect(s.activeNode).toBeNull();
    expect(s.nodes).toHaveLength(7);
    expect(s.nodes.every((n) => n.status === "pending")).toBe(true);
  });

  it("每个节点都有中文标签", () => {
    const s = initialProgressState();
    for (const n of s.nodes) {
      expect(NODE_LABELS[n.node]).toBeTruthy();
    }
  });
});

describe("reduceProgress 正常链路", () => {
  it("task started 后第一个节点点亮为 active", () => {
    const s = reduceProgress(initialProgressState(), started);
    expect(s.overall).toBe("running");
    expect(s.activeNode).toBe("schema_inspector");
    expect(status(s, "schema_inspector")).toBe("active");
  });

  it("节点事件依次完成并点亮下一个", () => {
    const events: SSEEvent[] = [
      started,
      nodeEvent("schema_inspector", { rewritten_query: "q", db_info: [], retrieval_hits: [], clarification: null, trace_id: "tr" }),
      nodeEvent("table_relationship", { table_relationship: [] }),
    ];
    const s = reduceProgressAll(initialProgressState(), events);
    expect(status(s, "schema_inspector")).toBe("done");
    expect(status(s, "table_relationship")).toBe("done");
    expect(status(s, "sql_generator")).toBe("active");
    expect(s.activeNode).toBe("sql_generator");
  });

  it("完整无修复链路 finished 后 repair 标记为 skipped", () => {
    const events: SSEEvent[] = [
      started,
      nodeEvent("schema_inspector", { rewritten_query: "q", db_info: [], retrieval_hits: [], clarification: null, trace_id: "tr" }),
      nodeEvent("table_relationship", { table_relationship: [] }),
      nodeEvent("sql_generator", { sql_plan: {}, generated_sql: "SELECT 1", chart_type: "bar" }),
      nodeEvent("sql_executor", { execution_result: { columns: [], rows: [], row_count: 0, elapsed_ms: 1, error: null } }),
      nodeEvent("summarize", { summary: "ok" }),
      nodeEvent("data_render", { render_spec: {}, chart_type: "bar" }),
      finished,
    ];
    const s = reduceProgressAll(initialProgressState(), events);
    expect(s.overall).toBe("finished");
    expect(s.activeNode).toBeNull();
    expect(status(s, "sql_repair")).toBe("skipped");
    expect(status(s, "data_render")).toBe("done");
  });
});

describe("reduceProgress 修复回环", () => {
  it("sql_repair 事件记录第 N 次并显示修复中", () => {
    const events: SSEEvent[] = [
      started,
      nodeEvent("schema_inspector", { rewritten_query: "q", db_info: [], retrieval_hits: [], clarification: null, trace_id: "tr" }),
      nodeEvent("table_relationship", { table_relationship: [] }),
      nodeEvent("sql_generator", { sql_plan: {}, generated_sql: "SELECT x", chart_type: "bar" }),
      nodeEvent("sql_repair", { attempts: 1, generated_sql: "SELECT y", sql_plan: {} }),
    ];
    const s = reduceProgressAll(initialProgressState(), events);
    const repair = s.nodes.find((n) => n.node === "sql_repair")!;
    expect(repair.status).toBe("repairing");
    expect(repair.attempts).toBe(1);
    expect(s.activeNode).toBe("sql_repair");
  });

  it("修复后执行成功，repair 标记为 done", () => {
    const events: SSEEvent[] = [
      started,
      nodeEvent("sql_generator", { sql_plan: {}, generated_sql: "SELECT x", chart_type: "bar" }),
      nodeEvent("sql_repair", { attempts: 1, generated_sql: "SELECT y", sql_plan: {} }),
      nodeEvent("sql_repair", { attempts: 2, generated_sql: "SELECT z", sql_plan: {} }),
      nodeEvent("sql_executor", { execution_result: { columns: [], rows: [], row_count: 1, elapsed_ms: 1, error: null } }),
    ];
    const s = reduceProgressAll(initialProgressState(), events);
    const repair = s.nodes.find((n) => n.node === "sql_repair")!;
    expect(repair.status).toBe("done");
    expect(repair.attempts).toBe(2);
    expect(status(s, "sql_executor")).toBe("done");
  });
});

describe("reduceProgress 特殊结束", () => {
  it("澄清：schema_inspector 返回 clarification 则链路提前结束", () => {
    const s = reduceProgressAll(initialProgressState(), [
      started,
      nodeEvent("schema_inspector", {
        rewritten_query: "q",
        db_info: [],
        retrieval_hits: [],
        clarification: { question: "?", options: ["a"], reason: "r" },
        trace_id: "tr",
      }),
    ]);
    expect(status(s, "schema_inspector")).toBe("done");
    expect(s.overall).toBe("finished");
    expect(s.activeNode).toBeNull();
    expect(status(s, "table_relationship")).toBe("pending");
  });

  it("error 事件：当前活动节点标红，整体 error", () => {
    const s = reduceProgressAll(initialProgressState(), [
      started,
      { type: "error", payload: { task_id: "t1", code: "internal", message: "boom", trace_id: "tr" } },
    ]);
    expect(s.overall).toBe("error");
    expect(status(s, "schema_inspector")).toBe("error");
    expect(s.error?.message).toBe("boom");
  });

  it("cancelled 事件：整体 cancelled，活动节点回到 pending", () => {
    const s = reduceProgressAll(initialProgressState(), [
      started,
      { type: "cancelled", payload: { task_id: "t1", cancelled: true } },
    ]);
    expect(s.overall).toBe("cancelled");
    expect(status(s, "schema_inspector")).toBe("pending");
    expect(s.activeNode).toBeNull();
  });
});
