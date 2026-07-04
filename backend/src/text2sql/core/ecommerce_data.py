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

import datetime as _dt
import random
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

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
# 数据生成：纯标准库确定性生成。所有随机取值来自 random.Random(seed)，
# 每张表数据行 tuple 的字段顺序严格等于其在 SCHEMA 中的列顺序（写库契约）。
# ---------------------------------------------------------------------------

# 时间窗口：订单事实数据落在 2023-01-01 ~ 2024-12-31（24 个月）。
_START_DATE = _dt.date(2023, 1, 1)
_END_DATE = _dt.date(2024, 12, 31)
_START_DT = _dt.datetime(2023, 1, 1, 0, 0, 0)
_END_DT = _dt.datetime(2024, 12, 31, 23, 59, 59)
# 大促月（6·18 / 双 11 / 双 12）：订单量与客单价上浮，制造 MoM/YoY 信号。
_PROMO_MONTHS = {6, 11, 12}

# 真实感常量池：真实省市、真实中文类目、拟真品牌名（虚构规避商标）。
_PROVINCE_CITIES = [
    ("广东", "广州"), ("广东", "深圳"), ("广东", "东莞"), ("浙江", "杭州"),
    ("浙江", "宁波"), ("江苏", "南京"), ("江苏", "苏州"), ("北京", "北京"),
    ("上海", "上海"), ("四川", "成都"), ("山东", "青岛"), ("山东", "济南"),
    ("湖北", "武汉"), ("福建", "厦门"), ("福建", "福州"), ("陕西", "西安"),
    ("湖南", "长沙"), ("河南", "郑州"), ("辽宁", "沈阳"), ("辽宁", "大连"),
    ("重庆", "重庆"), ("天津", "天津"),
]
_DISTRICTS = ["高新区", "朝阳区", "西湖区", "天河区", "越秀区", "浦东新区",
              "锦江区", "历下区", "武侯区", "南山区", "海淀区", "江汉区"]
_STREETS = ["中山路", "人民路", "解放大道", "科技园路", "长江路", "和平大街",
            "建设路", "文化路", "滨江大道", "创新大道"]

# 多级类目树：level1(根) → level2 → level3(叶)，供递归 CTE 查询验证。
_CATEGORY_TREE = {
    "手机数码": {
        "手机通讯": ["智能手机", "老年机", "对讲机"],
        "电脑办公": ["笔记本电脑", "平板电脑", "台式机"],
        "影音智能": ["蓝牙耳机", "智能手表"],
    },
    "服装鞋包": {
        "女装": ["连衣裙", "羽绒服", "针织衫"],
        "男装": ["夹克", "衬衫"],
        "鞋靴箱包": ["运动鞋", "双肩包"],
    },
    "美妆个护": {
        "护肤": ["面霜", "面膜", "精华"],
        "彩妆": ["口红", "粉底液"],
        "个人护理": ["洗发水", "牙膏"],
    },
    "家用电器": {
        "大家电": ["冰箱", "洗衣机", "空调"],
        "厨房电器": ["电饭煲", "微波炉"],
        "生活电器": ["扫地机器人", "吹风机"],
    },
    "食品生鲜": {
        "休闲零食": ["坚果", "膨化食品"],
        "饮料冲调": ["咖啡", "茶叶"],
        "生鲜果蔬": ["时令水果", "海鲜水产"],
    },
    "母婴玩具": {
        "奶粉辅食": ["婴儿奶粉", "宝宝米粉"],
        "尿裤湿巾": ["纸尿裤"],
        "益智玩具": ["积木", "遥控车"],
    },
}

# 拟真品牌名：虚构但读感真实，规避真实商标。
_BRAND_NAMES = [
    "云度", "星野", "拾光", "沐光", "简岚", "格物", "初森", "朗诚", "悦品", "菲朵",
    "博宇", "锐界", "嘉沐", "优漾", "雅川", "恒歌", "佳邦", "睿联", "飞扬", "盛世",
    "光年", "栖町", "知潮", "牧云", "溪墨", "青柚", "南喃", "遇见", "拓野", "唐颂",
    "米印", "禾木", "锦上", "澜栖", "白鹿", "山岚", "海鸣", "橙月", "麦田", "森屿",
    "启点", "凡星", "羽然", "岚图", "沐野", "知渔", "壹见", "半亩", "喜番", "拙朴",
]
_SUPPLIER_WORDS = ["华", "宏", "盛", "泰", "鑫", "恒", "隆", "源", "瑞", "捷"]
_SUPPLIER_BIZ = ["供应链", "贸易", "实业", "科技", "电子商务", "仓储物流"]

_SURNAMES = list("王李张刘陈杨黄赵周吴徐孙马朱胡林郭何高罗郑梁谢宋唐许韩冯邓曹彭曾肖田")
_GIVEN_CHARS = list("伟芳娜秀英敏静丽强磊军洋勇艳杰娟涛明超霞平刚桂华建国志文博宇轩涵怡然梓萱诺乐")

_COLORS = ["黑色", "白色", "银色", "藏青", "米色", "深灰", "香槟金", "樱花粉", "天蓝", "墨绿"]
_SIZES = ["S", "M", "L", "XL", "XXL", "均码", "标准版", "旗舰版"]
_CAPACITIES = ["64GB", "128GB", "256GB", "512GB", "1TB"]
_MATERIALS = ["纯棉", "真皮", "合金", "ABS", "不锈钢", "玻璃"]
_MODEL_TOKENS = ["Pro", "Air", "Max", "Plus", "Lite", "X", "S", "GT", "Neo", "SE"]

_EMAIL_DOMAINS = ["qq.com", "163.com", "126.com", "gmail.com", "outlook.com"]
_PAY_METHODS = ["alipay", "wechat", "credit_card", "balance"]
_EVENT_SOURCES = ["app", "web", "mini_program"]
_RETURN_REASONS = ["quality", "wrong_item", "not_as_described", "no_reason", "damaged"]
_REVIEW_TEXTS = [
    "质量很好，物流也快，非常满意！", "包装完整，性价比高，会回购。", "和描述一致，用着不错。",
    "发货速度快，客服态度好。", "东西还可以，价格实惠。", "整体满意，推荐购买。",
    "做工精细，细节到位。", "一般般，符合预期。", "比想象中好，五星好评。",
    "已经是第二次购买了，值得信赖。",
]


def _person_name(rng: random.Random) -> str:
    """生成拟真中文姓名（姓 + 1~2 个名字用字）。"""

    surname = rng.choice(_SURNAMES)
    given = "".join(rng.choice(_GIVEN_CHARS) for _ in range(rng.randint(1, 2)))
    return surname + given


def _rand_date(rng: random.Random, start: _dt.date, end: _dt.date) -> _dt.date:
    return start + _dt.timedelta(days=rng.randint(0, (end - start).days))


def _rand_datetime(
    rng: random.Random, start: _dt.datetime, end: _dt.datetime
) -> _dt.datetime:
    span = int((end - start).total_seconds())
    return start + _dt.timedelta(seconds=rng.randint(0, max(span, 0)))


def _fmt_date(d: _dt.date) -> str:
    return d.isoformat()


def _fmt_dt(d: _dt.datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Dataset:
    """全部表数据的容器：表名 → 数据行列表（每行 tuple 按 SCHEMA 列顺序）。"""

    tables: dict[str, list[tuple]] = field(default_factory=dict)


# --- 维度表生成（Task 2）---------------------------------------------------

def _gen_membership_levels() -> list[tuple]:
    """5 档会员等级，min_amount 随等级递增，供 users 按累计消费定级。"""

    # (name, min_amount 累计消费门槛, discount_rate 会员折扣)
    data = [
        ("普通", 0.0, 1.0), ("银卡", 1000.0, 0.98), ("金卡", 5000.0, 0.95),
        ("铂金", 20000.0, 0.92), ("钻石", 50000.0, 0.88),
    ]
    return [(i, name, amt, rate) for i, (name, amt, rate) in enumerate(data, 1)]


def _gen_categories() -> list[tuple]:
    """由 _CATEGORY_TREE 展开三级类目，parent_id 自关联（根为 None）。"""

    rows: list[tuple] = []
    cid = 0
    for sort1, (lvl1, subs) in enumerate(_CATEGORY_TREE.items(), 1):
        cid += 1
        root_id = cid
        rows.append((root_id, lvl1, None, 1, sort1))
        for sort2, (lvl2, leaves) in enumerate(subs.items(), 1):
            cid += 1
            mid_id = cid
            rows.append((mid_id, lvl2, root_id, 2, sort2))
            for sort3, leaf in enumerate(leaves, 1):
                cid += 1
                rows.append((cid, leaf, mid_id, 3, sort3))
    return rows


def _gen_brands(rng: random.Random) -> list[tuple]:
    countries = ["中国", "中国", "中国", "中国", "中国", "美国", "日本", "韩国", "德国", "法国"]
    rows: list[tuple] = []
    for i, name in enumerate(_BRAND_NAMES, 1):
        created = _rand_datetime(
            rng, _dt.datetime(2015, 1, 1), _dt.datetime(2022, 12, 31)
        )
        rows.append((i, name, rng.choice(countries), _fmt_dt(created)))
    return rows


def _gen_suppliers(rng: random.Random, n: int = 30) -> list[tuple]:
    rows: list[tuple] = []
    for i in range(1, n + 1):
        prov, city = rng.choice(_PROVINCE_CITIES)
        name = f"{city}{rng.choice(_SUPPLIER_WORDS)}{rng.choice(_SUPPLIER_BIZ)}有限公司"
        since = _rand_date(rng, _dt.date(2018, 1, 1), _dt.date(2023, 12, 31))
        rows.append((i, name, _person_name(rng), prov, _fmt_date(since)))
    return rows


def _gen_spus(
    rng: random.Random,
    leaf_cat_ids: list[int],
    brand_names: dict[int, str],
    leaf_names: dict[int, str],
    supplier_ids: list[int],
    n: int = 300,
) -> list[tuple]:
    """商品 SPU：挂在叶子类目下，命名 = 品牌 + 叶类目 + 型号。"""

    status_pool = ["on_sale"] * 8 + ["off_sale", "pending"]
    brand_ids = list(brand_names.keys())
    rows: list[tuple] = []
    for i in range(1, n + 1):
        cat_id = rng.choice(leaf_cat_ids)
        brand_id = rng.choice(brand_ids)
        supplier_id = rng.choice(supplier_ids) if rng.random() < 0.95 else None
        model = f"{rng.choice(_MODEL_TOKENS)}{rng.randint(1, 99)}"
        spu_name = f"{brand_names[brand_id]}{leaf_names[cat_id]}{model}"
        listing = _rand_date(rng, _dt.date(2022, 1, 1), _dt.date(2024, 11, 30))
        rows.append(
            (i, spu_name, cat_id, brand_id, supplier_id, _fmt_date(listing),
             rng.choice(status_pool))
        )
    return rows


def _gen_skus(rng: random.Random, spus: list[tuple]) -> list[tuple]:
    """每个 SPU 派生 1~4 个 SKU；price = cost × markup，markup≥1.3 保证售价>成本。"""

    status_pool = ["on_sale"] * 8 + ["off_sale", "pending"]
    rows: list[tuple] = []
    sku_id = 0
    for spu in spus:
        spu_id, spu_name = spu[0], spu[1]
        for _ in range(rng.randint(1, 4)):
            sku_id += 1
            variant = f"{rng.choice(_COLORS)} {rng.choice(_SIZES)}"
            cost = round(rng.uniform(10, 2000), 2)
            price = round(cost * rng.uniform(1.3, 2.6), 2)
            barcode = "69" + "".join(str(rng.randint(0, 9)) for _ in range(11))
            weight = round(rng.uniform(0.1, 20), 2)
            rows.append(
                (sku_id, spu_id, f"{spu_name} {variant}", price, cost, barcode,
                 weight, rng.choice(status_pool))
            )
    return rows


def _gen_sku_attributes(rng: random.Random, sku_ids: list[int]) -> list[tuple]:
    """每个 SKU 造 2 个属性：颜色 + 尺寸/容量/材质 之一。"""

    rows: list[tuple] = []
    attr_id = 0
    for sku_id in sku_ids:
        pairs = [("颜色", rng.choice(_COLORS))]
        pairs.append(rng.choice([
            ("尺寸", rng.choice(_SIZES)),
            ("容量", rng.choice(_CAPACITIES)),
            ("材质", rng.choice(_MATERIALS)),
        ]))
        for attr_name, attr_value in pairs:
            attr_id += 1
            rows.append((attr_id, sku_id, attr_name, attr_value))
    return rows


def _gen_warehouses(rng: random.Random) -> list[tuple]:
    data = [
        ("华北仓", "北京", "北京"), ("华东仓", "上海", "上海"), ("华南仓", "广东", "广州"),
        ("华中仓", "湖北", "武汉"), ("西南仓", "四川", "成都"), ("东北仓", "辽宁", "沈阳"),
        ("西北仓", "陕西", "西安"), ("华东二仓", "浙江", "杭州"),
    ]
    return [
        (i, name, prov, city, rng.randint(50000, 200000))
        for i, (name, prov, city) in enumerate(data, 1)
    ]


def _gen_logistics_companies() -> list[tuple]:
    data = [
        ("顺丰速运", "SF"), ("圆通速递", "YTO"), ("中通快递", "ZTO"), ("申通快递", "STO"),
        ("韵达快递", "YD"), ("京东物流", "JD"), ("邮政EMS", "EMS"), ("德邦快递", "DBL"),
    ]
    return [(i, name, code) for i, (name, code) in enumerate(data, 1)]


def _gen_employees(rng: random.Random) -> list[tuple]:
    """组织层级：1 CEO(manager_id=None) → 5 VP → 10 经理 → 若干专员，共 30 人。"""

    regions = ["华北", "华东", "华南", "华中", "西南", "东北", "西北"]
    rows: list[tuple] = []
    dept_of: dict[int, str] = {}
    eid = 1
    ceo_hire = _rand_date(rng, _dt.date(2016, 1, 1), _dt.date(2018, 12, 31))
    rows.append((eid, _person_name(rng), None, "总裁办", "CEO", _fmt_date(ceo_hire), "总部"))

    vp_ids: list[int] = []
    for dept, role in [("销售部", "销售VP"), ("市场部", "市场VP"), ("技术部", "技术VP"),
                       ("运营部", "运营VP"), ("供应链部", "供应链VP")]:
        eid += 1
        vp_ids.append(eid)
        dept_of[eid] = dept
        hire = _rand_date(rng, _dt.date(2017, 1, 1), _dt.date(2020, 12, 31))
        rows.append((eid, _person_name(rng), 1, dept, role, _fmt_date(hire), "总部"))

    mgr_ids: list[int] = []
    for _ in range(10):
        eid += 1
        vp = rng.choice(vp_ids)
        mgr_ids.append(eid)
        dept_of[eid] = dept_of[vp]
        hire = _rand_date(rng, _dt.date(2019, 1, 1), _dt.date(2022, 12, 31))
        rows.append((eid, _person_name(rng), vp, dept_of[vp], "经理", _fmt_date(hire),
                     rng.choice(regions)))

    while eid < 30:
        eid += 1
        mgr = rng.choice(mgr_ids)
        hire = _rand_date(rng, _dt.date(2020, 1, 1), _dt.date(2024, 6, 30))
        rows.append((eid, _person_name(rng), mgr, dept_of[mgr], "专员", _fmt_date(hire),
                     rng.choice(regions)))
    return rows


def _gen_coupons(rng: random.Random, n: int = 50) -> list[tuple]:
    """优惠券：满减/折扣/现金三类，valid_to < 数据集末即视为 expired。"""

    rows: list[tuple] = []
    for i in range(1, n + 1):
        ctype = rng.choice(["full_reduction", "full_reduction", "discount", "cash"])
        if ctype == "full_reduction":
            threshold = float(rng.choice([100, 200, 300, 500]))
            value = float(rng.choice([10, 20, 30, 50, 80]))
            name = f"满{int(threshold)}减{int(value)}券"
        elif ctype == "discount":
            threshold = None
            value = rng.choice([0.95, 0.9, 0.85, 0.8])
            name = f"{int(round(value * 10))}折券"
        else:
            threshold = None
            value = float(rng.choice([5, 10, 15, 20]))
            name = f"{int(value)}元现金券"
        valid_from = _rand_date(rng, _START_DATE, _dt.date(2024, 6, 1))
        valid_to = valid_from + _dt.timedelta(days=rng.choice([30, 60, 90]))
        status = "active" if valid_to >= _END_DATE else "expired"
        rows.append((i, name, ctype, threshold, value, _fmt_date(valid_from),
                     _fmt_date(valid_to), rng.randint(500, 20000), status))
    return rows


def _gen_promotions(rng: random.Random, n: int = 30) -> list[tuple]:
    """促销活动：部分对齐大促月，折扣类带 discount_rate。"""

    rows: list[tuple] = []
    for i in range(1, n + 1):
        ptype = rng.choice(["full_reduction", "discount", "flash_sale"])
        start = _rand_date(rng, _START_DATE, _dt.date(2024, 12, 1))
        end = start + _dt.timedelta(days=rng.choice([3, 7, 15]))
        rate = None if ptype == "full_reduction" else round(rng.uniform(0.5, 0.95), 2)
        if start.month == 6:
            name = f"{start.year}年618大促"
        elif start.month == 11:
            name = f"{start.year}年双11狂欢"
        elif start.month == 12:
            name = f"{start.year}年双12年终盛典"
        else:
            name = f"{start.year}年{start.month}月限时活动"
        rows.append((i, name, ptype, _fmt_date(start), _fmt_date(end), rate))
    return rows


def _gen_user_profiles(rng: random.Random, n: int = 2000) -> list[dict]:
    """生成用户档案（不含 total_spent/level_id，二者在订单生成后回填）。

    register_date 按 2022/2023/2024 递减权重分布，保证订单窗口早期已有可下单用户。
    """

    genders = ["男", "男", "女", "女", "未知"]
    status_pool = ["active"] * 8 + ["inactive", "banned"]
    profiles: list[dict] = []
    for uid in range(1, n + 1):
        r = rng.random()
        if r < 0.40:
            reg = _rand_date(rng, _dt.date(2022, 1, 1), _dt.date(2022, 12, 31))
        elif r < 0.75:
            reg = _rand_date(rng, _dt.date(2023, 1, 1), _dt.date(2023, 12, 31))
        else:
            reg = _rand_date(rng, _dt.date(2024, 1, 1), _dt.date(2024, 12, 31))
        birth = _rand_date(rng, _dt.date(1970, 1, 1), _dt.date(2005, 12, 31))
        phone = "1" + rng.choice("3456789") + "".join(
            str(rng.randint(0, 9)) for _ in range(9)
        )
        last_login = None
        if rng.random() < 0.9:
            login_dt = _rand_datetime(
                rng, _dt.datetime.combine(reg, _dt.time(8, 0)), _END_DT
            )
            last_login = _fmt_dt(login_dt)
        profiles.append({
            "user_id": uid,
            "username": _person_name(rng),
            "gender": rng.choice(genders),
            "birth_date": _fmt_date(birth),
            "phone": phone,
            "email": f"user{uid}@{rng.choice(_EMAIL_DOMAINS)}",
            "reg_date": reg,
            "register_date": _fmt_date(reg),
            "status": rng.choice(status_pool),
            "last_login_at": last_login,
        })
    return profiles


def _gen_user_addresses(
    rng: random.Random, profiles: list[dict]
) -> tuple[list[tuple], dict[int, list[dict]]]:
    """每个用户 1~3 个收货地址，第一个为默认；返回行 + 用户→地址快照映射。"""

    rows: list[tuple] = []
    addr_by_user: dict[int, list[dict]] = {}
    addr_id = 0
    for p in profiles:
        uid = p["user_id"]
        count = rng.choices([1, 2, 3], weights=[40, 40, 20])[0]
        user_addrs: list[dict] = []
        for j in range(count):
            addr_id += 1
            prov, city = rng.choice(_PROVINCE_CITIES)
            detail = (f"{rng.choice(_STREETS)}{rng.randint(1, 200)}号"
                      f"{rng.randint(1, 30)}栋{rng.randint(101, 2508)}室")
            receiver = p["username"] if rng.random() < 0.7 else _person_name(rng)
            rows.append((addr_id, uid, receiver, prov, city,
                         rng.choice(_DISTRICTS), detail, 1 if j == 0 else 0))
            user_addrs.append({"province": prov, "city": city})
        addr_by_user[uid] = user_addrs
    return rows, addr_by_user


def _finalize_users(
    profiles: list[dict], spent_by_user: dict[int, float], levels: list[tuple]
) -> list[tuple]:
    """回填 total_spent（已完成订单金额之和）并据累计消费门槛定级。"""

    # levels 行：(level_id, name, min_amount, discount_rate)，按门槛升序取最高满足档
    thresholds = sorted((row[2], row[0]) for row in levels)
    rows: list[tuple] = []
    for p in profiles:
        spent = round(spent_by_user.get(p["user_id"], 0.0), 2)
        level_id = 1
        for min_amount, lid in thresholds:
            if spent >= min_amount:
                level_id = lid
        rows.append((
            p["user_id"], p["username"], p["gender"], p["birth_date"], p["phone"],
            p["email"], level_id, p["register_date"], spent, p["status"],
            p["last_login_at"],
        ))
    return rows


# --- 事实表生成（Task 3）---------------------------------------------------

# 订单状态 → 状态流转链（order_status_history 逐条落库）。
_STATUS_STEPS = {
    "created": ("created",),
    "paid": ("created", "paid"),
    "shipped": ("created", "paid", "shipped"),
    "completed": ("created", "paid", "shipped", "completed"),
    "cancelled": ("created", "cancelled"),
    "closed": ("created", "closed"),
}
_STATUS_OPERATOR = {
    "created": "system", "paid": "system", "shipped": "仓库",
    "completed": "system", "cancelled": "客服", "closed": "system",
}


def _clamp_dt(d: _dt.datetime) -> _dt.datetime:
    """把时间戳收敛到数据集末尾，避免事件落在窗口之外。"""

    return d if d <= _END_DT else _END_DT


def _txn_no(rng: random.Random) -> str:
    return "TXN" + "".join(str(rng.randint(0, 9)) for _ in range(16))


def _tracking_no(rng: random.Random) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(12))


def _session_id(rng: random.Random) -> str:
    return f"sess-{rng.randint(100000, 999999)}"


def _choose_status(rng: random.Random, order_day: _dt.date) -> str:
    """按距数据集末尾的天数决定订单状态：足够久的订单才可能进入 completed。

    这样保证 completed 订单的发货/签收时间仍落在 2024-12-31 之内。
    """

    if (_END_DATE - order_day).days >= 20:
        return rng.choices(
            ["completed", "cancelled", "closed", "shipped", "paid"],
            weights=[68, 16, 6, 6, 4],
        )[0]
    # 临近末尾的订单还在履约途中，不产生 completed
    return rng.choices(
        ["created", "paid", "shipped", "cancelled"], weights=[32, 30, 26, 12]
    )[0]


def _month_bounds(year: int, month: int) -> tuple[_dt.date, _dt.date]:
    start = _dt.date(year, month, 1)
    if month == 12:
        end = _dt.date(year, 12, 31)
    else:
        end = _dt.date(year, month + 1, 1) - _dt.timedelta(days=1)
    return start, end


def _order_count_for_month(rng: random.Random, year: int, month: int) -> int:
    """月订单量：2024 同比上浮、大促月(6/11/12)放大，制造 MoM/YoY 信号。"""

    base = 270
    year_factor = 1.0 if year == 2023 else 1.15
    promo_factor = 1.8 if month in _PROMO_MONTHS else 1.0
    return int(base * year_factor * promo_factor) + rng.randint(-15, 15)


def _build_order_items(
    rng: random.Random, sku_ids: list[int], sku_price: dict[int, float],
    start_item_id: int, is_promo: bool,
) -> tuple[list[tuple], float, int]:
    """构造一笔订单的明细：返回 (明细四元组列表, 商品总额, 最新 order_item_id)。

    明细四元组为 (order_item_id, sku_id, quantity, unit_price, subtotal)，
    subtotal = unit_price × quantity，商品总额 = Σ subtotal（金额自洽的源头）。
    """

    n_items = min(rng.randint(1, 4) + (1 if is_promo else 0), len(sku_ids))
    product_amount = 0.0
    items: list[tuple] = []
    item_id = start_item_id
    for sku in rng.sample(sku_ids, n_items):
        qty = rng.randint(1, 5) + (rng.randint(0, 2) if is_promo else 0)
        base_price = sku_price[sku]
        # 20% 概率成交价低于标价（促销成交），其余按标价；无论如何 subtotal 自洽
        unit_price = (
            round(base_price * rng.uniform(0.8, 0.98), 2)
            if rng.random() < 0.2 else base_price
        )
        subtotal = round(unit_price * qty, 2)
        product_amount += subtotal
        item_id += 1
        items.append((item_id, sku, qty, unit_price, subtotal))
    return items, round(product_amount, 2), item_id


def _apply_discount(
    rng: random.Random, product_amount: float, coupons: list[tuple],
    promotions: list[tuple],
) -> tuple[float, int | None, int | None]:
    """按概率使用优惠券或参加活动，返回 (discount, coupon_id, promotion_id)。

    discount 上限为 product_amount×0.95，保证 total_amount 恒为正。
    """

    coupon_id: int | None = None
    promotion_id: int | None = None
    discount = 0.0
    roll = rng.random()
    if roll < 0.30:
        coupon = rng.choice(coupons)
        # coupons 列: (coupon_id, name, type, threshold, value, ...)
        ctype, threshold, value = coupon[2], coupon[3], coupon[4]
        if ctype == "full_reduction":
            if threshold is not None and product_amount >= threshold:
                coupon_id, discount = coupon[0], value
        elif ctype == "discount":
            coupon_id, discount = coupon[0], round(product_amount * (1 - value), 2)
        else:  # cash
            coupon_id, discount = coupon[0], value
    elif roll < 0.45:
        promo = rng.choice(promotions)
        # promotions 列: (promotion_id, name, type, start, end, discount_rate)
        if promo[5] is not None:
            promotion_id = promo[0]
            discount = round(product_amount * (1 - promo[5]), 2)
    discount = round(min(max(discount, 0.0), round(product_amount * 0.95, 2)), 2)
    return discount, coupon_id, promotion_id


def _gen_orders_chain(
    rng: random.Random, profiles: list[dict], addr_by_user: dict[int, list[dict]],
    sku_price: dict[int, float], sku_ids: list[int], coupons: list[tuple],
    promotions: list[tuple], company_ids: list[int],
):
    """按月生成订单及其完整业务链（明细/状态流转/支付/退款/物流/售后）。

    返回 (八张链路表 dict, spent_by_user, user_paid_orders, review_pool)。
    """

    orders: list[tuple] = []
    order_items: list[tuple] = []
    history: list[tuple] = []
    payments: list[tuple] = []
    refunds: list[tuple] = []
    shipments: list[tuple] = []
    return_orders: list[tuple] = []
    return_items: list[tuple] = []
    spent_by_user: dict[int, float] = {}
    user_paid_orders: dict[int, list[int]] = {}
    review_pool: list[dict] = []

    counters = {"oid": 0, "iid": 0, "hid": 0, "pid": 0,
                "rfid": 0, "shid": 0, "rid": 0, "ritid": 0}

    # 用户按注册日期排序，逐月扩充「可下单用户池」，保证 order_date ≥ register_date
    sorted_profiles = sorted(profiles, key=lambda p: p["reg_date"])
    pool: list[dict] = []
    cursor = 0

    for year in (2023, 2024):
        for month in range(1, 13):
            month_start, month_end = _month_bounds(year, month)
            while (cursor < len(sorted_profiles)
                   and sorted_profiles[cursor]["reg_date"] <= month_end):
                pool.append(sorted_profiles[cursor])
                cursor += 1
            if not pool:
                continue
            is_promo = month in _PROMO_MONTHS
            for _ in range(_order_count_for_month(rng, year, month)):
                user = rng.choice(pool)
                _emit_one_order(
                    rng, user, month_start, month_end, is_promo, sku_ids, sku_price,
                    coupons, promotions, company_ids, addr_by_user, counters,
                    orders, order_items, history, payments, refunds, shipments,
                    return_orders, return_items, spent_by_user, user_paid_orders,
                    review_pool,
                )

    chain = {
        "orders": orders, "order_items": order_items,
        "order_status_history": history, "payments": payments,
        "refunds": refunds, "shipments": shipments,
        "return_orders": return_orders, "return_items": return_items,
    }
    return chain, spent_by_user, user_paid_orders, review_pool


def _emit_one_order(
    rng, user, month_start, month_end, is_promo, sku_ids, sku_price, coupons,
    promotions, company_ids, addr_by_user, counters, orders, order_items, history,
    payments, refunds, shipments, return_orders, return_items, spent_by_user,
    user_paid_orders, review_pool,
) -> None:
    """构造单笔订单的全部关联记录，并保证 §5 各项业务约束自洽。"""

    uid = user["user_id"]
    order_day = _rand_date(rng, max(month_start, user["reg_date"]), month_end)
    counters["oid"] += 1
    oid = counters["oid"]

    items, product_amount, counters["iid"] = _build_order_items(
        rng, sku_ids, sku_price, counters["iid"], is_promo
    )
    discount, coupon_id, promotion_id = _apply_discount(
        rng, product_amount, coupons, promotions
    )
    shipping = 0.0 if product_amount >= 99 else float(rng.choice([6, 8, 10, 12]))
    total_amount = round(product_amount - discount + shipping, 2)

    addrs = addr_by_user.get(uid)
    snap = rng.choice(addrs) if addrs else {"province": None, "city": None}

    status = _choose_status(rng, order_day)
    pay_status = "paid" if status in ("completed", "shipped", "paid") else "unpaid"
    created_dt = _dt.datetime.combine(
        order_day, _dt.time(rng.randint(8, 22), rng.randint(0, 59))
    )

    # --- 支付：已支付订单一条 success，金额 = total_amount；否则视状态造 failed/pending ---
    success_pid = None
    paid_dt = None
    if pay_status == "paid":
        paid_dt = _clamp_dt(created_dt + _dt.timedelta(minutes=rng.randint(2, 720)))
        counters["pid"] += 1
        success_pid = counters["pid"]
        payments.append((success_pid, oid, rng.choice(_PAY_METHODS), total_amount,
                         "success", _fmt_dt(paid_dt), _txn_no(rng)))
    elif status == "cancelled" and rng.random() < 0.5:
        counters["pid"] += 1
        payments.append((counters["pid"], oid, rng.choice(_PAY_METHODS), total_amount,
                         "failed", None, _txn_no(rng)))
    elif status == "created" and rng.random() < 0.3:
        counters["pid"] += 1
        payments.append((counters["pid"], oid, rng.choice(_PAY_METHODS), total_amount,
                         "pending", None, _txn_no(rng)))

    # --- 物流：shipped/completed 才发货；completed 一定签收(signed)且有 delivered_at ---
    delivered_dt = None
    if status in ("shipped", "completed"):
        ship_base = paid_dt or created_dt
        shipped_dt = _clamp_dt(
            ship_base + _dt.timedelta(days=rng.randint(1, 2), hours=rng.randint(0, 12))
        )
        counters["shid"] += 1
        if status == "completed":
            delivered_dt = _clamp_dt(shipped_dt + _dt.timedelta(days=rng.randint(1, 5)))
            shipments.append((counters["shid"], oid, rng.choice(company_ids),
                              _tracking_no(rng), "signed", _fmt_dt(shipped_dt),
                              _fmt_dt(delivered_dt), snap["province"], snap["city"]))
        else:
            shipments.append((counters["shid"], oid, rng.choice(company_ids),
                              _tracking_no(rng), rng.choice(["shipped", "in_transit"]),
                              _fmt_dt(shipped_dt), None, snap["province"], snap["city"]))

    # --- 状态流转历史：按 _STATUS_STEPS 逐条落库，时间递增 ---
    prev = None
    changed = created_dt
    for step in _STATUS_STEPS[status]:
        counters["hid"] += 1
        if step != "created":
            changed = _clamp_dt(changed + _dt.timedelta(hours=rng.randint(3, 60)))
        history.append((counters["hid"], oid, prev, step, _fmt_dt(changed),
                        _STATUS_OPERATOR[step]))
        prev = step

    # --- 售后：仅 completed 且已支付订单，~11% 发起退货；退款金额 ≤ 已付 ---
    if status == "completed" and pay_status == "paid" and rng.random() < 0.11:
        pay_status = _emit_return(
            rng, oid, uid, items, total_amount, delivered_dt or paid_dt or created_dt,
            success_pid, counters, return_orders, return_items, refunds,
        )

    # --- 落库明细行；completed 订单进入评价池并累计用户消费 ---
    for order_item_id, sku, qty, unit_price, subtotal in items:
        order_items.append((order_item_id, oid, sku, qty, unit_price, subtotal))
    if status == "completed":
        for order_item_id, sku, _qty, _up, _sub in items:
            review_pool.append({"order_item_id": order_item_id, "user_id": uid,
                                "sku_id": sku, "time": delivered_dt or created_dt})
        spent_by_user[uid] = round(spent_by_user.get(uid, 0.0) + total_amount, 2)
    if pay_status in ("paid", "partial_refund", "refunded"):
        user_paid_orders.setdefault(uid, []).append(oid)

    order_no = f"EC{order_day.strftime('%Y%m%d')}{oid:07d}"
    orders.append((oid, order_no, uid, _fmt_date(order_day), status, pay_status,
                   total_amount, product_amount, discount, shipping,
                   snap["province"], snap["city"], coupon_id, promotion_id))


def _emit_return(
    rng, oid, uid, items, total_amount, base_dt, success_pid, counters,
    return_orders, return_items, refunds,
) -> str:
    """生成一笔退货单（含退货明细，退款时追加 refund 行），返回订单最新 pay_status。"""

    counters["rid"] += 1
    rid = counters["rid"]
    ret_selection = rng.sample(items, min(len(items), rng.randint(1, 2)))
    returned_amount = 0.0
    chosen: list[tuple] = []
    for order_item_id, sku, qty, unit_price, _subtotal in ret_selection:
        ret_qty = rng.randint(1, qty)
        returned_amount += round(unit_price * ret_qty, 2)
        chosen.append((order_item_id, sku, ret_qty))
    # 退款不得超过实付金额
    returned_amount = round(min(returned_amount, total_amount), 2)

    # 多数退货最终退款成功（refunded/completed 合计 ~85%），贴近真实售后闭环
    rstatus = rng.choices(
        ["applied", "approved", "rejected", "refunded", "completed"],
        weights=[5, 5, 5, 50, 35],
    )[0]
    reason = rng.choice(_RETURN_REASONS)
    apply_date = base_dt.date() + _dt.timedelta(days=rng.randint(1, 7))
    if apply_date > _END_DATE:
        apply_date = _END_DATE

    refund_amount = None
    complete_date = None
    pay_status = "paid"
    if rstatus in ("refunded", "completed"):
        refund_amount = returned_amount
        complete_date = apply_date + _dt.timedelta(days=rng.randint(1, 7))
        if complete_date > _END_DATE:
            complete_date = _END_DATE
        counters["rfid"] += 1
        refunded_at = _clamp_dt(
            _dt.datetime.combine(complete_date, _dt.time(rng.randint(9, 20), 0))
        )
        refunds.append((counters["rfid"], success_pid, oid, refund_amount, reason,
                        "success", _fmt_dt(refunded_at)))
        pay_status = "refunded" if refund_amount >= total_amount - 0.01 else "partial_refund"

    return_orders.append((rid, oid, uid, reason, rstatus, _fmt_date(apply_date),
                          refund_amount,
                          _fmt_date(complete_date) if complete_date else None))
    for order_item_id, sku, ret_qty in chosen:
        counters["ritid"] += 1
        return_items.append((counters["ritid"], rid, order_item_id, sku, ret_qty))
    return pay_status


def _gen_carts(
    rng: random.Random, profiles: list[dict], sku_ids: list[int], n_carts: int = 1500
) -> tuple[list[tuple], list[tuple]]:
    """加购漏斗：约 1500 个购物车，每车 1~5 个 SKU。"""

    cart_rows: list[tuple] = []
    item_rows: list[tuple] = []
    cart_id = 0
    item_id = 0
    for p in rng.sample(profiles, min(n_carts, len(profiles))):
        cart_id += 1
        created = _rand_datetime(
            rng, _dt.datetime.combine(p["reg_date"], _dt.time(9, 0)), _END_DT
        )
        updated = _clamp_dt(
            created + _dt.timedelta(days=rng.randint(0, 10), hours=rng.randint(0, 23))
        )
        cart_rows.append((cart_id, p["user_id"], _fmt_dt(created), _fmt_dt(updated)))
        for sku in rng.sample(sku_ids, rng.randint(1, 5)):
            item_id += 1
            added = _clamp_dt(created + _dt.timedelta(hours=rng.randint(0, 48)))
            item_rows.append((item_id, cart_id, sku, rng.randint(1, 3), _fmt_dt(added)))
    return cart_rows, item_rows


def _gen_user_coupons(
    rng: random.Random, profiles: list[dict], coupons: list[tuple],
    user_paid_orders: dict[int, list[int]], target: int = 6000,
) -> list[tuple]:
    """领券记录：used 状态尽量关联该用户的真实已支付订单（核销率分析）。"""

    coupon_ids = [c[0] for c in coupons]
    reg_map = {p["user_id"]: p["reg_date"] for p in profiles}
    user_ids = [p["user_id"] for p in profiles]
    rows: list[tuple] = []
    for ucid in range(1, target + 1):
        uid = rng.choice(user_ids)
        coupon_id = rng.choice(coupon_ids)
        status = rng.choices(["unused", "used", "expired"], weights=[50, 30, 20])[0]
        received = _rand_datetime(
            rng, _dt.datetime.combine(reg_map[uid], _dt.time(10, 0)), _END_DT
        )
        used_at = None
        order_id = None
        if status == "used":
            paid = user_paid_orders.get(uid)
            if paid:
                order_id = rng.choice(paid)
                used_at = _fmt_dt(
                    _clamp_dt(received + _dt.timedelta(days=rng.randint(0, 5)))
                )
            else:
                status = "unused"  # 无可核销订单则回退为未使用
        rows.append((ucid, coupon_id, uid, status, _fmt_dt(received), used_at, order_id))
    return rows


def _gen_inventory(
    rng: random.Random, sku_ids: list[int], wh_ids: list[int], order_ids: list[int]
) -> tuple[list[tuple], list[tuple]]:
    """库存与出入库流水：逐条保持运行净值 ≥ 0，最终净值即 inventory.quantity。"""

    inv_rows: list[tuple] = []
    mv_rows: list[tuple] = []
    inv_id = 0
    mv_id = 0
    for sku in sku_ids:
        for wh in rng.sample(wh_ids, rng.randint(1, 3)):
            running = 0
            for _ in range(rng.randint(5, 12)):
                # running 为 0 时必须入库，避免出库导致净值为负
                if running == 0 or rng.random() < 0.55:
                    mtype = "inbound"
                    qty = rng.randint(20, 200)
                    running += qty
                else:
                    mtype = "outbound"
                    qty = rng.randint(1, running)
                    running -= qty
                mv_id += 1
                ref = (rng.choice(order_ids)
                       if mtype == "outbound" and order_ids and rng.random() < 0.3
                       else None)
                created = _rand_datetime(rng, _START_DT, _END_DT)
                mv_rows.append((mv_id, sku, wh, mtype, qty, ref, _fmt_dt(created)))
            inv_id += 1
            updated = _rand_datetime(rng, _START_DT, _END_DT)
            inv_rows.append((inv_id, sku, wh, running, rng.randint(10, 50),
                             _fmt_dt(updated)))
    return inv_rows, mv_rows


def _gen_promotion_products(
    rng: random.Random, promotions: list[tuple], sku_price: dict[int, float]
) -> list[tuple]:
    """活动商品池：每个活动挂 15~25 个 SKU，promo_price 为标价的 6~9 折。"""

    sku_ids = list(sku_price.keys())
    rows: list[tuple] = []
    pid = 0
    for promo in promotions:
        for sku in rng.sample(sku_ids, rng.randint(15, 25)):
            pid += 1
            rows.append((pid, promo[0], sku,
                         round(sku_price[sku] * rng.uniform(0.6, 0.9), 2)))
    return rows


def _gen_product_reviews(
    rng: random.Random, review_pool: list[dict], target: int = 5000
) -> list[tuple]:
    """商品评价：仅来自 completed 订单明细，评分偏高（好评率分析）。"""

    pool = (review_pool if len(review_pool) <= target
            else rng.sample(review_pool, target))
    rows: list[tuple] = []
    for i, item in enumerate(pool, 1):
        rating = rng.choices([1, 2, 3, 4, 5], weights=[3, 5, 12, 30, 50])[0]
        created = _clamp_dt(
            item["time"] + _dt.timedelta(days=rng.randint(1, 10), hours=rng.randint(0, 23))
        )
        reply = "感谢您的评价，期待再次光临！" if rng.random() < 0.3 else None
        rows.append((i, item["order_item_id"], item["user_id"], item["sku_id"], rating,
                     rng.choice(_REVIEW_TEXTS), 1 if rng.random() < 0.4 else 0,
                     _fmt_dt(created), reply))
    return rows


def _gen_user_events(
    rng: random.Random, profiles: list[dict], sku_ids: list[int], target: int = 50000
) -> list[tuple]:
    """行为流量（最大表）：view/add_cart/favorite/share 漏斗，event_time ≥ 注册日。"""

    event_pool = ["view"] * 60 + ["add_cart"] * 20 + ["favorite"] * 12 + ["share"] * 8
    user_ids = [p["user_id"] for p in profiles]
    reg_map = {p["user_id"]: p["reg_date"] for p in profiles}
    rows: list[tuple] = []
    for eid in range(1, target + 1):
        uid = rng.choice(user_ids)
        sku = rng.choice(sku_ids) if rng.random() < 0.95 else None
        event_time = _rand_datetime(
            rng, _dt.datetime.combine(reg_map[uid], _dt.time(0, 0)), _END_DT
        )
        rows.append((eid, uid, sku, rng.choice(event_pool), _fmt_dt(event_time),
                     _session_id(rng), rng.choice(_EVENT_SOURCES)))
    return rows


def _gen_facts(
    rng: random.Random, ds: Dataset, profiles: list[dict],
    addr_by_user: dict[int, list[dict]],
) -> dict[int, float]:
    """生成全部事实表并写入 ds.tables，返回 spent_by_user 供 users 回填。"""

    sku_price = {r[0]: r[3] for r in ds.tables["skus"]}
    sku_ids = list(sku_price.keys())
    wh_ids = [r[0] for r in ds.tables["warehouses"]]
    company_ids = [r[0] for r in ds.tables["logistics_companies"]]
    coupons = ds.tables["coupons"]
    promotions = ds.tables["promotions"]

    chain, spent_by_user, user_paid_orders, review_pool = _gen_orders_chain(
        rng, profiles, addr_by_user, sku_price, sku_ids, coupons, promotions,
        company_ids,
    )
    ds.tables.update(chain)
    order_ids = [o[0] for o in chain["orders"]]

    ds.tables["carts"], ds.tables["cart_items"] = _gen_carts(rng, profiles, sku_ids)
    ds.tables["inventory"], ds.tables["inventory_movements"] = _gen_inventory(
        rng, sku_ids, wh_ids, order_ids
    )
    ds.tables["promotion_products"] = _gen_promotion_products(rng, promotions, sku_price)
    ds.tables["product_reviews"] = _gen_product_reviews(rng, review_pool)
    ds.tables["user_events"] = _gen_user_events(rng, profiles, sku_ids)
    ds.tables["user_coupons"] = _gen_user_coupons(
        rng, profiles, coupons, user_paid_orders
    )
    return spent_by_user


def generate(seed: int = SEED) -> Dataset:
    """确定性生成全部 30 张表；生成顺序保证被引用的父表先于子表可用。"""

    rng = random.Random(seed)
    ds = Dataset()

    # ① 商品目录域：类目 → 品牌/供应商 → SPU → SKU → 属性
    ds.tables["membership_levels"] = _gen_membership_levels()
    categories = _gen_categories()
    ds.tables["categories"] = categories
    parent_ids = {r[2] for r in categories if r[2] is not None}
    leaf_cat_ids = [r[0] for r in categories if r[0] not in parent_ids]
    leaf_names = {r[0]: r[1] for r in categories if r[0] in set(leaf_cat_ids)}

    brands = _gen_brands(rng)
    ds.tables["brands"] = brands
    brand_names = {r[0]: r[1] for r in brands}

    suppliers = _gen_suppliers(rng)
    ds.tables["suppliers"] = suppliers
    supplier_ids = [r[0] for r in suppliers]

    spus = _gen_spus(rng, leaf_cat_ids, brand_names, leaf_names, supplier_ids)
    ds.tables["spus"] = spus

    skus = _gen_skus(rng, spus)
    ds.tables["skus"] = skus
    sku_ids = [r[0] for r in skus]
    ds.tables["sku_attributes"] = _gen_sku_attributes(rng, sku_ids)

    # ② 仓储/物流/组织/营销维表
    warehouses = _gen_warehouses(rng)
    ds.tables["warehouses"] = warehouses
    ds.tables["logistics_companies"] = _gen_logistics_companies()
    ds.tables["employees"] = _gen_employees(rng)
    ds.tables["coupons"] = _gen_coupons(rng)
    ds.tables["promotions"] = _gen_promotions(rng)

    # ③ 用户与地址（total_spent/level 待订单生成后回填）
    profiles = _gen_user_profiles(rng)
    addresses, addr_by_user = _gen_user_addresses(rng, profiles)
    ds.tables["user_addresses"] = addresses

    # ④ 事实表与业务链路，并据已完成订单回填用户累计消费与会员等级
    spent_by_user = _gen_facts(rng, ds, profiles, addr_by_user)
    ds.tables["users"] = _finalize_users(
        profiles, spent_by_user, ds.tables["membership_levels"]
    )
    return ds


# ---------------------------------------------------------------------------
# 写库 / 导出 / 自检 / CLI（Task 4-5）
# ---------------------------------------------------------------------------

def write_sqlite(ds: Dataset, path: str) -> None:
    """用标准库 sqlite3 建库并写入全部数据（零第三方依赖）。

    不开启 PRAGMA foreign_keys：外键仅作为关系元数据，插入按 SCHEMA 顺序即可，
    避免父子表插入顺序约束。INSERT 的列顺序严格等于 SCHEMA 中的列顺序。
    """

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript("\n".join(render_ddl("sqlite")))
        for table in SCHEMA:
            rows = ds.tables.get(table.name, [])
            if not rows:
                continue
            placeholders = ", ".join(["?"] * len(table.columns))
            conn.executemany(
                f"INSERT INTO {table.name} VALUES ({placeholders})", rows
            )
        conn.commit()
    finally:
        conn.close()


def _mysql_literal(value: object) -> str:
    """把单个值渲染为 MySQL 字面量：None→NULL、数值原样、字符串转义。"""

    if value is None:
        return "NULL"
    # bool 是 int 的子类，需先于数值判断，避免渲染成 True/False
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = str(value).replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"


def write_mysql_script(ds: Dataset, path: str) -> None:
    """导出贴近生产的 MySQL 建表 + 数据脚本（生成时无需 MySQL 在线）。

    用 SET FOREIGN_KEY_CHECKS=0/1 包裹，规避表间插入顺序约束；
    INSERT 每 500 行一批，避免单条 SQL 过长。
    """

    lines: list[str] = ["SET FOREIGN_KEY_CHECKS=0;"]
    lines.extend(render_ddl("mysql"))
    for table in SCHEMA:
        rows = ds.tables.get(table.name, [])
        for start in range(0, len(rows), 500):
            chunk = rows[start:start + 500]
            values = ",\n".join(
                "(" + ", ".join(_mysql_literal(v) for v in row) + ")" for row in chunk
            )
            lines.append(f"INSERT INTO {table.name} VALUES\n{values};")
    lines.append("SET FOREIGN_KEY_CHECKS=1;")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


class DataIntegrityError(ValueError):
    """生成数据违背 design §5 业务约束时抛出，使 CLI 立即失败退出。"""


_MONEY_EPS = 0.01  # 金额比较容差（浮点/四舍五入）


def _as_dicts(ds: Dataset, name: str) -> list[dict]:
    """把某表数据行按 SCHEMA 列名转成 dict，便于按列名做约束校验。"""

    cols = [c.name for c in next(t for t in SCHEMA if t.name == name).columns]
    return [dict(zip(cols, row)) for row in ds.tables[name]]


def _check_row_counts(ds: Dataset) -> None:
    if len(ds.tables) != 30:
        raise DataIntegrityError(f"表数应为 30，实际 {len(ds.tables)}")
    n_orders = len(ds.tables["orders"])
    if not 5000 <= n_orders <= 12000:
        raise DataIntegrityError(f"orders 行数 {n_orders} 超出合理区间")
    if len(ds.tables["order_items"]) < n_orders:
        raise DataIntegrityError("order_items 不应少于 orders")
    if len(ds.tables["user_events"]) < 40000:
        raise DataIntegrityError("user_events 规模不足 40000")
    if len(ds.tables["membership_levels"]) != 5:
        raise DataIntegrityError("membership_levels 应为 5 档")


def _check_amounts(orders: list[dict], items: list[dict]) -> None:
    """金额自洽：product_amount = Σ subtotal；total = product - discount + shipping。"""

    subtotal_by_order: dict[int, float] = {}
    for it in items:
        subtotal_by_order[it["order_id"]] = round(
            subtotal_by_order.get(it["order_id"], 0.0) + it["subtotal"], 2
        )
    for o in orders:
        oid = o["order_id"]
        product_sum = subtotal_by_order.get(oid, 0.0)
        if abs(o["product_amount"] - product_sum) > _MONEY_EPS:
            raise DataIntegrityError(
                f"order {oid}: product_amount {o['product_amount']} != Σsubtotal {product_sum}"
            )
        expected_total = round(
            o["product_amount"] - o["discount_amount"] + o["shipping_fee"], 2
        )
        if abs(o["total_amount"] - expected_total) > _MONEY_EPS:
            raise DataIntegrityError(f"order {oid}: total_amount 不满足金额公式")
        if o["discount_amount"] < -_MONEY_EPS or (
            o["discount_amount"] > o["product_amount"] + _MONEY_EPS
        ):
            raise DataIntegrityError(f"order {oid}: discount_amount 越界")
        if o["total_amount"] < -_MONEY_EPS:
            raise DataIntegrityError(f"order {oid}: total_amount 为负")


def _check_payments_shipments(
    orders: list[dict], payments: list[dict], shipments: list[dict],
    history: list[dict],
) -> dict[int, float]:
    """支付一致 + 状态联动；返回 success 支付金额映射供退款校验复用。"""

    success_amount: dict[int, float] = {}
    for p in payments:
        if p["status"] == "success":
            success_amount[p["order_id"]] = p["amount"]
    signed = {s["order_id"] for s in shipments if s["status"] == "signed"}
    has_history = {h["order_id"] for h in history}
    for o in orders:
        oid, pay_status, status = o["order_id"], o["pay_status"], o["status"]
        if pay_status in ("paid", "partial_refund", "refunded"):
            if oid not in success_amount:
                raise DataIntegrityError(f"order {oid}: 已支付却无 success 支付")
            if abs(success_amount[oid] - o["total_amount"]) > _MONEY_EPS:
                raise DataIntegrityError(f"order {oid}: success 支付金额 != total_amount")
        elif oid in success_amount:
            raise DataIntegrityError(f"order {oid}: 未支付订单却存在 success 支付")
        if status == "completed":
            if oid not in success_amount:
                raise DataIntegrityError(f"completed order {oid}: 缺少 success 支付")
            if oid not in signed:
                raise DataIntegrityError(f"completed order {oid}: 缺少已签收物流")
            if oid not in has_history:
                raise DataIntegrityError(f"completed order {oid}: 缺少状态流转历史")
    return success_amount


def _check_returns_refunds(
    orders: list[dict], returns: list[dict], refunds: list[dict],
    success_amount: dict[int, float],
) -> None:
    """售后前提：退货/退款只发生在已支付订单，且金额不超过已付金额。"""

    paid_orders = {o["order_id"] for o in orders
                   if o["pay_status"] in ("paid", "partial_refund", "refunded")}
    total_by_order = {o["order_id"]: o["total_amount"] for o in orders}
    for r in returns:
        if r["order_id"] not in paid_orders:
            raise DataIntegrityError(f"return {r['return_id']}: 退货指向未支付订单")
        if r["refund_amount"] is not None and (
            r["refund_amount"] > total_by_order[r["order_id"]] + _MONEY_EPS
        ):
            raise DataIntegrityError(f"return {r['return_id']}: 退款额超过实付")
    for rf in refunds:
        if rf["order_id"] not in success_amount:
            raise DataIntegrityError(f"refund {rf['refund_id']}: 无对应 success 支付")
        if rf["amount"] > success_amount[rf["order_id"]] + _MONEY_EPS:
            raise DataIntegrityError(f"refund {rf['refund_id']}: 退款额超过支付额")


def _check_inventory(inventory: list[dict], movements: list[dict]) -> None:
    """库存非负：inventory.quantity = Σinbound − Σoutbound ≥ 0。"""

    net: dict[tuple, int] = {}
    for m in movements:
        key = (m["sku_id"], m["warehouse_id"])
        if m["movement_type"] == "inbound":
            net[key] = net.get(key, 0) + m["quantity"]
        elif m["movement_type"] == "outbound":
            net[key] = net.get(key, 0) - m["quantity"]
    for inv in inventory:
        computed = net.get((inv["sku_id"], inv["warehouse_id"]), 0)
        if computed < 0:
            raise DataIntegrityError(f"inventory {inv['inventory_id']}: 净库存为负")
        if inv["quantity"] != computed:
            raise DataIntegrityError(
                f"inventory {inv['inventory_id']}: quantity 与流水净值不符"
            )


def _check_membership(
    users: list[dict], orders: list[dict], levels: list[dict]
) -> None:
    """会员一致：total_spent = 已完成订单金额之和，等级与累计消费门槛匹配。"""

    spent: dict[int, float] = {}
    for o in orders:
        if o["status"] == "completed":
            spent[o["user_id"]] = round(
                spent.get(o["user_id"], 0.0) + o["total_amount"], 2
            )
    thresholds = sorted((lv["min_amount"], lv["level_id"]) for lv in levels)
    for u in users:
        expected_spent = round(spent.get(u["user_id"], 0.0), 2)
        if abs(u["total_spent"] - expected_spent) > _MONEY_EPS:
            raise DataIntegrityError(f"user {u['user_id']}: total_spent 与订单不符")
        expected_level = 1
        for min_amount, level_id in thresholds:
            if expected_spent >= min_amount:
                expected_level = level_id
        if u["level_id"] != expected_level:
            raise DataIntegrityError(f"user {u['user_id']}: 会员等级与累计消费不匹配")


def _check_foreign_keys(
    orders: list[dict], items: list[dict], spus: list[dict], ds: Dataset
) -> None:
    """外键引用存在性抽查（长链路 JOIN 的前提）。"""

    sku_ids = {r[0] for r in ds.tables["skus"]}
    user_ids = {r[0] for r in ds.tables["users"]}
    category_ids = {r[0] for r in ds.tables["categories"]}
    for it in items:
        if it["sku_id"] not in sku_ids:
            raise DataIntegrityError(f"order_item {it['order_item_id']}: sku_id 悬空")
    for o in orders:
        if o["user_id"] not in user_ids:
            raise DataIntegrityError(f"order {o['order_id']}: user_id 悬空")
    for s in spus:
        if s["category_id"] not in category_ids:
            raise DataIntegrityError(f"spu {s['spu_id']}: category_id 悬空")


def self_check(ds: Dataset) -> None:
    """校验 design §5 全部业务约束；任一不满足抛 DataIntegrityError。"""

    _check_row_counts(ds)
    orders = _as_dicts(ds, "orders")
    items = _as_dicts(ds, "order_items")
    _check_amounts(orders, items)
    success_amount = _check_payments_shipments(
        orders, _as_dicts(ds, "payments"), _as_dicts(ds, "shipments"),
        _as_dicts(ds, "order_status_history"),
    )
    _check_returns_refunds(
        orders, _as_dicts(ds, "return_orders"), _as_dicts(ds, "refunds"),
        success_amount,
    )
    _check_inventory(
        _as_dicts(ds, "inventory"), _as_dicts(ds, "inventory_movements")
    )
    _check_membership(
        _as_dicts(ds, "users"), orders, _as_dicts(ds, "membership_levels")
    )
    _check_foreign_keys(orders, items, _as_dicts(ds, "spus"), ds)


def main() -> None:
    """CLI：确定性生成数据 → 自检 → 写 SQLite 库 + 导出 MySQL 脚本。"""

    import argparse

    parser = argparse.ArgumentParser(
        description="生成电商场景 Text2SQL 测试数据集（SQLite 库 + MySQL 脚本）"
    )
    parser.add_argument("--sqlite-out", default="examples/ecommerce/ecommerce.db")
    parser.add_argument("--mysql-out", default="examples/ecommerce/ecommerce_mysql.sql")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    ds = generate(seed=args.seed)
    self_check(ds)
    write_sqlite(ds, args.sqlite_out)
    write_mysql_script(ds, args.mysql_out)

    total = sum(len(rows) for rows in ds.tables.values())
    print(f"[ecommerce] tables={len(ds.tables)} rows={total}")
    print(f"[ecommerce] sqlite -> {args.sqlite_out}")
    print(f"[ecommerce] mysql  -> {args.mysql_out}")


if __name__ == "__main__":
    main()
