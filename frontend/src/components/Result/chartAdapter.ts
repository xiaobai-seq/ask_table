// 图表适配器：把后端推荐的 chart_type + execution_result + render_spec 转成 ECharts option。
// 设计原则（契约 §4）：
// - 后端只「推荐」render_spec，前端负责「渲染」。
// - 阶段 3 至少支持 line/bar(含 horizontal_bar/stacked_bar)/pie/donut/area/scatter/kpi/table。
// - 未知类型一律回退 table，且任何异常都不得让组件崩溃。

import type { EChartsOption } from "echarts";

import type { ChartType, ExecutionResult, RenderSpec } from "../../api/types";

// 适配结果交给 ResultTabs：echarts 走 ECharts，table 走表格，kpi 走指标卡。
export type ChartRender =
  | { kind: "echarts"; option: EChartsOption }
  | { kind: "table" }
  | { kind: "kpi"; items: KpiItem[] };

export interface KpiItem {
  label: string;
  value: unknown;
}

// 安全数值转换：非数字（空、文本）回退为 0，避免 ECharts 渲染异常。
function toNumber(v: unknown): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : 0;
}

// 确定 x（类目）字段：优先用 render_spec.x，否则回退首列。
function resolveX(spec: RenderSpec, columns: string[]): string | null {
  if (spec.x && columns.includes(spec.x)) return spec.x;
  return columns[0] ?? null;
}

// 确定 y（数值）字段集合：过滤掉不存在的列；若过滤后为空，回退为除 x/series 外的其余列。
function resolveY(spec: RenderSpec, columns: string[], xField: string | null, seriesField: string | null): string[] {
  const valid = spec.y.filter((c) => columns.includes(c));
  if (valid.length > 0) return valid;
  return columns.filter((c) => c !== xField && c !== seriesField);
}

// 按出现顺序取唯一值（用于透视时的类目轴与系列名）。
function uniqueInOrder(values: unknown[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const v of values) {
    const key = String(v);
    if (!seen.has(key)) {
      seen.add(key);
      out.push(key);
    }
  }
  return out;
}

interface CartesianFlags {
  seriesType: "line" | "bar" | "scatter";
  isArea: boolean;
  isStacked: boolean;
  isHorizontal: boolean;
}

function cartesianFlags(chartType: ChartType): CartesianFlags {
  const isArea = chartType === "area" || chartType === "stacked_area";
  const isStacked = chartType === "stacked_area" || chartType === "stacked_bar";
  const isHorizontal = chartType === "horizontal_bar";
  const seriesType =
    chartType === "scatter" ? "scatter" : chartType === "bar" || chartType === "stacked_bar" || chartType === "horizontal_bar" ? "bar" : "line";
  return { seriesType, isArea, isStacked, isHorizontal };
}

// 构造一条系列的基础配置（线/柱共用 areaStyle、stack 等开关）。
function makeSeries(name: string, type: CartesianFlags["seriesType"], data: unknown[], flags: CartesianFlags) {
  return {
    name,
    type,
    data,
    smooth: type === "line",
    stack: flags.isStacked ? "total" : undefined,
    areaStyle: flags.isArea ? {} : undefined,
  };
}

// 直角坐标系图表（line/area/bar/scatter），支持按 series 字段透视为多系列。
function buildCartesian(
  chartType: ChartType,
  result: ExecutionResult,
  spec: RenderSpec,
): EChartsOption {
  const flags = cartesianFlags(chartType);
  const { columns, rows } = result;
  const seriesField = spec.series && columns.includes(spec.series) ? spec.series : null;
  const xField = resolveX(spec, columns);
  const yFields = resolveY(spec, columns, xField, seriesField);
  const yField = yFields[0];

  const isScatter = flags.seriesType === "scatter";
  let categories: string[] = [];
  let series: ReturnType<typeof makeSeries>[] = [];

  if (seriesField && xField && yField) {
    // 透视：按 series 维度拆成多条系列，类目轴取 x 的唯一值。
    categories = uniqueInOrder(rows.map((r) => r[xField]));
    const seriesNames = uniqueInOrder(rows.map((r) => r[seriesField]));
    series = seriesNames.map((sv) => {
      const data = categories.map((c) => {
        const hit = rows.find((r) => String(r[xField]) === c && String(r[seriesField]) === sv);
        if (!hit) return isScatter ? [toNumber(c), 0] : 0;
        return isScatter ? [toNumber(c), toNumber(hit[yField])] : toNumber(hit[yField]);
      });
      return makeSeries(sv, flags.seriesType, data, flags);
    });
  } else {
    // 非透视：每个 y 字段一条系列。
    categories = xField ? rows.map((r) => String(r[xField])) : [];
    series = yFields.map((y) => {
      const data = isScatter
        ? rows.map((r) => [toNumber(xField ? r[xField] : 0), toNumber(r[y])])
        : rows.map((r) => toNumber(r[y]));
      return makeSeries(y, flags.seriesType, data, flags);
    });
  }

  // 坐标轴：散点双数值轴；horizontal_bar 交换类目/数值轴；其余为「类目 x + 数值 y」。
  let xAxis: Record<string, unknown>;
  let yAxis: Record<string, unknown>;
  if (isScatter) {
    xAxis = { type: "value", name: xField ?? "" };
    yAxis = { type: "value", name: yField ?? "" };
  } else if (flags.isHorizontal) {
    xAxis = { type: "value" };
    yAxis = { type: "category", data: categories };
  } else {
    xAxis = { type: "category", data: categories };
    yAxis = { type: "value" };
  }

  return {
    title: spec.title ? { text: spec.title, left: "center", textStyle: { fontSize: 14 } } : undefined,
    tooltip: { trigger: isScatter ? "item" : "axis" },
    legend: series.length > 1 ? { top: spec.title ? 28 : 0, type: "scroll" } : undefined,
    grid: { left: "3%", right: "4%", bottom: "3%", containLabel: true },
    xAxis,
    yAxis,
    series,
  } as EChartsOption;
}

// 饼图 / 环形图。
function buildPie(chartType: ChartType, result: ExecutionResult, spec: RenderSpec): EChartsOption {
  const { columns, rows } = result;
  const nameField = resolveX(spec, columns);
  const valueFields = resolveY(spec, columns, nameField, null);
  const valueField = valueFields[0];

  const data = rows.map((r) => ({
    name: String(nameField ? r[nameField] : ""),
    value: toNumber(valueField ? r[valueField] : 0),
  }));

  return {
    title: spec.title ? { text: spec.title, left: "center", textStyle: { fontSize: 14 } } : undefined,
    tooltip: { trigger: "item" },
    legend: { bottom: 0, type: "scroll" },
    series: [
      {
        type: "pie",
        radius: chartType === "donut" ? ["40%", "70%"] : "60%",
        center: ["50%", "50%"],
        data,
        label: { formatter: "{b}: {d}%" },
      },
    ],
  } as EChartsOption;
}

// KPI 指标卡：取 y 字段（或首个数值列）在首行的值。
function buildKpi(result: ExecutionResult, spec: RenderSpec): KpiItem[] {
  const { columns, rows } = result;
  if (rows.length === 0) return [];
  const fields = resolveY(spec, columns, null, null);
  const row = rows[0];
  if (fields.length === 0) return [];
  return fields.map((f) => ({ label: f, value: row[f] }));
}

// 入口：根据 chart_type 选择渲染策略；任何异常都回退为 table，保证「绝不崩溃」。
export function buildChartRender(
  chartType: ChartType,
  result: ExecutionResult,
  spec: RenderSpec,
): ChartRender {
  try {
    switch (chartType) {
      case "table":
        return { kind: "table" };
      case "kpi":
        return { kind: "kpi", items: buildKpi(result, spec) };
      case "line":
      case "area":
      case "stacked_area":
      case "bar":
      case "stacked_bar":
      case "horizontal_bar":
      case "scatter":
        return { kind: "echarts", option: buildCartesian(chartType, result, spec) };
      case "pie":
      case "donut":
        return { kind: "echarts", option: buildPie(chartType, result, spec) };
      default:
        // 其余 ChartType（sankey/heatmap/...）阶段 3 暂未实现，回退表格。
        return { kind: "table" };
    }
  } catch {
    return { kind: "table" };
  }
}
