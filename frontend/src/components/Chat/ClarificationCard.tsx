import { Button, Card, Space, Typography } from "antd";
import { QuestionCircleOutlined } from "@ant-design/icons";

import type { Clarification } from "../../api/types";

interface ClarificationCardProps {
  clarification: Clarification;
  disabled?: boolean;
  onSelect: (option: string) => void;
}

// 澄清卡片：展示后端的澄清问题与候选项，点击候选项即作为新问题继续提问。
export default function ClarificationCard({ clarification, disabled, onSelect }: ClarificationCardProps) {
  return (
    <Card
      size="small"
      style={{ background: "#fffbe6", borderColor: "#ffe58f" }}
      title={
        <Space>
          <QuestionCircleOutlined style={{ color: "#faad14" }} />
          <span>需要你补充一下</span>
        </Space>
      }
    >
      <Typography.Paragraph strong style={{ marginBottom: 4 }}>
        {clarification.question}
      </Typography.Paragraph>
      {clarification.reason && (
        <Typography.Paragraph type="secondary" style={{ marginBottom: 12, fontSize: 13 }}>
          {clarification.reason}
        </Typography.Paragraph>
      )}
      <Space wrap>
        {clarification.options.map((opt) => (
          <Button key={opt} disabled={disabled} onClick={() => onSelect(opt)}>
            {opt}
          </Button>
        ))}
      </Space>
    </Card>
  );
}
