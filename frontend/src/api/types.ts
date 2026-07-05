// 本文件严格对照《Text2SQL 前后端接口契约 v1》第 1、3 节。
// 字段名 / 事件名 / 端点结构必须与契约一致，前端不得擅自增删字段。

// ---------------------------------------------------------------------------
// 基础数据结构（契约 §1 数据结构，与后端 dataclass 一一对应）
// ---------------------------------------------------------------------------

export type ChartType =
  | "line"
  | "bar"
  | "stacked_bar"
  | "horizontal_bar"
  | "pie"
  | "donut"
  | "scatter"
  | "bubble"
  | "heatmap"
  | "treemap"
  | "sankey"
  | "funnel"
  | "radar"
  | "gauge"
  | "table"
  | "area"
  | "stacked_area"
  | "histogram"
  | "boxplot"
  | "waterfall"
  | "map"
  | "candlestick"
  | "kpi";

export interface ColumnInfo {
  name: string;
  data_type: string;
  comment: string;
  nullable: boolean;
  primary_key: boolean;
  semantic_tags: string[];
}

export interface ForeignKeyInfo {
  source_table: string;
  source_column: string;
  target_table: string;
  target_column: string;
}

export interface TableInfo {
  name: string;
  comment: string;
  columns: ColumnInfo[];
  foreign_keys: ForeignKeyInfo[];
  semantic_tags: string[];
  row_count: number | null;
}

export interface RelationshipPath {
  source: string;
  target: string;
  joins: ForeignKeyInfo[];
}

export interface RetrievalHit {
  table: TableInfo;
  score: number;
  bm25_rank: number | null;
  vector_rank: number | null;
  rerank_score: number | null;
  reasons: string[];
}

export interface Clarification {
  question: string;
  options: string[];
  reason: string;
}

export interface SQLPlan {
  sql: string | null;
  chart_type: ChartType;
  reasoning: string;
  confidence: number;
  advanced_features: string[];
  warnings: string[];
}

export interface ExecutionResult {
  columns: string[];
  rows: Record<string, unknown>[];
  row_count: number;
  elapsed_ms: number;
  error: string | null;
}

export interface RenderSpec {
  chart_type: ChartType;
  x: string | null;
  y: string[];
  series: string | null;
  title: string;
  options: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// SSE 节点事件名与各节点 data 负载（契约 §1 事件序列）
// ---------------------------------------------------------------------------

// 后端工作流节点顺序（sql_repair 为可选回环，可出现 0~N 次）。
export const NODE_SEQUENCE = [
  "schema_inspector",
  "table_relationship",
  "sql_generator",
  "sql_repair",
  "sql_executor",
  "summarize",
  "data_render",
] as const;

export type NodeName = (typeof NODE_SEQUENCE)[number];

export interface SchemaInspectorData {
  rewritten_query: string;
  db_info: TableInfo[];
  retrieval_hits: RetrievalHit[];
  clarification: Clarification | null;
  trace_id: string;
}

export interface TableRelationshipData {
  table_relationship: RelationshipPath[];
}

export interface SQLGeneratorData {
  sql_plan: SQLPlan;
  generated_sql: string | null;
  chart_type: ChartType;
}

export interface SQLRepairData {
  attempts: number;
  generated_sql: string | null;
  sql_plan: SQLPlan;
}

export interface SQLExecutorData {
  execution_result: ExecutionResult;
}

export interface SummarizeData {
  summary: string;
}

export interface DataRenderData {
  render_spec: RenderSpec;
  chart_type: ChartType;
}

// 每种节点名对应的 data.data 负载类型映射。
export interface NodeDataMap {
  schema_inspector: SchemaInspectorData;
  table_relationship: TableRelationshipData;
  sql_generator: SQLGeneratorData;
  sql_repair: SQLRepairData;
  sql_executor: SQLExecutorData;
  summarize: SummarizeData;
  data_render: DataRenderData;
}

// ---------------------------------------------------------------------------
// SSE 顶层事件（契约 §1 事件序列 + 特殊事件）
// ---------------------------------------------------------------------------

export interface TaskEventPayload {
  task_id: string;
  status: "started" | "finished";
}

export interface NodeEventPayload<N extends NodeName = NodeName> {
  task_id: string;
  node: N;
  data: NodeDataMap[N];
}

export interface CancelledEventPayload {
  task_id?: string;
  cancelled: true;
}

export interface ErrorEventPayload {
  task_id: string;
  code: string;
  message: string;
  trace_id: string;
}

// 解析后的强类型事件（判别联合，type 对应 SSE 的 event 名）。
export type SSEEvent =
  | { type: "task"; payload: TaskEventPayload }
  | { type: NodeName; payload: NodeEventPayload }
  | { type: "cancelled"; payload: CancelledEventPayload }
  | { type: "error"; payload: ErrorEventPayload };

// ---------------------------------------------------------------------------
// REST：会话与历史（契约 §3）
// ---------------------------------------------------------------------------

export interface SessionSummary {
  session_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  turn_count: number;
}

export interface SessionListResponse {
  sessions: SessionSummary[];
}

export interface HistoryTurn {
  id: number;
  user_query: string;
  rewritten_query?: string;
  generated_sql: string;
  tables: string[];
  summary: string;
  chart_type: ChartType;
  row_count: number;
  elapsed_ms: number;
  trace_id: string;
  status: string;
  created_at: string;
}

export interface SessionHistoryResponse {
  session_id: string;
  history: HistoryTurn[];
}

export interface HistoryDetail {
  id: number;
  session_id: string;
  user_query: string;
  generated_sql: string;
  summary: string;
  chart_type: ChartType;
  render_spec?: RenderSpec;
  execution_result?: ExecutionResult;
  created_at: string;
}

export interface DeleteSessionResponse {
  session_id: string;
  deleted: true;
}

export interface CancelResponse {
  task_id: string;
  cancelled: true;
}

export interface AppConfig {
  domain_profile: string;
  description: string;
  example_queries: string[];
  clarification_options: string[];
}

// 统一错误体（契约基础约定）。
export interface ApiError {
  code: string;
  message: string;
  trace_id: string;
}
