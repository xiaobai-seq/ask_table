import unittest

from text2sql.core.models import ColumnInfo, ForeignKeyInfo, RelationshipPath, RetrievalHit, TableInfo
from text2sql.core.sql_generator import DeterministicSQLGenerator, parse_llm_sql_plan
from text2sql.core.sql_validator import SQLValidationError, SQLValidator


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


if __name__ == "__main__":
    unittest.main()
