# Enterprise Text2SQL

一个面向「业务人员无 SQL 能力却需自助取数」的生产级自然语言数据问答系统，前后端分离的 monorepo：

- **后端（`backend/`，Python + LangGraph）**：schema 检索 → 关系分析 → SQL 生成 → SQL 执行 → 数据总结 → 可视化推荐的流式工作流。
- **前端（`frontend/`，React + TypeScript）**：对话式问答、SSE 流式节点进度、ECharts 图表渲染、历史记录回看/删除。

后端核心能力：

- BM25 + 向量召回 + RRF + rerank 的三层混合表检索，schema 指纹缓存与持久化向量索引。
- schema 语义增强（中文别名/业务描述/枚举词典）+ few-shot 示例库注入，提升中文取数准确率。
- 约束化 SQL prompt、复杂查询策略、模糊问题澄清；执行报错/空结果时进入 `sql_repair` 自修复重试（默认 2 次）。
- MySQL 持久化（会话/查询历史/评测结果）、会话/IP 限流（Redis 可选降级内存）、统一结构化错误与 trace。
- SSE 流式响应、任务取消、结果级比对的评测 CLI 与样例数据。

仓库结构：

```
backend/    Python 后端（src/text2sql: core/ accuracy/ persistence/ api/ config/）
frontend/   React + TS 前端（Vite + Ant Design + ECharts + Zustand）
docs/       设计文档、实施计划与冻结的接口契约（docs/contracts/api-contract-v1.md）
```

本仓库默认可在缺少 DashScope、FAISS、Neo4j、SQLAlchemy、FastAPI、MySQL、Redis 的本地环境中运行核心测试；安装依赖后会自动启用对应生产适配器。

## 链路速览

一次查询的主链路在 `backend/src/text2sql/core/graph.py` 的 `Text2SQLWorkflow` 中编排：

1. `schema_inspector`：用 `ConversationMemory` 改写追问，并通过 `HybridTableRetriever` 检索候选表。
2. `table_relationship`：用外键图或 Neo4j 解析候选表之间的 JOIN 路径。
3. `sql_generator`：优先走 LLM prompt，未配置或失败时走规则生成器，产出 `SQLPlan`。
4. `sql_executor`：用 `SQLValidator` 拦截非只读 SQL 和未知表字段，再执行查询。
5. `summarize`：把执行结果转成业务摘要，LLM 不可用时用本地统计兜底。
6. `data_render`：根据 SQL 计划和结果字段推荐图表类型，并把本轮结果写入会话记忆。

API 层位于 `backend/src/text2sql/api/`（`text2sql.api` 包），只负责把上述节点的增量状态包装成 SSE 事件。

## 快速开始

后端代码位于 `backend/`，以下命令均在该目录下执行：

```bash
cd backend
PYTHONPATH=src python3 -m text2sql.core.sample_data --output examples/demo.db
PYTHONPATH=src python3 -m unittest discover -s tests
```

安装依赖并启动 API（推荐）：

```bash
./scripts/start-backend.sh
```

脚本默认读取根目录 `.env`，必要时创建/复用 `backend/.venv` 并执行 `pip install -e backend`，在缺少样例库时生成 `backend/examples/demo.db`，并启动 `http://127.0.0.1:8000`。常用覆盖项：

```bash
TEXT2SQL_API_PORT=8001 ./scripts/start-backend.sh
TEXT2SQL_API_RELOAD=0 ./scripts/start-backend.sh
TEXT2SQL_INSTALL_DEPS=0 ./scripts/start-backend.sh
```

启用 LLM 时，SQL 生成和结果总结都使用 `DASHSCOPE_LLM_MODEL`，默认
`qwen3.7-plus`。

也可以手动启动：

```bash
cd backend
python3 -m pip install -e .
python3 -m text2sql.core.sample_data --output examples/demo.db
uvicorn text2sql.api:app --reload
```

评测：

```bash
cd backend
python3 -m text2sql.eval --db examples/demo.db --cases examples/eval_cases.jsonl --report examples/eval_report.json
```

前端（需后端在 `localhost:8000` 运行，dev server 默认把 `/api/*` 代理到后端）：

```bash
cd frontend
npm install
npm run dev      # 启动开发服务器
npm test         # 单元测试（vitest）
npm run build    # 生产构建
```

## 电商测试数据集（复杂多表场景）

除内置 `demo.db` 外，仓库提供一套约 30 张表的电商场景数据集（用户/会员、商品 SPU-SKU、库存、购物车、订单、支付、物流、售后、营销、评价、行为等 12 个业务域，中等规模真实感确定性数据），用于在「表多、关系复杂」场景下测试系统的表检索、JOIN 路径分析与复杂 SQL 生成。

生成数据（同时产出 SQLite 库与 MySQL 建表脚本，固定随机种子可复现）：

```bash
cd backend
PYTHONPATH=src python3 -m text2sql.core.ecommerce_data \
  --sqlite-out examples/ecommerce/ecommerce.db \
  --mysql-out  examples/ecommerce/ecommerce_mysql.sql
```

通过环境变量把整套资产（查询库 + schema 语义 + few-shot）切到电商场景后评测：

```bash
cd backend
TEXT2SQL_SCHEMA_METADATA_PATH=./examples/ecommerce/schema_metadata.yaml \
TEXT2SQL_FEW_SHOT_SEED_PATH=./examples/ecommerce/few_shot_seed.jsonl \
PYTHONPATH=src python3 -m text2sql.eval \
  --db examples/ecommerce/ecommerce.db \
  --cases examples/ecommerce/eval_cases.jsonl \
  --report examples/ecommerce/eval_report.json
```

启动 API 时另设 `TEXT2SQL_DATABASE_URL=sqlite:///./examples/ecommerce/ecommerce.db`，问答即走电商库。

> `ecommerce.db` 与 `ecommerce_mysql.sql` 是确定性生成物，已被 `.gitignore` 忽略、随时可复现；仓库只提交 `schema_metadata.yaml`、`few_shot_seed.jsonl`、`eval_cases.jsonl` 三份文本资产。评测在本地无 LLM 时走规则生成器，仅趋势/环比/同比/递归等内置模板可通过；配置 `DASHSCOPE_API_KEY` 与 `TEXT2SQL_USE_LLM=1` 后 few-shot 生效，复杂查询准确率显著提升。

## API

`POST /query`

```json
{
  "query": "按月份统计订单金额趋势，并计算环比增长率",
  "session_id": "demo-session"
}
```

响应为 SSE，事件会按节点持续返回：`schema_inspector`、`table_relationship`、`sql_generator`、`sql_repair`（自修复重试时出现）、`sql_executor`、`summarize`、`data_render`。

`POST /cancel/{task_id}` 可取消长查询。会话与历史接口（`GET /sessions`、`GET /sessions/{id}/history`、`DELETE /sessions/{id}` 等）及完整事件结构见 `docs/contracts/api-contract-v1.md`。

## 设计重点

对话上下文通过 `ConversationMemory` 做 session 级窗口管理、指代补全和历史 SQL/表结构继承。

评测通过 JSONL case 管理，输出表召回、SQL 关键词、精确匹配、执行成功率等指标，适合频繁回归。

复杂 SQL 通过 prompt 规则和生成策略共同支持窗口函数、同比/环比、排名、滚动统计和递归 CTE。对于信息不足或歧义问题，系统返回澄清问题，而不是冒进生成 SQL。
