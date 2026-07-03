from __future__ import annotations

import re


INCLUDED_OPEN_FUND_TYPES = {"股票型", "混合型", "指数型"}
EXCLUDED_NAME_KEYWORDS = (
    "QDII",
    "债",
    "货币",
    "现金",
    "REIT",
    "FOF",
    "黄金",
    "商品",
    "原油",
    "纳斯达克",
    "日经",
    "标普",
    "恒生",
    "港股",
    "快线",
    "快钱",
)


def is_equity_fund(name: str, fund_type: str | None = None) -> bool:
    normalized_name = (name or "").upper()
    if any(keyword.upper() in normalized_name for keyword in EXCLUDED_NAME_KEYWORDS):
        return False
    if not fund_type:
        return True
    return any(allowed in fund_type for allowed in INCLUDED_OPEN_FUND_TYPES)


def is_domestic_etf(name: str) -> bool:
    normalized_name = (name or "").upper()
    if "ETF" not in normalized_name:
        return False
    return not any(keyword.upper() in normalized_name for keyword in EXCLUDED_NAME_KEYWORDS)


def is_c_share_class(name: str) -> bool:
    normalized = (name or "").strip().upper()
    return bool(re.search(r"(?:[\s\-_/]*C(?:类|份额)?)$", normalized))

SHARE_CLASS_PATTERN = re.compile(r"(?:[\s\-_/]*(A/B|A|B|C|E|I|H|R|Y)(?:类|份额)?)$", re.IGNORECASE)
SHARE_CLASS_PRIORITY = {"A": 0, "": 1, "A/B": 2, "B": 3, "H": 4, "R": 4, "Y": 4, "I": 5, "E": 6, "C": 7}


def fund_share_class(name: str) -> str:
    match = SHARE_CLASS_PATTERN.search((name or "").strip())
    return match.group(1).upper() if match else ""


def fund_family_key(name: str) -> str:
    normalized = SHARE_CLASS_PATTERN.sub("", (name or "").strip())
    return re.sub(r"[\s\-_/（）()]+", "", normalized).upper()


def share_class_priority(name: str) -> int:
    return SHARE_CLASS_PRIORITY.get(fund_share_class(name), 9)


def primary_fund_codes(rows: list[tuple[str, str]]) -> set[str]:
    families: dict[str, list[tuple[str, str]]] = {}
    for code, name in rows:
        families.setdefault(fund_family_key(name), []).append((code, name))
    return {
        min(members, key=lambda item: (share_class_priority(item[1]), item[0]))[0]
        for members in families.values()
        if members
    }


def is_secondary_share_class(name: str) -> bool:
    return fund_share_class(name) in {"B", "C", "E", "I", "H", "R", "Y"}