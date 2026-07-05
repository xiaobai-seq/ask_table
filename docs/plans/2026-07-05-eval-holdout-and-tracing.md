# Eval 去泄露 + 结果校验 + MySQL Trace 落盘 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development for each task (RED→GREEN→REFACTOR), commit frequently.

**Goal:** 用 holdout 集去除 few-shot 泄露、补 `expected_result` 结果校验、并把每次评测逐 case 全环节 trace 落 MySQL 以便回溯。

**Architecture:** 仅在评测侧与 persistence 层扩展；线上 workflow 只新增 `state["sql_prompt"]` 字段（无副作用）。新增 `eval_case_results` 表 + repository，`EvalResult` 携带 `trace` 一并进 JSON 报告与 MySQL。

**Tech Stack:** Python, dataclass, SQLAlchemy(ORM, JSON 列), unittest, DashScope(LLM, 仅最终跑评测用)。

---

## Task 1: `EvalResult` 携带 trace

**Files:**
- Modify: `backend/src/text2sql/core/models.py`（`EvalResult` 加 `trace: dict | None = None`）
- Test: `backend/tests/test_eval_tracing.py`（新建）

**测试要点(RED):** `to_plain(EvalResult("c", True, {}, "sql", (), trace={"prompt": "x"}))` 结果含 `trace.prompt == "x"`。
**实现(GREEN):** 给 frozen dataclass 末尾加可选字段（保持既有位置参数兼容）。
**Commit:** `feat(eval): EvalResult carries per-case trace`

## Task 2: workflow 暴露最终 prompt 到 state

**Files:**
- Read: `backend/src/text2sql/core/sql_generator.py`、`backend/src/text2sql/core/graph.py`
- Modify: 让 `agenerate`/`generate` 回传所用 prompt；`graph.py` `generate_sql` 节点写 `state["sql_prompt"]`
- Test: `backend/tests/test_eval_tracing.py`

**测试要点(RED):** 用模板(非 LLM)workflow 跑一个查询，`state.get("sql_prompt")` 非空且包含候选表段。
**实现(GREEN):** 最小改动暴露 prompt；不改变既有返回结构的其它字段。
**Commit:** `feat(graph): expose final sql prompt in state for tracing`

## Task 3: `eval_case_results` ORM 表

**Files:**
- Modify: `backend/src/text2sql/persistence/models.py`（新增 `EvalCaseResult`）
- Test: `backend/tests/test_eval_case_repository.py`（新建）

**测试要点(RED):** `init_models(engine)` 后 `EvalCaseResult` 表存在（SQLite 内存），可插入一行。
**实现(GREEN):** 按设计文档表结构定义；`prompt` 用 `Text`，结构化字段用 `JSON`。
**Commit:** `feat(db): add eval_case_results table`

## Task 4: `EvalCaseResultRecord` + repository

**Files:**
- Modify: `backend/src/text2sql/persistence/repository.py`
- Test: `backend/tests/test_eval_case_repository.py`

**测试要点(RED):**
- InMemory: `record_case_result(run_id, rec)` 后 `list_case_results(run_id)` 能取回，字段一致。
- SqlAlchemy(SQLite 内存): 同样往返，JSON 字段(retrieval_hits/metrics)保真。
**实现(GREEN):** 新增 `EvalCaseResultRecord` dataclass；`EvalRunRepository` 协议加 `record_case_result`/`list_case_results`；两实现补齐；缺库降级内存。
**Commit:** `feat(repo): persist and query per-case eval results`

## Task 5: eval.py 收集 trace 并落 MySQL

**Files:**
- Modify: `backend/src/text2sql/eval.py`（`run_case` 组装 trace；新增 `persist_case_results(repo, run_id, results)`；`main` 串联）
- Test: `backend/tests/test_eval_tracing.py`

**测试要点(RED):**
- `run_case`(用 fake workflow 返回带 retrieval_hits/sql_prompt/execution_result 的 state)产出的 `EvalResult.trace` 含 `retrieval_hits/prompt/generated_sql/execution_rows`。
- `persist_case_results(InMemoryEvalRunRepository, run_id, results)` 落库条数 == len(results)。
**实现(GREEN):** trace 从 state 提取，execution_rows 截断前 N 行；`main` 中 `record_run`→拿 `run_id`→`persist_case_results`。
**Commit:** `feat(eval): collect per-case trace and persist to metadata db`

## Task 6: Holdout 评测集 + `expected_result` 生成

**Files:**
- Create: `backend/examples/ecommerce/eval_cases_holdout.jsonl`
- Create: `backend/scripts/gen_holdout_expected.py`（跑 expected_sql 生成 expected_result 并回填）

**步骤:**
1. 编写 15-18 条全新分析问题 + `expected_sql`(人工)，覆盖各难度，确保与 few_shot 无逐条对应。
2. 脚本连电商 SQLite 库执行每条 `expected_sql`，自检可执行且非空，写回 `expected_result`。
3. 人工抽查若干条 SQL 语义正确。
**Commit:** `test(eval): add holdout eval set with verified expected_result`

## Task 7: 跑对照评测并落库（验证）

**步骤:**
1. `source .env`，分别跑 `eval_cases.jsonl`(开卷) 与 `eval_cases_holdout.jsonl`(holdout)，LLM 模式。
2. 确认逐 case trace 落 `eval_case_results`(MySQL)，聚合落 `eval_runs`；本地 JSON 报告生成。
3. 对比开卷 vs holdout pass_rate 与 `value_set_exact`，量化泄露，产出 bad case 清单。

## Task 8: 回归 + 文档

**步骤:**
1. 干净环境跑全量 unittest 绿。
2. 更新 `backend/examples/ecommerce/README`(如有) 说明 holdout 与 trace 表。
3. 汇总结论。
**Commit:** `docs(eval): document holdout comparison and tracing`
