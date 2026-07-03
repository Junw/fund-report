from __future__ import annotations

import json
import re
import time
from datetime import date
from dataclasses import dataclass
from threading import RLock
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models import Asset, DailyQuote
from app.services.fund_filters import is_secondary_share_class


SECTOR_ALIASES: dict[str, tuple[str, ...]] = {
    "半导体": ("半导体", "芯片", "集成电路"),
    "电子化学品": ("电子化学", "光刻胶", "半导体材料"),
    "电池": ("电池", "锂电", "储能", "新能源"),
    "光伏": ("光伏", "太阳能"),
    "证券": ("证券", "券商"),
    "军工": ("军工", "国防", "航天", "航空"),
    "人工智能": ("人工智能", "AI", "算力", "大模型"),
    "机器人": ("机器人", "人形机器人"),
    "计算机": ("计算机", "软件", "信创"),
    "通信": ("通信", "5G", "光通信"),
    "医药": ("医药", "医疗", "创新药"),
    "汽车": ("汽车", "新能源车", "智能车"),
    "有色金属": ("有色", "金属", "稀土"),
    "小金属": ("小金属", "稀土", "锂", "钨"),
    "食品饮料": ("食品饮料", "白酒", "消费"),
}

POSITIVE_NEWS_WORDS = ("政策", "规划", "支持", "补贴", "促进", "加快", "改革", "专项", "突破", "增长")
NEGATIVE_NEWS_WORDS = ("风险", "限制", "处罚", "下调", "退坡", "监管", "警示", "亏损")

CATALOG_CACHE_SECONDS = 3600
_CACHE_LOCK = RLock()
_CATALOG_CACHE: dict[int, tuple[float, tuple[list["FundAsset"], dict, dict]]] = {}
_QUOTE_CACHE: dict[tuple[int, date], tuple[float, dict[str, "EtfQuote"]]] = {}


@dataclass(frozen=True)
class FundAsset:
    asset_type: str
    code: str
    name: str
    category: str | None


@dataclass(frozen=True)
class EtfQuote:
    close: float | None
    change_pct: float | None
    extra_json: str


class SectorRecommendationEngine:
    def __init__(self, session: Session, trade_date: date, news: list[dict[str, Any]] | None = None) -> None:
        self.session = session
        self.trade_date = trade_date
        self.news = news or []
        self.bind_key = id(session.get_bind())
        self.assets, self.asset_searchable, self.asset_gram_index = self._load_catalog()
        self.latest_etf_quotes = self._load_latest_etf_quotes_cached()

    def related_funds(self, sector_name: str, limit: int = 30) -> list[dict[str, Any]]:
        terms = sector_terms(sector_name)
        matches: list[dict[str, Any]] = []
        for asset in self._asset_candidates(terms):
            if asset.asset_type == "fund" and is_secondary_share_class(asset.name):
                continue
            searchable = self.asset_searchable[(asset.asset_type, asset.code)]
            if not any(term.lower() in searchable for term in terms):
                continue
            quote = self.latest_etf_quotes.get(asset.code) if asset.asset_type == "etf" else None
            extra = _extra(quote.extra_json) if quote else {}
            matches.append(
                {
                    "asset_type": asset.asset_type,
                    "code": asset.code,
                    "name": asset.name,
                    "category": asset.category,
                    "close": quote.close if quote else None,
                    "change_pct": quote.change_pct if quote else None,
                    "premium": _number(extra.get("premium")),
                    "url": f"https://fund.eastmoney.com/{asset.code}.html",
                }
            )
        matches.sort(key=lambda item: (item["asset_type"] != "etf", item["name"], item["code"]))
        return matches[:limit]

    def _build_asset_gram_index(self) -> dict[str, list[FundAsset]]:
        index: dict[str, list[FundAsset]] = {}
        for asset in self.assets:
            text = self.asset_searchable[(asset.asset_type, asset.code)]
            for gram in {text[pos : pos + 2] for pos in range(max(0, len(text) - 1))}:
                index.setdefault(gram, []).append(asset)
        return index

    def _asset_candidates(self, terms: tuple[str, ...]) -> list[FundAsset]:
        candidates: dict[tuple[str, str], FundAsset] = {}
        requires_full_scan = False
        for term in terms:
            normalized = term.lower()
            if len(normalized) < 2:
                requires_full_scan = True
                continue
            for asset in self.asset_gram_index.get(normalized[:2], []):
                candidates[(asset.asset_type, asset.code)] = asset
        return self.assets if requires_full_scan else list(candidates.values())

    def _load_catalog(self) -> tuple[list[FundAsset], dict, dict]:
        now = time.monotonic()
        with _CACHE_LOCK:
            cached = _CATALOG_CACHE.get(self.bind_key)
            if cached and now - cached[0] < CATALOG_CACHE_SECONDS:
                return cached[1]
        rows = self.session.execute(
            select(Asset).where(Asset.asset_type.in_(("fund", "etf"))).order_by(Asset.asset_type, Asset.code)
        ).scalars()
        assets = [FundAsset(row.asset_type, row.code, row.name, row.category) for row in rows]
        searchable = {
            (asset.asset_type, asset.code): f"{asset.name} {asset.category or ''}".lower() for asset in assets
        }
        self.assets = assets
        self.asset_searchable = searchable
        gram_index = self._build_asset_gram_index()
        payload = (assets, searchable, gram_index)
        with _CACHE_LOCK:
            _CATALOG_CACHE[self.bind_key] = (now, payload)
        return payload

    def _load_latest_etf_quotes_cached(self) -> dict[str, EtfQuote]:
        key = (self.bind_key, self.trade_date)
        now = time.monotonic()
        with _CACHE_LOCK:
            cached = _QUOTE_CACHE.get(key)
            if cached and now - cached[0] < CATALOG_CACHE_SECONDS:
                return cached[1]
        quotes = self._load_latest_etf_quotes()
        with _CACHE_LOCK:
            _QUOTE_CACHE[key] = (now, quotes)
        return quotes

    def score(self, heat: Any) -> dict[str, Any]:
        name = _value(heat, "name", "")
        heat_score = float(_value(heat, "heat_score", 0) or 0)
        return_7d = float(_value(heat, "return_7d", 0) or 0)
        return_1m = float(_value(heat, "return_1m", 0) or 0)
        related = self.related_funds(name, limit=100)

        trend = _clamp(heat_score * 0.45, 0, 45)
        persistence = _clamp(10 + return_7d * 0.45 + return_1m * 0.25, 0, 20)
        if return_7d >= 12:
            persistence = max(0, persistence - 4)

        terms = sector_terms(name)
        matched_news = []
        news_score = 10.0
        for item in self.news[:30]:
            text = f"{item.get('title', '')} {item.get('summary', '')}"
            if not any(term.lower() in text.lower() for term in terms):
                continue
            matched_news.append(item.get("title", ""))
            news_score += 1
            news_score += 3 if any(word in text for word in POSITIVE_NEWS_WORDS) else 0
            news_score -= 3 if any(word in text for word in NEGATIVE_NEWS_WORDS) else 0
        news_score = _clamp(news_score, 0, 20)

        premiums = [abs(item["premium"]) for item in related if item["premium"] is not None]
        avg_premium = sum(premiums) / len(premiums) if premiums else None
        premium_score = _premium_score(avg_premium)
        total = round(_clamp(trend + persistence + news_score + premium_score, 0, 100), 1)
        return {
            "score": total,
            "level": recommendation_level(total),
            "trend_score": round(trend, 1),
            "persistence_score": round(persistence, 1),
            "news_score": round(news_score, 1),
            "premium_score": round(premium_score, 1),
            "avg_abs_premium": round(avg_premium, 2) if avg_premium is not None else None,
            "related_count": len(related),
            "matched_news": matched_news[:3],
        }

    def _load_latest_etf_quotes(self) -> dict[str, EtfQuote]:
        latest_dates = (
            select(DailyQuote.code, func.max(DailyQuote.trade_date).label("latest_date"))
            .where(DailyQuote.asset_type == "etf", DailyQuote.trade_date <= self.trade_date)
            .group_by(DailyQuote.code)
            .subquery()
        )
        rows = self.session.execute(
            select(DailyQuote).join(
                latest_dates,
                and_(DailyQuote.code == latest_dates.c.code, DailyQuote.trade_date == latest_dates.c.latest_date),
            ).where(DailyQuote.asset_type == "etf")
        ).scalars()
        return {row.code: EtfQuote(row.close, row.change_pct, row.extra_json) for row in rows}


def clear_sector_recommendation_cache() -> None:
    with _CACHE_LOCK:
        _CATALOG_CACHE.clear()
        _QUOTE_CACHE.clear()


def sector_terms(name: str) -> tuple[str, ...]:
    clean = re.sub(r"(行业|板块|概念)$", "", name).strip()
    for key, aliases in SECTOR_ALIASES.items():
        if key in clean or clean in key:
            return tuple(dict.fromkeys((clean, *aliases)))
    return (clean,) if clean else (name,)


def recommendation_level(score: float) -> str:
    if score >= 80:
        return "较高"
    if score >= 65:
        return "偏高"
    if score >= 50:
        return "中性"
    if score >= 35:
        return "偏低"
    return "较低"


def _premium_score(avg_abs_premium: float | None) -> float:
    if avg_abs_premium is None:
        return 10
    if avg_abs_premium <= 0.5:
        return 15
    if avg_abs_premium <= 1:
        return 12
    if avg_abs_premium <= 2:
        return 8
    if avg_abs_premium <= 3:
        return 5
    return 2


def _value(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _extra(raw: str | None) -> dict[str, Any]:
    try:
        return json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
