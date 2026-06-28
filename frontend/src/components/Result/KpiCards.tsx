import { Card, Col, Empty, Row, Statistic } from "antd";

import type { KpiItem } from "./chartAdapter";

interface KpiCardsProps {
  items: KpiItem[];
}

// KPI 指标卡：把单值结果渲染为醒目的统计数字。
export default function KpiCards({ items }: KpiCardsProps) {
  if (items.length === 0) {
    return <Empty description="无指标数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />;
  }
  return (
    <Row gutter={[16, 16]}>
      {items.map((item) => (
        <Col key={item.label} xs={24} sm={12} md={8}>
          <Card variant="borderless" style={{ background: "#f7f8fa" }}>
            <Statistic
              title={item.label}
              value={item.value as number | string}
              valueStyle={{ color: "#2f54eb", fontSize: 28 }}
            />
          </Card>
        </Col>
      ))}
    </Row>
  );
}
