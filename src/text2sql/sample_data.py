from __future__ import annotations

"""本地演示数据库生成器。

样例库覆盖客户、商品、订单、订单明细和员工层级，刚好能演示趋势、
多表 JOIN、TopN、分布和递归 CTE 等核心 Text2SQL 能力。
"""

import argparse
import sqlite3
from pathlib import Path


def create_sample_database(path: str) -> None:
    """创建可重复生成的 SQLite demo.db。"""

    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(path)
    try:
        # 表结构既有事实表/维表外键，也有 employees 自关联，用来测试 JOIN 和递归。
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE customers (
              customer_id INTEGER PRIMARY KEY,
              customer_name TEXT NOT NULL,
              region TEXT NOT NULL,
              city TEXT NOT NULL,
              created_date TEXT NOT NULL
            );

            CREATE TABLE products (
              product_id INTEGER PRIMARY KEY,
              product_name TEXT NOT NULL,
              category TEXT NOT NULL,
              unit_price REAL NOT NULL
            );

            CREATE TABLE orders (
              order_id INTEGER PRIMARY KEY,
              customer_id INTEGER NOT NULL,
              order_date TEXT NOT NULL,
              status TEXT NOT NULL,
              total_amount REAL NOT NULL,
              FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
            );

            CREATE TABLE order_items (
              item_id INTEGER PRIMARY KEY,
              order_id INTEGER NOT NULL,
              product_id INTEGER NOT NULL,
              quantity INTEGER NOT NULL,
              amount REAL NOT NULL,
              FOREIGN KEY(order_id) REFERENCES orders(order_id),
              FOREIGN KEY(product_id) REFERENCES products(product_id)
            );

            CREATE TABLE employees (
              employee_id INTEGER PRIMARY KEY,
              employee_name TEXT NOT NULL,
              manager_id INTEGER,
              department TEXT NOT NULL,
              FOREIGN KEY(manager_id) REFERENCES employees(employee_id)
            );
            """
        )
        connection.executemany(
            # 客户维表：地区和城市字段用于“按地区/城市”分析。
            "INSERT INTO customers VALUES (?, ?, ?, ?, ?)",
            [
                (1, "Acme", "East", "Shanghai", "2025-01-02"),
                (2, "Byte Retail", "North", "Beijing", "2025-01-05"),
                (3, "Ocean Shop", "South", "Shenzhen", "2025-02-11"),
                (4, "Future Mart", "East", "Hangzhou", "2025-03-19"),
            ],
        )
        connection.executemany(
            # 商品维表：品类字段用于关联维度聚合。
            "INSERT INTO products VALUES (?, ?, ?, ?)",
            [
                (1, "Notebook", "Office", 12.5),
                (2, "Keyboard", "Electronics", 99.0),
                (3, "Monitor", "Electronics", 899.0),
                (4, "Chair", "Furniture", 399.0),
            ],
        )
        connection.executemany(
            # 订单事实表：日期和金额字段用于趋势、环比、KPI。
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?)",
            [
                (1, 1, "2025-01-12", "paid", 1200.0),
                (2, 2, "2025-01-23", "paid", 900.0),
                (3, 3, "2025-02-03", "paid", 1500.0),
                (4, 1, "2025-02-18", "refunded", 300.0),
                (5, 4, "2025-03-08", "paid", 2200.0),
                (6, 2, "2025-03-21", "paid", 1800.0),
                (7, 3, "2025-04-02", "paid", 2600.0),
            ],
        )
        connection.executemany(
            # 明细事实表：商品级数量和金额用于品类/商品分析。
            "INSERT INTO order_items VALUES (?, ?, ?, ?, ?)",
            [
                (1, 1, 2, 5, 495.0),
                (2, 1, 1, 20, 250.0),
                (3, 2, 3, 1, 899.0),
                (4, 3, 4, 3, 1197.0),
                (5, 5, 3, 2, 1798.0),
                (6, 6, 2, 10, 990.0),
                (7, 7, 4, 5, 1995.0),
            ],
        )
        connection.executemany(
            # 员工表包含 manager_id 自关联，用于 WITH RECURSIVE 示例。
            "INSERT INTO employees VALUES (?, ?, ?, ?)",
            [
                (1, "CEO", None, "Executive"),
                (2, "Sales VP", 1, "Sales"),
                (3, "East Manager", 2, "Sales"),
                (4, "North Manager", 2, "Sales"),
                (5, "Data Lead", 1, "Data"),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def main() -> None:
    """命令行入口，默认输出 examples/demo.db。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="examples/demo.db")
    args = parser.parse_args()
    create_sample_database(args.output)
    print(args.output)


if __name__ == "__main__":
    main()
