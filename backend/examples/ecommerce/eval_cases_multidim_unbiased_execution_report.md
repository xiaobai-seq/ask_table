# 电商多维度无偏评测集执行报告

## 构造思路

- 新增评测集文件：`eval_cases_multidim_unbiased.jsonl`，不覆盖原有 `eval_cases.jsonl`。
- 仅参考用户指定的 `ecommerce_mysql.sql` 表结构与 `eval_cases.jsonl` 字段风格；未查看评测体系代码、现有评测报告或其他评测集内容。
- 评测集共 40 条，8 个主业务域各 5 条：销售财务、用户会员、商品供应链、库存仓储、营销促销、物流售后、行为转化、数据质量。
- 每条 case 保留参考格式字段，并新增 `difficulty`、`dimensions`、`expected_sql`、`expected_result`。
- 标准答案以 `ecommerce.db` 的 SQLite 可执行 SQL 为准；所有指标均明确日期、支付状态、聚合粒度、排序和 TopN 口径。
- 为降低偏差，题型覆盖简单聚合、多表 join、CTE、窗口函数、时间窗口、反连接、条件聚合、金额勾稽和数据一致性检查。

## 执行效果

- JSONL 行数：40。
- `case_id`：40 个唯一值，无重复。
- `allow_clarification`：全部为 `false`。
- 主维度分布：8 个业务域各 5 条。
- 难度分布：easy 12、medium 22、hard 6。
- SQL 执行：40/40 在 `ecommerce.db` 上执行成功。
- 结果校验：40/40 的实际执行结果与 `expected_result` 完全一致。
- 结果规模：每条 1 到 24 行，无大结果集倾倒。
- 关键词检查：40/40 的 `required_sql_keywords` 均在 `expected_sql` 中命中。

## 审查结果

- Agent A 审查业务真实性、无偏覆盖和口径清晰度。
  - 初审发现 P1：优惠券 NULL 不一致漏算、新客 30 天观察窗不完整、行为转化链路数据过稀、促销 SKU 计数口径不一致。
  - 修订后复核：无 P0/P1；确认 8 个业务域覆盖仍均衡。
- Agent B 审查 SQL 正确性、执行可复现性和答案最优性。
  - 初审确认 40/40 可执行且结果一致，同时发现 P1：优惠券 NULL-safe 口径、月度会话数应按 `user_id + session_id` 去重。
  - 修订后复核：40/40 SQL 仍执行一致，字段完整，关键词命中；无剩余 P0/P1。

## 关键修订

- `dq_used_coupon_order_link_consistency`：将 `orders.coupon_id IS NULL` 计入优惠券不一致，最终 `coupon_mismatch_count = 1554`。
- `um_new_user_30d_first_purchase`：限定注册日期在完整可观测窗口 `2023-01-01` 至 `2024-12-01`。
- 行为域替换为更适合当前数据分布的行为日志分析题，避免用稀疏随机日志硬评测购买链路转化。
- `mk_promotion_discount_depth_by_type`：参与 SKU 数改为 `COUNT(DISTINCT pp.sku_id)`，另保留促销商品行数。
- `ps_category_return_rate_top10`：退货率分子分母统一限定 `pay_status IN ('paid', 'partial_refund')`。
- `bc_monthly_behavior_trend_2024`：会话数按 `COUNT(DISTINCT user_id || '|' || session_id)` 统计。
- `ps_new_spu_90d_sales_top10`：90 天新品观察窗同时校验订单日期左右边界。
- `sf_quarter_gmv_yoy`：同比改为去年同季自连接，避免依赖季度连续完整。
- 库存近 30/90 天指标改为严格按最大时间戳滚动窗口。
- 优惠来源空桶的折扣率显式输出为 `0`。

## 结论

评测集已完成构造、真实执行校验和双 agent 多维审查闭环。最终版本无 P0/P1 阻塞问题，可作为独立多维度无偏电商 text2SQL 评测集使用。
