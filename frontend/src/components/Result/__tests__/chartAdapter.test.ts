import { describe, it, expect } from "vitest";

import { buildChartRender } from "../chartAdapter";
import type { ChartType, ExecutionResult, RenderSpec } from "../../../api/types";

// 构造一份"月份-金额"的执行结果。
function monthlyResult(): ExecutionResult {
  return {
    columns: ["month", "amount"],
    rows: [
      { month: "2026-01", amount: 100 },
      { month: "2026-02", amount: 220 },
      { month: "2026-03", amount: 180 },
    ],
    row_count: 3,
    elapsed_ms: 12.3,
    error: null,
  };
}

function spec(chart_type: ChartType, over: Partial<RenderSpec> = {}): RenderSpec {
  return {
    chart_type,
    x: "month",
    y: ["amount"],
    series: null,
    title: "月度金额",
    options: {},
    ...over,
  };
}

// 从返回结果中取出 ECharts option（断言为 echarts 类型）。
function optionOf(chart_type: ChartType, over: Partial<RenderSpec> = {}, result = monthlyResult()) {
  const r = buildChartRender(chart_type, result, spec(chart_type, over));
  if (r.kind !== "echarts") throw new Error(`expected echarts, got ${r.kind}`);
  return r.option as Record<string, any>;
}

describe("buildChartRender 基础图表", () => {
  it("line：生成 line series 且 x 轴类目正确", () => {
    const opt = optionOf("line");
    const series = opt.series as any[];
    expect(series[0].type).toBe("line");
    expect(series[0].data).toEqual([100, 220, 180]);
    expect(opt.xAxis.data).toEqual(["2026-01", "2026-02", "2026-03"]);
  });

  it("area：line series 带 areaStyle", () => {
    const opt = optionOf("area");
    const series = opt.series as any[];
    expect(series[0].type).toBe("line");
    expect(series[0].areaStyle).toBeDefined();
  });

  it("bar：生成 bar series", () => {
    const opt = optionOf("bar");
    expect((opt.series as any[])[0].type).toBe("bar");
  });

  it("horizontal_bar：类目轴在 y 轴、数值轴在 x 轴", () => {
    const opt = optionOf("horizontal_bar");
    expect(opt.yAxis.type).toBe("category");
    expect(opt.xAxis.type).toBe("value");
    expect(opt.yAxis.data).toEqual(["2026-01", "2026-02", "2026-03"]);
  });

  it("stacked_bar：series 设置了 stack", () => {
    const result: ExecutionResult = {
      columns: ["month", "a", "b"],
      rows: [
        { month: "1月", a: 1, b: 2 },
        { month: "2月", a: 3, b: 4 },
      ],
      row_count: 2,
      elapsed_ms: 1,
      error: null,
    };
    const r = buildChartRender("stacked_bar", result, spec("stacked_bar", { y: ["a", "b"] }));
    const opt = (r as any).option;
    const series = opt.series as any[];
    expect(series).toHaveLength(2);
    expect(series.every((s) => typeof s.stack === "string")).toBe(true);
  });

  it("pie：饼图 data 为 name/value 对", () => {
    const opt = optionOf("pie");
    const series = opt.series as any[];
    expect(series[0].type).toBe("pie");
    expect(series[0].data).toEqual([
      { name: "2026-01", value: 100 },
      { name: "2026-02", value: 220 },
      { name: "2026-03", value: 180 },
    ]);
  });

  it("donut：环形图 radius 为区间数组", () => {
    const opt = optionOf("donut");
    const series = opt.series as any[];
    expect(series[0].type).toBe("pie");
    expect(Array.isArray(series[0].radius)).toBe(true);
  });

  it("scatter：散点 data 为 [x,y] 数对", () => {
    const result: ExecutionResult = {
      columns: ["price", "sales"],
      rows: [
        { price: 10, sales: 5 },
        { price: 20, sales: 8 },
      ],
      row_count: 2,
      elapsed_ms: 1,
      error: null,
    };
    const r = buildChartRender("scatter", result, spec("scatter", { x: "price", y: ["sales"] }));
    const opt = (r as any).option;
    const series = opt.series as any[];
    expect(series[0].type).toBe("scatter");
    expect(series[0].data).toEqual([
      [10, 5],
      [20, 8],
    ]);
  });
});

describe("buildChartRender 多列分组 (series 透视)", () => {
  it("line 带 series 字段时按维度拆成多条线", () => {
    const result: ExecutionResult = {
      columns: ["month", "region", "amount"],
      rows: [
        { month: "1月", region: "华北", amount: 10 },
        { month: "1月", region: "华南", amount: 20 },
        { month: "2月", region: "华北", amount: 30 },
        { month: "2月", region: "华南", amount: 40 },
      ],
      row_count: 4,
      elapsed_ms: 1,
      error: null,
    };
    const r = buildChartRender("line", result, spec("line", { x: "month", y: ["amount"], series: "region" }));
    const opt = (r as any).option;
    const series = opt.series as any[];
    expect(series).toHaveLength(2);
    const names = series.map((s) => s.name).sort();
    expect(names).toEqual(["华北", "华南"]);
  });
});

describe("buildChartRender KPI 与 表格", () => {
  it("kpi：返回 kpi 类型与数值", () => {
    const result: ExecutionResult = {
      columns: ["total"],
      rows: [{ total: 12345 }],
      row_count: 1,
      elapsed_ms: 1,
      error: null,
    };
    const r = buildChartRender("kpi", result, spec("kpi", { x: null, y: ["total"] }));
    expect(r.kind).toBe("kpi");
    if (r.kind === "kpi") {
      expect(r.items[0].value).toBe(12345);
    }
  });

  it("table：返回 table 类型交给表格渲染", () => {
    const r = buildChartRender("table", monthlyResult(), spec("table"));
    expect(r.kind).toBe("table");
  });
});

describe("buildChartRender 健壮性 (绝不崩溃)", () => {
  it("未知图表类型回退为 table", () => {
    const r = buildChartRender("sankey" as ChartType, monthlyResult(), spec("sankey" as ChartType));
    expect(r.kind).toBe("table");
  });

  it("空数据不抛异常", () => {
    const empty: ExecutionResult = { columns: [], rows: [], row_count: 0, elapsed_ms: 0, error: null };
    expect(() => buildChartRender("line", empty, spec("line"))).not.toThrow();
  });

  it("render_spec.x 缺失时回退到首列", () => {
    const opt = optionOf("line", { x: null });
    expect(opt.xAxis.data).toEqual(["2026-01", "2026-02", "2026-03"]);
  });

  it("render_spec.y 指向不存在列时回退到其余列", () => {
    const opt = optionOf("bar", { y: ["nonexistent"] });
    const series = opt.series as any[];
    expect(series[0].data).toEqual([100, 220, 180]);
  });
});
