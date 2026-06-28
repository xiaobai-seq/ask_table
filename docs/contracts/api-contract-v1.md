# Text2SQL 前后端接口契约 v1（冻结）

> 本文件是后端轨与前端轨**唯一的接口事实来源**。两轨在各自 worktree 并行开发：后端按本契约实现，前端按本契约对接（开发期可用 mock）。任何契约变更必须先改本文件并同步两轨，不得各自臆测。

基础约定：
- Base URL：开发期 `http://localhost:8000`，前端 dev server 代理 `/api/*` → 后端（或直连）。
- 编码：UTF-8，JSON `ensure_ascii=false`。
- 错误体统一结构：`{ "code": string, "message": string, "trace_id": string }`（HTTP 错误与 SSE error 事件一致；message 为对外安全文案，细节在服务端日志按 trace_id 关联）。

---

## 1. 流式查询 `POST /query`

请求体：
```json
{ "query": "按月份统计订单金额趋势", "session_id": "demo-session", "task_id": "可选，前端可自带便于取消" }
```
响应：`text/event-stream`（SSE）。响应头 `X-Task-ID: <task_id>`。

### SSE 事件格式
每条事件：
```
event: <event_name>
data: <json>

```
所有 data 均含 `task_id`。节点事件 data 形如 `{ "task_id", "node", "data": <节点增量状态> }`。

### 事件序列（正常链路）
1. `task` — `{ "task_id", "status": "started" }`
2. `schema_inspector`
3. `table_relationship`
4. `sql_generator`
5. `sql_repair`（**仅在需要自修复重试时出现，可 0~N 次**）
6. `sql_executor`
7. `summarize`
8. `data_render`
9. `task` — `{ "task_id", "status": "finished" }`

特殊事件（提前结束）：
- `cancelled` — `{ "cancelled": true }`（被 `POST /cancel` 取消）
- `error` — `{ "task_id", "code", "message", "trace_id" }`
- 若 `schema_inspector` 返回 `clarification`，链路在澄清处提前结束（前端渲染澄清卡片，不会有后续节点事件）。

### 各节点 `data` 字段（`data.data` 内）
- **schema_inspector**：`rewritten_query: string`、`db_info: TableInfo[]`、`retrieval_hits: RetrievalHit[]`、`clarification: Clarification | null`、`trace_id: string`
- **table_relationship**：`table_relationship: RelationshipPath[]`
- **sql_generator**：`sql_plan: SQLPlan`、`generated_sql: string | null`、`chart_type: ChartType`
- **sql_repair**：`attempts: number`、`generated_sql: string | null`、`sql_plan: SQLPlan`（每次重试一条事件，前端展示"修复中/第 N 次"）
- **sql_executor**：`execution_result: ExecutionResult`
- **summarize**：`summary: string`
- **data_render**：`render_spec: RenderSpec`、`chart_type: ChartType`

### 数据结构（与后端 dataclass 一一对应，字段名即 JSON key）
```ts
type ChartType =
  | "line" | "bar" | "stacked_bar" | "horizontal_bar" | "pie" | "donut"
  | "scatter" | "bubble" | "heatmap" | "treemap" | "sankey" | "funnel"
  | "radar" | "gauge" | "table" | "area" | "stacked_area" | "histogram"
  | "boxplot" | "waterfall" | "map" | "candlestick" | "kpi";

interface ColumnInfo { name: string; data_type: string; comment: string; nullable: boolean; primary_key: boolean; semantic_tags: string[]; }
interface ForeignKeyInfo { source_table: string; source_column: string; target_table: string; target_column: string; }
interface TableInfo { name: string; comment: string; columns: ColumnInfo[]; foreign_keys: ForeignKeyInfo[]; semantic_tags: string[]; row_count: number | null; }
interface RelationshipPath { source: string; target: string; joins: ForeignKeyInfo[]; }
interface RetrievalHit { table: TableInfo; score: number; bm25_rank: number | null; vector_rank: number | null; rerank_score: number | null; reasons: string[]; }
interface Clarification { question: string; options: string[]; reason: string; }
interface SQLPlan { sql: string | null; chart_type: ChartType; reasoning: string; confidence: number; advanced_features: string[]; warnings: string[]; }
interface ExecutionResult { columns: string[]; rows: Record<string, unknown>[]; row_count: number; elapsed_ms: number; error: string | null; }
interface RenderSpec { chart_type: ChartType; x: string | null; y: string[]; series: string | null; title: string; options: Record<string, unknown>; }
```

---

## 2. 取消 `POST /cancel/{task_id}`
- 成功：`{ "task_id": string, "cancelled": true }`
- 未找到：HTTP 404 + 标准错误体。

---

## 3. 会话与历史（阶段 2 新增）

### `GET /sessions`
返回会话列表，按更新时间倒序：
```json
{ "sessions": [ { "session_id": "demo-session", "title": "按月份统计订单金额趋势", "created_at": "ISO8601", "updated_at": "ISO8601", "turn_count": 5 } ] }
```

### `GET /sessions/{session_id}/history`
返回该会话所有轮次（按时间正序）：
```json
{ "session_id": "demo-session", "history": [ { "id": 123, "user_query": "...", "rewritten_query": "...", "generated_sql": "SELECT ...", "tables": ["orders"], "summary": "...", "chart_type": "line", "row_count": 12, "elapsed_ms": 34.5, "trace_id": "...", "status": "success", "created_at": "ISO8601" } ] }
```

### `GET /history/{id}`
返回单条历史明细（字段同上 history 元素；可附完整 `render_spec` 与 `execution_result` 以便前端回看时重绘）：
```json
{ "id": 123, "session_id": "demo-session", "user_query": "...", "generated_sql": "...", "summary": "...", "chart_type": "line", "render_spec": { /* RenderSpec */ }, "execution_result": { /* ExecutionResult */ }, "created_at": "ISO8601" }
```

### `DELETE /sessions/{session_id}`
删除会话及其历史：`{ "session_id": string, "deleted": true }`；未找到 404。

### `DELETE /history/{id}`（可选）
删除单条历史：`{ "id": number, "deleted": true }`；未找到 404。
- 仅删除该条记录；即使会话因此清空也**保留会话元信息**（`turn_count` 归零、`title` 保留），会话仍出现在 `GET /sessions` 中。
- 如需清空并移除整个会话，请使用 `DELETE /sessions/{id}`。

### `GET /healthz`
健康检查：`{ "status": "ok" }`（200）。

---

## 4. 前端 chart_type → ECharts 映射约定
- 后端负责**推荐** `render_spec`（含 `chart_type`、`x`、`y[]`、`series`、`title`）；前端 `chartAdapter` 负责把 `chart_type` + `execution_result`（columns/rows）+ `render_spec` 转成 ECharts option。
- 阶段 3 前端**至少**实现：`line`、`bar`（含 `horizontal_bar`/`stacked_bar`）、`pie`/`donut`、`area`、`scatter`、`kpi`、`table`。其余 ChartType 先回退为 `table` 渲染（前端不得因未知类型崩溃）。
- `table` 直接用 `execution_result.columns` + `rows` 渲染表格。

---

## 5. 限流与错误
- 限流维度：
  - `POST /query` 按 **`session_id`（请求 body）** 维度限流，确保多会话相互独立、同一 IP 不会互相挤占配额；
  - 其它端点按**客户端 IP** 维度限流；
  - `GET /healthz` 豁免限流。
- 阈值取 `Settings.rate_limit_per_minute`（默认 60/分钟，令牌桶）。
- 超过限流：HTTP 429 + 标准错误体（`code: "rate_limited"`）。
- Redis 故障策略：限流默认接 Redis（配置 `redis_url` 时），初始化不可用时降级为进程内内存令牌桶；Redis 运行期异常时放行并告警（fail-open，优先保可用性）。
- 任何 5xx：标准错误体，前端按 `message` 提示并保留 `trace_id` 供反馈。
