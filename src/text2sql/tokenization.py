from __future__ import annotations

"""检索用分词与同义词扩展。

schema 里常见英文表字段，用户问题常是中文。本模块把中文业务词扩展到英文
同义词，帮助 BM25、rerank 和启发式 overlap 都能跨语言命中。
"""

import re
from functools import lru_cache

try:  # pragma: no cover - optional dependency
    import jieba
except Exception:  # pragma: no cover
    jieba = None


_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[\u4e00-\u9fff]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


DOMAIN_SYNONYMS: dict[str, tuple[str, ...]] = {
    # 业务同义词会直接进入 token 集合，让“订单金额”能匹配 orders.total_amount。
    "订单": ("order", "orders", "sales", "交易", "成交"),
    "销售": ("sales", "amount", "gmv", "revenue", "营收", "收入"),
    "客户": ("customer", "customers", "user", "用户", "买家"),
    "商品": ("product", "products", "sku", "item", "货品"),
    "员工": ("employee", "staff", "member", "组织", "部门"),
    "金额": ("amount", "price", "payment", "gmv", "revenue"),
    "时间": ("date", "time", "created", "month", "day", "year"),
    "日期": ("date", "time", "created", "day"),
    "月份": ("month", "date", "time", "period"),
    "月": ("month", "date", "time", "period"),
    "地区": ("region", "province", "city", "area"),
    "排名": ("rank", "top", "排序"),
    "增长": ("growth", "rate", "increase", "环比", "同比"),
    "趋势": ("trend", "time", "period", "line"),
    "环比": ("growth", "rate", "lag", "period", "time"),
    "同比": ("growth", "rate", "lag", "period", "time"),
}


@lru_cache(maxsize=4096)
def tokenize(text: str) -> tuple[str, ...]:
    """把输入文本分成检索 token，并追加领域同义词。"""

    text = (text or "").strip().lower()
    if not text:
        return ()

    if jieba is not None:
        raw_tokens = [token.strip().lower() for token in jieba.cut(text) if token.strip()]
    else:
        # 没有 jieba 时，中文连续文本用 bi-gram + 原文兜底，保证离线测试仍可召回。
        raw_tokens = _WORD_RE.findall(text)
        for cjk_text in _CJK_RE.findall(text):
            if len(cjk_text) > 1:
                raw_tokens.extend(cjk_text[i : i + 2] for i in range(len(cjk_text) - 1))
                raw_tokens.append(cjk_text)

    expanded: list[str] = []
    for token in raw_tokens:
        normalized = normalize_token(token)
        if not normalized:
            continue
        expanded.append(normalized)
        expanded.extend(DOMAIN_SYNONYMS.get(normalized, ()))
    return tuple(expanded)


def normalize_token(token: str) -> str:
    """规整单个 token。"""

    return re.sub(r"\s+", "", token.strip().lower())


def identifier_tokens(identifier: str) -> tuple[str, ...]:
    """拆分 snake_case / 非单词分隔的标识符。"""

    parts = re.split(r"[_\W]+", identifier.lower())
    return tuple(part for part in parts if part)


def overlap_ratio(query: str, document: str) -> float:
    """计算 query token 中有多少能在 document token 中找到。"""

    query_tokens = set(tokenize(query))
    if not query_tokens:
        return 0.0
    doc_tokens = set(tokenize(document))
    if not doc_tokens:
        return 0.0
    return len(query_tokens & doc_tokens) / len(query_tokens)
