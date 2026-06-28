import { useMemo } from "react";
import { Alert, Empty, Tabs, Typography } from "antd";

import type { ChartType, ExecutionResult, RenderSpec } from "../../api/types";
import { buildChartRender } from "./chartAdapter";
import DataTable from "./DataTable";
import EChart from "./EChart";
import KpiCards from "./KpiCards";
import SqlView from "./SqlView";

interface ResultTabsProps {
  chartType: ChartType;
  executionResult: ExecutionResult | null;
  renderSpec: RenderSpec | null;
  sql: string | null | undefined;
  summary: string | null | undefined;
}

// 默认 render_spec：当后端未给出时退化为「按当前 chart_type 直接渲染」。
function fallbackSpec(chartType: ChartType): RenderSpec {
  return { chart_type: chartType, x: null, y: [], series: null, title: "", options: {} };
}

// 结果区：图表 / 数据表格 / SQL（只读高亮）/ 业务摘要 四个 Tab。
export default function ResultTabs({ chartType, executionResult, renderSpec, sql, summary }: ResultTabsProps) {
  // 图表渲染策略随 chart_type / 数据变化而记忆，避免每次渲染重复计算。
  const chart = useMemo(() => {
    if (!executionResult) return null;
    return buildChartRender(chartType, executionResult, renderSpec ?? fallbackSpec(chartType));
  }, [chartType, executionResult, renderSpec]);

  const chartPane = () => {
    if (!executionResult) return <Empty description="暂无结果" />;
    if (executionResult.error) {
      return <Alert type="error" showIcon message="SQL 执行失败" description={executionResult.error} />;
    }
    if (!chart || chart.kind === "table") return <DataTable result={executionResult} />;
    if (chart.kind === "kpi") return <KpiCards items={chart.items} />;
    return <EChart option={chart.option} />;
  };

  const items = [
    { key: "chart", label: "图表", children: chartPane() },
    {
      key: "table",
      label: "数据表格",
      children: executionResult ? <DataTable result={executionResult} /> : <Empty description="暂无数据" />,
    },
    { key: "sql", label: "SQL", children: <SqlView sql={sql} /> },
    {
      key: "summary",
      label: "业务摘要",
      children: summary ? (
        <Typography.Paragraph style={{ whiteSpace: "pre-wrap", margin: 0 }}>{summary}</Typography.Paragraph>
      ) : (
        <Empty description="暂无摘要" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ),
    },
  ];

  return <Tabs defaultActiveKey="chart" items={items} />;
}
