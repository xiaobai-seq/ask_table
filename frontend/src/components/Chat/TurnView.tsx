import { Avatar, Card, Space, Tag, Typography } from "antd";
import { RobotOutlined, UserOutlined } from "@ant-design/icons";

import StepIndicator from "../Progress/StepIndicator";
import ResultTabs from "../Result/ResultTabs";
import ClarificationCard from "./ClarificationCard";
import type { ChatTurn } from "../../store/turn";

interface TurnViewProps {
  turn: ChatTurn;
  onClarify: (option: string) => void;
}

// 是否已有可展示的结果（执行结果 / 摘要 / SQL 任一存在）。
function hasResult(turn: ChatTurn): boolean {
  const r = turn.result;
  return Boolean(r.executionResult || r.summary || r.generatedSql);
}

// 单轮对话视图：用户气泡（右）+ 助手区（左，含进度、澄清、结果）。
export default function TurnView({ turn, onClarify }: TurnViewProps) {
  const { result } = turn;
  return (
    <div style={{ marginBottom: 24 }}>
      {/* 用户提问气泡（右对齐） */}
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
        <Space align="start">
          <div
            style={{
              background: "#2f54eb",
              color: "#fff",
              padding: "8px 14px",
              borderRadius: "10px 10px 2px 10px",
              maxWidth: 520,
            }}
          >
            {turn.query}
          </div>
          <Avatar icon={<UserOutlined />} style={{ background: "#2f54eb" }} />
        </Space>
      </div>

      {/* 助手响应区（左对齐） */}
      <div style={{ display: "flex", justifyContent: "flex-start" }}>
        <Space align="start" style={{ maxWidth: "100%" }}>
          <Avatar icon={<RobotOutlined />} style={{ background: "#52c41a" }} />
          <Card size="small" style={{ minWidth: 480, maxWidth: 760 }}>
            {result.rewrittenQuery && (
              <Typography.Paragraph type="secondary" style={{ marginBottom: 8, fontSize: 13 }}>
                <Tag color="blue">已理解为</Tag>
                {result.rewrittenQuery}
              </Typography.Paragraph>
            )}

            <StepIndicator state={turn.progress} />

            {turn.status === "clarifying" && result.clarification && (
              <div style={{ marginTop: 16 }}>
                <ClarificationCard clarification={result.clarification} onSelect={onClarify} />
              </div>
            )}

            {turn.status !== "clarifying" && hasResult(turn) && (
              <div style={{ marginTop: 16 }}>
                <ResultTabs
                  chartType={result.chartType ?? "table"}
                  executionResult={result.executionResult ?? null}
                  renderSpec={result.renderSpec ?? null}
                  sql={result.generatedSql}
                  summary={result.summary}
                />
              </div>
            )}
          </Card>
        </Space>
      </div>
    </div>
  );
}
