# 电商场景 Text2SQL 测试数据集设计

日期：2026-07-04
状态：已评审通过（brainstorming 阶段产出）

## 1. 背景与目标

本仓库是一个面向「业务人员自助取数」的通用 Text2SQL 系统。现有样例库（`backend/examples/demo.db`，由 `core/sample_data.py` 生成）只有 5 张表、每表 4-7 行数据，不足以体现真实业务的复杂度，也无法充分压测 schema 检索、JOIN 路径分析与复杂 SQL 生成能力。

目标：新增一套**电商场景**的测试数据集（约 30 张表 + 中等规模真实感数据 + 配套语义/示例/评测资产），用于全面测试通用 Text2SQL 系统在「表多、关系复杂」场景下的表现。系统本身保持通用，仅新增一套可切换的电商测试资产。

## 2. 交付范围（已与用户确认）

纳入范围：
- 电商 schema：**大型规模，约 30 张表**，覆盖用户/会员、商品(SPU-SKU)、库存仓储、购物车、订单、支付、物流、售后、营销促销、评价、行为流量、组织 12 个业务域。
- 真实感数据：**中等规模**（事实表数千行、行为表数万行、维表数十~数百行），程序化确定性生成。
- 双引擎：**SQLite（快速测试）+ MySQL（贴近生产的建表脚本）**，同一套 schema。
- 配套测试资产（全套）：`schema_metadata.yaml`（中文语义）、`few_shot_seed.jsonl`（示例）、`eval_cases.jsonl`（评测用例）。

不纳入：
- 不修改通用 Text2SQL 内核逻辑（仅新增数据资产 + 生成器 + CLI 入口）。
- 不替换现有 `demo.db` 四件套（现有测试依赖其硬编码期望值，保持原样）。
- 超大型规模（40+ 表：多店铺/多仓/财务对账/风控/分销）本期不做。

## 3. 关键决策与默认假设

- 建模风格：**方案 A —— 真实 OLTP 规范化电商库（3NF）**。业务人员提问天然是 OLTP 语义，规范化 schema 提供最丰富的外键关系与 JOIN 路径，最能压测 `schema_inspector` 与 `table_relationship` 节点。
- 双引擎实现：数据生成**引擎无关**（纯标准库生成 `list[tuple]`）；schema 用抽象类型定义一次，渲染 SQLite / MySQL 两种方言 DDL，避免维护两套 DDL 不一致。
- SQLite 产物走标准库 `sqlite3`（零依赖，与 `sample_data.py` 理念一致）；MySQL 产物导出为 `.sql` 脚本（DDL + INSERT），生成时无需 MySQL 在线。
- 数据确定性：固定随机种子（`seed=20240101`），每次生成结果完全一致，保证评测期望值可复现。
- 时间跨度：`2023-01-01 ~ 2024-12-31`（24 个月），在 6·18 / 双 11 / 双 12 大促月放大订单量与客单价，制造趋势 / 环比(MoM) / 同比(YoY) 信号。
- 切换方式：通过环境变量 `TEXT2SQL_DATABASE_URL` / `TEXT2SQL_SCHEMA_METADATA_PATH` / `TEXT2SQL_FEW_SHOT_SEED_PATH` 指向电商资产，**核心代码零改动**。
- 产物入库策略：文本资产（yaml/jsonl/sql）提交；二进制 `ecommerce.db` 靠脚本复现并加入 `.gitignore`。
- 真实感来源：真实中文类目、真实省市区、拟真品牌名（虚构但像真的，规避商标）。

## 4. 领域划分与表清单（30 张表）

字段仅列关键项，`PK`=主键，`FK`=外键，`[...]`=枚举取值。

### ① 用户与会员域
- `membership_levels`(level_id PK, name[普通/银卡/金卡/铂金/钻石], min_amount 累计门槛, discount_rate 会员折扣)
- `users`(user_id PK, username, gender, birth_date, phone, email, level_id FK, register_date, total_spent 累计消费, status[active/inactive/banned], last_login_at)
- `user_addresses`(address_id PK, user_id FK, receiver_name, province, city, district, detail, is_default)

### ② 商品目录域
- `categories`(category_id PK, name, parent_id FK→自关联, level, sort_order) —— **多级类目，递归 CTE**
- `brands`(brand_id PK, name, country, created_at)
- `suppliers`(supplier_id PK, name, contact, province, cooperation_since)
- `spus`(spu_id PK, spu_name, category_id FK, brand_id FK, supplier_id FK, listing_date 上架日期, status[on_sale/off_sale/pending])
- `skus`(sku_id PK, spu_id FK, sku_name, price 售价, cost 成本, barcode, weight_kg, status)
- `sku_attributes`(attr_id PK, sku_id FK, attr_name[颜色/尺寸/内存…], attr_value)

### ③ 库存与仓储域
- `warehouses`(warehouse_id PK, name, province, city, capacity)
- `inventory`(inventory_id PK, sku_id FK, warehouse_id FK, quantity 可用库存, safety_stock 安全库存, updated_at) —— UNIQUE(sku_id, warehouse_id)
- `inventory_movements`(movement_id PK, sku_id FK, warehouse_id FK, movement_type[inbound/outbound/transfer], quantity, ref_order_id 可空, created_at) —— **库存时序**

### ④ 购物车域
- `carts`(cart_id PK, user_id FK, created_at, updated_at)
- `cart_items`(cart_item_id PK, cart_id FK, sku_id FK, quantity, added_at) —— **加购→下单漏斗**

### ⑤ 订单交易域
- `orders`(order_id PK, order_no, user_id FK, order_date, status[created/paid/shipped/completed/cancelled/closed], pay_status[unpaid/paid/partial_refund/refunded], total_amount 应付, product_amount 商品总额, discount_amount 优惠, shipping_fee 运费, receiver_province/receiver_city 地址快照, coupon_id 可空, promotion_id 可空)
- `order_items`(order_item_id PK, order_id FK, sku_id FK, quantity, unit_price 成交单价, subtotal 小计)
- `order_status_history`(history_id PK, order_id FK, from_status, to_status, changed_at, operator) —— **状态时序**

### ⑥ 支付域
- `payments`(payment_id PK, order_id FK, method[alipay/wechat/credit_card/balance], amount, status[success/failed/pending], paid_at, transaction_no)
- `refunds`(refund_id PK, payment_id FK, order_id FK, amount, reason, status[pending/success/failed], refunded_at)

### ⑦ 物流域
- `logistics_companies`(company_id PK, name, code)
- `shipments`(shipment_id PK, order_id FK, company_id FK, tracking_no, status[pending/shipped/in_transit/delivered/signed], shipped_at, delivered_at, receiver_province/receiver_city) —— **时效分析**

### ⑧ 售后域
- `return_orders`(return_id PK, order_id FK, user_id FK, reason[quality/wrong_item/not_as_described/no_reason/damaged], status[applied/approved/rejected/refunded/completed], apply_date, refund_amount, complete_date)
- `return_items`(return_item_id PK, return_id FK, order_item_id FK, sku_id FK, quantity)

### ⑨ 营销促销域
- `coupons`(coupon_id PK, name, type[full_reduction 满减/discount 折扣/cash 现金], threshold 满减门槛, value 优惠值, valid_from, valid_to, total_issued, status[active/expired])
- `user_coupons`(user_coupon_id PK, coupon_id FK, user_id FK, status[unused/used/expired], received_at, used_at, order_id 可空) —— **核销率**
- `promotions`(promotion_id PK, name, type[full_reduction/discount/flash_sale], start_date, end_date, discount_rate)
- `promotion_products`(id PK, promotion_id FK, sku_id FK, promo_price)

### ⑩ 评价内容域
- `product_reviews`(review_id PK, order_item_id FK, user_id FK, sku_id FK, rating 1-5, content, has_image, created_at, reply_content 可空) —— **好评率/评分分布**

### ⑪ 行为流量域
- `user_events`(event_id PK, user_id FK, sku_id 可空 FK, event_type[view/add_cart/favorite/share], event_time, session_id, source[app/web/mini_program]) —— **转化漏斗/留存，最大表**

### ⑫ 组织运营域
- `employees`(employee_id PK, employee_name, manager_id FK→自关联, department, role, hire_date, region) —— **递归组织层级**（沿用现有能力）

## 5. 核心关系与关键约束

主链路外键连接（决定 JOIN 路径）：

```text
membership_levels ──< users ──< user_addresses
                        │
                        ├──< orders ──< order_items >── skus >── spus >── categories(自关联)
                        │       │                        │               spus >── brands / suppliers
                        │       ├──< payments ──< refunds skus ──< sku_attributes
                        │       ├──< shipments >── logistics_companies
                        │       ├──< order_status_history
                        │       └──< return_orders ──< return_items
                        │
                        ├──< carts ──< cart_items >── skus
                        ├──< user_events (>── skus 可空)
                        ├──< product_reviews >── skus
                        └──< user_coupons >── coupons

skus ──< inventory >── warehouses
skus ──< inventory_movements >── warehouses
promotions ──< promotion_products >── skus
employees (自关联 manager_id)
```

关键业务约束（生成器需保证）：
- 金额自洽：`orders.product_amount = Σ order_items.subtotal`；`orders.total_amount = product_amount − discount_amount + shipping_fee`。
- 支付一致：已支付订单的 `payments.amount(success) = orders.total_amount`。
- 状态联动：`completed` 订单必有成功支付 + 已签收物流；`cancelled/closed` 订单无有效支付。
- 售后前提：退货/退款只发生在已支付订单，`refund_amount ≤` 已付金额。
- 库存非负：`inventory.quantity = 初始 + Σ inbound − Σ outbound ≥ 0`；出库流水与订单发货对应。
- 会员一致：`users.total_spent` 与其已完成订单金额之和一致，等级与累计消费门槛匹配。
- 地址快照：`orders.receiver_province/city` 取自下单时地址，不随 `user_addresses` 变化。

## 6. 数据生成策略

- **规模目标（中等真实感）**：

| 表 | 目标行数 | 表 | 目标行数 |
|---|---|---|---|
| membership_levels | 5 | orders | ~8,000 |
| users | ~2,000 | order_items | ~20,000 |
| user_addresses | ~3,000 | order_status_history | ~25,000 |
| categories | ~40（3-4 级） | payments | ~8,000 |
| brands | ~50 | refunds | ~500 |
| suppliers | ~30 | logistics_companies | ~8 |
| spus | ~300 | shipments | ~7,000 |
| skus | ~800 | return_orders | ~600（约 7%） |
| sku_attributes | ~1,600 | return_items | ~800 |
| warehouses | ~8 | coupons | ~50 |
| inventory | ~1,600 | user_coupons | ~6,000 |
| inventory_movements | ~15,000 | promotions | ~30 |
| carts | ~1,500 | promotion_products | ~600 |
| cart_items | ~4,000 | product_reviews | ~5,000 |
| employees | ~30 | user_events | ~50,000 |

- **确定性**：固定 `seed=20240101`，所有随机取值可复现。
- **时间分布**：24 个月按月生成订单，大促月（6/11/12 月）订单量与客单价上浮，制造 MoM/YoY 信号；工作日/周末有轻微波动。
- **真实感**：类目（手机数码/服装鞋包/美妆个护/家用电器/食品生鲜/母婴/图书…）、省市区来自真实行政区划、拟真品牌名。
- **自检**：生成结束后运行内建校验（§5 约束 + 行数区间），任一不满足即报错退出。

## 7. 双引擎实现

- 抽象类型 → 方言映射：

| 抽象类型 | SQLite | MySQL |
|---|---|---|
| INT / PK 自增 | INTEGER PRIMARY KEY [AUTOINCREMENT] | INT / BIGINT AUTO_INCREMENT PRIMARY KEY |
| BIGINT | INTEGER | BIGINT |
| DECIMAL(p,s)（金额） | REAL | DECIMAL(p,s) |
| VARCHAR(n) | TEXT | VARCHAR(n) |
| TEXT | TEXT | TEXT |
| DATE / DATETIME | TEXT | DATE / DATETIME |
| BOOL | INTEGER(0/1) | TINYINT(1) |

- 生成器结构：`core/ecommerce_data.py`
  - schema 定义（表 → 列(抽象类型) → 外键/索引）一次性声明。
  - 数据构造函数按域生成 `list[tuple]`（引擎无关）。
  - `render_ddl(dialect)` 渲染建表语句；`write_sqlite(path)` 用 `sqlite3` 建库写入；`write_mysql_script(path)` 导出 `.sql`。
- CLI 入口（`pyproject.toml [project.scripts]` 新增 `text2sql-ecommerce-db`）：
  - `--sqlite-out examples/ecommerce/ecommerce.db`
  - `--mysql-out examples/ecommerce/ecommerce_mysql.sql`

## 8. 配套测试资产

- `examples/ecommerce/schema_metadata.yaml`：为 30 张表关键列补中文别名/描述/枚举字典（覆盖所有 §4 枚举）。
- `examples/ecommerce/few_shot_seed.jsonl`：约 20 条「问题 → SQL」示例，覆盖趋势、环比、同比、TopN、排名、占比、分布、漏斗、递归、RFM、核销率、好评率、库存周转等。
- `examples/ecommerce/eval_cases.jsonl`：约 20 条评测用例，含 `expected_tables` + `required_sql_keywords`；数据确定性使部分用例可带 `expected_result` 精确值。

## 9. 文件组织与集成

```text
backend/src/text2sql/core/ecommerce_data.py     # 生成器（新增）
backend/examples/ecommerce/
  ├── schema_metadata.yaml    # 提交
  ├── few_shot_seed.jsonl     # 提交
  ├── eval_cases.jsonl        # 提交
  ├── ecommerce_mysql.sql     # 提交（文本可 diff）
  └── ecommerce.db            # 脚本生成，.gitignore
```

- 现有 `demo.db` 四件套原样保留，互不干扰。
- 使用方式（切换到电商库）：

```bash
cd backend
PYTHONPATH=src python3 -m text2sql.core.ecommerce_data \
  --sqlite-out examples/ecommerce/ecommerce.db \
  --mysql-out  examples/ecommerce/ecommerce_mysql.sql

export TEXT2SQL_DATABASE_URL="sqlite:///./examples/ecommerce/ecommerce.db"
export TEXT2SQL_SCHEMA_METADATA_PATH="./examples/ecommerce/schema_metadata.yaml"
export TEXT2SQL_FEW_SHOT_SEED_PATH="./examples/ecommerce/few_shot_seed.jsonl"

python3 -m text2sql.eval --db examples/ecommerce/ecommerce.db \
  --cases examples/ecommerce/eval_cases.jsonl --report examples/ecommerce/eval_report.json
```

## 10. 验证策略

- **生成器自检**：行数区间 + §5 业务约束（金额自洽、状态联动、库存非负、外键完整）校验，失败即报错。
- **冒烟单测**：新增生成器 smoke test（生成到临时 SQLite 库，断言表数=30 与核心约束成立），不触碰现有测试。
- **端到端评测**：用电商 `eval_cases.jsonl` 跑评测 CLI，验证 Text2SQL 全链路在电商 schema 下的表召回、SQL 关键词与精确结果。
- **双引擎一致性**：SQLite 建库与 MySQL 脚本导入后逐表行数一致。

## 11. 附：可覆盖的分析能力清单

| 能力 | 依赖的表/字段 |
|---|---|
| 趋势 / 环比 MoM / 同比 YoY | orders.order_date + total_amount（24 月 + 大促） |
| TopN / 排名（窗口函数） | 品类/品牌/商品销量、用户消费额 |
| 占比 / 分布 | 品类销额占比、支付方式分布、评分分布 |
| 多级层级（递归 CTE） | categories 自关联、employees 自关联 |
| 转化漏斗 | user_events(view→add_cart) → orders → payments |
| 地域分析 | user_addresses / orders 省市 |
| 会员分层 / RFM | membership_levels + users + orders |
| 营销效果 | coupons / user_coupons 核销率、promotions ROI |
| 售后质量 | return_orders 退货率/原因分布 |
| 物流时效 | shipments 发货→签收时长 |
| 库存周转 | inventory + inventory_movements |
| 长链路多表 JOIN | orders→items→skus→spus→categories/brands→suppliers |
