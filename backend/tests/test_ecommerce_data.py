from __future__ import annotations

"""电商数据集生成器测试。

Task 1 覆盖 schema 契约与 DDL 渲染；后续任务（数据生成、写库、自检）在此追加
TestDimensions / TestFacts / TestWriters / TestSelfCheck 等测试类。
"""

import unittest

from text2sql.core import ecommerce_data as ed


class TestSchema(unittest.TestCase):
    def test_schema_has_30_tables(self):
        names = [t.name for t in ed.SCHEMA]
        self.assertEqual(len(names), 30)
        self.assertEqual(len(set(names)), 30)  # 无重名
        for expected in (
            "users", "orders", "order_items", "skus", "spus",
            "categories", "employees", "payments", "user_events",
        ):
            self.assertIn(expected, names)

    def test_render_ddl_sqlite_money_is_real(self):
        ddl = "\n".join(ed.render_ddl("sqlite"))
        self.assertIn("CREATE TABLE orders", ddl)
        self.assertIn("total_amount REAL", ddl)

    def test_render_ddl_mysql_money_is_decimal(self):
        ddl = "\n".join(ed.render_ddl("mysql"))
        self.assertIn("total_amount DECIMAL(12,2)", ddl)
        self.assertIn("AUTO_INCREMENT", ddl)

    def test_render_ddl_emits_foreign_keys(self):
        ddl = "\n".join(ed.render_ddl("sqlite"))
        # orders.user_id 外键应渲染为关系元数据，供 schema 关系发现使用
        self.assertIn("FOREIGN KEY (user_id) REFERENCES users(user_id)", ddl)
        # 自引用外键（多级类目）
        self.assertIn(
            "FOREIGN KEY (parent_id) REFERENCES categories(category_id)", ddl
        )


if __name__ == "__main__":
    unittest.main()
