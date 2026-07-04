from __future__ import annotations

"""电商场景 Text2SQL 测试数据集生成器。

设计目标（详见 docs/plans/2026-07-04-ecommerce-test-dataset-design.md）：
- 抽象类型声明一次 schema，渲染 SQLite / MySQL 两种方言 DDL，避免维护两套 DDL；
- 纯标准库确定性生成各表数据（固定随机种子），保证评测期望值可复现；
- write_sqlite 用标准库 sqlite3 建库（零第三方依赖）、write_mysql_script 导出 .sql；
- 生成时无需 MySQL 在线。

本模块的 SCHEMA 是数据生成、语义元数据、few-shot、评测用例四件套的共享契约：
每张表数据行的字段顺序必须与其在 SCHEMA 中的列顺序严格一致。
"""

from dataclasses import dataclass

# 全局随机种子：所有随机取值都必须来自 random.Random(SEED)，
# 禁止使用全局 random 或 datetime.now()，否则破坏可复现性。
SEED = 20240101


@dataclass(frozen=True)
class Column:
    """一列的抽象定义。

    type 取值：pk | int | bigint | money | varchar:N | text | date | datetime | bool
    fk：形如 "table.column"，仅作为关系元数据渲染进 DDL（供 schema 关系发现），
    不强制约束，避免插入顺序问题。
    """

    name: str
    type: str
    nullable: bool = True
    fk: str | None = None


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    unique: tuple[tuple[str, ...], ...] = ()


# 抽象类型 → 方言具体类型。varchar:N 需单独处理（见 _render_type）。
_TYPE_MAP = {
    "sqlite": {
        "pk": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "int": "INTEGER",
        "bigint": "INTEGER",
        "money": "REAL",
        "text": "TEXT",
        "date": "TEXT",
        "datetime": "TEXT",
        "bool": "INTEGER",
    },
    "mysql": {
        "pk": "INT AUTO_INCREMENT PRIMARY KEY",
        "int": "INT",
        "bigint": "BIGINT",
        "money": "DECIMAL(12,2)",
        "text": "TEXT",
        "date": "DATE",
        "datetime": "DATETIME",
        "bool": "TINYINT(1)",
    },
}


def _render_type(col_type: str, dialect: str) -> str:
    """把抽象类型渲染为目标方言的列类型。"""

    if col_type.startswith("varchar:"):
        length = col_type.split(":", 1)[1]
        # SQLite 无长度语义，统一 TEXT；MySQL 保留 VARCHAR(N) 以贴近生产。
        return "TEXT" if dialect == "sqlite" else f"VARCHAR({length})"
    return _TYPE_MAP[dialect][col_type]


def render_ddl(dialect: str) -> list[str]:
    """为每张表渲染 CREATE TABLE；SQLite 与 MySQL 共用同一 SCHEMA 定义。

    外键以 FOREIGN KEY 子句输出，用于 schema introspection 的关系发现；
    自引用（如 categories.parent_id）同样输出。
    """

    statements: list[str] = []
    for table in SCHEMA:
        lines: list[str] = []
        for col in table.columns:
            # pk 类型已隐含 PRIMARY KEY，不再追加 NOT NULL。
            not_null = "" if (col.nullable or col.type == "pk") else " NOT NULL"
            lines.append(f"  {col.name} {_render_type(col.type, dialect)}{not_null}")
        for unique_cols in table.unique:
            lines.append(f"  UNIQUE ({', '.join(unique_cols)})")
        for col in table.columns:
            if col.fk:
                ref_table, ref_col = col.fk.split(".")
                lines.append(
                    f"  FOREIGN KEY ({col.name}) REFERENCES {ref_table}({ref_col})"
                )
        body = ",\n".join(lines)
        suffix = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" if dialect == "mysql" else ""
        statements.append(f"CREATE TABLE {table.name} (\n{body}\n){suffix};")
    return statements


# ---------------------------------------------------------------------------
# SCHEMA：30 张表，按 12 个业务域组织（见 design.md §4）。
# 列顺序即数据行 tuple 的字段顺序，是四件套的共享契约，改动需同步生成逻辑。
# ---------------------------------------------------------------------------
SCHEMA: list[Table] = [
    # ① 用户与会员域
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
    Table("user_addresses", (
        Column("address_id", "pk"),
        Column("user_id", "int", nullable=False, fk="users.user_id"),
        Column("receiver_name", "varchar:50", nullable=False),
        Column("province", "varchar:20", nullable=False),
        Column("city", "varchar:20", nullable=False),
        Column("district", "varchar:20"),
        Column("detail", "varchar:200"),
        Column("is_default", "bool", nullable=False),
    )),
    # ② 商品目录域
    Table("categories", (
        Column("category_id", "pk"),
        Column("name", "varchar:50", nullable=False),
        Column("parent_id", "int", fk="categories.category_id"),
        Column("level", "int", nullable=False),
        Column("sort_order", "int"),
    )),
    Table("brands", (
        Column("brand_id", "pk"),
        Column("name", "varchar:50", nullable=False),
        Column("country", "varchar:30"),
        Column("created_at", "datetime"),
    )),
    Table("suppliers", (
        Column("supplier_id", "pk"),
        Column("name", "varchar:100", nullable=False),
        Column("contact", "varchar:50"),
        Column("province", "varchar:20"),
        Column("cooperation_since", "date"),
    )),
    Table("spus", (
        Column("spu_id", "pk"),
        Column("spu_name", "varchar:100", nullable=False),
        Column("category_id", "int", nullable=False, fk="categories.category_id"),
        Column("brand_id", "int", nullable=False, fk="brands.brand_id"),
        Column("supplier_id", "int", fk="suppliers.supplier_id"),
        Column("listing_date", "date", nullable=False),
        Column("status", "varchar:20", nullable=False),
    )),
    Table("skus", (
        Column("sku_id", "pk"),
        Column("spu_id", "int", nullable=False, fk="spus.spu_id"),
        Column("sku_name", "varchar:150", nullable=False),
        Column("price", "money", nullable=False),
        Column("cost", "money", nullable=False),
        Column("barcode", "varchar:50"),
        Column("weight_kg", "money"),
        Column("status", "varchar:20", nullable=False),
    )),
    Table("sku_attributes", (
        Column("attr_id", "pk"),
        Column("sku_id", "int", nullable=False, fk="skus.sku_id"),
        Column("attr_name", "varchar:30", nullable=False),
        Column("attr_value", "varchar:50", nullable=False),
    )),
    # ③ 库存与仓储域
    Table("warehouses", (
        Column("warehouse_id", "pk"),
        Column("name", "varchar:50", nullable=False),
        Column("province", "varchar:20"),
        Column("city", "varchar:20"),
        Column("capacity", "int"),
    )),
    Table("inventory", (
        Column("inventory_id", "pk"),
        Column("sku_id", "int", nullable=False, fk="skus.sku_id"),
        Column("warehouse_id", "int", nullable=False, fk="warehouses.warehouse_id"),
        Column("quantity", "int", nullable=False),
        Column("safety_stock", "int", nullable=False),
        Column("updated_at", "datetime"),
    ), unique=(("sku_id", "warehouse_id"),)),
    Table("inventory_movements", (
        Column("movement_id", "pk"),
        Column("sku_id", "int", nullable=False, fk="skus.sku_id"),
        Column("warehouse_id", "int", nullable=False, fk="warehouses.warehouse_id"),
        Column("movement_type", "varchar:20", nullable=False),
        Column("quantity", "int", nullable=False),
        Column("ref_order_id", "int", fk="orders.order_id"),
        Column("created_at", "datetime", nullable=False),
    )),
    # ④ 购物车域
    Table("carts", (
        Column("cart_id", "pk"),
        Column("user_id", "int", nullable=False, fk="users.user_id"),
        Column("created_at", "datetime", nullable=False),
        Column("updated_at", "datetime"),
    )),
    Table("cart_items", (
        Column("cart_item_id", "pk"),
        Column("cart_id", "int", nullable=False, fk="carts.cart_id"),
        Column("sku_id", "int", nullable=False, fk="skus.sku_id"),
        Column("quantity", "int", nullable=False),
        Column("added_at", "datetime", nullable=False),
    )),
    # ⑤ 订单交易域
    Table("orders", (
        Column("order_id", "pk"),
        Column("order_no", "varchar:32", nullable=False),
        Column("user_id", "int", nullable=False, fk="users.user_id"),
        Column("order_date", "date", nullable=False),
        Column("status", "varchar:20", nullable=False),
        Column("pay_status", "varchar:20", nullable=False),
        Column("total_amount", "money", nullable=False),
        Column("product_amount", "money", nullable=False),
        Column("discount_amount", "money", nullable=False),
        Column("shipping_fee", "money", nullable=False),
        Column("receiver_province", "varchar:20"),
        Column("receiver_city", "varchar:20"),
        Column("coupon_id", "int", fk="coupons.coupon_id"),
        Column("promotion_id", "int", fk="promotions.promotion_id"),
    )),
    Table("order_items", (
        Column("order_item_id", "pk"),
        Column("order_id", "int", nullable=False, fk="orders.order_id"),
        Column("sku_id", "int", nullable=False, fk="skus.sku_id"),
        Column("quantity", "int", nullable=False),
        Column("unit_price", "money", nullable=False),
        Column("subtotal", "money", nullable=False),
    )),
    Table("order_status_history", (
        Column("history_id", "pk"),
        Column("order_id", "int", nullable=False, fk="orders.order_id"),
        Column("from_status", "varchar:20"),
        Column("to_status", "varchar:20", nullable=False),
        Column("changed_at", "datetime", nullable=False),
        Column("operator", "varchar:50"),
    )),
    # ⑥ 支付域
    Table("payments", (
        Column("payment_id", "pk"),
        Column("order_id", "int", nullable=False, fk="orders.order_id"),
        Column("method", "varchar:20", nullable=False),
        Column("amount", "money", nullable=False),
        Column("status", "varchar:20", nullable=False),
        Column("paid_at", "datetime"),
        Column("transaction_no", "varchar:64"),
    )),
    Table("refunds", (
        Column("refund_id", "pk"),
        Column("payment_id", "int", nullable=False, fk="payments.payment_id"),
        Column("order_id", "int", nullable=False, fk="orders.order_id"),
        Column("amount", "money", nullable=False),
        Column("reason", "varchar:50"),
        Column("status", "varchar:20", nullable=False),
        Column("refunded_at", "datetime"),
    )),
    # ⑦ 物流域
    Table("logistics_companies", (
        Column("company_id", "pk"),
        Column("name", "varchar:50", nullable=False),
        Column("code", "varchar:20", nullable=False),
    )),
    Table("shipments", (
        Column("shipment_id", "pk"),
        Column("order_id", "int", nullable=False, fk="orders.order_id"),
        Column("company_id", "int", nullable=False, fk="logistics_companies.company_id"),
        Column("tracking_no", "varchar:64"),
        Column("status", "varchar:20", nullable=False),
        Column("shipped_at", "datetime"),
        Column("delivered_at", "datetime"),
        Column("receiver_province", "varchar:20"),
        Column("receiver_city", "varchar:20"),
    )),
    # ⑧ 售后域
    Table("return_orders", (
        Column("return_id", "pk"),
        Column("order_id", "int", nullable=False, fk="orders.order_id"),
        Column("user_id", "int", nullable=False, fk="users.user_id"),
        Column("reason", "varchar:30", nullable=False),
        Column("status", "varchar:20", nullable=False),
        Column("apply_date", "date", nullable=False),
        Column("refund_amount", "money"),
        Column("complete_date", "date"),
    )),
    Table("return_items", (
        Column("return_item_id", "pk"),
        Column("return_id", "int", nullable=False, fk="return_orders.return_id"),
        Column("order_item_id", "int", nullable=False, fk="order_items.order_item_id"),
        Column("sku_id", "int", nullable=False, fk="skus.sku_id"),
        Column("quantity", "int", nullable=False),
    )),
    # ⑨ 营销促销域
    Table("coupons", (
        Column("coupon_id", "pk"),
        Column("name", "varchar:50", nullable=False),
        Column("type", "varchar:20", nullable=False),
        Column("threshold", "money"),
        Column("value", "money", nullable=False),
        Column("valid_from", "date"),
        Column("valid_to", "date"),
        Column("total_issued", "int"),
        Column("status", "varchar:20", nullable=False),
    )),
    Table("user_coupons", (
        Column("user_coupon_id", "pk"),
        Column("coupon_id", "int", nullable=False, fk="coupons.coupon_id"),
        Column("user_id", "int", nullable=False, fk="users.user_id"),
        Column("status", "varchar:20", nullable=False),
        Column("received_at", "datetime", nullable=False),
        Column("used_at", "datetime"),
        Column("order_id", "int", fk="orders.order_id"),
    )),
    Table("promotions", (
        Column("promotion_id", "pk"),
        Column("name", "varchar:50", nullable=False),
        Column("type", "varchar:20", nullable=False),
        Column("start_date", "date", nullable=False),
        Column("end_date", "date", nullable=False),
        Column("discount_rate", "money"),
    )),
    Table("promotion_products", (
        Column("id", "pk"),
        Column("promotion_id", "int", nullable=False, fk="promotions.promotion_id"),
        Column("sku_id", "int", nullable=False, fk="skus.sku_id"),
        Column("promo_price", "money", nullable=False),
    )),
    # ⑩ 评价内容域
    Table("product_reviews", (
        Column("review_id", "pk"),
        Column("order_item_id", "int", nullable=False, fk="order_items.order_item_id"),
        Column("user_id", "int", nullable=False, fk="users.user_id"),
        Column("sku_id", "int", nullable=False, fk="skus.sku_id"),
        Column("rating", "int", nullable=False),
        Column("content", "varchar:500"),
        Column("has_image", "bool", nullable=False),
        Column("created_at", "datetime", nullable=False),
        Column("reply_content", "varchar:500"),
    )),
    # ⑪ 行为流量域
    Table("user_events", (
        Column("event_id", "pk"),
        Column("user_id", "int", nullable=False, fk="users.user_id"),
        Column("sku_id", "int", fk="skus.sku_id"),
        Column("event_type", "varchar:20", nullable=False),
        Column("event_time", "datetime", nullable=False),
        Column("session_id", "varchar:64"),
        Column("source", "varchar:20"),
    )),
    # ⑫ 组织运营域
    Table("employees", (
        Column("employee_id", "pk"),
        Column("employee_name", "varchar:50", nullable=False),
        Column("manager_id", "int", fk="employees.employee_id"),
        Column("department", "varchar:30", nullable=False),
        Column("role", "varchar:30"),
        Column("hire_date", "date"),
        Column("region", "varchar:20"),
    )),
]


# ---------------------------------------------------------------------------
# 数据生成 / 写库 / 自检 / CLI：由后续任务（Task 2-5）在此追加。
#   generate(seed) -> Dataset
#   write_sqlite(ds, path) / write_mysql_script(ds, path)
#   self_check(ds)
#   main()
# ---------------------------------------------------------------------------
