from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from app.schemas import QuoteRecord
from app.services.calculations import pct_change


@dataclass(frozen=True)
class SectorHeatItem:
    trade_date: date
    asset_type: str
    code: str
    name: str
    return_7d: float | None
    return_1m: float | None
    heat_score: float | None
    heat_rank: int | None
    heat_level: str | None
    data_status: str


def calculate_sector_heat(quotes: Iterable[QuoteRecord], asset_type: str, as_of: date) -> list[SectorHeatItem]:
    records = [q for q in quotes if q.asset_type == asset_type and q.trade_date <= as_of]
    by_code: dict[str, list[QuoteRecord]] = defaultdict(list)
    for record in records:
        by_code[record.code].append(record)

    drafts: list[SectorHeatItem] = []
    for code, series in by_code.items():
        ordered = sorted(series, key=lambda item: item.trade_date)
        current = next((item for item in reversed(ordered) if item.trade_date == as_of), None)
        if current is None:
            continue

        return_7d = _window_return(ordered, 7)
        return_1m = _window_return(ordered, 21)
        has_intraday_signal = current.change_pct is not None
        status = "ok" if return_7d is not None and return_1m is not None else "partial" if has_intraday_signal else "history_insufficient"
        drafts.append(
            SectorHeatItem(
                trade_date=as_of,
                asset_type=asset_type,
                code=code,
                name=current.name,
                return_7d=return_7d,
                return_1m=return_1m,
                heat_score=None,
                heat_rank=None,
                heat_level=None,
                data_status=status,
            )
        )

    current_change = {
        code: next((item.change_pct for item in reversed(sorted(series, key=lambda row: row.trade_date)) if item.trade_date == as_of), None)
        for code, series in by_code.items()
    }
    score_1d = _percentile_scores(current_change)
    score_7d = _percentile_scores({item.code: item.return_7d for item in drafts})
    score_1m = _percentile_scores({item.code: item.return_1m for item in drafts})
    scores = {
        item.code: _weighted_score(
            (
                (score_1d.get(item.code), 0.20),
                (score_7d.get(item.code), 0.45),
                (score_1m.get(item.code), 0.35),
            )
        )
        for item in drafts
    }
    scores = {code: score for code, score in scores.items() if score is not None}
    ranks = _rank_scores(scores)

    return [
        SectorHeatItem(
            trade_date=item.trade_date,
            asset_type=item.asset_type,
            code=item.code,
            name=item.name,
            return_7d=item.return_7d,
            return_1m=item.return_1m,
            heat_score=scores.get(item.code),
            heat_rank=ranks.get(item.code),
            heat_level=heat_level(scores.get(item.code)) if item.code in scores else None,
            data_status=item.data_status,
        )
        for item in drafts
    ]


def heat_level(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 80:
        return "hot"
    if score >= 60:
        return "warming"
    if score >= 40:
        return "neutral"
    if score >= 20:
        return "cooling"
    return "cold"


def heat_action_label(item: SectorHeatItem) -> str:
    score = item.heat_score
    if score is None:
        return "历史不足"
    if item.data_status != "ok":
        if score >= 80:
            return "当日异动"
        if score >= 60:
            return "升温观察"
        if score <= 20:
            return "弱势观察"
        return "等待确认"
    if score >= 80 and (item.return_7d or 0) > 0 and (item.return_1m or 0) > 0:
        return "趋势确认"
    if score >= 60:
        return "建仓观察"
    if score <= 20:
        return "风险升高"
    if (item.return_1m or 0) > 0 and (item.return_7d or 0) < 0:
        return "等待修复"
    return "中性观察"


def heat_status_label(status: str) -> str:
    return {
        "ok": "完整历史",
        "partial": "临时热度",
        "history_insufficient": "历史不足",
        "source_unavailable": "数据缺口",
    }.get(status, status)


def _window_return(ordered: list[QuoteRecord], length: int) -> float | None:
    priced = [item for item in ordered if item.close is not None]
    if len(priced) < length:
        return None
    current = priced[-1]
    previous = priced[-length]
    return pct_change(current.close, previous.close)


def _weighted_score(values: tuple[tuple[float | None, float], ...]) -> float | None:
    available = [(score, weight) for score, weight in values if score is not None]
    if not available:
        return None
    total_weight = sum(weight for _, weight in available)
    if total_weight <= 0:
        return None
    return round(sum((score or 0) * weight for score, weight in available) / total_weight, 2)


def _percentile_scores(values: dict[str, float | None]) -> dict[str, float]:
    valid = [(code, value) for code, value in values.items() if value is not None]
    if not valid:
        return {}
    if len(valid) == 1:
        return {valid[0][0]: 100.0}

    ordered = sorted(valid, key=lambda item: item[1], reverse=True)
    scores: dict[str, float] = {}
    index = 0
    total = len(ordered)
    while index < total:
        value = ordered[index][1]
        end = index
        while end < total and ordered[end][1] == value:
            end += 1
        avg_rank = (index + 1 + end) / 2
        score = round(100 * (total - avg_rank) / (total - 1), 2)
        for code, _ in ordered[index:end]:
            scores[code] = score
        index = end
    return scores


def _rank_scores(scores: dict[str, float]) -> dict[str, int]:
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    ranks: dict[str, int] = {}
    index = 0
    while index < len(ordered):
        score = ordered[index][1]
        end = index
        while end < len(ordered) and ordered[end][1] == score:
            end += 1
        rank = index + 1
        for code, _ in ordered[index:end]:
            ranks[code] = rank
        index = end
    return ranks
