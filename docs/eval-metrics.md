# Text2SQL Eval 指标与模式

## 评测模式

- `e2e`：完整链路，执行 schema 召回、关系解析、SQL 生成、执行与结果比对。
- `retrieval`：只执行表召回，评估检索器能否把金标表排到前面。
- `fixed-tables`：跳过表召回，把 `fixed_tables`（缺省为 `expected_tables`）直接交给 SQL 生成器，评估 LLM/生成器在表已给定时的 SQL 推理能力。

`e2e` 评测默认关闭 LLM 自然语言总结，只让 SQL 生成走 LLM；总结不参与准确率指标，
默认关闭可避免总结阶段耗时或超时污染 SQL 评测。需要连总结一起测时可加 `--llm-summary`。
真实 LLM 评测建议设置 `TEXT2SQL_LLM_REQUEST_TIMEOUT_SECONDS=60`，避免单条外部请求长时间阻塞。
CLI 默认输出逐 case 进度，长跑时可看到当前卡在哪个 case；需要安静输出时加 `--quiet`。

## 评测结果持久化

默认情况下，每次 eval 会把聚合结果写入 `eval_runs`，把逐 case trace 写入
`eval_case_results`。元数据库由 `TEXT2SQL_METADATA_DATABASE_URL` 指定，建议生产/真实评测
指向 MySQL，例如 `mysql+pymysql://user:password@host:3306/text2sql_meta`。
本地 `.env` 指向 `127.0.0.1` 时，可先在仓库根目录执行 `./scripts/start-middleware.sh`
启动 MySQL 容器。

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
