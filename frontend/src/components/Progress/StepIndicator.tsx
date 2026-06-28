import {
  CheckCircleFilled,
  CloseCircleFilled,
  LoadingOutlined,
  MinusCircleOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import { Alert } from "antd";

import { NODE_LABELS, type NodeProgress, type ProgressState } from "./progress";

// 各状态对应的图标与配色（企业浅色主题：绿=完成 蓝=进行 橙=修复 红=错误 灰=待执行/跳过）。
const STATUS_STYLE: Record<NodeProgress["status"], { color: string; icon: React.ReactNode }> = {
  pending: { color: "#bfbfbf", icon: <MinusCircleOutlined /> },
  active: { color: "#2f54eb", icon: <LoadingOutlined spin /> },
  repairing: { color: "#fa8c16", icon: <SyncOutlined spin /> },
  done: { color: "#52c41a", icon: <CheckCircleFilled /> },
  error: { color: "#f5222d", icon: <CloseCircleFilled /> },
  skipped: { color: "#d9d9d9", icon: <MinusCircleOutlined /> },
};

// 节点辅助文案：进行中 / 修复中·第 N 次 / 已修复 / 已跳过。
function nodeHint(n: NodeProgress): string {
  if (n.status === "repairing") return `修复中·第 ${n.attempts ?? 1} 次`;
  if (n.node === "sql_repair" && n.status === "done" && n.attempts) return `已修复 ${n.attempts} 次`;
  if (n.status === "active") return "进行中";
  if (n.status === "skipped") return "已跳过";
  return "";
}

interface StepIndicatorProps {
  state: ProgressState;
}

// 流式进度指示器：横向展示 7 个节点，随 SSE 事件点亮 / 报错。
export default function StepIndicator({ state }: StepIndicatorProps) {
  return (
    <div data-testid="step-indicator" data-overall={state.overall}>
      <div style={{ display: "flex", alignItems: "flex-start", flexWrap: "wrap", gap: 4 }}>
        {state.nodes.map((n, idx) => {
          const style = STATUS_STYLE[n.status];
          const hint = nodeHint(n);
          return (
            <div key={n.node} style={{ display: "flex", alignItems: "flex-start" }}>
              <div
                data-node={n.node}
                data-status={n.status}
                style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  minWidth: 84,
                  textAlign: "center",
                }}
              >
                <span style={{ color: style.color, fontSize: 20, lineHeight: 1 }}>{style.icon}</span>
                <span style={{ marginTop: 6, fontSize: 13, color: "#1f2329" }}>{NODE_LABELS[n.node]}</span>
                {hint && (
                  <span style={{ marginTop: 2, fontSize: 12, color: style.color }}>{hint}</span>
                )}
              </div>
              {idx < state.nodes.length - 1 && (
                <div
                  aria-hidden
                  style={{
                    width: 24,
                    height: 1,
                    background: "#e5e6eb",
                    marginTop: 10,
                  }}
                />
              )}
            </div>
          );
        })}
      </div>
      {state.overall === "error" && state.error && (
        <Alert
          style={{ marginTop: 12 }}
          type="error"
          showIcon
          message="处理失败"
          description={`${state.error.message}${state.error.trace_id ? `（trace_id: ${state.error.trace_id}）` : ""}`}
        />
      )}
    </div>
  );
}
