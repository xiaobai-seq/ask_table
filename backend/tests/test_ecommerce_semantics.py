from __future__ import annotations

"""电商 schema 语义元数据测试。

验证 examples/ecommerce/schema_metadata.yaml 能被 SchemaSemantics 正确加载：
1. 关键表中文别名到位；
2. 任务约定的枚举字段取值齐全（订单/支付/物流/售后/营销/行为等）；
3. YAML 中的表名与列名严格对齐 core.ecommerce_data.SCHEMA（防止臆造列名）；
4. 全部 30 张表都有中文别名（覆盖完整）。

注意：SchemaSemantics.from_yaml 用相对路径加载，测试须在 backend/ 目录下运行：
    PYTHONPATH=src python3 -m unittest discover -s tests -p "test_ecommerce_semantics.py" -v
"""

import unittest
from pathlib import Path

import yaml

from text2sql.accuracy.schema_semantics import SchemaSemantics
from text2sql.core import ecommerce_data as ed

# 与生产调用方一致：相对 backend/ 的路径，从 backend/ 目录运行测试。
_METADATA_PATH = "examples/ecommerce/schema_metadata.yaml"


class EcommerceSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sem = SchemaSemantics.from_yaml(_METADATA_PATH)

    def _assert_enum_superset(self, table: str, column: str, expected: tuple[str, ...]) -> None:
        """断言某列枚举包含全部期望取值，失败时给出可读定位信息。"""
        values = self.sem.enum_values(table, column)
        for item in expected:
            self.assertIn(item, values, f"{table}.{column} 缺少枚举值 {item}（实际：{values}）")

    def test_metadata_file_exists(self) -> None:
        # 相对路径依赖 cwd=backend；先确认文件存在，避免降级为空语义时误判
        self.assertTrue(
            Path(_METADATA_PATH).exists(),
            f"未找到 {_METADATA_PATH}，请在 backend/ 目录下运行测试",
        )

    def test_key_table_aliases(self) -> None:
        expected_aliases = {
            "orders": "订单",
            "payments": "支付记录",
            "users": "用户",
            "skus": "商品SKU",
            "user_events": "用户行为事件",
            "employees": "员工",
        }
        for table, alias in expected_aliases.items():
            self.assertEqual(self.sem.table_alias(table), alias)

    def test_order_status_and_pay_status_enum(self) -> None:
        self._assert_enum_superset(
            "orders", "status",
            ("created", "paid", "shipped", "completed", "cancelled", "closed"),
        )
        self._assert_enum_superset(
            "orders", "pay_status",
            ("unpaid", "paid", "partial_refund", "refunded"),
        )

    def test_payment_method_and_status_enum(self) -> None:
        self._assert_enum_superset(
            "payments", "method", ("alipay", "wechat", "credit_card", "balance"),
        )
        self._assert_enum_superset("payments", "status", ("success", "failed", "pending"))

    def test_shipment_status_enum(self) -> None:
        self._assert_enum_superset(
            "shipments", "status",
            ("pending", "shipped", "in_transit", "delivered", "signed"),
        )

    def test_return_order_status_and_reason_enum(self) -> None:
        self._assert_enum_superset(
            "return_orders", "status",
            ("applied", "approved", "rejected", "refunded", "completed"),
        )
        self._assert_enum_superset(
            "return_orders", "reason",
            ("quality", "wrong_item", "not_as_described", "no_reason", "damaged"),
        )

    def test_coupon_and_user_coupon_enum(self) -> None:
        self._assert_enum_superset("coupons", "type", ("full_reduction", "discount", "cash"))
        self._assert_enum_superset("user_coupons", "status", ("unused", "used", "expired"))

    def test_user_event_type_enum(self) -> None:
        self._assert_enum_superset(
            "user_events", "event_type", ("view", "add_cart", "favorite", "share"),
        )

    def test_membership_level_names_enum(self) -> None:
        self._assert_enum_superset(
            "membership_levels", "name", ("普通", "银卡", "金卡", "铂金", "钻石"),
        )

    def test_other_status_type_fields_have_enum(self) -> None:
        # design 未显式列出、但任务要求补齐的 status/type 类字段
        self.assertTrue(self.sem.enum_values("spus", "status"))
        self.assertTrue(self.sem.enum_values("skus", "status"))
        self.assertTrue(self.sem.enum_values("promotions", "type"))
        self.assertTrue(self.sem.enum_values("inventory_movements", "movement_type"))

    def test_all_thirty_tables_have_alias(self) -> None:
        # 语义元数据应覆盖 SCHEMA 全部 30 张表
        self.assertEqual(len(ed.SCHEMA), 30)
        for table in ed.SCHEMA:
            self.assertIsNotNone(
                self.sem.table_alias(table.name),
                f"表 {table.name} 缺少中文别名",
            )

    def test_yaml_table_and_column_names_align_with_schema(self) -> None:
        # 关键约束：YAML 中的表名/列名必须与 SCHEMA 完全一致，杜绝臆造
        raw = yaml.safe_load(Path(_METADATA_PATH).read_text(encoding="utf-8"))
        schema_columns = {t.name: {c.name for c in t.columns} for t in ed.SCHEMA}
        for table_name, table_meta in (raw.get("tables") or {}).items():
            self.assertIn(table_name, schema_columns, f"表 {table_name} 不在 SCHEMA 中")
            for column_name in (table_meta.get("columns") or {}):
                self.assertIn(
                    column_name,
                    schema_columns[table_name],
                    f"列 {table_name}.{column_name} 不在 SCHEMA 中",
                )


if __name__ == "__main__":
    unittest.main()
