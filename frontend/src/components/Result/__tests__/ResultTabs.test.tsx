import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// jsdom 无真实绘图能力，mock echarts，只验证组件编排逻辑而非渲染像素。
vi.mock("echarts", () => ({
  init: () => ({ setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() }),
}));

import ResultTabs from "../ResultTabs";
import type { ExecutionResult, RenderSpec } from "../../../api/types";

const result: ExecutionResult = {
  columns: ["month", "amount"],
  rows: [
    { month: "2026-01", amount: 100 },
    { month: "2026-02", amount: 220 },
  ],
  row_count: 2,
  elapsed_ms: 5,
  error: null,
};

const spec: RenderSpec = {
  chart_type: "line",
  x: "month",
  y: ["amount"],
  series: null,
  title: "月度金额",
  options: {},
};

describe("ResultTabs", () => {
  it("渲染四个 Tab：图表/数据表格/SQL/业务摘要", () => {
    render(
      <ResultTabs chartType="line" executionResult={result} renderSpec={spec} sql="SELECT 1" summary="同比增长" />,
    );
    expect(screen.getByRole("tab", { name: "图表" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "数据表格" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "SQL" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "业务摘要" })).toBeInTheDocument();
  });

  it("切到数据表格 Tab 展示列与单元格", async () => {
    const user = userEvent.setup();
    render(
      <ResultTabs chartType="line" executionResult={result} renderSpec={spec} sql="SELECT 1" summary="s" />,
    );
    await user.click(screen.getByRole("tab", { name: "数据表格" }));
    expect(await screen.findByText("2026-01")).toBeInTheDocument();
    expect(screen.getByText("220")).toBeInTheDocument();
  });

  it("SQL Tab 展示带高亮的 SQL 文本", async () => {
    const user = userEvent.setup();
    render(
      <ResultTabs chartType="line" executionResult={result} renderSpec={spec} sql="SELECT amount FROM orders" summary="s" />,
    );
    await user.click(screen.getByRole("tab", { name: "SQL" }));
    // 关键字与表名分散在多个 span，用 textContent 聚合断言。
    expect(await screen.findByText("SELECT", { exact: false })).toBeInTheDocument();
    expect(screen.getByText(/orders/)).toBeInTheDocument();
  });

  it("业务摘要 Tab 展示摘要文本", async () => {
    const user = userEvent.setup();
    render(
      <ResultTabs chartType="line" executionResult={result} renderSpec={spec} sql="SELECT 1" summary="一月到二月金额上升 120%" />,
    );
    await user.click(screen.getByRole("tab", { name: "业务摘要" }));
    expect(await screen.findByText(/金额上升 120%/)).toBeInTheDocument();
  });

  it("执行出错时图表 Tab 显示错误提示", () => {
    const errResult: ExecutionResult = { ...result, error: "syntax error near FROM" };
    render(
      <ResultTabs chartType="line" executionResult={errResult} renderSpec={spec} sql="SELECT" summary="" />,
    );
    expect(screen.getByText(/syntax error near FROM/)).toBeInTheDocument();
  });

  it("table 类型在图表 Tab 直接回退为表格", () => {
    render(
      <ResultTabs chartType="table" executionResult={result} renderSpec={null} sql="SELECT 1" summary="s" />,
    );
    // 图表 Tab 默认激活，table 类型应渲染表格内容。
    expect(screen.getAllByText("2026-01").length).toBeGreaterThan(0);
  });
});
