from __future__ import annotations

"""电商数据集生成器测试。

Task 1 覆盖 schema 契约与 DDL 渲染；后续任务（数据生成、写库、自检）在此追加
TestDimensions / TestFacts / TestWriters / TestSelfCheck 等测试类。
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

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


def _cols(name: str) -> list[str]:
    """返回某张表在 SCHEMA 中的列名顺序，用于把数据行 tuple 转成 dict。"""

    return [c.name for c in next(t for t in ed.SCHEMA if t.name == name).columns]


class TestDimensions(unittest.TestCase):
    """Task 2：维度表确定性生成校验。"""

    @classmethod
    def setUpClass(cls):
        cls.ds = ed.generate(seed=ed.SEED)

    def test_all_30_tables_generated(self):
        # generate() 必须为 SCHEMA 中每张表都产出数据键
        schema_names = {t.name for t in ed.SCHEMA}
        self.assertEqual(set(self.ds.tables.keys()), schema_names)

    def test_row_counts_in_range(self):
        rows = self.ds.tables
        self.assertGreaterEqual(len(rows["users"]), 1500)
        self.assertGreaterEqual(len(rows["skus"]), 600)
        self.assertEqual(len(rows["membership_levels"]), 5)

    def test_categories_multi_level(self):
        # categories 列顺序: (category_id, name, parent_id, level, sort_order)
        levels = {r[3] for r in self.ds.tables["categories"]}
        self.assertGreaterEqual(max(levels), 3)  # 至少 3 级
        roots = [r for r in self.ds.tables["categories"] if r[2] is None]
        self.assertGreaterEqual(len(roots), 1)  # 至少 1 个根类目

    def test_categories_parent_reference_valid(self):
        ids = {r[0] for r in self.ds.tables["categories"]}
        parents = [r[2] for r in self.ds.tables["categories"] if r[2] is not None]
        self.assertTrue(all(p in ids for p in parents))

    def test_employees_self_reference(self):
        # employees: (employee_id, employee_name, manager_id, ...)
        ids = {r[0] for r in self.ds.tables["employees"]}
        mgr = [r[2] for r in self.ds.tables["employees"] if r[2] is not None]
        self.assertTrue(all(m in ids for m in mgr))  # manager 指向真实员工
        # 恰有 1 个 CEO（manager_id 为空）
        self.assertEqual(sum(1 for r in self.ds.tables["employees"] if r[2] is None), 1)

    def test_sku_price_gt_cost(self):
        # skus: (sku_id, spu_id, sku_name, price, cost, ...)
        self.assertTrue(all(r[3] > r[4] for r in self.ds.tables["skus"]))

    def test_membership_levels_thresholds_sorted(self):
        # (level_id, name, min_amount, discount_rate) 门槛应随等级递增
        amounts = [r[2] for r in self.ds.tables["membership_levels"]]
        self.assertEqual(amounts, sorted(amounts))

    def test_deterministic(self):
        ds2 = ed.generate(seed=ed.SEED)
        # 逐表比较，保证两次生成完全一致（可复现）
        for name in self.ds.tables:
            self.assertEqual(self.ds.tables[name], ds2.tables[name], name)


class TestFacts(unittest.TestCase):
    """Task 3：事实表与业务链路自洽性校验。"""

    @classmethod
    def setUpClass(cls):
        cls.ds = ed.generate(seed=ed.SEED)

    def _by_name(self, name):
        cols = _cols(name)
        return [dict(zip(cols, row)) for row in self.ds.tables[name]]

    def test_fact_row_counts_reasonable(self):
        rows = self.ds.tables
        self.assertGreaterEqual(len(rows["orders"]), 5000)
        self.assertGreaterEqual(len(rows["order_items"]), len(rows["orders"]))
        self.assertGreaterEqual(len(rows["inventory_movements"]), 8000)

    def test_order_amount_consistency(self):
        orders = {o["order_id"]: o for o in self._by_name("orders")}
        agg: dict[int, float] = {}
        for it in self._by_name("order_items"):
            agg[it["order_id"]] = round(agg.get(it["order_id"], 0) + it["subtotal"], 2)
        for oid, product_sum in agg.items():
            self.assertAlmostEqual(orders[oid]["product_amount"], product_sum, places=2)

    def test_total_amount_formula(self):
        # total_amount = product_amount - discount_amount + shipping_fee
        for o in self._by_name("orders"):
            expected = round(
                o["product_amount"] - o["discount_amount"] + o["shipping_fee"], 2
            )
            self.assertAlmostEqual(o["total_amount"], expected, places=2)

    def test_paid_orders_have_success_payment_equal_total(self):
        orders = {o["order_id"]: o for o in self._by_name("orders")}
        success = {}
        for p in self._by_name("payments"):
            if p["status"] == "success":
                success[p["order_id"]] = p["amount"]
        for oid, o in orders.items():
            if o["pay_status"] in ("paid", "partial_refund", "refunded"):
                self.assertIn(oid, success)
                self.assertAlmostEqual(success[oid], o["total_amount"], places=2)

    def test_completed_orders_fully_linked(self):
        # completed 订单必有 success 支付 + signed 物流 + 状态流转历史
        completed = {o["order_id"] for o in self._by_name("orders")
                     if o["status"] == "completed"}
        success_pay = {p["order_id"] for p in self._by_name("payments")
                       if p["status"] == "success"}
        signed = {s["order_id"] for s in self._by_name("shipments")
                  if s["status"] == "signed"}
        history = {h["order_id"] for h in self._by_name("order_status_history")}
        for oid in completed:
            self.assertIn(oid, success_pay)
            self.assertIn(oid, signed)
            self.assertIn(oid, history)

    def test_returns_only_on_paid_orders(self):
        paid = {o["order_id"] for o in self._by_name("orders")
                if o["pay_status"] in ("paid", "partial_refund", "refunded")}
        for r in self._by_name("return_orders"):
            self.assertIn(r["order_id"], paid)

    def test_user_total_spent_matches_completed_orders(self):
        spent: dict[int, float] = {}
        for o in self._by_name("orders"):
            if o["status"] == "completed":
                spent[o["user_id"]] = round(
                    spent.get(o["user_id"], 0.0) + o["total_amount"], 2
                )
        for u in self._by_name("users"):
            self.assertAlmostEqual(
                u["total_spent"], spent.get(u["user_id"], 0.0), places=2
            )

    def test_inventory_net_non_negative(self):
        # 每个 (sku, warehouse) 的 inbound-outbound 净值应 = inventory.quantity 且 ≥ 0
        net: dict[tuple, int] = {}
        for m in self._by_name("inventory_movements"):
            key = (m["sku_id"], m["warehouse_id"])
            delta = m["quantity"] if m["movement_type"] == "inbound" else (
                -m["quantity"] if m["movement_type"] == "outbound" else 0
            )
            net[key] = net.get(key, 0) + delta
        for inv in self._by_name("inventory"):
            key = (inv["sku_id"], inv["warehouse_id"])
            self.assertGreaterEqual(net.get(key, 0), 0)
            self.assertEqual(inv["quantity"], net.get(key, 0))

    def test_time_span_covers_24_months(self):
        months = {o["order_date"][:7] for o in self._by_name("orders")}
        self.assertGreaterEqual(len(months), 24)
        self.assertTrue(any(m.startswith("2023") for m in months))
        self.assertTrue(any(m.startswith("2024") for m in months))

    def test_promo_month_volume_higher(self):
        from collections import Counter
        counter = Counter(o["order_date"][:7] for o in self._by_name("orders"))
        # 双 11 所在月订单量高于相邻普通月
        self.assertGreater(counter["2024-11"], counter["2024-09"])

    def test_user_events_is_largest(self):
        self.assertGreaterEqual(len(self.ds.tables["user_events"]), 40000)


class TestWriters(unittest.TestCase):
    """Task 4：SQLite 建库 + MySQL 脚本导出。"""

    @classmethod
    def setUpClass(cls):
        cls.ds = ed.generate(seed=ed.SEED)

    def test_write_sqlite_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "ec.db")
            ed.write_sqlite(self.ds, path)
            conn = sqlite3.connect(path)
            try:
                # 排除 AUTOINCREMENT 触发生成的内部表 sqlite_sequence
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                self.assertEqual(len(tables), 30)
                for name in ("orders", "order_items", "user_events"):
                    n = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                    self.assertEqual(n, len(self.ds.tables[name]))
                # 长链路多表 JOIN 能跑通（order_items→skus→spus→categories）
                joined = conn.execute(
                    "SELECT c.name FROM order_items oi "
                    "JOIN skus s ON oi.sku_id=s.sku_id "
                    "JOIN spus p ON s.spu_id=p.spu_id "
                    "JOIN categories c ON p.category_id=c.category_id LIMIT 1"
                ).fetchall()
                self.assertEqual(len(joined), 1)
            finally:
                conn.close()

    def test_write_mysql_script(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "ec.sql")
            ed.write_mysql_script(self.ds, path)
            text = Path(path).read_text(encoding="utf-8")
            self.assertIn("SET FOREIGN_KEY_CHECKS=0;", text)
            self.assertIn("SET FOREIGN_KEY_CHECKS=1;", text)
            self.assertIn("CREATE TABLE orders", text)
            self.assertIn("DECIMAL(12,2)", text)
            self.assertIn("INSERT INTO orders", text)


class TestSelfCheck(unittest.TestCase):
    """Task 5：业务约束自检（design §5），违背即抛 DataIntegrityError。"""

    def _set(self, ds, table, row_idx, col, value):
        cols = _cols(table)
        row = list(ds.tables[table][row_idx])
        row[cols.index(col)] = value
        ds.tables[table][row_idx] = tuple(row)

    def test_valid_dataset_passes(self):
        ds = ed.generate(seed=ed.SEED)
        ed.self_check(ds)  # 不抛异常即通过

    def test_broken_product_amount_raises(self):
        ds = ed.generate(seed=ed.SEED)
        # 篡改首单 product_amount，破坏「product_amount = Σ subtotal」
        self._set(ds, "orders", 0, "product_amount",
                  ds.tables["orders"][0][_cols("orders").index("product_amount")] + 999.0)
        with self.assertRaises(ed.DataIntegrityError):
            ed.self_check(ds)

    def test_broken_total_formula_raises(self):
        ds = ed.generate(seed=ed.SEED)
        # 篡改首单 total_amount，破坏「total = product - discount + shipping」
        self._set(ds, "orders", 0, "total_amount",
                  ds.tables["orders"][0][_cols("orders").index("total_amount")] + 500.0)
        with self.assertRaises(ed.DataIntegrityError):
            ed.self_check(ds)

    def test_return_on_unpaid_order_raises(self):
        ds = ed.generate(seed=ed.SEED)
        # 找一个未支付订单，伪造一条退货单指向它 → 违背「退货仅发生在已支付订单」
        unpaid_id = next(o[_cols("orders").index("order_id")]
                         for o in ds.tables["orders"]
                         if o[_cols("orders").index("pay_status")] == "unpaid")
        rid = len(ds.tables["return_orders"]) + 1
        ds.tables["return_orders"].append(
            (rid, unpaid_id, 1, "no_reason", "applied", "2024-01-01", None, None)
        )
        with self.assertRaises(ed.DataIntegrityError):
            ed.self_check(ds)

    def test_negative_inventory_raises(self):
        ds = ed.generate(seed=ed.SEED)
        # 篡改一条库存 quantity 与流水净值不符（且为负）
        self._set(ds, "inventory", 0, "quantity", -5)
        with self.assertRaises(ed.DataIntegrityError):
            ed.self_check(ds)


if __name__ == "__main__":
    unittest.main()
