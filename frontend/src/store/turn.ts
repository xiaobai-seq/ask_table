// 单轮对话的纯状态模型与归约逻辑（与网络解耦，便于单测）。
// 一个 turn = 用户一次提问 + 助手随 SSE 累积出来的进度/结果/澄清/错误。

import {
  initialProgressState,
  reduceProgress,
  type ProgressState,
} from "../components/Progress/progress";
import type {
  ApiError,
  ChartType,
  Clarification,
  ExecutionResult,
  RenderSpec,
  SSEEvent,
} from "../api/types";

export type TurnStatus = "streaming" | "finished" | "clarifying" | "cancelled" | "error";

// 助手侧累积出来的结构化结果（用于驱动 ResultTabs）。
export interface TurnResult {
  rewrittenQuery?: string;
  generatedSql?: string | null;
  chartType?: ChartType;
  executionResult?: ExecutionResult | null;
  renderSpec?: RenderSpec | null;
  summary?: string;
  clarification?: Clarification | null;
}

export interface ChatTurn {
  id: string;
  query: string;
  taskId: string | null;
  status: TurnStatus;
  progress: ProgressState;
  result: TurnResult;
  error: ApiError | null;
}

export function createTurn(id: string, query: string): ChatTurn {
  return {
    id,
    query,
    taskId: null,
    status: "streaming",
    progress: initialProgressState(),
    result: {},
    error: null,
  };
}

// 把节点事件的 data 合并进 result。各节点字段见契约 §1。
function mergeNodeData(result: TurnResult, node: string, data: Record<string, unknown>): TurnResult {
  switch (node) {
    case "schema_inspector":
      return {
        ...result,
        rewrittenQuery: data.rewritten_query as string,
        clarification: (data.clarification as Clarification | null) ?? null,
      };
    case "sql_generator":
      return {
        ...result,
        generatedSql: (data.generated_sql as string | null) ?? null,
        chartType: data.chart_type as ChartType,
      };
    case "sql_repair":
      // 修复回环：始终采用最新一次生成的 SQL。
      return { ...result, generatedSql: (data.generated_sql as string | null) ?? result.generatedSql ?? null };
    case "sql_executor":
      return { ...result, executionResult: data.execution_result as ExecutionResult };
    case "summarize":
      return { ...result, summary: data.summary as string };
    case "data_render":
      return {
        ...result,
        renderSpec: data.render_spec as RenderSpec,
        chartType: data.chart_type as ChartType,
      };
    default:
      return result;
  }
}

// 单步归约：把一条 SSE 事件应用到 turn 上（进度 + 结果 + 状态）。
export function applyEventToTurn(turn: ChatTurn, event: SSEEvent): ChatTurn {
  const progress = reduceProgress(turn.progress, event);
  let { status, result, error } = turn;

  switch (event.type) {
    case "task":
      status = event.payload.status === "finished" ? "finished" : "streaming";
      break;
    case "cancelled":
      status = "cancelled";
      break;
    case "error":
      status = "error";
      error = event.payload;
      break;
    default: {
      // 节点事件：合并 data。
      const data = event.payload.data as unknown as Record<string, unknown>;
      result = mergeNodeData(result, event.payload.node, data);
      // 澄清在 schema_inspector 处提前结束链路。
      if (event.payload.node === "schema_inspector" && result.clarification) {
        status = "clarifying";
      }
      break;
    }
  }

  return { ...turn, progress, status, result, error };
}

export function applyEventsToTurn(turn: ChatTurn, events: SSEEvent[]): ChatTurn {
  return events.reduce(applyEventToTurn, turn);
}
