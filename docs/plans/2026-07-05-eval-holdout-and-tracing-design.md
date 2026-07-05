# Eval 去泄露 + 结果校验 + MySQL 全环节 Trace 落盘（设计）

日期：2026-07-05
分支：`feat/eval-tracing-and-holdout`（git worktree，隔离于其他 agent）

## 背景与问题

对电商数据集用 LLM 跑评测得到 19/21，但复盘发现该分数不可信：

1. **数据泄露（开卷）**：`eval_cases.jsonl` 的 21 条里 15 条与 `few_shot_seed.jsonl` 的问题**逐字完全相同**，其余 6 条仅措辞微调。few-shot 检索会命中同题标准答案并注入 prompt，LLM 直接照抄。
2. **指标太松**：`0/21` 配了 `expected_result`／`expected_sql`。`keyword_recall`（有无 join/sum/group by）+ `execution_success`（能否执行）都不校验结果正确性——一条驴唇不对马嘴的 SQL 也能满分。
3. **无法回溯**：只有聚合结果落 `eval_runs` 表；逐 case 的中间环节（检索命中、prompt、few-shot、执行行）完全没落盘。

## 目标

1. **Holdout 去泄露评测集**：用 few-shot 里没有的全新问题，测真实泛化。
2. **结果校验落地**：补 `expected_result`，让已存在的 `compare_result_sets` 生效，`value_set_exact` 纳入 pass 判定。
3. **每次评测逐 case 全环节 trace 落 MySQL**：便于事后回溯与多次对比。

## 非目标

- 修检索（选表召回）——另立任务，本次只把问题量化暴露出来。
- 改线上 workflow 行为——仅新增一个 state 字段存 prompt，无副作用。

## Part 1：Holdout 评测集

- 新建 `backend/examples/ecommerce/eval_cases_holdout.jsonl`，约 15-18 条**全新分析问题**，保证与 `few_shot_seed.jsonl` 无逐条对应。
- 难度覆盖：单表聚合、双表 JOIN、多跳桥接（`order_items→skus→spus→categories/brands`）、窗口函数、递归 CTE、子查询、时间过滤、多条件筛选。
- 每条字段：`case_id, query, expected_sql, expected_result, expected_tables, required_sql_keywords, allow_clarification`。
- 保留原 `eval_cases.jsonl` 作「开卷对照」，两份分别跑，量化 few-shot 泄露带来的虚高。

## Part 2：结果校验

- `eval.py` 已有 `compare_result_sets`（行数/列集/值集精确/值集召回）与对 `case.expected_result` 的比对分支，**无需改比对代码**。
- 关键是补数据：每条 holdout 的 `expected_sql` 由人工编写并在电商 SQLite 库上验证正确，跑一次得到 `expected_result` 一并写入 JSONL。
- pass 判定沿用现有逻辑（所有 metrics ≥ 1.0），此时会强制要求 `value_set_exact == 1.0`。

## Part 3：MySQL 全环节 Trace 落盘

### 3.1 workflow 暴露 prompt

- `sql_generator.agenerate` 把最终 prompt 回传（或存入 `SQLPlan`）；`graph.py` 的 `generate_sql` 节点把它写进 `state["sql_prompt"]`。
- 仅新增字段，线上 API 不读取即无影响。

### 3.2 新增 ORM 表 `eval_case_results`

`backend/src/text2sql/persistence/models.py` 新增：

| 列 | 类型 | 说明 |
|---|---|---|
| id | Integer PK | 自增 |
| run_id | Integer FK→eval_runs.id, index | 关联一次运行 |
| case_id | String(128), index | 用例标识 |
| query | Text | 原始问题 |
| rewritten_query | Text | 改写后问题 |
| passed | Integer | 是否通过（0/1） |
| retrieval_hits | JSON | `[{table, score}]` |
| table_relationship | JSON | 关系路径提示 |
| few_shot_examples | JSON | 命中的 few-shot `[{question, sql}]` |
| prompt | Text(LONGTEXT) | 完整 prompt |
| generated_sql | Text | 生成 SQL |
| execution_rows | JSON | 执行结果样例行（截断前 N 行） |
| row_count | Integer | 结果行数 |
| clarification | JSON | 澄清对象（如有） |
| metrics | JSON | 各指标 |
| errors | JSON | 失败原因列表 |
| created_at | DateTime | 落库时间 |

### 3.3 repository

- 新增 `EvalCaseResultRecord` dataclass（与 ORM 解耦）。
- 扩展 `EvalRunRepository` 协议：`record_case_result(run_id, record)` 与 `list_case_results(run_id)`。
- `InMemoryEvalRunRepository` + `SqlAlchemyEvalRunRepository` 双实现，缺 MySQL/SQLAlchemy 时降级内存，不报错。

### 3.4 EvalResult 携带 trace + eval.py 落库

- `core/models.py` 的 `EvalResult` 增加可选 `trace: dict | None = None`，`to_plain` 自动带进 JSON 报告。
- `run_case` 从 `state` 提取 trace 组装进 `EvalResult`。
- `main()`：`record_run` 拿到 `run_id` 后，逐 case `record_case_result(run_id, ...)` 落 MySQL；同时保留本地 JSON 报告便于快速查看。

## 数据流

```
query
 → workflow.run
   → state{rewritten_query, retrieval_hits, table_relationship, sql_prompt,
           generated_sql, execution_result, clarification}
 → run_case: 提取 trace + 计算 metrics → EvalResult(trace=...)
 → write_report(JSON)            # 本地快照
 → record_run(eval_runs)         # 聚合（已有）
 → record_case_result(eval_case_results, run_id)   # 逐 case trace 落 MySQL（新增）
```

## 测试策略（TDD）

- `compare_result_sets` 结果校验：构造期望/实际行断言各指标（已有可补充）。
- 新表 + repository：用 SQLite 内存库（同一 ORM）验证 `record_case_result`/`list_case_results` 往返。
- Holdout 用例：脚本跑每条 `expected_sql` 自检可执行且非空，作为 `expected_result` 生成来源。
- prompt 落盘：断言 `state["sql_prompt"]` 非空且含候选表/few-shot 段。
- 全程不依赖真实 MySQL / LLM（降级 + 内存库）。

## 隔离保证

- 全部改动在 git worktree `.worktrees/eval-tracing`、分支 `feat/eval-tracing-and-holdout` 上进行。
- 绝不在主工作目录切分支或提交，避免影响正在 `codex/*` 分支作业的其他 agent。
