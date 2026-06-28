# 企业级 Text2SQL 改造实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把现有 Text2SQL demo 骨架改造为生产级自然语言数据问答系统，端到端实现"提问 → 检索 → 关系 → 生成 → 执行 → 总结 → 可视化"闭环。

**Architecture:** 方案 A 增量演进——保留现有 LangGraph 内核，按职责轻度分层（`core/accuracy/persistence/api/config`），分 4 阶段叠加准确率工程、MySQL 持久化与服务化加固、React 前端。每阶段独立可验证、可交付。

**Tech Stack:** Python 3.10+ / LangGraph / FastAPI / SQLAlchemy 2.0 + Alembic / MySQL / Redis（可选）/ pydantic-settings / React 18 + TypeScript + Vite + Ant Design + ECharts + Zustand。

**关联设计文档:** `docs/plans/2026-06-28-enterprise-text2sql-transformation-design.md`

**贯穿原则:** DRY、YAGNI、TDD（先写失败测试）、小步频繁提交、surgical 改动（每行改动可追溯到设计）。现有"缺依赖可降级跑测试"的特性必须保留。

---

## 阶段 0 — 地基

目标：分层目录 + 集中配置 + 统一错误骨架，测试全绿、API 可起。

### Task 0.1：目录迁移到 backend/ 并修复 import

**Files:**
- Move: `src/` → `backend/src/`，`tests/` → `backend/tests/`，`pyproject.toml` → `backend/pyproject.toml`，`examples/` → `backend/examples/`
- Modify: `backend/pyproject.toml`（`packages.find` where 仍为 `src`）

**Step 1:** `git mv` 迁移目录（保留历史）：
```bash
mkdir -p backend && git mv src backend/src && git mv tests backend/tests && git mv pyproject.toml backend/pyproject.toml && git mv examples backend/examples
```
**Step 2:** 运行测试确认（在 `backend/` 下）：
Run: `cd backend && python3 -m unittest discover -s tests`
Expected: 全部 PASS（包内 import 用的是 `text2sql.*` 绝对包名，迁移目录不影响）。
**Step 3:** 修正 README 中的路径与启动命令（`uvicorn text2sql.api:app` 需在 `backend/` 下执行）。
**Step 4:** Commit：
```bash
git add -A && git commit -m "refactor: move backend into backend/ directory"
```

### Task 0.2：核心模块归位到 core/ 子包

**Files:**
- Move: `backend/src/text2sql/{graph,retrieval,rerank,tokenization,embeddings,schema,relationships,sql_generator,sql_validator,executor,summarizer,render,clarification,context,observability,models,llm,sample_data}.py` → `backend/src/text2sql/core/`
- Create: `backend/src/text2sql/core/__init__.py`（re-export 现有公共符号，保持 `from text2sql.core import ...`）
- Keep at top level: `api.py`、`eval.py`、`__init__.py`

**Step 1:** 写"导入契约"测试（先失败）：`backend/tests/test_package_layout.py`
```python
def test_core_exports():
    from text2sql.core import Text2SQLWorkflow, AgentState
    assert Text2SQLWorkflow is not None
```
**Step 2:** Run: `python3 -m unittest tests.test_package_layout -v` → Expected: FAIL（ImportError）。
**Step 3:** `git mv` 模块进 `core/`，更新模块间相对/绝对 import（`text2sql.X` → `text2sql.core.X`），在 `core/__init__.py` re-export。更新 `api.py`、`eval.py` 的 import。
**Step 4:** Run: `python3 -m unittest discover -s tests` → Expected: 全 PASS。
**Step 5:** Commit：`git commit -am "refactor: group workflow internals under core package"`

### Task 0.3：集中式配置 config/

**Files:**
- Create: `backend/src/text2sql/config/__init__.py`、`backend/src/text2sql/config/settings.py`
- Create: `backend/tests/test_config.py`
- Modify: `backend/src/text2sql/api.py`（用 settings 替代散落 `os.getenv`）

**Step 1:** 写失败测试：默认值 + 环境变量覆盖。
```python
def test_settings_defaults_and_override(monkeypatch):
    from text2sql.config.settings import Settings
    s = Settings()
    assert s.sql_repair_max_retries == 2
    monkeypatch.setenv("TEXT2SQL_SQL_REPAIR_MAX_RETRIES", "3")
    assert Settings().sql_repair_max_retries == 3
```
**Step 2:** Run → FAIL。
**Step 3:** 用 `pydantic-settings` 实现 `Settings`（字段：`database_url`(查询库)、`metadata_database_url`(MySQL 元数据库)、`redis_url|None`、`llm_*`、`sql_repair_max_retries=2`、`rate_limit_*`、`cors_origins`、`few_shot_top_k`）。env 前缀 `TEXT2SQL_`。把 `pydantic-settings` 加入 `pyproject.toml` 依赖。
**Step 4:** Run → PASS。`api.py` startup 改用 `Settings()`。
**Step 5:** Commit：`git commit -am "feat: centralized settings via pydantic-settings"`

### Task 0.4：统一错误处理骨架

**Files:**
- Create: `backend/src/text2sql/api/errors.py`（结构化错误体 `{code, message, trace_id}` + 全局异常处理器注册函数）
- Create: `backend/tests/test_api_errors.py`
- Modify: `backend/src/text2sql/api.py`（注册异常处理器；`stream_query` 增加 `error` SSE 事件）

> 注：此处把 `api.py` 拆成 `api/` 子包（`api/__init__.py` 暴露 `app`/`create_app`，`api/errors.py` 等）。

**Step 1:** 写失败测试：构造错误返回结构化体含 trace_id。
**Step 2:** Run → FAIL。
**Step 3:** 实现错误模型与处理器；workflow 节点异常被捕获为 `error` 事件而非中断（与设计 §6 一致）。
**Step 4:** Run → PASS；`unittest discover` 全绿。
**Step 5:** Commit：`git commit -am "feat: unified structured error handling for API and SSE"`

**阶段 0 验收:** `cd backend && python3 -m unittest discover -s tests` 全绿；`uvicorn text2sql.api:app` 可启动；`/healthz` 暂未加（阶段 2）。

---

## 阶段 1 — 准确率闭环

目标：schema 语义增强 + few-shot 库 + SQL 自修复重试 + 评测强化，准确率指标可量化上升。

### Task 1.1：Schema 语义元数据（YAML）

**Files:**
- Create: `backend/src/text2sql/accuracy/__init__.py`、`backend/src/text2sql/accuracy/schema_semantics.py`
- Create: `backend/examples/schema_metadata.yaml`（表/列：`alias`、`description`、`enum_values`）
- Create: `backend/tests/test_schema_semantics.py`
- Modify: `core/retrieval.py`（语义并入召回语料）、`core/sql_generator.py`（语义注入 prompt）

**Steps（TDD）:**
1. 失败测试：加载 YAML → 给定表名返回中文别名/列描述/枚举词典；缺失时安全降级为空。
2. Run → FAIL。
3. 实现 `SchemaSemantics`：加载 YAML、提供 `enrich_corpus(table)` 与 `prompt_hints(tables)`。
4. 在 `HybridTableRetriever` 建语料处拼接别名/描述；在 SQL prompt 组装处注入枚举词典提示。
5. Run 全测 → PASS（含原有检索/生成测试不回归）。
6. Commit：`git commit -am "feat: schema semantic enrichment for retrieval and SQL prompt"`

### Task 1.2：Few-shot 示例库（先内存 + 接口，落库在阶段 2）

**Files:**
- Create: `backend/src/text2sql/accuracy/few_shot.py`（`FewShotStore` 接口 + 内存实现；向量相似检索 Top-K）
- Create: `backend/examples/few_shot_seed.jsonl`（初始优质 问题→SQL 示例）
- Create: `backend/tests/test_few_shot.py`
- Modify: `core/sql_generator.py`（生成前注入 Top-K 示例）

**Steps:**
1. 失败测试：给定问题，返回最相似的 K 条示例；空库返回空、生成器照常降级。
2. Run → FAIL。
3. 实现内存 `FewShotStore`（复用现有 `embeddings.py`；无 embedding 依赖时降级为关键词相似）。`few_shot_top_k` 取自 settings。
4. 在 `PromptedSQLGenerator` prompt 中注入示例块。
5. Run → PASS。
6. Commit：`git commit -am "feat: few-shot example retrieval injected into SQL prompt"`

### Task 1.3：SQL 自修复重试节点

**Files:**
- Modify: `core/graph.py`（新增 `sql_repair` 节点 + `executor → 判定 → repair/继续` 条件回环；重试上限取 settings，默认 2）
- Modify: `core/sql_generator.py`（新增 `aregenerate_with_error(sql, error, schema, ...)`）
- Modify: `backend/tests/test_workflow_and_eval.py`（新增重试路径用例）

**Steps:**
1. 失败测试：mock executor 首次报错、二次成功 → 最终 state 含成功结果且 `attempts==1`；连续失败到上限 → 降级返回错误体 + 已生成 SQL。
2. Run → FAIL。
3. 实现 repair 节点与条件边（手写 fallback 链路与 LangGraph 链路保持同一语义，见现有 `astream`）。每次重试经 SSE 透出（节点名 `sql_repair`）。
4. Run → PASS（澄清分支、空 SQL 提前结束等既有语义不回归）。
5. Commit：`git commit -am "feat: SQL self-repair retry loop in workflow"`

### Task 1.4：评测体系强化

**Files:**
- Modify: `backend/src/text2sql/eval.py`（增加执行结果级比对：行数/列集/值集对比；指标聚合）
- Modify: `backend/examples/eval_cases.jsonl`（扩充用例，含期望结果或期望 SQL）
- Create: `backend/tests/test_eval_metrics.py`

**Steps:**
1. 失败测试：给定预期结果集与实际结果集 → 计算结果级匹配指标（精确/部分）。
2. Run → FAIL。
3. 实现结果级比对与报告聚合（落库在阶段 2 接入）。
4. Run → PASS；跑一次 `python3 -m text2sql.eval ...` 生成报告。
5. Commit：`git commit -am "feat: result-level comparison in evaluation"`

**阶段 1 验收:** 评测报告含结果级准确率指标且较基线提升；所有单测含新路径全绿。

---

## 阶段 2 — 后端服务化加固

目标：MySQL 持久化 + 限流 + REST 接口 + 健康检查 + 可观测。

### Task 2.1：持久化基础（SQLAlchemy + Alembic）

**Files:**
- Create: `backend/src/text2sql/persistence/{__init__,models,db}.py`（ORM 模型：`sessions/query_history/few_shot_examples/schema_metadata/eval_runs`；engine/session 工厂）
- Create: `backend/alembic/`（init + 首个 migration）
- Create: `backend/tests/test_persistence_models.py`
- Modify: `pyproject.toml`（`alembic`、`pymysql` 驱动）

**Steps:**
1. 失败测试：用 SQLite in-memory 建表、写读一条 `query_history`。
2. Run → FAIL。
3. 定义 ORM 模型 + `db.py`（从 settings 取 `metadata_database_url`）；`alembic init` 并生成首版迁移。
4. Run → PASS（测试用 SQLite，生产 MySQL）。
5. Commit：`git commit -am "feat: persistence models and alembic migrations"`

### Task 2.2：会话/历史 repository（双实现）

**Files:**
- Create: `backend/src/text2sql/persistence/repository.py`（`HistoryRepository` 接口 + 内存实现 + SQLAlchemy 实现）
- Modify: `core/context.py`（`ConversationMemory` 依赖 repository，默认内存实现，可注入 DB 实现）
- Create: `backend/tests/test_history_repository.py`

**Steps:**
1. 失败测试：写入轮次 → 读取会话历史；两实现行为一致（参数化）。
2. Run → FAIL。
3. 实现接口与两实现；workflow `_remember_turn` 落库。
4. Run → PASS（内存实现保证离线测试不依赖 MySQL）。
5. Commit：`git commit -am "feat: history repository with memory and SQL implementations"`

### Task 2.3：限流中间件（Redis 可选降级内存）

**Files:**
- Create: `backend/src/text2sql/api/rate_limit.py`（令牌桶；Redis 实现 + 内存降级）
- Create: `backend/tests/test_rate_limit.py`
- Modify: `backend/src/text2sql/api/__init__.py`（注册中间件，阈值取 settings）

**Steps:** 失败测试（超阈值返回 429）→ FAIL → 实现 → PASS → Commit `git commit -am "feat: session/IP rate limiting with redis fallback"`

### Task 2.4：REST 接口 + 健康检查 + CORS

**Files:**
- Modify: `backend/src/text2sql/api/__init__.py`（新增 `GET /sessions`、`GET /sessions/{id}/history`、`GET /history/{id}`、`DELETE /sessions/{id}`、可选 `DELETE /history/{id}`、`GET /healthz`；CORSMiddleware）
- Create: `backend/tests/test_api_endpoints.py`（用 FastAPI TestClient，repository 用内存实现）

**Steps:** 失败测试（列表/详情/删除/healthz）→ FAIL → 实现 → PASS → Commit `git commit -am "feat: history REST endpoints, health check, CORS"`

### Task 2.5：评测结果落库 + trace 贯穿

**Files:**
- Modify: `eval.py`（结果写 `eval_runs`，支持多次对比）
- Modify: `core/graph.py` / `core/observability.py`（trace_id 写入 `query_history`，关键节点结构化日志）

**Steps:** 失败测试 → 实现 → PASS → Commit `git commit -am "feat: persist eval runs and propagate trace id"`

**阶段 2 验收:** 历史可落 MySQL 并经 REST 查询/删除；限流生效；`/healthz` 正常；集成测试全绿。

---

## 阶段 3 — 前端可视化

目标：React SPA 实现对话式问答 + SSE 流式进度 + ECharts 图表 + 历史记录闭环。

### Task 3.1：前端脚手架

**Files:** Create `frontend/`（Vite + React + TS + Ant Design + ECharts + Zustand），`frontend/package.json`、`vite.config.ts`、`tsconfig.json`、基础 `App.tsx`、代理后端的 dev server 配置。
**Steps:** 初始化 → `npm install` → `npm run build` 通过 → Commit `git commit -am "chore: scaffold React+TS frontend"`

### Task 3.2：SSE 客户端 + 类型

**Files:** Create `frontend/src/api/sse.ts`（`POST /query` 流读取，按 `node` 事件分发）、`frontend/src/api/types.ts`、`frontend/src/api/__tests__/sse.test.ts`（Vitest）。
**Steps:** 失败测试（解析多事件流）→ 实现 → PASS → Commit。

### Task 3.3：流式进度指示器组件

**Files:** Create `frontend/src/components/Progress/StepIndicator.tsx`（schema_inspector → table_relationship → sql_generator → sql_repair → sql_executor → summarize → data_render，随事件点亮/报错）+ 测试。
**Steps:** 失败测试 → 实现 → PASS → Commit。

### Task 3.4：图表渲染适配器 + 结果区

**Files:** Create `frontend/src/components/Result/chartAdapter.ts`（`chart_type → ECharts option`：line/bar/pie/table）+ 测试；`frontend/src/components/Result/ResultTabs.tsx`（图表/表格/SQL/摘要 Tab）。
**Steps:** 失败测试（各 chart_type 映射）→ 实现 → PASS → Commit。

### Task 3.5：主问答页 + 澄清交互 + 取消

**Files:** Create `frontend/src/pages/Chat.tsx`（输入、对话气泡、接 SSE、澄清卡片、取消按钮调 `POST /cancel/{task_id}`）、`frontend/src/store/chat.ts`（Zustand）。
**Steps:** 组件关键逻辑测试 → 实现 → 手动端到端联调（启动后端 + 前端）→ Commit。

### Task 3.6：历史侧边栏（列表/回看/删除）

**Files:** Create `frontend/src/components/History/HistorySidebar.tsx`（接 `GET /sessions`、`GET /sessions/{id}/history`、`DELETE /sessions/{id}`）+ 测试。
**Steps:** 失败测试 → 实现 → PASS → 端到端验证 → Commit。

**阶段 3 验收:** 浏览器中从输入问题 → 实时看到节点进度 → 渲染图表/表格/SQL/摘要 → 写入并可回看/删除历史，完整闭环可演示。

---

## 阶段 4 — 最小化 CI（收尾）

### Task 4.1：GitHub Actions

**Files:** Create `.github/workflows/ci.yml`（job1：`cd backend && pip install -e . && python -m unittest discover -s tests` + `ruff check`；job2：`cd frontend && npm ci && npm run build && npm test`）。
**Steps:** 写 workflow → 本地用 `act` 或 push 验证 → Commit `git commit -am "ci: minimal backend+frontend pipeline"`

**验收:** push 后 CI 绿。

---

## 执行顺序与提交纪律

- 严格按 阶段 0 → 1 → 2 → 3 → 4 顺序；每个 Task 内部按 TDD 步骤，先红后绿。
- 每个 Task 结束即 commit，commit message 用 `feat/refactor/chore/ci/docs` 前缀。
- 任何阶段结束跑一次完整后端测试 + （阶段 3 后）前端测试，确保无回归。
- 保持现有"缺依赖降级"特性：MySQL/Redis/LLM 不可用时测试仍可离线跑。
