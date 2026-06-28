// 流式进度状态机（纯函数，便于单测）。
// 把一连串 SSE 事件归约为「每个节点的状态 + 整体状态」，组件只负责把状态渲染出来。
//
// 关键约定（契约 §1）：
// - 节点事件在「该节点产出结果」时下发，因此收到事件即代表该节点完成（done）。
// - sql_repair 可出现 0~N 次，需展示「修复中·第 N 次」。
// - schema_inspector 返回 clarification 时链路在澄清处提前结束，后续节点不再触发。

import { NODE_SEQUENCE, type NodeName, type SSEEvent, type ErrorEventPayload } from "../../api/types";

export type NodeStatus = "pending" | "active" | "repairing" | "done" | "error" | "skipped";
export type OverallStatus = "idle" | "running" | "finished" | "cancelled" | "error";

export interface NodeProgress {
  node: NodeName;
  status: NodeStatus;
  attempts?: number; // 仅 sql_repair 使用，记录已修复次数
}

export interface ProgressState {
  nodes: NodeProgress[];
  overall: OverallStatus;
  activeNode: NodeName | null;
  error: ErrorEventPayload | null;
}

// 节点中文标签（企业语境下更易读）。
export const NODE_LABELS: Record<NodeName, string> = {
  schema_inspector: "理解与检索",
  table_relationship: "关系分析",
  sql_generator: "SQL 生成",
  sql_repair: "SQL 修复",
  sql_executor: "SQL 执行",
  summarize: "结果摘要",
  data_render: "图表渲染",
};

// 正常链路里每个节点之后「预期进行中的下一个节点」。
// 注意：sql_generator 直接指向 sql_executor —— 修复是可选回环，只有真正收到 sql_repair 事件才点亮修复步骤。
const NEXT_EXPECTED: Record<NodeName, NodeName | null> = {
  schema_inspector: "table_relationship",
  table_relationship: "sql_generator",
  sql_generator: "sql_executor",
  sql_repair: "sql_executor",
  sql_executor: "summarize",
  summarize: "data_render",
  data_render: null,
};

export function initialProgressState(): ProgressState {
  return {
    nodes: NODE_SEQUENCE.map((node) => ({ node, status: "pending" as NodeStatus })),
    overall: "idle",
    activeNode: null,
    error: null,
  };
}

// 历史回看用的「已完成」进度：除可选的 sql_repair 标记为跳过外，其余节点均为完成。
export function completedProgressState(): ProgressState {
  return {
    nodes: NODE_SEQUENCE.map((node) => ({
      node,
      status: (node === "sql_repair" ? "skipped" : "done") as NodeStatus,
    })),
    overall: "finished",
    activeNode: null,
    error: null,
  };
}

// 不可变地更新某个节点的状态。
function patchNode(nodes: NodeProgress[], node: NodeName, patch: Partial<NodeProgress>): NodeProgress[] {
  return nodes.map((n) => (n.node === node ? { ...n, ...patch } : n));
}

function reduceTask(state: ProgressState, status: "started" | "finished"): ProgressState {
  if (status === "started") {
    return {
      ...initialProgressState(),
      overall: "running",
      activeNode: "schema_inspector",
      nodes: patchNode(initialProgressState().nodes, "schema_inspector", { status: "active" }),
    };
  }
  // finished：清空活动节点；从未触发的 sql_repair 视为「已跳过」。
  let nodes = state.nodes;
  const repair = nodes.find((n) => n.node === "sql_repair");
  if (repair && repair.status === "pending") {
    nodes = patchNode(nodes, "sql_repair", { status: "skipped" });
  }
  return { ...state, nodes, overall: "finished", activeNode: null };
}

// 处理节点事件，data 为该节点的增量负载（结构见契约）。
function reduceNode(state: ProgressState, node: NodeName, data: Record<string, unknown>): ProgressState {
  // 澄清：schema_inspector 返回 clarification → 链路提前结束。
  if (node === "schema_inspector" && data?.clarification) {
    return {
      ...state,
      nodes: patchNode(state.nodes, "schema_inspector", { status: "done" }),
      overall: "finished",
      activeNode: null,
    };
  }

  // 修复事件：记录第 N 次，标记为「修复中」，活动节点指向修复步骤（可能还会再来一次）。
  if (node === "sql_repair") {
    const attempts = typeof data?.attempts === "number" ? data.attempts : undefined;
    return {
      ...state,
      nodes: patchNode(state.nodes, "sql_repair", { status: "repairing", attempts }),
      activeNode: "sql_repair",
      overall: "running",
    };
  }

  let nodes = patchNode(state.nodes, node, { status: "done" });
  // 执行成功意味着修复回环结束：把仍处于 repairing 的修复步骤收敛为 done。
  if (node === "sql_executor") {
    const repair = nodes.find((n) => n.node === "sql_repair");
    if (repair && repair.status === "repairing") {
      nodes = patchNode(nodes, "sql_repair", { status: "done" });
    }
  }

  const next = NEXT_EXPECTED[node];
  if (next) {
    nodes = patchNode(nodes, next, { status: "active" });
  }
  return { ...state, nodes, overall: "running", activeNode: next };
}

function reduceCancelled(state: ProgressState): ProgressState {
  // 取消：当前活动节点回退为 pending（中止），整体置为 cancelled。
  const nodes = state.activeNode
    ? patchNode(state.nodes, state.activeNode, { status: "pending" })
    : state.nodes;
  return { ...state, nodes, overall: "cancelled", activeNode: null };
}

function reduceError(state: ProgressState, payload: ErrorEventPayload): ProgressState {
  const nodes = state.activeNode
    ? patchNode(state.nodes, state.activeNode, { status: "error" })
    : state.nodes;
  return { ...state, nodes, overall: "error", activeNode: null, error: payload };
}

// 单步归约：state + event → 新 state。
export function reduceProgress(state: ProgressState, event: SSEEvent): ProgressState {
  switch (event.type) {
    case "task":
      return reduceTask(state, event.payload.status);
    case "cancelled":
      return reduceCancelled(state);
    case "error":
      return reduceError(state, event.payload);
    default:
      // 其余皆为节点事件。各节点 data 结构不同，这里以宽松字典读取需要的字段。
      return reduceNode(state, event.payload.node, event.payload.data as unknown as Record<string, unknown>);
  }
}

// 批量归约：便于一次处理多条事件或在测试中回放整条链路。
export function reduceProgressAll(state: ProgressState, events: SSEEvent[]): ProgressState {
  return events.reduce(reduceProgress, state);
}
