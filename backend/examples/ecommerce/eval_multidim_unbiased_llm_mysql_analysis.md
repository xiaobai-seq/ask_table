# 电商多维无偏评测集 LLM + MySQL E2E 分析

## 运行范围

- 测试集：`examples/ecommerce/eval_cases_multidim_unbiased.jsonl`，40 条。
- 查询库：MySQL `text2sql_ecommerce`，`mysql+pymysql://text2sql_app:***@127.0.0.1:3308/text2sql_ecommerce?charset=utf8mb4`。
- 元数据库：MySQL `text2sql_meta`，`eval_run_id=2`，逐 case trace 40 条已落 `eval_case_results`。
- 报告：`eval_multidim_unbiased_llm_mysql_e2e.json`。
- LLM：`TEXT2SQL_USE_LLM=1`，SQL 生成走 DashScope LLM 优先路径；本报告分析真实 LLM + MySQL 执行结果。

## 总体结果

| 指标 | 结果 |
| --- | ---: |
| E2E pass | 17/40 = 42.5% |
| table_recall | 1.0 |
| table_accuracy | 1.0 |
| execution_success | 1.0 |
| value_set_exact | 0.425 |
| value_set_recall | 0.44375 |
| exact_sql | 0.075 |
| keyword_recall | 0.9108 |

结论：本轮失败主因不是表召回，也不是 SQL 语法或 MySQL 执行问题。40 条全部召回正确表、全部执行成功；23 条失败集中在 LLM 生成 SQL 的业务口径和输出结果集不满足标准答案。

## 分域结果

| 业务域 | 通过 | 失败特征 |
| --- | ---: | --- |
| 销售财务 | 2/5 | 已支付口径漏 `partial_refund`，输出列被压缩 |
| 用户会员 | 2/5 | 少会员等级/计数列，复购窗口排序不完整 |
| 商品供应链 | 1/5 | 已支付口径漏 `partial_refund`，TopN 输出列不足 |
| 库存仓储 | 0/5 | 核心值多接近，但普遍少 ID/拆分指标/空桶保留 |
| 营销促销 | 4/5 | 整体最好，过期券 case 少券信息列且过期口径不严谨 |
| 物流售后 | 2/5 | SLA/时长类少 count 与明细指标列，排序不稳 |
| 行为转化 | 4/5 | 整体较好，SKU 兴趣分少事件拆分列和 tie-breaker |
| 数据质量 | 2/5 | 对账 case 需要精确异常口径和 NULL/0 处理 |

## 失败根因分组

1. 输出列不完整：LLM 常把结果压成 `dimension_value/metric_value`，而标准答案要求 ID、名称、计数、金额、率、拆分指标同时输出。这是最大类问题。
2. 已支付订单口径缩窄：多个 case 把 `pay_status IN ('paid', 'partial_refund')` 写成 `pay_status = 'paid'`，导致金额、数量、排序都偏小。
3. 空桶/零值维度未保留：优惠来源、仓库、出库窗口等问题需要基准维表或枚举 CTE + `LEFT JOIN`，LLM 往往只聚合已有事实行。
4. 排序和 tie-breaker 不完整：TopN、窗口函数和等级聚合缺少稳定二级排序，如 `order_id`、主键或业务要求排序，导致同值时入榜/顺序变化。
5. 时间窗口边界不完整：新品 90 天观察窗、最近 N 天窗口需要基于数据最大时间和严格边界，LLM 容易只写近似条件。
6. 数据质量对账口径错误：先过滤异常再聚合会把无异常场景的 `MAX/SUM` 变成 NULL；未区分 unpaid 订单会产生假异常。

## 失败 Case 明细

| case | 主要问题 |
| --- | --- |
| `sf_category_gross_margin_top10` | 漏 `partial_refund`；只输出品类名和毛利率，少 `category_id`、销量、销售额、成本、毛利额。 |
| `sf_province_aov_top10` | 漏 `partial_refund`；金额和订单数偏小，排序基于偏小口径。 |
| `sf_discount_source_mix` | 漏 `partial_refund`；没有输出四类固定桶，`coupon_and_promotion` 空桶缺失；标签也从英文标准值变成中文。 |
| `um_high_value_silent_users_top10` | 核心用户排序基本对，但少 `membership_levels.name`。 |
| `um_member_repurchase_interval` | 漏 `partial_refund`；少 repeat pair 计数；`LAG` 排序缺 `order_id`；按平均间隔排序而非会员等级。 |
| `um_default_address_city_match_rate` | 漏 `partial_refund`；少用户数和匹配用户数；最近订单排序缺 `order_id`；按比例排序而非会员等级。 |
| `ps_sku_margin_top10_paid` | 漏 `partial_refund`；只输出 SKU 名和毛利额，少 SKU ID、销量、销售额、成本、毛利率。 |
| `ps_new_spu_90d_sales_top10` | 漏 `partial_refund`；未要求 90 天窗口完全落在订单日期范围内；少 SPU ID、上市日期、销量。 |
| `ps_supplier_revenue_margin_top10` | 漏 `partial_refund`；少 supplier_id 和销量，销售额/毛利偏小。 |
| `ps_color_attribute_sales_share_top10` | 漏 `partial_refund`；销售额和销量偏小，占比也随之偏差。 |
| `iw_low_stock_sku_warehouse_top20` | 缺 `warehouse_id`、`sku_id`、当前库存、安全库存，只有名称和缺口；同缺口排序缺稳定键。 |
| `iw_warehouse_capacity_utilization` | 少 `warehouse_id`；使用内连接而非左连接；字段名和值列不完整。 |
| `iw_recent_30d_outbound_by_warehouse` | 核心出库量对，但少 `warehouse_id`；未用 `LEFT JOIN` 保留零出库仓库；窗口下界使用 `>=` 而标准是严格 `>`。 |
| `iw_recent_90d_sku_net_inflow_top10` | 核心净流入排序对，但少 SKU ID、入库量、出库量；窗口下界使用 `>=` 而标准是严格 `>`。 |
| `iw_supplier_low_stock_risk_top10` | 核心数值对，但少 supplier_id；排序缺低库存 SKU 数和主键 tie-breaker。 |
| `mk_expired_unused_coupon_rate_top10` | 核心数值接近/对，但少 coupon_id、type、valid_to；过期券过滤用了 `status='expired'`，标准是 `valid_to < '2024-12-31'`。 |
| `lr_72h_delivery_sla_by_company` | 平均时长和 72h 比例对，但少 company_id、签收数、72h 数；按公司名排序而非 SLA 指标排序。 |
| `lr_province_delivery_duration_slowest_top10` | 省份和平均时长对，但少 signed_shipment_count。 |
| `lr_return_completion_sla_by_reason` | 平均处理天数和 3 日率对，但少退货数和 3 日内完成数；排序按 reason 而非 return_count。 |
| `bc_sku_interest_score_top10_2024` | 兴趣分公式对，但少 SKU ID 和四类事件拆分计数；同分 tie-breaker 缺 `view_events DESC, sku_id`。 |
| `dq_order_amount_reconciliation` | 无异常时先过滤 mismatch 导致 `MAX/SUM` 为 NULL；标准要求输出 0.0。 |
| `dq_success_payment_reconciliation_by_status` | 把 unpaid 订单也计入缺成功支付/金额不一致，标准只对 paid/partial_refund/refunded 检查缺失成功支付。 |
| `dq_user_total_spent_reconciliation_top10` | 漏 `partial_refund`；少 signed amount diff；TopN 因金额口径变化而错。 |

## 优化建议

优先改 prompt 和评测期 SQL 生成约束，而不是继续调召回：

1. 在 SQL prompt 硬约束里明确：除非用户只要求单指标图表，否则必须输出问题中提到的全部维度、ID、计数、金额、率、拆分指标；禁止把多指标结果压缩成 `dimension_value/metric_value`。
2. 在电商 domain profile 或 prompt 中固化业务口径：`已支付订单` 默认等价于 `pay_status IN ('paid', 'partial_refund')`。
3. 对 TopN/排序增加硬约束：按用户指定指标排序，并追加稳定 tie-breaker（实体主键、名称等）；窗口函数也要追加主键排序。
4. 对“全部输出、四类组合、仓库/券/省份保留零值”等语义，要求使用枚举/维表 CTE + `LEFT JOIN`。
5. 对“最近 N 天/上市 N 天完整观察窗”语义，要求先抽取数据最大时间或全局 min/max，再用严格边界。
6. 对数据质量 SQL，要求不要先过滤异常再计算总览指标；应先构造全量 diff，再用 `CASE WHEN` 聚合，空值用 `COALESCE` 输出 0。

## 已落地优化

- SQL 生成 prompt 新增“业务口径与结果契约”，把输出列完整性、已支付订单口径、实体 ID + 名称、率/比例分子分母、零值桶、时间窗口、数据质量聚合等约束注入 LLM。
- LLM 首次生成后、执行前新增静态质量门禁；命中高置信问题时自动触发一次二次修复 prompt。
- 门禁覆盖的主要模式：`pay_status='paid'` 漏 `partial_refund`、多指标压缩成 `dimension_value/metric_value`、实体主键缺失、计数列缺失、率/比例缺分子分母、零值桶缺基准、最近窗口边界、完整观察窗、窗口排序 tie-breaker、过期券口径、对账先过滤异常、成功支付一致性未区分 unpaid。
- 对旧报告回放：23 个失败 case 全部命中质量门禁，17 个已通过 case 没有误触发。
- eval JSON trace 新增 `sql_warnings`、`quality_gate_issues`、`quality_gate_remaining_issues`、`quality_gate_repaired`、`quality_gate_repair_failed`、`quality_gate_repair_unresolved`、`sql_reasoning`、`sql_confidence`、`sql_advanced_features`，下一轮真实 LLM + MySQL 重跑后可直接看每个 case 是否走了二次修复、修复原因以及是否还有残留静态问题。
