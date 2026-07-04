# 电商场景 Text2SQL 测试数据集 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 新增一套约 30 张表的 OLTP 电商测试数据集（生成器 + 双引擎产物 + 语义/示例/评测三件套），用于压测通用 Text2SQL 在复杂多表场景下的表现，且不改动内核、不破坏现有 demo 测试。

**Architecture:** 单一生成器模块 `core/ecommerce_data.py`：抽象类型声明一次 schema → 渲染 SQLite/MySQL 两种方言 DDL；纯标准库确定性生成各表数据行；`write_sqlite` 建库、`write_mysql_script` 导出 `.sql`；`self_check` 校验业务约束。配套 `examples/ecommerce/` 下的 yaml/jsonl 资产通过环境变量切换启用。

**Tech Stack:** Python 3.10+，标准库 `sqlite3`/`random`/`datetime`/`dataclasses`，`unittest`（沿用现有 `backend/tests` 测试风格），可选 `pyyaml`（已在依赖）。

设计依据见 `docs/plans/2026-07-04-ecommerce-test-dataset-design.md`。

---

## 约定

- 所有命令在 `backend/` 目录下执行，测试用 `PYTHONPATH=src python3 -m unittest`。
- 提交信息用 conventional commits（`feat:` / `test:` / `docs:` / `chore:`）。
- 遵循现有代码风格：`from __future__ import annotations` 在前、模块 docstring 在后（ruff 已忽略 E402）；中文注释解释「为什么」。
- 生成确定性：所有随机取值来自 `random.Random(SEED)`，禁止用全局 `random` 或 `datetime.now()`。

---

## Task 0: 目录与 gitignore 准备

**Files:**
- Create: `backend/examples/ecommerce/.gitkeep`
- Modify: `.gitignore`（仓库根）

**Step 1: 创建目录占位**

```bash
mkdir -p backend/examples/ecommerce && touch backend/examples/ecommerce/.gitkeep
```

**Step 2: 忽略生成的二进制库**

在根 `.gitignore` 追加（若无 `.gitignore` 则创建）：

```gitignore
# 电商测试数据集：SQLite 产物靠脚本复现，不入库
backend/examples/ecommerce/*.db
```

**Step 3: 验证**

Run: `git check-ignore backend/examples/ecommerce/ecommerce.db`
Expected: 输出该路径（表示已被忽略）。

**Step 4: Commit**

```bash
git add .gitignore backend/examples/ecommerce/.gitkeep
git commit -m "chore: prepare ecommerce dataset dir and gitignore db artifact"
```

---

## Task 1: Schema 定义 + 双方言 DDL 渲染

**Files:**
- Create: `backend/src/text2sql/core/ecommerce_data.py`
- Test: `backend/tests/test_ecommerce_data.py`

**Step 1: Write the failing test**

```python
from __future__ import annotations

import unittest

from text2sql.core import ecommerce_data as ed


class TestSchema(unittest.TestCase):
    def test_schema_has_30_tables(self):
        names = [t.name for t in ed.SCHEMA]
        self.assertEqual(len(names), 30)
        self.assertEqual(len(set(names)), 30)  # 无重名
        # 抽查关键表存在
        for expected in ("users", "orders", "order_items", "skus", "categories", "employees"):
            self.assertIn(expected, names)

    def test_render_ddl_sqlite_money_is_real(self):
        ddl = "\n".join(ed.render_ddl("sqlite"))
        self.assertIn("CREATE TABLE orders", ddl)
        # 金额列在 SQLite 映射为 REAL
        self.assertIn("total_amount REAL", ddl)

    def test_render_ddl_mysql_money_is_decimal(self):
        ddl = "\n".join(ed.render_ddl("mysql"))
        self.assertIn("total_amount DECIMAL(12,2)", ddl)
        self.assertIn("AUTO_INCREMENT", ddl)
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_ecommerce_data -v`
Expected: FAIL（`AttributeError: module has no attribute 'SCHEMA'`）。

**Step 3: Write minimal implementation**

在 `ecommerce_data.py` 中实现类型系统与 DDL 渲染（骨架，需补全 30 张表）：

```python
from __future__ import annotations

"""电商场景 Text2SQL 测试数据集生成器。

抽象类型声明一次 schema，渲染 SQLite / MySQL 两种方言 DDL；纯标准库确定性
生成各表数据；write_sqlite 建库、write_mysql_script 导出 .sql。
生成时无需 MySQL 在线，SQLite 路径零第三方依赖。
"""

from dataclasses import dataclass, field

SEED = 20240101


@dataclass(frozen=True)
class Column:
    name: str
    type: str          # pk|int|bigint|money|varchar:N|text|date|datetime|bool
    nullable: bool = True
    fk: str | None = None   # "table.column"


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    unique: tuple[tuple[str, ...], ...] = ()


# 抽象类型 → 方言映射；varchar:N 单独处理
_TYPE_MAP = {
    "sqlite": {"pk": "INTEGER PRIMARY KEY AUTOINCREMENT", "int": "INTEGER",
               "bigint": "INTEGER", "money": "REAL", "text": "TEXT",
               "date": "TEXT", "datetime": "TEXT", "bool": "INTEGER"},
    "mysql": {"pk": "INT AUTO_INCREMENT PRIMARY KEY", "int": "INT",
              "bigint": "BIGINT", "money": "DECIMAL(12,2)", "text": "TEXT",
              "date": "DATE", "datetime": "DATETIME", "bool": "TINYINT(1)"},
}


def _render_type(col_type: str, dialect: str) -> str:
    if col_type.startswith("varchar:"):
        n = col_type.split(":", 1)[1]
        return "TEXT" if dialect == "sqlite" else f"VARCHAR({n})"
    return _TYPE_MAP[dialect][col_type]


def render_ddl(dialect: str) -> list[str]:
    """为每张表渲染建表语句；SQLite 与 MySQL 共用同一 schema 定义。"""
    statements: list[str] = []
    for table in SCHEMA:
        lines = []
        for col in table.columns:
            null = "" if col.nullable or col.type == "pk" else " NOT NULL"
            lines.append(f"  {col.name} {_render_type(col.type, dialect)}{null}")
        for cols in table.unique:
            lines.append(f"  UNIQUE ({', '.join(cols)})")
        body = ",\n".join(lines)
        suffix = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" if dialect == "mysql" else ""
        statements.append(f"CREATE TABLE {table.name} (\n{body}\n){suffix};")
    return statements


# schema 定义：按 design.md §4 补全全部 30 张表。示例（用户与会员域）：
SCHEMA: list[Table] = [
    Table("membership_levels", (
        Column("level_id", "pk"),
        Column("name", "varchar:20", nullable=False),
        Column("min_amount", "money", nullable=False),
        Column("discount_rate", "money", nullable=False),
    )),
    Table("users", (
        Column("user_id", "pk"),
        Column("username", "varchar:50", nullable=False),
        Column("gender", "varchar:10"),
        Column("birth_date", "date"),
        Column("phone", "varchar:20"),
        Column("email", "varchar:100"),
        Column("level_id", "int", nullable=False, fk="membership_levels.level_id"),
        Column("register_date", "date", nullable=False),
        Column("total_spent", "money", nullable=False),
        Column("status", "varchar:20", nullable=False),
        Column("last_login_at", "datetime"),
    )),
    # ... 其余 28 张表按 design.md §4 逐一定义 ...
]
```

> 执行提示：本步只需让 3 个测试通过（`SCHEMA` 至少含被抽查的表、DDL 类型映射正确）。**Task 2/3 会用到全部 30 张表，所以本步就把 30 张表按 design.md §4 一次性定义完整**（`test_schema_has_30_tables` 强制 =30）。外键先记录在 `Column.fk`，Task 4 决定是否在 DDL 输出 `FOREIGN KEY`（SQLite 默认不强约束，可仅 MySQL 输出，或都不输出仅作元数据——推荐都不输出，靠生成器保证完整性，避免插入顺序问题）。

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.test_ecommerce_data -v`
Expected: PASS（3 个测试）。

**Step 5: Commit**

```bash
git add backend/src/text2sql/core/ecommerce_data.py backend/tests/test_ecommerce_data.py
git commit -m "feat: add ecommerce schema definition and dual-dialect DDL rendering"
```

---

## Task 2: 维度表数据生成（确定性）

**Files:**
- Modify: `backend/src/text2sql/core/ecommerce_data.py`
- Test: `backend/tests/test_ecommerce_data.py`

覆盖维表：membership_levels、users、user_addresses、categories(多级)、brands、suppliers、spus、skus、sku_attributes、warehouses、logistics_companies、employees(自关联)、coupons、promotions。

**Step 1: Write the failing test**

```python
class TestDimensions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = ed.generate(seed=ed.SEED)

    def test_row_counts_in_range(self):
        rows = self.ds.tables
        self.assertGreaterEqual(len(rows["users"]), 1500)
        self.assertGreaterEqual(len(rows["skus"]), 600)
        self.assertEqual(len(rows["membership_levels"]), 5)

    def test_categories_multi_level(self):
        # categories 列顺序: (category_id, name, parent_id, level, sort_order)
        levels = {r[3] for r in self.ds.tables["categories"]}
        self.assertTrue(max(levels) >= 3)  # 至少 3 级
        roots = [r for r in self.ds.tables["categories"] if r[2] is None]
        self.assertTrue(len(roots) >= 1)

    def test_employees_self_reference(self):
        # employees: (employee_id, name, manager_id, ...)
        ids = {r[0] for r in self.ds.tables["employees"]}
        mgr = [r[2] for r in self.ds.tables["employees"] if r[2] is not None]
        self.assertTrue(all(m in ids for m in mgr))  # manager 指向真实员工
        self.assertTrue(any(r[2] is None for r in self.ds.tables["employees"]))  # 有 CEO

    def test_sku_price_gt_cost(self):
        # skus: (sku_id, spu_id, sku_name, price, cost, ...)
        self.assertTrue(all(r[3] > r[4] for r in self.ds.tables["skus"]))

    def test_deterministic(self):
        ds2 = ed.generate(seed=ed.SEED)
        self.assertEqual(self.ds.tables["users"], ds2.tables["users"])
```

**Step 2: Run to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_ecommerce_data.TestDimensions -v`
Expected: FAIL（`generate` 未定义）。

**Step 3: Implement**

新增 `Dataset` 容器与 `generate()`，及各维度生成函数。骨架：

```python
import random

@dataclass
class Dataset:
    tables: dict[str, list[tuple]] = field(default_factory=dict)


# 真实感常量池（截断示例，实现时按 design.md §6 扩充）
_PROVINCES = ["广东", "浙江", "江苏", "北京", "上海", "四川", "山东", "湖北", "福建", "陕西"]
_CATEGORY_TREE = {
    "手机数码": ["智能手机", "笔记本电脑", "平板", "耳机"],
    "服装鞋包": ["男装", "女装", "运动鞋", "箱包"],
    "美妆个护": ["面部护理", "彩妆", "香水", "洗护"],
    "家用电器": ["电视", "冰箱", "洗衣机", "空调"],
    "食品生鲜": ["零食", "饮料", "生鲜", "粮油"],
}

def generate(seed: int = SEED) -> Dataset:
    """确定性生成全部 30 张表；顺序保证外键引用的父表先生成。"""
    rng = random.Random(seed)
    ds = Dataset()
    ds.tables["membership_levels"] = _gen_membership_levels()
    ds.tables["categories"] = _gen_categories(rng)
    ds.tables["brands"] = _gen_brands(rng)
    ds.tables["suppliers"] = _gen_suppliers(rng)
    ds.tables["spus"] = _gen_spus(rng, ds)
    ds.tables["skus"] = _gen_skus(rng, ds)
    ds.tables["sku_attributes"] = _gen_sku_attributes(rng, ds)
    ds.tables["users"] = _gen_users(rng)
    ds.tables["user_addresses"] = _gen_user_addresses(rng, ds)
    ds.tables["warehouses"] = _gen_warehouses(rng)
    ds.tables["logistics_companies"] = _gen_logistics_companies()
    ds.tables["employees"] = _gen_employees(rng)
    ds.tables["coupons"] = _gen_coupons(rng)
    ds.tables["promotions"] = _gen_promotions(rng)
    # 事实表在 Task 3 追加
    return ds
```

各 `_gen_*` 函数逐行构造 `tuple`，列顺序**必须**与 `SCHEMA` 中该表列顺序一致（Task 4 写库依赖此约定）。`categories` 用 `_CATEGORY_TREE` 造 1 级根+2 级子+3 级叶；`employees` 造 1 个 CEO(manager_id=None)+若干层下属。

**Step 4: Run to verify it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.test_ecommerce_data.TestDimensions -v`
Expected: PASS。

**Step 5: Commit**

```bash
git add backend/src/text2sql/core/ecommerce_data.py backend/tests/test_ecommerce_data.py
git commit -m "feat: generate deterministic ecommerce dimension tables"
```

---

## Task 3: 事实表与业务链路生成

**Files:**
- Modify: `backend/src/text2sql/core/ecommerce_data.py`
- Test: `backend/tests/test_ecommerce_data.py`

覆盖：orders、order_items、order_status_history、payments、refunds、shipments、carts、cart_items、user_coupons、inventory、inventory_movements、product_reviews、user_events、return_orders、return_items、promotion_products。

**Step 1: Write the failing test**

```python
class TestFacts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = ed.generate(seed=ed.SEED)

    def _by_name(self, name):
        cols = [c.name for c in next(t for t in ed.SCHEMA if t.name == name).columns]
        return [dict(zip(cols, row)) for row in self.ds.tables[name]]

    def test_order_amount_consistency(self):
        orders = {o["order_id"]: o for o in self._by_name("orders")}
        items = self._by_name("order_items")
        agg: dict[int, float] = {}
        for it in items:
            agg[it["order_id"]] = round(agg.get(it["order_id"], 0) + it["subtotal"], 2)
        for oid, product_sum in agg.items():
            self.assertAlmostEqual(orders[oid]["product_amount"], product_sum, places=2)

    def test_returns_only_on_paid_orders(self):
        paid = {o["order_id"] for o in self._by_name("orders") if o["pay_status"] in ("paid", "partial_refund", "refunded")}
        for r in self._by_name("return_orders"):
            self.assertIn(r["order_id"], paid)

    def test_time_span_covers_24_months(self):
        months = {o["order_date"][:7] for o in self._by_name("orders")}
        self.assertGreaterEqual(len(months), 24)
        self.assertTrue(any(m.startswith("2023") for m in months))
        self.assertTrue(any(m.startswith("2024") for m in months))

    def test_promo_month_volume_higher(self):
        from collections import Counter
        c = Counter(o["order_date"][:7] for o in self._by_name("orders"))
        # 双11所在月订单量高于相邻普通月
        self.assertGreater(c["2024-11"], c["2024-09"])

    def test_user_events_is_largest(self):
        self.assertGreaterEqual(len(self.ds.tables["user_events"]), 40000)
```

**Step 2: Run to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_ecommerce_data.TestFacts -v`
Expected: FAIL（事实表为空/键缺失）。

**Step 3: Implement**

在 `generate()` 尾部追加事实表生成，核心规则：
- 按 24 个月循环，基础月订单量 ~300，大促月（6/11/12）×1.8 且客单价上浮。
- 每笔订单：随机选用户 → 选 1-4 个 sku 造 `order_items`（`subtotal = unit_price * quantity`）→ 回填 `product_amount = Σ subtotal`，`discount_amount`（部分订单用券/活动），`total_amount = product_amount - discount + shipping_fee`。
- 依 `status` 联动：`completed`→造 success `payment`(amount=total) + `shipment`(status=signed, delivered_at) + `order_status_history` 链；`cancelled`→无有效支付。
- 约 7% 已支付订单造 `return_orders`(+`return_items`)，`refund_amount ≤` 已付。
- `inventory` 每个 sku×主仓一行；`inventory_movements` 由入库批次 + 出库(对应发货)构成，保证净库存≥0。
- `user_events` 最大表：为活跃用户造 view/add_cart/favorite/share，加购事件数与下单存在漏斗比例。
- `user_coupons`/`promotion_products`/`product_reviews`/`carts`/`cart_items` 按 design 规模生成。

**Step 4: Run to verify it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.test_ecommerce_data.TestFacts -v`
Expected: PASS。

**Step 5: Commit**

```bash
git add backend/src/text2sql/core/ecommerce_data.py backend/tests/test_ecommerce_data.py
git commit -m "feat: generate ecommerce fact tables with business-consistent linkage"
```

---

## Task 4: SQLite 写库 + MySQL 脚本导出 + CLI

**Files:**
- Modify: `backend/src/text2sql/core/ecommerce_data.py`
- Modify: `backend/pyproject.toml`（新增 script 入口）
- Test: `backend/tests/test_ecommerce_data.py`

**Step 1: Write the failing test**

```python
import sqlite3
import tempfile
from pathlib import Path


class TestWriters(unittest.TestCase):
    def test_write_sqlite_roundtrip(self):
        ds = ed.generate(seed=ed.SEED)
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "ec.db")
            ed.write_sqlite(ds, path)
            conn = sqlite3.connect(path)
            try:
                tbls = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertEqual(len(tbls), 30)
                n = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
                self.assertEqual(n, len(ds.tables["orders"]))
                # 抽样一条多表 JOIN 能跑通
                conn.execute(
                    "SELECT c.name FROM order_items oi "
                    "JOIN skus s ON oi.sku_id=s.sku_id "
                    "JOIN spus p ON s.spu_id=p.spu_id "
                    "JOIN categories c ON p.category_id=c.category_id LIMIT 1"
                ).fetchall()
            finally:
                conn.close()

    def test_write_mysql_script(self):
        ds = ed.generate(seed=ed.SEED)
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "ec.sql")
            ed.write_mysql_script(ds, path)
            text = Path(path).read_text(encoding="utf-8")
            self.assertIn("CREATE TABLE orders", text)
            self.assertIn("DECIMAL(12,2)", text)
            self.assertIn("INSERT INTO orders", text)
```

**Step 2: Run to verify it fails**

Expected: FAIL（`write_sqlite` 未定义）。

**Step 3: Implement**

```python
import sqlite3
from pathlib import Path


def _placeholder_row(row: tuple) -> str:
    # MySQL 脚本用字面量；None→NULL，字符串转义单引号，数字原样
    parts = []
    for v in row:
        if v is None:
            parts.append("NULL")
        elif isinstance(v, (int, float)):
            parts.append(repr(v))
        else:
            parts.append("'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'")
    return "(" + ", ".join(parts) + ")"


def write_sqlite(ds: Dataset, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript("\n".join(render_ddl("sqlite")))
        for table in SCHEMA:                       # 按 schema 顺序，父表先插
            rows = ds.tables.get(table.name, [])
            if not rows:
                continue
            ph = ", ".join(["?"] * len(table.columns))
            conn.executemany(f"INSERT INTO {table.name} VALUES ({ph})", rows)
        conn.commit()
    finally:
        conn.close()


def write_mysql_script(ds: Dataset, path: str) -> None:
    lines = ["SET FOREIGN_KEY_CHECKS=0;"]
    lines += render_ddl("mysql")
    for table in SCHEMA:
        rows = ds.tables.get(table.name, [])
        for i in range(0, len(rows), 500):          # 批量 INSERT，避免超长单句
            chunk = rows[i:i + 500]
            values = ",\n".join(_placeholder_row(r) for r in chunk)
            lines.append(f"INSERT INTO {table.name} VALUES\n{values};")
    lines.append("SET FOREIGN_KEY_CHECKS=1;")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    """CLI：生成 SQLite 库与 MySQL 脚本。"""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite-out", default="examples/ecommerce/ecommerce.db")
    parser.add_argument("--mysql-out", default="examples/ecommerce/ecommerce_mysql.sql")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    ds = generate(seed=args.seed)
    self_check(ds)                                  # Task 5 引入
    write_sqlite(ds, args.sqlite_out)
    write_mysql_script(ds, args.mysql_out)
    print(args.sqlite_out, args.mysql_out)


if __name__ == "__main__":
    main()
```

在 `pyproject.toml` 的 `[project.scripts]` 追加：

```toml
text2sql-ecommerce-db = "text2sql.core.ecommerce_data:main"
```

> 注：本 Task 若 `self_check` 尚未实现，先在 `main` 里临时去掉该调用，Task 5 再加回；或先实现空的 `def self_check(ds): pass`。推荐后者。

**Step 4: Run to verify it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.test_ecommerce_data.TestWriters -v`
Expected: PASS。

**Step 5: Commit**

```bash
git add backend/src/text2sql/core/ecommerce_data.py backend/pyproject.toml backend/tests/test_ecommerce_data.py
git commit -m "feat: add sqlite writer, mysql script exporter and CLI entrypoint"
```

---

## Task 5: 生成器自检（业务约束校验）

**Files:**
- Modify: `backend/src/text2sql/core/ecommerce_data.py`
- Test: `backend/tests/test_ecommerce_data.py`

**Step 1: Write the failing test**

```python
class TestSelfCheck(unittest.TestCase):
    def test_valid_dataset_passes(self):
        ds = ed.generate(seed=ed.SEED)
        ed.self_check(ds)  # 不抛异常即通过

    def test_broken_amount_raises(self):
        ds = ed.generate(seed=ed.SEED)
        # 篡改第一条订单的 product_amount 制造不自洽
        cols = [c.name for c in next(t for t in ed.SCHEMA if t.name == "orders").columns]
        idx = cols.index("product_amount")
        row = list(ds.tables["orders"][0])
        row[idx] = row[idx] + 999.0
        ds.tables["orders"][0] = tuple(row)
        with self.assertRaises(ed.DataIntegrityError):
            ed.self_check(ds)
```

**Step 2: Run to verify it fails**

Expected: FAIL（`self_check`/`DataIntegrityError` 未定义或为空 pass）。

**Step 3: Implement**

```python
class DataIntegrityError(ValueError):
    pass


def self_check(ds: Dataset) -> None:
    """校验 design.md §5 业务约束；任一不满足抛 DataIntegrityError。"""
    def dicts(name):
        cols = [c.name for c in next(t for t in SCHEMA if t.name == name).columns]
        return [dict(zip(cols, r)) for r in ds.tables[name]]

    # 1) 表数
    if len(ds.tables) != 30:
        raise DataIntegrityError(f"expected 30 tables, got {len(ds.tables)}")

    # 2) 订单金额自洽
    orders = {o["order_id"]: o for o in dicts("orders")}
    agg: dict[int, float] = {}
    for it in dicts("order_items"):
        agg[it["order_id"]] = round(agg.get(it["order_id"], 0) + it["subtotal"], 2)
    for oid, s in agg.items():
        if abs(orders[oid]["product_amount"] - s) > 0.01:
            raise DataIntegrityError(f"order {oid} product_amount mismatch")

    # 3) 退货仅发生在已支付订单
    paid = {o["order_id"] for o in orders.values()
            if o["pay_status"] in ("paid", "partial_refund", "refunded")}
    for r in dicts("return_orders"):
        if r["order_id"] not in paid:
            raise DataIntegrityError(f"return on unpaid order {r['order_id']}")

    # 4) 库存净值非负（按 sku+warehouse 汇总 movements）
    # 5) 外键引用存在性（抽查 order_items.sku_id ∈ skus 等）
    # ...按 design §5 补全其余约束...
```

**Step 4: Run to verify it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.test_ecommerce_data.TestSelfCheck -v`
Expected: PASS。恢复 `main()` 中对 `self_check(ds)` 的调用。

**Step 5: 生成实际产物并提交脚本**

```bash
cd backend
PYTHONPATH=src python3 -m text2sql.core.ecommerce_data \
  --sqlite-out examples/ecommerce/ecommerce.db \
  --mysql-out  examples/ecommerce/ecommerce_mysql.sql
# 校验 .db 被忽略、.sql 纳入
git status --short
git add backend/src/text2sql/core/ecommerce_data.py backend/tests/test_ecommerce_data.py backend/examples/ecommerce/ecommerce_mysql.sql
git commit -m "feat: add dataset self-check and generate mysql import script"
```

---

## Task 6: schema_metadata.yaml（电商中文语义）

**Files:**
- Create: `backend/examples/ecommerce/schema_metadata.yaml`
- Test: `backend/tests/test_ecommerce_data.py`

**Step 1: Write the failing test**

```python
class TestSemantics(unittest.TestCase):
    def test_semantics_loads_key_enums(self):
        from text2sql.accuracy.schema_semantics import SchemaSemantics
        sem = SchemaSemantics.from_yaml("examples/ecommerce/schema_metadata.yaml")
        self.assertEqual(sem.table_alias("orders"), "订单")
        self.assertIn("paid", sem.enum_values("orders", "status"))
        self.assertIn("alipay", sem.enum_values("payments", "method"))
```

> 该测试要求安装 pyyaml；若最小环境无 pyyaml，`from_yaml` 返回空语义会使断言失败。执行时确保 `pip install pyyaml`（已在 pyproject 依赖），或用 `unittest.skipUnless` 包裹。

**Step 2: Run to verify it fails**

Expected: FAIL（文件不存在 → 空语义 → alias 为 None）。

**Step 3: Implement**

按 `examples/schema_metadata.yaml` 的结构，为 30 张表关键列写 `alias`/`description`/`enum_values`，覆盖 design §4 全部枚举。片段示例：

```yaml
tables:
  orders:
    alias: 订单
    description: 订单交易事实表，记录金额、状态、下单时间与收货地址快照
    columns:
      order_date: {alias: 下单日期, description: 用于趋势/环比/同比分析}
      status:
        alias: 订单状态
        enum_values: [created, paid, shipped, completed, cancelled, closed]
      pay_status:
        alias: 支付状态
        enum_values: [unpaid, paid, partial_refund, refunded]
      total_amount: {alias: 订单应付金额}
  payments:
    alias: 支付记录
    columns:
      method:
        alias: 支付方式
        enum_values: [alipay, wechat, credit_card, balance]
  # ...其余表...
```

**Step 4: Run to verify it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.test_ecommerce_data.TestSemantics -v`
Expected: PASS。

**Step 5: Commit**

```bash
git add backend/examples/ecommerce/schema_metadata.yaml backend/tests/test_ecommerce_data.py
git commit -m "feat: add ecommerce schema semantic metadata (aliases/enums)"
```

---

## Task 7: few_shot_seed.jsonl（电商示例库）

**Files:**
- Create: `backend/examples/ecommerce/few_shot_seed.jsonl`
- Test: `backend/tests/test_ecommerce_data.py`

**Step 1: Write the failing test**

```python
class TestFewShot(unittest.TestCase):
    def test_fewshot_loads_and_valid(self):
        from text2sql.accuracy.few_shot import InMemoryFewShotStore
        store = InMemoryFewShotStore.from_jsonl("examples/ecommerce/few_shot_seed.jsonl")
        got = store.search("按月统计销售额环比", top_k=3)
        self.assertTrue(len(got) >= 1)
```

**Step 2: Run to verify it fails**

Expected: FAIL（文件不存在 → 空库 → 结果为空）。

**Step 3: Implement**

写约 20 条 `{"question","sql","chart_type"}`，SQL 必须以 SELECT/WITH 开头、只引用真实表列，覆盖：月度销售趋势+环比(LAG)、同比(YoY)、品类销额占比(pie)、品牌 TopN(RANK)、支付方式分布、退货率、优惠券核销率、会员分层消费、加购转化漏斗、多级类目递归(WITH RECURSIVE)、员工层级、物流时效、评分分布、客单价、复购用户。示例：

```json
{"question": "按月份统计订单金额趋势并计算环比", "sql": "WITH m AS (SELECT substr(order_date,1,7) period, SUM(total_amount) v FROM orders GROUP BY period) SELECT period, v, LAG(v) OVER (ORDER BY period) prev, ROUND((v-LAG(v) OVER (ORDER BY period))*1.0/LAG(v) OVER (ORDER BY period),4) mom FROM m ORDER BY period", "chart_type": "line"}
{"question": "各商品品类销售额占比", "sql": "SELECT c.name dimension_value, SUM(oi.subtotal) metric_value FROM order_items oi JOIN skus s ON oi.sku_id=s.sku_id JOIN spus p ON s.spu_id=p.spu_id JOIN categories c ON p.category_id=c.category_id GROUP BY c.name ORDER BY metric_value DESC", "chart_type": "pie"}
```

**Step 4: Run to verify it passes**

Expected: PASS。

**Step 5: Commit**

```bash
git add backend/examples/ecommerce/few_shot_seed.jsonl backend/tests/test_ecommerce_data.py
git commit -m "feat: add ecommerce few-shot example seed"
```

---

## Task 8: eval_cases.jsonl（电商评测用例）

**Files:**
- Create: `backend/examples/ecommerce/eval_cases.jsonl`
- Test: 复用评测 CLI（`text2sql.eval`）做端到端验证

**Step 1: 写约 20 条用例**

字段沿用 `examples/eval_cases.jsonl`：`case_id`/`query`/`expected_tables`/`required_sql_keywords`/`allow_clarification`，可选 `expected_result`。覆盖 §11 能力清单。示例：

```json
{"case_id":"cat_sales_share","query":"各商品品类的销售额占比","expected_tables":["order_items","skus","spus","categories"],"required_sql_keywords":["join","sum","group by"],"allow_clarification":false}
{"case_id":"category_tree","query":"展示商品类目层级","expected_tables":["categories"],"required_sql_keywords":["with recursive"],"allow_clarification":false}
{"case_id":"ambiguous_ec","query":"看看情况","allow_clarification":true}
```

**Step 2: 生成库后跑评测**

Run:
```bash
cd backend
PYTHONPATH=src python3 -m text2sql.core.ecommerce_data --sqlite-out examples/ecommerce/ecommerce.db --mysql-out examples/ecommerce/ecommerce_mysql.sql
PYTHONPATH=src python3 -m text2sql.eval --db examples/ecommerce/ecommerce.db --cases examples/ecommerce/eval_cases.jsonl --report examples/ecommerce/eval_report.json
```
Expected: 生成 `eval_report.json`，表召回/关键词指标可读；带 `expected_result` 的用例精确匹配（数据确定性保证）。

**Step 3: 校准 expected_result**

对确定性聚合类用例，先运行一次拿到真实结果，回填 `expected_result`，再复跑确保 100% 匹配。

**Step 4: Commit**

```bash
git add backend/examples/ecommerce/eval_cases.jsonl
git commit -m "test: add ecommerce evaluation cases with deterministic expectations"
```

---

## Task 9: 文档与端到端收尾

**Files:**
- Modify: `README.md`（根）与/或 `backend/README.md`
- Test: 全量测试

**Step 1: README 增加电商数据集使用段落**

说明生成命令、环境变量切换、评测命令（复用 design §9 的 bash 片段）。

**Step 2: 全量回归**

Run:
```bash
cd backend
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
Expected: 全绿（现有测试不受影响 + 新增 `test_ecommerce_data` 通过）。

**Step 3: 双引擎一致性抽查**

若本地有 MySQL：`mysql < examples/ecommerce/ecommerce_mysql.sql` 后对比 `SELECT COUNT(*)` 与 SQLite 一致；无 MySQL 则跳过并在 README 注明验证方式。

**Step 4: Commit**

```bash
git add README.md backend/README.md
git commit -m "docs: document ecommerce test dataset usage and switching"
```

---

## 完成标准（Definition of Done）

- `PYTHONPATH=src python3 -m unittest discover -s tests` 全绿，现有 demo 相关测试无回归。
- `text2sql-ecommerce-db` 可一键生成 `ecommerce.db`（30 表）与 `ecommerce_mysql.sql`，`self_check` 通过。
- 切换环境变量后 `text2sql.eval` 能对电商用例出报告，`expected_result` 用例精确匹配。
- 文本资产（yaml/jsonl/sql）已提交，`ecommerce.db` 被 gitignore。
