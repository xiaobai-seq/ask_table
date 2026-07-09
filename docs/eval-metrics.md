# Text2SQL Eval 指标与模式

## 评测模式

- `e2e`：完整链路，执行 schema 召回、关系解析、SQL 生成、执行与结果比对。
- `retrieval`：只执行表召回，评估检索器能否把金标表排到前面。
- `fixed-tables`：跳过表召回，把 `fixed_tables`（缺省为 `expected_tables`）直接交给 SQL 生成器，评估 LLM/生成器在表已给定时的 SQL 推理能力。

`e2e` 评测默认关闭 LLM 自然语言总结，只让 SQL 生成走 LLM；总结不参与准确率指标，
默认关闭可避免总结阶段耗时或超时污染 SQL 评测。需要连总结一起测时可加 `--llm-summary`。
真实 LLM 评测建议设置 `TEXT2SQL_LLM_REQUEST_TIMEOUT_SECONDS=60`，避免单条外部请求长时间阻塞。
CLI 默认输出逐 case 进度，长跑时可看到当前卡在哪个 case；需要安静输出时加 `--quiet`。
当 eval 会使用外部 AI 服务时，CLI 会要求显式授权：加 `--allow-external-ai-eval`
或设置 `TEXT2SQL_ALLOW_EXTERNAL_AI_EVAL=1`。这表示已确认本地 schema、case 问题、
few-shot 示例、SQL prompt、schema 文档可能发送到配置的 LLM/embedding/rerank endpoint。
旧参数 `--allow-external-llm-eval` 和旧环境变量 `TEXT2SQL_ALLOW_EXTERNAL_LLM_EVAL=1`
仍作为兼容别名可用。注意：只要 `DASHSCOPE_API_KEY` 存在，schema 召回默认也会使用
DashScope embedding/rerank；即使 `TEXT2SQL_USE_LLM=0`，当前三种 eval 模式也可能在
workflow 初始化或召回阶段发生外部调用。
SQL 生成 prompt 会根据 `--db` 推断目标方言；例如 `mysql+pymysql://...` 会显式要求
MySQL 8.0 函数与语法，避免 LLM 在真实 MySQL eval 中生成 SQLite 的
`strftime/julianday/date(x, '+N days')` 等函数。
few-shot 示例也会按目标方言过滤：MySQL 评测不会注入含 `strftime/julianday/||`
等 SQLite 专属语法的示例，防止 LLM 被参考 SQL 带偏。
执行前 SQL 质量门禁同样会按目标方言检查生成结果；如果 MySQL 评测中仍出现
SQLite-only 函数，会触发二次修复并写入 `quality_gate_issues`。

## 评测结果持久化

默认情况下，每次 eval 会把聚合结果写入 `eval_runs`，把逐 case trace 写入
`eval_case_results`。元数据库由 `TEXT2SQL_METADATA_DATABASE_URL` 指定，建议生产/真实评测
指向 MySQL，例如 `mysql+pymysql://user:password@host:3306/text2sql_meta`。
本地 `.env` 指向 `127.0.0.1` 时，可先在仓库根目录执行 `./scripts/start-middleware.sh`
启动 MySQL 容器。
SQL 生成阶段的诊断会写入逐 case `metrics.sql_generation`，包括
`quality_gate_issues`、`quality_gate_repaired`、`quality_gate_remaining_issues`、
`quality_gate_template_fallback`、`sql_warnings` 等字段；不需要新增表结构即可在
MySQL 元库中回溯。`quality_gate_template_fallback=true` 表示 LLM 初稿触发高置信
质量门禁后，系统找到可证明干净的高置信规则模板，并在一次修复尝试后由模板接管。

如果元数据库不可用，CLI 会直接失败，不会静默降级到内存仓库。这能避免终端显示
`eval_run_id=1` 但 MySQL 里没有记录的假象。

- 本地只想生成 JSON 报告：加 `--no-persist`。
- 开发/单测临时允许内存兜底：加 `--allow-inmemory-persist`。

## 检索规则配置

表召回的领域增强不写在检索代码里，而放在 `TEXT2SQL_DOMAIN_PROFILE_PATH`
指向的 YAML 中。核心扩展点位于 `retrieval`：

- `tag_boost_rules`：按查询意图给带有 `time`、`metric` 等字段语义标签的表加/减分。
- `table_boost_rules`：按查询词命中配置，把指定表、表名前缀或表名片段加/减分。
- `relationship_path`：配置多跳 JOIN 路径补全的启用条件、锚点表、事实表、桥接表规则和路径得分权重。

换业务域时优先复制 `examples/domain_profile.yaml` 中的 `retrieval` 段并改配置；
`HybridTableRetriever` 只解释这些规则，不包含电商表名或业务词判断。

## 表召回与表准确

设金标表集合为 `G`，检索 top K 返回表集合为 `R_K`。

- 表召回率：`table_recall@K = |G ∩ R_K| / |G|`
- 表准确率：`table_precision@K = |G ∩ R_K| / |R_K|`
- 表 F1：`table_f1@K = 2 * precision * recall / (precision + recall)`
- 表集准确率：`table_accuracy = 1{set(first |G| tables in R_K) == G}`
- Top1 命中：`table_top1_hit = 1{first table in R_K ∈ G}`

`retrieval` 模式的 case 通过条件是 `table_recall == 1` 且 `table_accuracy == 1`。
默认 `top_k` 为 8，以覆盖多跳分析中需要 7 张表的样例；较小的 K 会让这类 case
在数学上无法达到满召回。

## LLM 生成准确

设标准结果行多重集为 `E`，实际执行结果行多重集为 `A`。行签名会把单元格归一化成字符串、数值四舍五入到 2 位，并忽略列名/列顺序。

- 执行成功率：`execution_success = 1{SQL executed without error}`
- 结果值集准确：`value_set_exact = 1{E == A}`
- 结果值集召回：`value_set_recall = matched_rows(E ∩ A) / |E|`
- LLM 生成准确率：`llm_generation_accuracy = value_set_exact`

`fixed-tables` 模式的 case 通过条件是 `execution_success == 1`、`value_set_exact == 1` 且 `llm_generation_accuracy == 1`。

## 命令

```bash
cd backend

TEXT2SQL_SCHEMA_METADATA_PATH=./examples/ecommerce/schema_metadata.yaml \
TEXT2SQL_FEW_SHOT_SEED_PATH=./examples/ecommerce/few_shot_seed.jsonl \
TEXT2SQL_LLM_REQUEST_TIMEOUT_SECONDS=60 \
PYTHONPATH=src python3 -m text2sql.eval \
  --db examples/ecommerce/ecommerce.db \
  --cases examples/ecommerce/eval_cases_table_retrieval.jsonl \
  --mode retrieval \
  --top-k 8 \
  --cache-dir /tmp/text2sql-eval-cache-retrieval \
  --report examples/ecommerce/eval_table_retrieval_report.json

TEXT2SQL_SCHEMA_METADATA_PATH=./examples/ecommerce/schema_metadata.yaml \
TEXT2SQL_FEW_SHOT_SEED_PATH=./examples/ecommerce/few_shot_seed.jsonl \
TEXT2SQL_LLM_REQUEST_TIMEOUT_SECONDS=60 \
PYTHONPATH=src python3 -m text2sql.eval \
  --db examples/ecommerce/ecommerce.db \
  --cases examples/ecommerce/eval_cases_fixed_tables.jsonl \
  --mode fixed-tables \
  --cache-dir /tmp/text2sql-eval-cache-fixed \
  --report examples/ecommerce/eval_fixed_tables_report.json
```

真实 MySQL + 外部 AI e2e 评测会把本地 schema/case/few-shot prompt/schema 文档发送到配置的
LLM/embedding/rerank endpoint，需显式加 `--allow-external-ai-eval`：

```bash
cd backend
TEXT2SQL_USE_LLM=1 \
TEXT2SQL_SCHEMA_METADATA_PATH=./examples/ecommerce/schema_metadata.yaml \
TEXT2SQL_FEW_SHOT_SEED_PATH=./examples/ecommerce/few_shot_seed.jsonl \
TEXT2SQL_LLM_REQUEST_TIMEOUT_SECONDS=60 \
PYTHONPATH=src python3 -m text2sql.eval \
  --db 'mysql+pymysql://user:password@127.0.0.1:3308/text2sql_ecommerce?charset=utf8mb4' \
  --cases examples/ecommerce/eval_cases_multidim_unbiased.jsonl \
  --mode e2e \
  --top-k 8 \
  --cache-dir /tmp/text2sql-eval-cache-llm-mysql \
  --report examples/ecommerce/eval_multidim_unbiased_llm_mysql_e2e.json \
  --allow-external-ai-eval
```

已有 JSON 报告不需要重跑即可分析逐 case 和 SQL 质量门禁：

```bash
cd backend
PYTHONPATH=src python3 -m text2sql.eval_report \
  --report examples/ecommerce/eval_multidim_unbiased_llm_mysql_e2e_quality_gate.json \
  --cases examples/ecommerce/eval_cases_multidim_unbiased.jsonl \
  --output examples/ecommerce/eval_multidim_unbiased_llm_mysql_e2e_quality_gate_analysis.md \
  --json-output examples/ecommerce/eval_multidim_unbiased_llm_mysql_e2e_quality_gate_analysis.json
```

分析输出会同时展示报告内真实 `quality_gate_*` trace 字段，以及用当前
`inspect_sql_quality()` 对旧报告做的离线回放覆盖率，便于判断失败是否集中在 SQL
生成口径、二次修复是否成功、是否还有残留静态问题。

如果最终 JSON 写盘失败，但 eval 已经落了 `eval_runs/eval_case_results`，可以直接从
元数据库恢复同样的分析。`--db` 传真实评测目标库，用于回放 MySQL/SQLite 方言检查；
`--metadata-db` 可省略，默认读取 `TEXT2SQL_METADATA_DATABASE_URL`。

```bash
cd backend
PYTHONPATH=src python3 -m text2sql.eval_report \
  --latest-run \
  --db 'mysql+pymysql://user:password@127.0.0.1:3308/ecommerce' \
  --cases examples/ecommerce/eval_cases_multidim_unbiased.jsonl \
  --output examples/ecommerce/eval_latest_mysql_analysis.md \
  --json-output examples/ecommerce/eval_latest_mysql_analysis.json

PYTHONPATH=src python3 -m text2sql.eval_report \
  --run-id 123 \
  --db 'mysql+pymysql://user:password@127.0.0.1:3308/ecommerce' \
  --cases examples/ecommerce/eval_cases_multidim_unbiased.jsonl \
  --output examples/ecommerce/eval_run_123_mysql_analysis.md
```
