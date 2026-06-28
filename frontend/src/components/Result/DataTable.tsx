import { Empty, Table } from "antd";

import type { ExecutionResult } from "../../api/types";

// 把任意单元格值安全地转为可显示文本。
function renderCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

interface DataTableProps {
  result: ExecutionResult;
}

// 数据表格：直接用 execution_result.columns + rows 渲染（契约 §4 table 规则）。
export default function DataTable({ result }: DataTableProps) {
  if (result.columns.length === 0 || result.rows.length === 0) {
    return <Empty description="无数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />;
  }

  const columns = result.columns.map((col) => ({
    title: col,
    dataIndex: col,
    key: col,
    ellipsis: true,
    render: (value: unknown) => renderCell(value),
  }));

  const dataSource = result.rows.map((row, idx) => ({ key: idx, ...row }));

  return (
    <Table
      size="small"
      columns={columns}
      dataSource={dataSource}
      scroll={{ x: "max-content" }}
      pagination={result.rows.length > 20 ? { pageSize: 20, showSizeChanger: false } : false}
    />
  );
}
