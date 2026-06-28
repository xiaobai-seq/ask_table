# 企业级 Text2SQL 改造设计

日期：2026-06-28
状态：已评审通过（brainstorming 阶段产出）

## 1. 背景与目标

当前仓库是一个可运行的 Text2SQL 后端 demo / 骨架（约 2800 行 Python，无前端、无 git 历史）。已有能力：LangGraph 6 节点工作流、BM25+向量+RRF+rerank 混合召回、schema introspection、只读 SQL 校验、SSE 流式 API、会话记忆、澄清机制、Langfuse 可观测、评测 CLI。

目标：面向"业务人员无 SQL 能力却需自助取数"的痛点，将其改造为生产级自然语言数据问答系统，端到端实现"用户提问 → 表结构检索 → 表关系分析 → SQL 生成 → SQL 执行 → 数据总结 → 可视化渲染"的闭环，做到零门槛、实时反馈、高准确率。

## 2. 交付范围（已与用户确认）

纳入范围：
- 完整前端可视化界面（对话式问答 + 图表渲染 + 历史记录）。
- 后端生产化加固（持久化、限流、错误处理、配置管理）；**暂不做鉴权**。
- 准确率工程全套（few-shot 示例库、SQL 自修复重试、schema 语义增强、评测体系强化）。

不纳入（用户未选 / 显式降级）：
- 多数据源接入与管理（保留单库配置）。
- 部署运维重点投入（容器化 / CD 不做，CI 仅最小化）。

## 3. 关键决策与默认假设

- 执行策略：**方案 A 增量演进**（保留现有内核，轻度分层 + 分阶段叠加）。
- 后端：继续 Python + LangGraph；LLM 继续走 DashScope（沿用已抽象的 provider，不改动）。
- 查询目标库：保留 SQLite 样例库用于开发/测试；生产通过配置连**单个**真实库（MySQL/PG）。
- 元数据库：**MySQL**（SQLAlchemy 2.0 + Alembic 迁移）。
- 缓存/限流：**Redis 可选**，未配置时降级为内存令牌桶。
- 鉴权：本期不做（架构预留扩展点）。
- SQL 自修复：默认重试 **2 次**。
- schema 语义元数据：先用 **YAML 手工维护**（保留后续接口化扩展点）。
- 历史记录：提供删除接口（`DELETE /sessions/{id}`，可选 `DELETE /history/{id}`）。
- 前端：React 18 + TypeScript + Vite + Ant Design + ECharts + Zustand；企业浅色主题。
- CI：最小化（GitHub Actions 跑后端测试 + ruff + 前端 build/test），不做容器化 / CD。

## 4. 目标架构与仓库结构

```
text2SQL/
├── backend/                      # Python 后端（现有 src/ 迁入并轻度分层）
│   ├── src/text2sql/
│   │   ├── core/                 # 工作流内核：graph、nodes、retrieval、sql_generator…（现有逻辑迁入）
│   │   ├── accuracy/             # few-shot 库、self-repair、schema 语义增强
│   │   ├── persistence/          # MySQL 模型 + repository + Alembic 迁移
│   │   ├── api/                  # FastAPI 路由、SSE、限流、错误处理
│   │   └── config/               # 集中式 Settings（pydantic-settings）
│   ├── tests/
│   └── pyproject.toml
├── frontend/                     # React + TS SPA
│   ├── src/{pages,components,api,hooks,store}/
│   └── package.json
└── docs/plans/                   # 设计文档与实施计划
```

分层原则：现有 6 节点工作流不改变语义，仅按职责归位到 `core/`；新能力放进 `accuracy/`、`persistence/`，强化 `api/`。前端独立目录，通过 SSE/REST 与后端通信。

## 5. 分阶段路线图

每阶段独立可验证、可交付：

### 阶段 0 — 地基
- 建立首个 git commit（基线 demo）。
- 轻度分层目录（`src/` 迁入 `backend/`，按职责归位到 `core/`）。
- 集中配置（`pydantic-settings` 收敛散落的 `os.getenv`）。
- 统一错误处理骨架。
- 验证：测试全绿、API 可启动。

### 阶段 1 — 准确率闭环
- **Schema 语义增强**：表/列附中文别名、业务描述、枚举值字典（YAML 维护 + 可入库），并入召回语料与生成 prompt。
- **Few-shot 示例库**：MySQL `few_shot_examples`，向量检索 Top-K 相似优质示例注入 prompt；线上/评测通过 case 可回流。
- **SQL 自修复重试**：`sql_executor` 后加条件边，执行报错/空结果时回传 LLM 重生成，最多 2 次，每次重试经 SSE 透出，超限降级。工作流图新增 `sql_repair` 节点与回环。
- **评测强化**：扩充用例集；增加执行结果级比对（行/列/值）；结果落 `eval_runs` 支持多次对比与趋势看板。
- 验证：评测准确率指标上升、回归用例通过。

### 阶段 2 — 后端服务化加固
- **持久化（MySQL + SQLAlchemy 2.0 + Alembic）**：`sessions`、`query_history`、`few_shot_examples`、`schema_metadata`、`eval_runs`。
- `ConversationMemory` 改为 repository 接口 + 两实现（内存用于测试/降级，MySQL 用于生产），配置切换。
- **限流**：Redis（可选，降级内存令牌桶）按 session/IP 限流中间件。
- **统一错误处理**：全局异常处理器返回 `{code, message, trace_id}`；SSE 以 `error` 事件透出。
- **REST 接口**：`GET /sessions`、`GET /sessions/{id}/history`、`GET /history/{id}`、`DELETE /sessions/{id}`、`GET /healthz`；CORS 配置。
- **可观测**：trace_id 贯穿日志与 `query_history`，关键节点结构化日志。
- 验证：历史可落库可查、限流生效、集成测试通过。

### 阶段 3 — 前端可视化
- 主问答页（对话式气泡）。
- 流式进度指示器：渲染 schema_inspector → table_relationship → sql_generator → sql_repair → sql_executor → summarize → data_render，随 SSE 点亮/报错。
- 结果区 Tab：图表（ECharts，按 `render_spec.chart_type` 自动渲染）/ 数据表格 / SQL（只读高亮）/ 业务摘要。
- 澄清交互卡片。
- 历史侧边栏：会话列表 + 单会话历史，支持回看与删除。
- 数据流：`POST /query` 建 SSE → 按 `node` 事件增量更新 → 完成刷新历史；取消调 `POST /cancel/{task_id}`。
- 渲染映射：前端 `chart_type → ECharts option` 适配器（后端推荐、前端渲染）。
- 验证：端到端从提问到图表闭环可演示。

## 6. 错误处理策略

- **节点级**：每节点 try/except，失败写入 `state.errors` 并经 SSE `error` 透出，非阻断不中断整图。
- **SQL 级**：校验拦截 → 执行失败进入自修复重试 → 超限降级返回错误体 + 已生成 SQL。
- **依赖级**：LLM→规则生成器、Redis→内存限流、MySQL→内存 repository，保持"缺依赖可跑"。
- **API 级**：全局异常处理器统一结构化错误返回。

## 7. 测试策略

- **后端单测**：accuracy/persistence/config/限流各自单测；repository 用内存实现测逻辑、SQLite 测 SQL 兼容。
- **工作流测试**：扩充自修复重试、澄清分支、错误降级路径。
- **评测回归**：强化后的 `eval` 作为准确率回归门禁。
- **前端**：Vitest + React Testing Library（SSE 解析、chart 映射适配器）；E2E（Playwright）可选不强制。
- **CI**：最小化 GitHub Actions（后端测试 + ruff + 前端 build/test）。

## 8. 对现有代码的主要影响

- 目录迁移：`src/` → `backend/src/`，import 路径调整。
- `graph.py`：新增 `sql_repair` 节点与条件回环。
- `retrieval.py` / `sql_generator.py`：注入 schema 语义与 few-shot。
- `context.py`：`ConversationMemory` 抽象为 repository 接口。
- `eval.py`：增强结果级比对与落库。
- `api.py`：新增 REST 接口、限流、全局异常处理、CORS。
- 新增 `config/`、`accuracy/`、`persistence/`、`frontend/`。
