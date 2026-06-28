from __future__ import annotations

"""Schema 语义元数据。

真实数据库的表名/列名往往是英文缩写，业务人员的提问却是中文。本模块从 YAML
读取人工维护的「别名 / 业务描述 / 枚举字典」，在两个地方增强准确率：

1. 召回语料：把中文别名、描述、枚举词拼进表的检索文档，让中文提问更容易命中；
2. 生成 prompt：把列的枚举取值字典提示给生成器，避免编造不存在的状态值。

设计上保持「缺依赖/缺文件可降级」：PyYAML 未安装或文件缺失时返回空语义，
主链路照常运行，只是失去这层增强。
"""

from pathlib import Path

try:  # pragma: no cover - PyYAML 缺失时降级为空语义
    import yaml

    _HAS_YAML = True
except Exception:  # pragma: no cover
    _HAS_YAML = False


class SchemaSemantics:
    """表/列的语义元数据访问器。

    内部统一用小写表名/列名做键，屏蔽数据库大小写差异。所有查询接口在缺失时
    返回安全空值，让上层不必关心元数据是否配置。
    """

    def __init__(self, tables: dict[str, dict] | None = None) -> None:
        self._tables = tables or {}

    @classmethod
    def empty(cls) -> "SchemaSemantics":
        return cls({})

    @classmethod
    def from_yaml(cls, path: str | Path) -> "SchemaSemantics":
        """从 YAML 加载；文件不存在或无 PyYAML 时返回空语义。"""

        if not _HAS_YAML:
            return cls.empty()
        file_path = Path(path)
        if not file_path.exists():
            return cls.empty()
        try:
            payload = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        except Exception:  # pragma: no cover - 配置损坏时降级而非崩溃
            return cls.empty()
        raw_tables = payload.get("tables", {}) if isinstance(payload, dict) else {}
        return cls(cls._normalize(raw_tables))

    @staticmethod
    def _normalize(raw_tables: dict) -> dict[str, dict]:
        """把 YAML 原始结构规整为小写键，并把枚举值固化为元组。"""

        normalized: dict[str, dict] = {}
        for table_name, table_meta in (raw_tables or {}).items():
            table_meta = table_meta or {}
            columns: dict[str, dict] = {}
            for column_name, column_meta in (table_meta.get("columns") or {}).items():
                column_meta = column_meta or {}
                columns[str(column_name).lower()] = {
                    "alias": column_meta.get("alias"),
                    "description": column_meta.get("description"),
                    "enum_values": tuple(column_meta.get("enum_values") or ()),
                }
            normalized[str(table_name).lower()] = {
                "alias": table_meta.get("alias"),
                "description": table_meta.get("description"),
                "columns": columns,
            }
        return normalized

    def _table(self, table: str) -> dict:
        return self._tables.get(table.lower(), {})

    def _column(self, table: str, column: str) -> dict:
        return self._table(table).get("columns", {}).get(column.lower(), {})

    def table_alias(self, table: str) -> str | None:
        return self._table(table).get("alias")

    def table_description(self, table: str) -> str | None:
        return self._table(table).get("description")

    def column_alias(self, table: str, column: str) -> str | None:
        return self._column(table, column).get("alias")

    def column_description(self, table: str, column: str) -> str | None:
        return self._column(table, column).get("description")

    def enum_values(self, table: str, column: str) -> tuple[str, ...]:
        return tuple(self._column(table, column).get("enum_values", ()))

    def enrich_corpus(self, table: str) -> str:
        """生成可拼接进检索文档的语义文本：表/列别名、描述与枚举词。"""

        meta = self._table(table)
        if not meta:
            return ""
        parts: list[str] = []
        for key in ("alias", "description"):
            if meta.get(key):
                parts.append(str(meta[key]))
        for column_meta in meta.get("columns", {}).values():
            for key in ("alias", "description"):
                if column_meta.get(key):
                    parts.append(str(column_meta[key]))
            parts.extend(str(value) for value in column_meta.get("enum_values", ()))
        return " ".join(parts).strip()

    def prompt_hints(self, tables: list[str]) -> str:
        """为候选表生成枚举字典提示块，供 SQL prompt 注入。"""

        lines: list[str] = []
        for table in tables:
            meta = self._table(table)
            if not meta:
                continue
            for column_name, column_meta in meta.get("columns", {}).items():
                enum_values = column_meta.get("enum_values", ())
                if not enum_values:
                    continue
                alias = column_meta.get("alias") or column_name
                lines.append(
                    f"- {table.lower()}.{column_name}（{alias}）可选值: "
                    f"{', '.join(str(value) for value in enum_values)}"
                )
        return "\n".join(lines)
