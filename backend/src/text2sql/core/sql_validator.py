from __future__ import annotations

"""生成 SQL 的安全闸门。

这里做轻量静态校验：只允许 SELECT/WITH，拒绝变更类关键字，
并检查 FROM/JOIN 表名和已限定列名是否来自当前 schema。
"""

import re

from text2sql.core.models import TableInfo


# 即使 SQL 以 WITH 开头，只要包含这些关键字也直接拒绝。
MUTATING_KEYWORDS = {
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "truncate",
    "create",
    "replace",
    "merge",
    "grant",
    "revoke",
}


def normalize_sql(sql: str) -> str:
    """规整空白和结尾分号，方便比较和正则校验。"""

    return re.sub(r"\s+", " ", sql.strip().rstrip(";")).lower()


class SQLValidationError(ValueError):
    pass


class SQLValidator:
    """Text2SQL 执行前的最小安全校验。"""

    def __init__(self, tables: list[TableInfo]) -> None:
        self.tables = {table.name.lower(): table for table in tables}

    def validate(self, sql: str | None) -> None:
        if not sql:
            raise SQLValidationError("SQL is NULL")
        normalized = normalize_sql(sql)
        first = normalized.split(" ", 1)[0]
        # 只读查询必须以 SELECT 或 WITH 开头。
        if first not in {"select", "with"}:
            raise SQLValidationError("Only SELECT or WITH queries are allowed")
        tokens = set(re.findall(r"[a-z_][a-z0-9_]*", normalized))
        forbidden = tokens & MUTATING_KEYWORDS
        if forbidden:
            raise SQLValidationError(f"Mutating keyword is not allowed: {sorted(forbidden)[0]}")
        self._validate_tables(normalized)
        self._validate_qualified_columns(normalized)

    def _validate_tables(self, normalized: str) -> None:
        # CTE 名称不属于真实表，需从 unknown table 检查中排除。
        table_refs = re.findall(r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)", normalized)
        cte_names = set(re.findall(r"\bwith\s+(?:recursive\s+)?([a-z_][a-z0-9_]*)\s+as", normalized))
        cte_names.update(re.findall(r",\s*([a-z_][a-z0-9_]*)\s+as\s*\(", normalized))
        unknown = [
            table for table in table_refs if table.lower() not in self.tables and table not in cte_names
        ]
        if unknown:
            raise SQLValidationError(f"Unknown table referenced: {unknown[0]}")

    def _validate_qualified_columns(self, normalized: str) -> None:
        # 只校验 alias.column 形式；未限定列名在复杂 SQL 中容易与 CTE/表达式混淆。
        alias_to_table: dict[str, str] = {}
        for table, alias in re.findall(
            r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)(?:\s+(?:as\s+)?([a-z_][a-z0-9_]*))?",
            normalized,
        ):
            if table.lower() in self.tables:
                alias_to_table[table] = table
                if alias and alias not in {"on", "where", "group", "order", "limit", "join"}:
                    alias_to_table[alias] = table

        for alias, column in re.findall(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b", normalized):
            table_name = alias_to_table.get(alias)
            if not table_name:
                continue
            table = self.tables[table_name.lower()]
            if column not in {col.name.lower() for col in table.columns}:
                raise SQLValidationError(f"Unknown column referenced: {alias}.{column}")
