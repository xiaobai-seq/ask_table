from __future__ import annotations

"""领域配置。

DomainProfile 把业务词、字段角色、规则生成器意图词、澄清选项和前端示例问题
集中到一份可配置对象中。代码内置默认值只保留领域无关的基础规则；demo/ecommerce
等业务语义放在 YAML profile 中，通过 TEXT2SQL_DOMAIN_PROFILE_PATH 切换。
"""

from copy import deepcopy
from pathlib import Path
from typing import Any

try:  # pragma: no cover - PyYAML 缺失时回退默认配置
    import yaml

    _HAS_YAML = True
except Exception:  # pragma: no cover
    _HAS_YAML = False


DEFAULT_DOMAIN_PROFILE_CONFIG: dict[str, Any] = {
    "name": "base",
    "description": "Domain-neutral base profile. Business semantics should be supplied by YAML.",
    "synonyms": {},
    "schema": {
        "table_comment_rules": [],
        "table_tag_keywords": [],
        "column_tag_keywords": {
            "time": ["date", "time", "created", "month", "year"],
            "metric": ["amount", "value", "total", "count", "qty", "quantity", "number", "metric"],
            "key": ["id", "key"],
            "dimension": ["name", "category", "type", "status", "region", "city"],
        },
    },
    "sql": {
        "column_hints": {
            "time": ["date", "time", "created", "month"],
            "metric": ["amount", "value", "total", "quantity", "count", "number", "metric"],
            "dimension": ["category", "type", "status", "region", "city", "name"],
            "hierarchy_parent": ["parent", "manager"],
            "display_name": ["name"],
        },
        "intent_terms": {
            "hierarchy": ["递归", "层级", "上下级", "路径"],
            "growth": ["环比", "同比", "增长率", "增长", "趋势", "rolling", "滚动"],
            "ranking": ["排名", "排行", "top", "前"],
            "grouping": ["按", "每", "各", "分布", "占比"],
            "kpi": ["总", "金额", "数量", "指标"],
            "time_metric": ["环比", "同比", "增长", "趋势", "月份", "按月"],
            "metric": ["金额", "数量", "指标", "排名", "top", "前"],
        },
        "related_dimension_terms": ["维度", "分组", "地区", "区域", "城市"],
        "dimension_candidate_groups": [
            {"terms": ["维度", "分组"], "columns": ["dimension", "category", "type", "status", "name"]},
            {"terms": ["地区", "区域"], "columns": ["region", "area", "province"]},
            {"terms": ["城市"], "columns": ["city"]},
        ],
    },
    # Retrieval tuning is intentionally configuration-only. Built-in business rules live in
    # examples/domain_profile.yaml so another domain can swap them without changing code.
    "retrieval": {
        "tag_boost_rules": [],
        "table_boost_rules": [],
        "relationship_path": {},
    },
    "clarification": {
        "vague_words": ["情况", "数据", "看一下", "分析一下", "表现", "怎么样"],
        "metric_words": ["金额", "数量", "指标", "增长", "排名", "趋势"],
        "options": ["总金额", "总数量", "趋势", "排名"],
    },
    "render": {
        "numeric_hints": ["amount", "value", "count", "rate", "price", "metric", "total", "qty", "quantity"],
        "time_hints": ["date", "time", "month", "year", "period", "day"],
        "dimension_hints": ["name", "category", "type", "status", "region", "city", "dimension"],
        "series_hints": ["series", "category", "type", "status"],
        "chart_intents": {
            "line": ["趋势", "环比", "同比", "走势", "时间"],
            "ratio": ["占比", "比例"],
            "funnel": ["漏斗", "转化"],
            "sankey": ["桑基", "流向"],
            "heatmap": ["热力", "矩阵"],
            "scatter": ["散点", "相关"],
            "histogram": ["分布", "直方"],
        },
    },
    "frontend": {
        "example_queries": ["按月份统计指标趋势", "各维度指标占比", "指标排名前 10"],
    },
}


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


class DomainProfile:
    """领域配置访问器；所有缺失项都会回退到默认配置。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = _deep_merge(DEFAULT_DOMAIN_PROFILE_CONFIG, config or {})
        self.name = str(self.config.get("name") or "default")
        self.description = str(self.config.get("description") or "")

    @classmethod
    def default(cls) -> "DomainProfile":
        return cls({})

    @classmethod
    def from_yaml(cls, path: str | Path | None) -> "DomainProfile":
        if not path or not _HAS_YAML:
            return cls.default()
        file_path = Path(path)
        if not file_path.exists():
            return cls.default()
        try:
            payload = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        except Exception:  # pragma: no cover - 配置损坏时降级默认行为
            return cls.default()
        return cls(payload if isinstance(payload, dict) else {})

    @property
    def synonyms(self) -> dict[str, tuple[str, ...]]:
        return {str(key): _as_tuple(value) for key, value in self.config.get("synonyms", {}).items()}

    @property
    def frontend_examples(self) -> tuple[str, ...]:
        return _as_tuple(self.config.get("frontend", {}).get("example_queries"))

    @property
    def clarification_options(self) -> tuple[str, ...]:
        return _as_tuple(self.config.get("clarification", {}).get("options"))

    def schema_table_comment(self, table_name: str) -> str:
        lowered = table_name.lower()
        for rule in self.config.get("schema", {}).get("table_comment_rules", ()):
            if any(fragment in lowered for fragment in _as_tuple(rule.get("contains_any"))):
                return str(rule.get("comment") or "")
        return table_name.replace("_", " ")

    def schema_table_tags(self, table_name: str) -> tuple[str, ...]:
        lowered = table_name.lower()
        return tuple(
            keyword
            for keyword in _as_tuple(self.config.get("schema", {}).get("table_tag_keywords"))
            if keyword in lowered
        )

    def schema_column_tags(self, column_name: str, data_type: str) -> tuple[str, ...]:
        lowered = f"{column_name} {data_type}".lower()
        tags: list[str] = []
        tag_rules = self.config.get("schema", {}).get("column_tag_keywords", {})
        for tag, fragments in tag_rules.items():
            if any(fragment in lowered for fragment in _as_tuple(fragments)):
                tags.append(str(tag))
        return tuple(tags)

    def column_hints(self, role: str) -> tuple[str, ...]:
        return _as_tuple(self.config.get("sql", {}).get("column_hints", {}).get(role))

    def intent_terms(self, intent: str) -> tuple[str, ...]:
        return _as_tuple(self.config.get("sql", {}).get("intent_terms", {}).get(intent))

    def has_intent(self, text: str, intent: str) -> bool:
        return contains_any(text, self.intent_terms(intent))

    @property
    def related_dimension_terms(self) -> tuple[str, ...]:
        return _as_tuple(self.config.get("sql", {}).get("related_dimension_terms"))

    @property
    def dimension_candidate_groups(self) -> tuple[dict[str, tuple[str, ...]], ...]:
        groups: list[dict[str, tuple[str, ...]]] = []
        for group in self.config.get("sql", {}).get("dimension_candidate_groups", ()):
            groups.append(
                {
                    "terms": _as_tuple(group.get("terms")),
                    "columns": _as_tuple(group.get("columns")),
                }
            )
        return tuple(groups)

    @property
    def retrieval_tag_boost_rules(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.config.get("retrieval", {}).get("tag_boost_rules", ()))

    @property
    def retrieval_table_boost_rules(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.config.get("retrieval", {}).get("table_boost_rules", ()))

    @property
    def retrieval_relationship_path(self) -> dict[str, Any]:
        value = self.config.get("retrieval", {}).get("relationship_path", {})
        return value if isinstance(value, dict) else {}

    def clarification_terms(self, name: str) -> tuple[str, ...]:
        return _as_tuple(self.config.get("clarification", {}).get(name))

    def render_hints(self, name: str) -> tuple[str, ...]:
        return _as_tuple(self.config.get("render", {}).get(name))

    def chart_intent_terms(self, chart_intent: str) -> tuple[str, ...]:
        return _as_tuple(self.config.get("render", {}).get("chart_intents", {}).get(chart_intent))

    def public_config(self) -> dict[str, Any]:
        return {
            "domain_profile": self.name,
            "description": self.description,
            "example_queries": list(self.frontend_examples),
            "clarification_options": list(self.clarification_options),
        }


def contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = (text or "").lower()
    return any(term.lower() in lowered for term in terms)


_ACTIVE_DOMAIN_PROFILE = DomainProfile.default()


def get_domain_profile() -> DomainProfile:
    return _ACTIVE_DOMAIN_PROFILE


def set_active_domain_profile(profile: DomainProfile | None) -> None:
    global _ACTIVE_DOMAIN_PROFILE
    _ACTIVE_DOMAIN_PROFILE = profile or DomainProfile.default()
    # token/schema 推断都带缓存；profile 切换后要清理，避免跨场景污染。
    try:  # pragma: no cover - 防止导入环路影响启动
        from text2sql.core.tokenization import clear_tokenizer_cache

        clear_tokenizer_cache()
    except Exception:
        pass
    try:  # pragma: no cover
        from text2sql.core.schema import clear_schema_cache

        clear_schema_cache()
    except Exception:
        pass
