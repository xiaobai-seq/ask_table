import asyncio
import json
import unittest

from text2sql.core.models import ColumnInfo, ForeignKeyInfo, RelationshipPath, RetrievalHit, TableInfo
from text2sql.core.sql_generator import (
    DeterministicSQLGenerator,
    PromptedSQLGenerator,
    infer_sql_dialect,
    inspect_sql_quality,
    parse_llm_sql_plan,
)
from text2sql.core.sql_validator import SQLValidationError, SQLValidator


class _SequencedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0)


class SQLGenerationTests(unittest.TestCase):
    def setUp(self):
        self.orders = TableInfo(
            "orders",
            "订单",
            columns=(
                ColumnInfo("order_id", "INTEGER", primary_key=True),
                ColumnInfo("order_date", "TEXT", semantic_tags=("time",)),
                ColumnInfo("status", "TEXT", semantic_tags=("dimension",)),
                ColumnInfo("total_amount", "REAL", semantic_tags=("metric",)),
            ),
        )
        self.generator = DeterministicSQLGenerator()

    def test_growth_query_uses_lag_window_function(self):
        plan = self.generator.generate(
            "按月份统计订单金额趋势，并计算环比增长率",
            [RetrievalHit(self.orders, 1.0)],
            [],
        )

        self.assertIn("LAG", plan.sql)
        self.assertIn("OVER", plan.sql)
        self.assertIn("window_function", plan.advanced_features)

    def test_rank_query_uses_rank_window_function(self):
        plan = self.generator.generate("订单状态排名前3", [RetrievalHit(self.orders, 1.0)], [])

        self.assertIn("RANK() OVER", plan.sql)
        self.assertIn("metric_rank <= 3", plan.sql)

    def test_recursive_hierarchy_query_uses_recursive_cte(self):
        employees = TableInfo(
            "employees",
            "员工组织",
            columns=(
                ColumnInfo("employee_id", "INTEGER", primary_key=True),
                ColumnInfo("employee_name", "TEXT", semantic_tags=("dimension",)),
                ColumnInfo("manager_id", "INTEGER"),
            ),
            foreign_keys=(
                ForeignKeyInfo("employees", "manager_id", "employees", "employee_id"),
            ),
        )
        plan = self.generator.generate("查询员工组织层级路径", [RetrievalHit(employees, 1.0)], [])

        self.assertIn("WITH RECURSIVE", plan.sql)
        self.assertIn("recursive_cte", plan.advanced_features)

    def test_related_dimension_query_uses_relationship_join(self):
        customers = TableInfo(
            "customers",
            "客户",
            columns=(
                ColumnInfo("customer_id", "INTEGER", primary_key=True),
                ColumnInfo("region", "TEXT", semantic_tags=("dimension",)),
            ),
        )
        relationship = RelationshipPath(
            "orders",
            "customers",
            (ForeignKeyInfo("orders", "customer_id", "customers", "customer_id"),),
        )

        plan = self.generator.generate(
            "按客户地区统计订单金额",
            [RetrievalHit(self.orders, 1.0), RetrievalHit(customers, 0.9)],
            [relationship],
        )

        self.assertIn("JOIN customers", plan.sql)
        self.assertIn("customers.region", plan.sql)
        self.assertIn("SUM(orders.total_amount)", plan.sql)

    def test_parse_llm_sql_plan_accepts_json_block(self):
        plan = parse_llm_sql_plan(
            '```json\n{"sql":"SELECT 1","chart_type":"kpi","reasoning":"ok","confidence":0.9}\n```'
        )

        self.assertEqual(plan.sql, "SELECT 1")
        self.assertEqual(plan.chart_type, "kpi")

    def test_validator_rejects_mutating_sql_and_unknown_tables(self):
        validator = SQLValidator([self.orders])

        with self.assertRaises(SQLValidationError):
            validator.validate("DELETE FROM orders")
        with self.assertRaises(SQLValidationError):
            validator.validate("SELECT * FROM payments")

    def test_prompt_includes_result_contract_and_paid_order_semantics(self):
        orders = TableInfo(
            "orders",
            "订单",
            columns=(
                ColumnInfo("order_id", "INTEGER", primary_key=True),
                ColumnInfo("pay_status", "TEXT"),
                ColumnInfo("total_amount", "REAL"),
            ),
        )

        prompt = self.generator.build_prompt(
            "按收货省份统计已支付订单数、销售额和客单价",
            [RetrievalHit(orders, 1.0)],
            [],
        )

        self.assertIn("业务口径与结果契约", prompt)
        self.assertIn("禁止把多指标结果压缩成 dimension_value/metric_value", prompt)
        self.assertIn("pay_status IN ('paid', 'partial_refund')", prompt)

    def test_prompt_includes_target_mysql_dialect(self):
        orders = TableInfo(
            "orders",
            "订单",
            columns=(ColumnInfo("order_id", "INTEGER", primary_key=True),),
        )
        generator = DeterministicSQLGenerator(sql_dialect="mysql+pymysql")

        prompt = generator.build_prompt("统计订单数", [RetrievalHit(orders, 1.0)], [])

        self.assertIn("目标 SQL 方言", prompt)
        self.assertIn("MySQL 8.0", prompt)
        self.assertIn("DATE_FORMAT", prompt)
        self.assertIn("禁止使用 SQLite 专属函数", prompt)

    def test_infer_sql_dialect_from_database_url(self):
        self.assertEqual(
            infer_sql_dialect("mysql+pymysql://user:pw@127.0.0.1:3308/db"), "mysql"
        )
        self.assertEqual(infer_sql_dialect("sqlite:///tmp/demo.db"), "sqlite")
        self.assertEqual(infer_sql_dialect("examples/demo.db"), "sqlite")

    def test_sql_quality_flags_paid_only_and_compressed_output(self):
        orders = TableInfo(
            "orders",
            "订单",
            columns=(
                ColumnInfo("order_id", "INTEGER", primary_key=True),
                ColumnInfo("pay_status", "TEXT"),
                ColumnInfo("total_amount", "REAL"),
                ColumnInfo("receiver_province", "TEXT"),
            ),
        )

        issues = inspect_sql_quality(
            "按收货省份统计已支付订单数、销售额和客单价",
            """
            SELECT receiver_province AS dimension_value, SUM(total_amount) AS metric_value
            FROM orders
            WHERE pay_status = 'paid'
            GROUP BY receiver_province
            """,
            [RetrievalHit(orders, 1.0)],
        )

        self.assertTrue(any("partial_refund" in issue for issue in issues))
        self.assertTrue(any("dimension_value/metric_value" in issue for issue in issues))

    def test_sql_quality_flags_mysql_dialect_mismatch(self):
        shipments = TableInfo(
            "shipments",
            "物流",
            columns=(
                ColumnInfo("shipment_id", "INTEGER", primary_key=True),
                ColumnInfo("shipped_at", "DATETIME"),
                ColumnInfo("delivered_at", "DATETIME"),
            ),
        )

        issues = inspect_sql_quality(
            "统计平均配送小时数",
            "SELECT AVG((julianday(delivered_at) - julianday(shipped_at)) * 24) FROM shipments",
            [RetrievalHit(shipments, 1.0)],
            sql_dialect="mysql",
        )

        self.assertTrue(any("方言错误" in issue for issue in issues))

    def test_sql_quality_flags_missing_entity_ids_for_sku_reports(self):
        skus = TableInfo(
            "skus",
            "SKU",
            columns=(
                ColumnInfo("sku_id", "INTEGER", primary_key=True),
                ColumnInfo("sku_name", "TEXT"),
            ),
        )

        issues = inspect_sql_quality(
            "统计已支付订单中毛利额最高的10个SKU",
            "SELECT skus.sku_name AS dimension_value, SUM(1) AS metric_value FROM skus GROUP BY skus.sku_name",
            [RetrievalHit(skus, 1.0)],
        )

        self.assertTrue(any("sku_id" in issue for issue in issues))

    def test_sql_quality_flags_missing_requested_count_outputs(self):
        shipments = TableInfo(
            "shipments",
            "物流",
            columns=(
                ColumnInfo("shipment_id", "INTEGER", primary_key=True),
                ColumnInfo("receiver_province", "TEXT"),
                ColumnInfo("status", "TEXT"),
            ),
        )

        issues = inspect_sql_quality(
            "按收货省份统计已签收包裹平均配送小时数，仅保留包裹数不少于50的省份",
            "SELECT receiver_province AS dimension_value, AVG(1) AS metric_value FROM shipments GROUP BY receiver_province HAVING COUNT(*) >= 50",
            [RetrievalHit(shipments, 1.0)],
        )

        self.assertTrue(any("count" in issue for issue in issues))

    def test_sql_quality_flags_rate_without_count_outputs(self):
        shipments = TableInfo(
            "shipments",
            "物流",
            columns=(
                ColumnInfo("shipment_id", "INTEGER", primary_key=True),
                ColumnInfo("company_id", "INTEGER"),
            ),
        )
        companies = TableInfo(
            "logistics_companies",
            "物流公司",
            columns=(ColumnInfo("company_id", "INTEGER", primary_key=True),),
        )

        issues = inspect_sql_quality(
            "按物流公司统计已签收包裹的平均配送小时数和72小时内签收率",
            "SELECT lc.company_id, AVG(1) AS avg_delivery_hours, SUM(CASE WHEN 1=1 THEN 1 ELSE 0 END) / COUNT(*) AS within_72h_rate FROM shipments s JOIN logistics_companies lc ON s.company_id = lc.company_id GROUP BY lc.company_id",
            [RetrievalHit(shipments, 1.0), RetrievalHit(companies, 0.9)],
        )

        self.assertTrue(any("分子计数" in issue for issue in issues))

    def test_sql_quality_flags_user_ratio_without_count_outputs(self):
        users = TableInfo(
            "users",
            "用户",
            columns=(ColumnInfo("user_id", "INTEGER", primary_key=True),),
        )
        levels = TableInfo(
            "membership_levels",
            "会员等级",
            columns=(ColumnInfo("level_id", "INTEGER", primary_key=True),),
        )

        issues = inspect_sql_quality(
            "按会员等级统计有默认地址且有已支付订单的用户，其默认地址城市与最近一笔已支付订单收货城市一致的比例。",
            "SELECT ml.name AS level_name, ROUND(SUM(city_match) / COUNT(*), 4) AS city_match_rate FROM users u JOIN membership_levels ml ON u.level_id = ml.level_id GROUP BY ml.name",
            [RetrievalHit(users, 1.0), RetrievalHit(levels, 0.9)],
        )

        self.assertTrue(any("分子计数" in issue for issue in issues))

    def test_sql_quality_accepts_count_semantic_aliases(self):
        user_events = TableInfo(
            "user_events",
            "用户行为",
            columns=(
                ColumnInfo("event_id", "INTEGER", primary_key=True),
                ColumnInfo("user_id", "INTEGER"),
            ),
        )

        issues = inspect_sql_quality(
            "统计2024年1月至11月每月有行为事件的活跃用户数，以及这些用户下月仍有行为事件的留存用户数和留存率。",
            """
            SELECT month, active_users, retained_users,
                   ROUND(retained_users * 1.0 / active_users, 4) AS retention_rate
            FROM retention
            """,
            [RetrievalHit(user_events, 1.0)],
        )

        self.assertFalse(issues)

    def test_sql_quality_does_not_require_sku_id_for_participating_sku_filter(self):
        promotions = TableInfo(
            "promotions",
            "促销",
            columns=(
                ColumnInfo("promotion_id", "INTEGER", primary_key=True),
                ColumnInfo("name", "TEXT"),
            ),
        )
        skus = TableInfo(
            "skus",
            "SKU",
            columns=(ColumnInfo("sku_id", "INTEGER", primary_key=True),),
        )

        issues = inspect_sql_quality(
            "对每个促销，比较活动期内参与SKU已支付销售额与活动前等长周期销售额，按活动期销售额取前10。",
            """
            SELECT promotion_id, name AS promotion_name, promo_revenue, pre_revenue, lift_rate
            FROM sales
            ORDER BY promo_revenue DESC, promotion_id
            LIMIT 10
            """,
            [RetrievalHit(promotions, 1.0), RetrievalHit(skus, 0.9)],
        )

        self.assertFalse(any("sku_id" in issue for issue in issues))

    def test_sql_quality_accepts_pivoted_four_behavior_metrics(self):
        user_events = TableInfo(
            "user_events",
            "用户行为",
            columns=(
                ColumnInfo("event_id", "INTEGER", primary_key=True),
                ColumnInfo("event_type", "TEXT"),
                ColumnInfo("source", "TEXT"),
                ColumnInfo("user_id", "INTEGER"),
            ),
        )

        issues = inspect_sql_quality(
            "按来源统计2024年浏览、加购、收藏、分享四类行为的去重用户数，按浏览用户数降序。",
            """
            SELECT source,
                   COUNT(DISTINCT CASE WHEN event_type = 'view' THEN user_id END) AS view_users,
                   COUNT(DISTINCT CASE WHEN event_type = 'add_cart' THEN user_id END) AS add_cart_users,
                   COUNT(DISTINCT CASE WHEN event_type = 'favorite' THEN user_id END) AS favorite_users,
                   COUNT(DISTINCT CASE WHEN event_type = 'share' THEN user_id END) AS share_users
            FROM user_events
            GROUP BY source
            ORDER BY view_users DESC
            """,
            [RetrievalHit(user_events, 1.0)],
        )

        self.assertFalse(issues)

    def test_sql_quality_flags_high_value_user_without_level_name(self):
        users = TableInfo(
            "users",
            "用户",
            columns=(
                ColumnInfo("user_id", "INTEGER", primary_key=True),
                ColumnInfo("username", "TEXT"),
                ColumnInfo("level_id", "INTEGER"),
            ),
        )
        levels = TableInfo(
            "membership_levels",
            "会员等级",
            columns=(
                ColumnInfo("level_id", "INTEGER", primary_key=True),
                ColumnInfo("name", "TEXT"),
            ),
        )

        issues = inspect_sql_quality(
            "找出状态为active、users.total_spent记录值超过50000且最近登录早于2024-07-01的高价值沉默用户，按记录累计消费降序取前10。",
            "SELECT user_id, username, total_spent, last_login_at FROM users ORDER BY total_spent DESC LIMIT 10",
            [RetrievalHit(users, 1.0), RetrievalHit(levels, 0.9)],
        )

        self.assertTrue(any("level_name" in issue for issue in issues))

    def test_prompted_generator_repairs_high_confidence_quality_issues(self):
        orders = TableInfo(
            "orders",
            "订单",
            columns=(
                ColumnInfo("order_id", "INTEGER", primary_key=True),
                ColumnInfo("pay_status", "TEXT"),
                ColumnInfo("total_amount", "REAL"),
                ColumnInfo("receiver_province", "TEXT"),
            ),
        )
        llm = _SequencedLLM(
            [
                '{"sql":"SELECT receiver_province AS dimension_value, SUM(total_amount) AS metric_value FROM orders WHERE pay_status = \'paid\' GROUP BY receiver_province","chart_type":"bar","reasoning":"first"}',
                '{"sql":"SELECT receiver_province, COUNT(*) AS paid_order_count, ROUND(SUM(total_amount), 2) AS revenue, ROUND(AVG(total_amount), 2) AS avg_order_value FROM orders WHERE pay_status IN (\'paid\', \'partial_refund\') GROUP BY receiver_province","chart_type":"bar","reasoning":"fixed"}',
            ]
        )
        generator = PromptedSQLGenerator(llm)
        query = "按收货省份统计已支付订单数、销售额和客单价"

        plan = asyncio.run(generator.agenerate(query, [RetrievalHit(orders, 1.0)], []))

        self.assertEqual(len(llm.prompts), 2)
        self.assertIn("必须修复的问题", llm.prompts[1])
        self.assertIn("partial_refund", plan.sql)
        self.assertIn("paid_order_count", plan.sql)
        self.assertIn("quality_gate_repair", plan.warnings)
        self.assertTrue(any(warning.startswith("quality_gate_issue:") for warning in plan.warnings))

    def test_prompted_generator_repairs_dialect_quality_issues(self):
        shipments = TableInfo(
            "shipments",
            "物流",
            columns=(
                ColumnInfo("shipment_id", "INTEGER", primary_key=True),
                ColumnInfo("shipped_at", "DATETIME"),
                ColumnInfo("delivered_at", "DATETIME"),
            ),
        )
        llm = _SequencedLLM(
            [
                '{"sql":"SELECT AVG((julianday(delivered_at) - julianday(shipped_at)) * 24) AS avg_delivery_hours FROM shipments","chart_type":"kpi","reasoning":"first"}',
                '{"sql":"SELECT AVG(TIMESTAMPDIFF(HOUR, shipped_at, delivered_at)) AS avg_delivery_hours FROM shipments","chart_type":"kpi","reasoning":"fixed"}',
            ]
        )
        generator = PromptedSQLGenerator(llm, sql_dialect="mysql")

        plan = asyncio.run(generator.agenerate("统计平均配送小时数", [RetrievalHit(shipments, 1.0)], []))

        self.assertEqual(len(llm.prompts), 2)
        self.assertIn("方言错误", llm.prompts[1])
        self.assertIn("TIMESTAMPDIFF", plan.sql)
        self.assertIn("quality_gate_repair", plan.warnings)

    def test_prompted_generator_falls_back_to_clean_template_after_unresolved_quality_repair(self):
        tables = [
            TableInfo("orders"),
            TableInfo("order_items"),
            TableInfo("skus"),
            TableInfo("spus"),
            TableInfo("categories"),
        ]
        bad_sql = (
            "SELECT c.name AS dimension_value, SUM(oi.subtotal) AS metric_value "
            "FROM order_items oi "
            "JOIN orders o ON oi.order_id = o.order_id "
            "JOIN skus s ON oi.sku_id = s.sku_id "
            "JOIN spus sp ON s.spu_id = sp.spu_id "
            "JOIN categories c ON sp.category_id = c.category_id "
            "WHERE o.pay_status = 'paid' "
            "GROUP BY c.category_id, c.name "
            "ORDER BY metric_value DESC LIMIT 10"
        )
        llm = _SequencedLLM(
            [
                json.dumps({"sql": bad_sql, "chart_type": "bar", "reasoning": "first"}),
                json.dumps({"sql": bad_sql, "chart_type": "bar", "reasoning": "still bad"}),
            ]
        )
        generator = PromptedSQLGenerator(llm, sql_dialect="mysql")
        query = "统计已支付订单中销售额最高的10个商品品类毛利率；销售额用order_items.subtotal，成本用skus.cost乘以销量。"

        plan = asyncio.run(
            generator.agenerate(query, [RetrievalHit(table, 1.0) for table in tables], [])
        )

        self.assertEqual(len(llm.prompts), 2)
        self.assertIn("quality_gate_template_fallback", plan.warnings)
        self.assertIn("quality_gate_repair", plan.warnings)
        self.assertIn("category_id", plan.sql)
        self.assertIn("partial_refund", plan.sql)
        self.assertIn("gross_margin_rate", plan.sql)
        self.assertEqual(
            inspect_sql_quality(query, plan.sql, [RetrievalHit(table, 1.0) for table in tables], sql_dialect="mysql"),
            [],
        )

    def test_prompted_generator_prefers_clean_template_after_quality_repair(self):
        tables = [
            TableInfo("orders"),
            TableInfo("order_items"),
            TableInfo("skus"),
            TableInfo("spus"),
            TableInfo("categories"),
        ]
        bad_sql = (
            "SELECT c.name AS dimension_value, SUM(oi.subtotal) AS metric_value "
            "FROM order_items oi "
            "JOIN orders o ON oi.order_id = o.order_id "
            "JOIN skus s ON oi.sku_id = s.sku_id "
            "JOIN spus sp ON s.spu_id = sp.spu_id "
            "JOIN categories c ON sp.category_id = c.category_id "
            "WHERE o.pay_status = 'paid' "
            "GROUP BY c.category_id, c.name "
            "ORDER BY metric_value DESC LIMIT 10"
        )
        statically_clean_but_weaker_sql = (
            "SELECT c.category_id, c.name AS category_name, SUM(oi.quantity) AS units_sold, "
            "ROUND(SUM(oi.subtotal), 2) AS revenue, "
            "ROUND(SUM(s.cost * oi.quantity), 2) AS cost_amount, "
            "ROUND(SUM(oi.subtotal) - SUM(s.cost * oi.quantity), 2) AS gross_profit, "
            "ROUND((SUM(oi.subtotal) - SUM(s.cost * oi.quantity)) / NULLIF(SUM(oi.subtotal), 0), 4) AS gross_margin_rate "
            "FROM order_items oi "
            "JOIN orders o ON oi.order_id = o.order_id "
            "JOIN skus s ON oi.sku_id = s.sku_id "
            "JOIN spus sp ON s.spu_id = sp.spu_id "
            "JOIN categories c ON sp.category_id = c.category_id "
            "WHERE o.pay_status IN ('paid', 'partial_refund') "
            "GROUP BY c.category_id, c.name "
            "ORDER BY revenue DESC LIMIT 10"
        )
        llm = _SequencedLLM(
            [
                json.dumps({"sql": bad_sql, "chart_type": "bar", "reasoning": "first"}),
                json.dumps(
                    {
                        "sql": statically_clean_but_weaker_sql,
                        "chart_type": "bar",
                        "reasoning": "fixed",
                    }
                ),
            ]
        )
        generator = PromptedSQLGenerator(llm, sql_dialect="mysql")
        query = "统计已支付订单中销售额最高的10个商品品类毛利率；销售额用order_items.subtotal，成本用skus.cost乘以销量。"
        hits = [RetrievalHit(table, 1.0) for table in tables]

        plan = asyncio.run(generator.agenerate(query, hits, []))

        self.assertEqual(len(llm.prompts), 2)
        self.assertIn("quality_gate_template_fallback", plan.warnings)
        self.assertIn("Quality gate template fallback", plan.reasoning)
        self.assertIn("ORDER BY revenue DESC, c.category_id", plan.sql)
        self.assertEqual(inspect_sql_quality(query, plan.sql, hits, sql_dialect="mysql"), [])

    def test_ecommerce_quarterly_gmv_yoy_template(self):
        orders = TableInfo(
            "orders",
            "订单",
            columns=(
                ColumnInfo("order_id", "INTEGER", primary_key=True),
                ColumnInfo("order_date", "TEXT", semantic_tags=("time",)),
                ColumnInfo("pay_status", "TEXT", semantic_tags=("dimension",)),
                ColumnInfo("total_amount", "REAL", semantic_tags=("metric",)),
            ),
        )

        plan = self.generator.generate(
            "按季度统计已支付订单的GMV，并计算去年同季度同比增长率",
            [RetrievalHit(orders, 1.0)],
            [],
        )

        self.assertIn("prev.year = cur.year - 1", plan.sql)
        self.assertIn("pay_status IN ('paid', 'partial_refund')", plan.sql)
        self.assertIn("yoy_rate", plan.sql)

    def test_ecommerce_monthly_net_payment_template(self):
        payments = TableInfo(
            "payments",
            "支付记录",
            columns=(
                ColumnInfo("payment_id", "INTEGER", primary_key=True),
                ColumnInfo("paid_at", "TEXT", semantic_tags=("time",)),
                ColumnInfo("amount", "REAL", semantic_tags=("metric",)),
                ColumnInfo("status", "TEXT", semantic_tags=("dimension",)),
            ),
        )
        refunds = TableInfo(
            "refunds",
            "退款记录",
            columns=(
                ColumnInfo("refund_id", "INTEGER", primary_key=True),
                ColumnInfo("refunded_at", "TEXT", semantic_tags=("time",)),
                ColumnInfo("amount", "REAL", semantic_tags=("metric",)),
                ColumnInfo("status", "TEXT", semantic_tags=("dimension",)),
            ),
        )

        plan = self.generator.generate(
            "按月份统计成功支付金额、成功退款金额和净收款",
            [RetrievalHit(payments, 1.0), RetrievalHit(refunds, 0.9)],
            [],
        )

        self.assertIn("WITH paid AS", plan.sql)
        self.assertIn("net_payment", plan.sql)
        self.assertIn("status = 'success'", plan.sql)

    def test_ecommerce_discount_source_template_handles_empty_bucket_rate(self):
        orders = TableInfo(
            "orders",
            "订单",
            columns=(
                ColumnInfo("order_id", "INTEGER", primary_key=True),
                ColumnInfo("pay_status", "TEXT", semantic_tags=("dimension",)),
                ColumnInfo("product_amount", "REAL", semantic_tags=("metric",)),
                ColumnInfo("discount_amount", "REAL", semantic_tags=("metric",)),
                ColumnInfo("coupon_id", "INTEGER"),
                ColumnInfo("promotion_id", "INTEGER"),
            ),
        )

        plan = self.generator.generate(
            "比较已支付订单的优惠来源组合",
            [RetrievalHit(orders, 1.0)],
            [],
        )

        self.assertIn("coupon_and_promotion", plan.sql)
        self.assertIn("WHEN COALESCE(om.product_amount, 0) = 0 THEN 0", plan.sql)

    def test_ecommerce_member_repurchase_interval_template(self):
        users = TableInfo("users", "用户", columns=(ColumnInfo("user_id", "INTEGER"),))
        levels = TableInfo(
            "membership_levels",
            "会员等级",
            columns=(ColumnInfo("level_id", "INTEGER"), ColumnInfo("name", "TEXT")),
        )

        plan = self.generator.generate(
            "按会员等级统计已支付订单用户的平均复购间隔天数",
            [
                RetrievalHit(self.orders, 1.0),
                RetrievalHit(users, 0.9),
                RetrievalHit(levels, 0.8),
            ],
            [],
        )

        self.assertIn("LAG(order_date) OVER", plan.sql)
        self.assertIn("avg_repurchase_interval_days", plan.sql)

    def test_ecommerce_user_retention_template(self):
        events = TableInfo(
            "user_events",
            "用户行为事件",
            columns=(
                ColumnInfo("user_id", "INTEGER"),
                ColumnInfo("event_time", "TEXT", semantic_tags=("time",)),
            ),
        )

        plan = self.generator.generate(
            "统计2024年每月活跃用户数，以及这些用户下月仍有行为事件的留存率",
            [RetrievalHit(events, 1.0)],
            [],
        )

        self.assertIn("monthly_users", plan.sql)
        self.assertIn("next_month_retention_rate", plan.sql)

    def test_ecommerce_new_spu_90d_template(self):
        spus = TableInfo("spus", "商品SPU", columns=(ColumnInfo("spu_id", "INTEGER"),))
        skus = TableInfo("skus", "商品SKU", columns=(ColumnInfo("sku_id", "INTEGER"),))
        order_items = TableInfo("order_items", "订单明细", columns=(ColumnInfo("order_id", "INTEGER"),))

        plan = self.generator.generate(
            "统计观察窗完整的SPU上市后90天内已支付销售额最高的10个SPU",
            [
                RetrievalHit(spus, 1.0),
                RetrievalHit(skus, 0.9),
                RetrievalHit(order_items, 0.8),
                RetrievalHit(self.orders, 0.7),
            ],
            [],
        )

        self.assertIn("date(sp.listing_date, '+90 days')", plan.sql)
        self.assertIn("revenue_90d", plan.sql)

    def test_ecommerce_mysql_templates_use_mysql_date_functions(self):
        generator = DeterministicSQLGenerator(sql_dialect="mysql")
        cases = [
            (
                "按会员等级统计已支付订单用户的平均复购间隔天数",
                [self.orders, TableInfo("users"), TableInfo("membership_levels")],
                "DATEDIFF(po.order_date, po.prev_order_date)",
            ),
            (
                "统计观察窗完整的SPU上市后90天内已支付销售额最高的10个SPU",
                [TableInfo("spus"), TableInfo("skus"), TableInfo("order_items"), self.orders],
                "DATE_ADD(sp.listing_date, INTERVAL 90 DAY)",
            ),
            (
                "以库存流水最大created_at为基准，严格统计最近30天时间窗口内各仓库的出库总量",
                [TableInfo("inventory_movements"), TableInfo("warehouses")],
                "DATE_SUB(params.max_ts, INTERVAL 30 DAY)",
            ),
            (
                "以库存流水最大created_at为基准，严格统计最近90天时间窗口内库存净流入量最高的10个SKU",
                [TableInfo("inventory_movements"), TableInfo("skus")],
                "DATE_SUB(params.max_ts, INTERVAL 90 DAY)",
            ),
            (
                "按物流公司统计已签收包裹的平均配送小时数和72小时内签收率",
                [TableInfo("shipments"), TableInfo("logistics_companies")],
                "TIMESTAMPDIFF(SECOND, s.shipped_at, s.delivered_at) / 3600.0",
            ),
            (
                "按收货省份统计已签收包裹平均配送小时数，仅保留包裹数不少于50的省份，按配送最慢取前10",
                [TableInfo("shipments")],
                "TIMESTAMPDIFF(SECOND, shipped_at, delivered_at) / 3600.0",
            ),
            (
                "按退货原因统计已完成或已退款售后的平均处理天数和3日内完成率",
                [TableInfo("return_orders")],
                "DATEDIFF(complete_date, apply_date)",
            ),
        ]

        for query, tables, expected_fragment in cases:
            with self.subTest(query=query):
                hits = [RetrievalHit(table, 1.0) for table in tables]
                plan = generator.generate(query, hits, [])

                self.assertIn(expected_fragment, plan.sql)
                self.assertNotIn("julianday", plan.sql)
                self.assertNotIn("date(", plan.sql.lower())
                self.assertNotIn("datetime(", plan.sql.lower())
                self.assertEqual(
                    inspect_sql_quality(query, plan.sql, hits, sql_dialect="mysql"),
                    [],
                )

    def test_ecommerce_category_return_rate_template(self):
        categories = TableInfo("categories", "商品品类", columns=(ColumnInfo("category_id", "INTEGER"),))
        spus = TableInfo("spus", "商品SPU", columns=(ColumnInfo("spu_id", "INTEGER"),))
        skus = TableInfo("skus", "商品SKU", columns=(ColumnInfo("sku_id", "INTEGER"),))
        order_items = TableInfo("order_items", "订单明细", columns=(ColumnInfo("order_id", "INTEGER"),))
        return_items = TableInfo("return_items", "退货明细", columns=(ColumnInfo("return_id", "INTEGER"),))
        return_orders = TableInfo("return_orders", "退货单", columns=(ColumnInfo("return_id", "INTEGER"),))

        plan = self.generator.generate(
            "按商品品类统计退货件数相对销售件数的退货率",
            [
                RetrievalHit(categories, 1.0),
                RetrievalHit(spus, 0.9),
                RetrievalHit(skus, 0.8),
                RetrievalHit(order_items, 0.7),
                RetrievalHit(self.orders, 0.6),
                RetrievalHit(return_items, 0.5),
                RetrievalHit(return_orders, 0.4),
            ],
            [],
        )

        self.assertIn("WITH sales AS", plan.sql)
        self.assertIn("return_rate", plan.sql)
        self.assertIn("ro.status IN ('completed', 'refunded')", plan.sql)

    def test_ecommerce_low_stock_template(self):
        inventory = TableInfo("inventory", "库存", columns=(ColumnInfo("quantity", "INTEGER"),))
        skus = TableInfo("skus", "商品SKU", columns=(ColumnInfo("sku_id", "INTEGER"),))
        warehouses = TableInfo("warehouses", "仓库", columns=(ColumnInfo("warehouse_id", "INTEGER"),))

        plan = self.generator.generate(
            "查询当前库存低于安全库存的SKU-仓库组合，输出缺口量",
            [
                RetrievalHit(inventory, 1.0),
                RetrievalHit(skus, 0.9),
                RetrievalHit(warehouses, 0.8),
            ],
            [],
        )

        self.assertIn("i.safety_stock - i.quantity AS shortage_qty", plan.sql)
        self.assertIn("WHERE i.quantity < i.safety_stock", plan.sql)

    def test_ecommerce_sku_net_inflow_template(self):
        movements = TableInfo(
            "inventory_movements",
            "库存流水",
            columns=(ColumnInfo("created_at", "TEXT"), ColumnInfo("movement_type", "TEXT")),
        )
        skus = TableInfo("skus", "商品SKU", columns=(ColumnInfo("sku_id", "INTEGER"),))

        plan = self.generator.generate(
            "严格统计最近90天时间窗口内库存净流入量最高的10个SKU",
            [RetrievalHit(movements, 1.0), RetrievalHit(skus, 0.9)],
            [],
        )

        self.assertIn("datetime(params.max_ts, '-90 days')", plan.sql)
        self.assertIn("net_inflow_qty", plan.sql)

    def test_ecommerce_coupon_redeem_rate_template(self):
        coupons = TableInfo("coupons", "优惠券", columns=(ColumnInfo("coupon_id", "INTEGER"),))
        user_coupons = TableInfo(
            "user_coupons",
            "用户券",
            columns=(ColumnInfo("user_coupon_id", "INTEGER"),),
        )

        plan = self.generator.generate(
            "按优惠券类型统计领取张数、已使用张数和核销率",
            [RetrievalHit(coupons, 1.0), RetrievalHit(user_coupons, 0.9)],
            [],
        )

        self.assertIn("redeem_rate", plan.sql)
        self.assertIn("uc.status = 'used'", plan.sql)

    def test_ecommerce_promotion_sales_lift_template(self):
        promotions = TableInfo("promotions", "促销", columns=(ColumnInfo("promotion_id", "INTEGER"),))
        promotion_products = TableInfo(
            "promotion_products",
            "促销商品",
            columns=(ColumnInfo("promotion_id", "INTEGER"),),
        )
        order_items = TableInfo("order_items", "订单明细", columns=(ColumnInfo("order_id", "INTEGER"),))

        plan = self.generator.generate(
            "对每个促销，比较活动期内参与SKU已支付销售额与活动前等长周期销售额",
            [
                RetrievalHit(promotions, 1.0),
                RetrievalHit(promotion_products, 0.9),
                RetrievalHit(order_items, 0.8),
                RetrievalHit(self.orders, 0.7),
            ],
            [],
        )

        self.assertIn("promo_windows", plan.sql)
        self.assertIn("lift_rate", plan.sql)

    def test_ecommerce_paid_orders_without_signed_shipment_template(self):
        shipments = TableInfo("shipments", "物流", columns=(ColumnInfo("shipment_id", "INTEGER"),))

        plan = self.generator.generate(
            "统计已支付订单中尚未有已签收物流记录的订单状态分布",
            [RetrievalHit(self.orders, 1.0), RetrievalHit(shipments, 0.9)],
            [],
        )

        self.assertIn("LEFT JOIN shipments", plan.sql)
        self.assertIn("s.shipment_id IS NULL", plan.sql)

    def test_ecommerce_refund_reason_payment_method_template(self):
        refunds = TableInfo("refunds", "退款", columns=(ColumnInfo("payment_id", "INTEGER"),))
        payments = TableInfo("payments", "支付记录", columns=(ColumnInfo("payment_id", "INTEGER"),))

        plan = self.generator.generate(
            "按退款原因和原支付方式统计成功退款笔数与退款金额",
            [RetrievalHit(refunds, 1.0), RetrievalHit(payments, 0.9)],
            [],
        )

        self.assertIn("p.method AS payment_method", plan.sql)
        self.assertIn("r.status = 'success'", plan.sql)

    def test_ecommerce_sku_interest_score_template(self):
        events = TableInfo("user_events", "用户行为", columns=(ColumnInfo("sku_id", "INTEGER"),))
        skus = TableInfo("skus", "商品SKU", columns=(ColumnInfo("sku_id", "INTEGER"),))

        plan = self.generator.generate(
            "基于2024年SKU行为事件计算兴趣分",
            [RetrievalHit(events, 1.0), RetrievalHit(skus, 0.9)],
            [],
        )

        self.assertIn("add_cart_events * 3", plan.sql)
        self.assertIn("interest_score", plan.sql)

    def test_ecommerce_order_amount_reconciliation_template(self):
        order_items = TableInfo("order_items", "订单明细", columns=(ColumnInfo("order_id", "INTEGER"),))

        plan = self.generator.generate(
            "检查订单商品金额与订单明细小计之和是否一致",
            [RetrievalHit(self.orders, 1.0), RetrievalHit(order_items, 0.9)],
            [],
        )

        self.assertIn("item_amount", plan.sql)
        self.assertIn("mismatch_order_count", plan.sql)


if __name__ == "__main__":
    unittest.main()
