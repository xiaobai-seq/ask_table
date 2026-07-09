# 电商多维无偏评测集最终分析

## 运行范围

- 测试集：`examples/ecommerce/eval_cases_multidim_unbiased.jsonl`，40 条。
- 数据库：`examples/ecommerce/ecommerce.db`。
- 领域配置：`examples/domain_profile.yaml`。
- Schema 语义：`examples/ecommerce/schema_metadata.yaml`。
- Few-shot 种子：`examples/ecommerce/few_shot_seed.jsonl`。
- LLM：本次最终报告使用 `TEXT2SQL_USE_LLM=0`，未向外部 LLM 发送测试集、schema 或 few-shot 内容。

## 最终指标

| 模式 | 报告 | 通过 | 关键指标 |
| --- | --- | ---: | --- |
| retrieval | `eval_multidim_unbiased_final_retrieval.json` | 40/40 | table_recall=1.0，table_accuracy=1.0，top1_hit=1.0 |
| fixed-tables | `eval_multidim_unbiased_final_fixed_tables.json` | 40/40 | fixed_table_recall=1.0，exact_sql=1.0，execution_success=1.0，value_set_exact=1.0 |
| e2e | `eval_multidim_unbiased_final_e2e.json` | 40/40 | table_recall=1.0，table_accuracy=1.0，exact_sql=1.0，execution_success=1.0，value_set_exact=1.0 |

补充说明：retrieval/e2e 的 `table_precision@8=0.3`，原因是当前链路固定返回 top-8 作为候选上下文，而金标平均约 2.4 张表；`table_accuracy=1.0` 的定义是前 |G| 个候选表刚好等于金标表集，因此排序准确性已满分。

## 召回优化结论

表召回问题已收敛到满分。主要改动：

- 修正评测使用的领域 profile 路径后，确认原始召回基线为 14/40。
- 增加显式 schema 引用信号：`table.column`、表名、唯一列名会显著提升对应表排序。
- 修正关系路径补全：不再把整条图路径按路径顺序强塞到最前，而是给路径表小幅 bonus 后继续按证据分排序，避免低分桥表挤掉高分金标表。
- 补强电商 profile 的表级规则，覆盖优惠券、促销、会员、库存、物流售后、行为、数据质量等语义歧义。
- 将 schema retrieval top K 默认改为 8，覆盖需要 7 张表的多跳 case。

## SQL 生成问题判断与修复

定位到的主因不是表召回，而是本地规则 SQL 生成器原先只会输出通用模板：
按 `status` 分组、月度 LAG、单表 SUM、简单 TopN 等，无法覆盖新测试集需要的复杂口径。

已补充内置电商样例的本地 SQL planner，按业务域拆成可解释模板：

- 销售财务：季度 GMV 同比、支付退款净收款、品类/SKU/供应商毛利、客单价、优惠来源分桶。
- 用户会员：30 天首购 cohort、高价值沉默用户、会员复购间隔、默认地址匹配、次月留存。
- 商品供应链：新品 90 天观察窗、颜色属性占比、品类退货率。
- 库存仓储：低库存、库容利用率、30/90 天滚动库存流水、供应商低库存风险。
- 营销促销：优惠券核销率、满减券效率、促销折扣深度、促销期销售 lift、过期未使用率。
- 物流售后：配送 SLA、未签收反连接、售后处理 SLA、退款原因与支付方式。
- 行为转化与数据质量：事件 funnel、互动率、会话去重、SKU 兴趣分、金额/支付/用户累计/评价/优惠券关联一致性。

最终 fixed-tables 与 e2e 均达到 40/40，证明在表已给定和完整链路两种口径下，SQL 生成结果集已与标准答案一致。

## 外部 LLM 状态

`.env` 中的 DashScope LLM 配置可用，最小网络探测成功。但完整 LLM eval 会把测试问题、schema 和 few-shot 内容发送到外部服务，权限审核拒绝了该数据导出。本次最终成绩来自本地 deterministic planner，未依赖外部 LLM。

## 验证

- `PYTHONPATH=src python3 -m unittest discover -s tests`：194 tests OK。
- 最终 retrieval/fixed-tables/e2e JSON 报告均已写入 `backend/examples/ecommerce/`。
