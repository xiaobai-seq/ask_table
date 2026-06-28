import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import StepIndicator from "../StepIndicator";
import { initialProgressState, reduceProgressAll, NODE_LABELS } from "../progress";
import type { SSEEvent, NodeName } from "../../../api/types";

function nodeEvent<N extends NodeName>(node: N, data: unknown): SSEEvent {
  return { type: node, payload: { task_id: "t1", node, data } } as SSEEvent;
}
const started: SSEEvent = { type: "task", payload: { task_id: "t1", status: "started" } };

function step(node: NodeName) {
  return document.querySelector(`[data-node="${node}"]`);
}

describe("StepIndicator", () => {
  it("渲染全部 7 个节点中文标签", () => {
    render(<StepIndicator state={initialProgressState()} />);
    for (const label of Object.values(NODE_LABELS)) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("task started 后首节点为 active 并显示进行中", () => {
    const state = reduceProgressAll(initialProgressState(), [started]);
    render(<StepIndicator state={state} />);
    expect(step("schema_inspector")).toHaveAttribute("data-status", "active");
    expect(screen.getByText("进行中")).toBeInTheDocument();
  });

  it("修复事件显示「修复中·第 N 次」", () => {
    const state = reduceProgressAll(initialProgressState(), [
      started,
      nodeEvent("sql_generator", { sql_plan: {}, generated_sql: "SELECT x", chart_type: "bar" }),
      nodeEvent("sql_repair", { attempts: 2, generated_sql: "SELECT y", sql_plan: {} }),
    ]);
    render(<StepIndicator state={state} />);
    expect(step("sql_repair")).toHaveAttribute("data-status", "repairing");
    expect(screen.getByText("修复中·第 2 次")).toBeInTheDocument();
  });

  it("error 时活动节点标红并展示错误提示", () => {
    const state = reduceProgressAll(initialProgressState(), [
      started,
      { type: "error", payload: { task_id: "t1", code: "internal", message: "数据库连接失败", trace_id: "abc" } },
    ]);
    render(<StepIndicator state={state} />);
    expect(step("schema_inspector")).toHaveAttribute("data-status", "error");
    expect(screen.getByText(/数据库连接失败/)).toBeInTheDocument();
    expect(screen.getByText(/abc/)).toBeInTheDocument();
  });

  it("完成链路中 repair 标记为 skipped", () => {
    const state = reduceProgressAll(initialProgressState(), [
      started,
      nodeEvent("schema_inspector", { rewritten_query: "q", db_info: [], retrieval_hits: [], clarification: null, trace_id: "tr" }),
      nodeEvent("table_relationship", { table_relationship: [] }),
      nodeEvent("sql_generator", { sql_plan: {}, generated_sql: "SELECT 1", chart_type: "bar" }),
      nodeEvent("sql_executor", { execution_result: { columns: [], rows: [], row_count: 0, elapsed_ms: 1, error: null } }),
      nodeEvent("summarize", { summary: "ok" }),
      nodeEvent("data_render", { render_spec: {}, chart_type: "bar" }),
      { type: "task", payload: { task_id: "t1", status: "finished" } },
    ]);
    render(<StepIndicator state={state} />);
    expect(step("sql_repair")).toHaveAttribute("data-status", "skipped");
    expect(step("data_render")).toHaveAttribute("data-status", "done");
  });
});
