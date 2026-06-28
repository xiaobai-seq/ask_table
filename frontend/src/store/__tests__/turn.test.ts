import { describe, it, expect } from "vitest";

import {
  createTurn,
  applyEventToTurn,
  applyEventsToTurn,
  historyTurnToChatTurn,
  enrichTurnWithDetail,
} from "../turn";
import type { SSEEvent, NodeName, HistoryTurn, HistoryDetail } from "../../api/types";

function nodeEvent<N extends NodeName>(node: N, data: unknown): SSEEvent {
  return { type: node, payload: { task_id: "t1", node, data } } as SSEEvent;
}
const started: SSEEvent = { type: "task", payload: { task_id: "t1", status: "started" } };
const finished: SSEEvent = { type: "task", payload: { task_id: "t1", status: "finished" } };

describe("createTurn", () => {
  it("初始 turn：状态 streaming，结果为空", () => {
    const t = createTurn("id1", "按月统计金额");
    expect(t.query).toBe("按月统计金额");
    expect(t.status).toBe("streaming");
    expect(t.result.summary).toBeUndefined();
    expect(t.progress.overall).toBe("idle");
  });
});

describe("applyEventToTurn 结果累积", () => {
  it("逐节点累积 rewritten/sql/chart/execution/summary/render", () => {
    const events: SSEEvent[] = [
      started,
      nodeEvent("schema_inspector", {
        rewritten_query: "2026 年各月订单金额",
        db_info: [],
        retrieval_hits: [],
        clarification: null,
        trace_id: "tr",
      }),
      nodeEvent("table_relationship", { table_relationship: [] }),
      nodeEvent("sql_generator", { sql_plan: { sql: "SELECT 1" }, generated_sql: "SELECT 1", chart_type: "line" }),
      nodeEvent("sql_executor", {
        execution_result: { columns: ["m"], rows: [{ m: 1 }], row_count: 1, elapsed_ms: 2, error: null },
      }),
      nodeEvent("summarize", { summary: "金额逐月上升" }),
      nodeEvent("data_render", { render_spec: { chart_type: "line", x: "m", y: [], series: null, title: "t", options: {} }, chart_type: "line" }),
      finished,
    ];
    const t = applyEventsToTurn(createTurn("id1", "q"), events);
    expect(t.status).toBe("finished");
    expect(t.result.rewrittenQuery).toBe("2026 年各月订单金额");
    expect(t.result.generatedSql).toBe("SELECT 1");
    expect(t.result.chartType).toBe("line");
    expect(t.result.executionResult?.row_count).toBe(1);
    expect(t.result.summary).toBe("金额逐月上升");
    expect(t.result.renderSpec?.x).toBe("m");
  });

  it("修复事件更新为最新生成的 SQL", () => {
    const t = applyEventsToTurn(createTurn("id1", "q"), [
      started,
      nodeEvent("sql_generator", { sql_plan: {}, generated_sql: "SELECT bad", chart_type: "bar" }),
      nodeEvent("sql_repair", { attempts: 1, generated_sql: "SELECT good", sql_plan: {} }),
    ]);
    expect(t.result.generatedSql).toBe("SELECT good");
  });
});

describe("applyEventToTurn 澄清 / 取消 / 错误", () => {
  it("澄清：记录 clarification 且状态结束", () => {
    const t = applyEventToTurn(
      createTurn("id1", "q"),
      nodeEvent("schema_inspector", {
        rewritten_query: "q",
        db_info: [],
        retrieval_hits: [],
        clarification: { question: "哪个时间段?", options: ["今年", "上月"], reason: "时间不明" },
        trace_id: "tr",
      }),
    );
    expect(t.status).toBe("clarifying");
    expect(t.result.clarification?.options).toEqual(["今年", "上月"]);
  });

  it("error：记录错误体并置为 error", () => {
    const t = applyEventToTurn(createTurn("id1", "q"), {
      type: "error",
      payload: { task_id: "t1", code: "rate_limited", message: "请求过于频繁", trace_id: "x" },
    });
    expect(t.status).toBe("error");
    expect(t.error?.code).toBe("rate_limited");
  });

  it("cancelled：状态置为 cancelled", () => {
    const t = applyEventToTurn(createTurn("id1", "q"), {
      type: "cancelled",
      payload: { task_id: "t1", cancelled: true },
    });
    expect(t.status).toBe("cancelled");
  });
});

describe("历史回看转换", () => {
  const historyTurn: HistoryTurn = {
    id: 42,
    user_query: "按月统计金额",
    rewritten_query: "2026 年各月订单金额",
    generated_sql: "SELECT month, amount FROM orders",
    tables: ["orders"],
    summary: "金额逐月上升",
    chart_type: "line",
    row_count: 12,
    elapsed_ms: 34.5,
    trace_id: "tr",
    status: "success",
    created_at: "2026-06-28T00:00:00Z",
  };

  it("historyTurnToChatTurn：转为已完成态并带上可见字段", () => {
    const t = historyTurnToChatTurn(historyTurn);
    expect(t.id).toBe("history-42");
    expect(t.status).toBe("finished");
    expect(t.progress.overall).toBe("finished");
    expect(t.result.rewrittenQuery).toBe("2026 年各月订单金额");
    expect(t.result.generatedSql).toBe("SELECT month, amount FROM orders");
    expect(t.result.chartType).toBe("line");
    // 列表接口不含执行结果 / 渲染规格。
    expect(t.result.executionResult).toBeNull();
    expect(t.result.renderSpec).toBeNull();
  });

  it("enrichTurnWithDetail：用详情补全 render_spec / execution_result 以重绘", () => {
    const base = historyTurnToChatTurn(historyTurn);
    const detail: HistoryDetail = {
      id: 42,
      session_id: "s1",
      user_query: "按月统计金额",
      generated_sql: "SELECT month, amount FROM orders",
      summary: "金额逐月上升",
      chart_type: "line",
      render_spec: { chart_type: "line", x: "month", y: ["amount"], series: null, title: "月度金额", options: {} },
      execution_result: { columns: ["month", "amount"], rows: [{ month: "1月", amount: 1 }], row_count: 1, elapsed_ms: 2, error: null },
      created_at: "2026-06-28T00:00:00Z",
    };
    const enriched = enrichTurnWithDetail(base, detail);
    expect(enriched.result.renderSpec?.x).toBe("month");
    expect(enriched.result.executionResult?.row_count).toBe(1);
  });
});
