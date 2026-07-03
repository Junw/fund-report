from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from app.schemas import QuoteRecord
from app.services.fund_filters import primary_fund_codes


WINDOWS = ("1d", "3d", "7d", "1m")
WINDOW_LENGTHS = {"3d": 3, "7d": 7, "1m": 21}


@dataclass(frozen=True)
class RankingItem:
    asset_type: str
    window: str
    rank_type: str
    rank: int
    code: str
    name: str
    value: float | None
    close: float | None
    amount: float | None
    note: str | None = None


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current / previous - 1) * 100


def latest_trade_date(quotes: Iterable[QuoteRecord]) -> date | None:
    dates = [quote.trade_date for quote in quotes]
    return max(dates) if dates else None


def calculate_rankings(
    quotes: Iterable[QuoteRecord],
    asset_type: str,
    as_of: date,
    limit: int = 30,
    one_month_overrides: dict[str, float] | None = None,
) -> list[RankingItem]:
    records = [q for q in quotes if q.asset_type == asset_type and q.trade_date <= as_of]
    if asset_type == "fund":
        current_rows = [(q.code, q.name) for q in records if q.trade_date == as_of]
        primary_codes = primary_fund_codes(current_rows)
        records = [q for q in records if q.code in primary_codes]
    by_code: dict[str, list[QuoteRecord]] = defaultdict(list)
    for record in records:
        by_code[record.code].append(record)

    results: list[RankingItem] = []
    overrides = one_month_overrides or {}

    for window in WINDOWS:
        gain_items: list[RankingItem] = []
        loss_items: list[RankingItem] = []
        amount_items: list[RankingItem] = []

        for code, series in by_code.items():
            ordered = sorted(series, key=lambda item: item.trade_date)
            current = next((item for item in reversed(ordered) if item.trade_date == as_of), None)
            if current is None:
                continue

            value: float | None
            note: str | None = None
            if window == "1d":
                value = current.change_pct
            elif window == "1m" and code in overrides:
                value = overrides[code]
            else:
                length = WINDOW_LENGTHS[window]
                if len(ordered) <= length - 1:
                    value = None
                    note = "历史不足"
                else:
                    previous = ordered[-length]
                    value = pct_change(current.close, previous.close)

            note = _merge_notes(note, _quote_source_note(current))
            base = {
                "asset_type": asset_type,
                "window": window,
                "code": code,
                "name": current.name,
                "close": current.close,
                "amount": current.amount,
                "note": note,
            }
            gain_items.append(RankingItem(rank_type="gain", rank=0, value=value, **base))
            loss_items.append(RankingItem(rank_type="loss", rank=0, value=value, **base))
            if current.amount is not None:
                amount_items.append(RankingItem(rank_type="amount", rank=0, value=current.amount, **base))

        results.extend(_rank(gain_items, limit, reverse=True))
        results.extend(_rank(loss_items, limit, reverse=False))
        results.extend(_rank(amount_items, limit, reverse=True))

    return results


def _merge_notes(*notes: str | None) -> str | None:
    parts = [note for note in notes if note]
    return "；".join(parts) if parts else None


def _quote_source_note(quote: QuoteRecord) -> str | None:
    source = quote.extra.get("source") if quote.extra else None
    labels = {
        "eastmoney_concept_spot": "东方财富概念",
        "eastmoney_board_change": "东方财富异动",
        "eastmoney_board": "东方财富板块",
        "ths": "同花顺兜底",
        "eastmoney": "东方财富",
        "sina": "新浪兜底",
    }
    parts: list[str] = []
    if source in labels:
        parts.append(labels[source])
    elif source:
        parts.append(str(source))

    if quote.extra.get("verified_by_ths"):
        ths_name = quote.extra.get("ths_name")
        parts.append(f"同花顺校验: {ths_name}" if ths_name else "同花顺校验")

    cross_source = quote.extra.get("cross_source")
    if cross_source:
        cross_label = labels.get(str(cross_source), str(cross_source))
        parts.append(f"交叉源: {cross_label}")

    cross_delta = quote.extra.get("cross_delta_pct")
    if cross_delta is not None:
        try:
            parts.append(f"涨幅差: {float(cross_delta):+.2f}pct")
        except (TypeError, ValueError):
            pass

    return "，".join(parts) if parts else None


def _rank(items: list[RankingItem], limit: int, reverse: bool) -> list[RankingItem]:
    valid = [item for item in items if item.value is not None]
    ordered = sorted(valid, key=lambda item: item.value or 0, reverse=reverse)[:limit]
    return [
        RankingItem(
            asset_type=item.asset_type,
            window=item.window,
            rank_type=item.rank_type,
            rank=index,
            code=item.code,
            name=item.name,
            value=item.value,
            close=item.close,
            amount=item.amount,
            note=item.note,
        )
        for index, item in enumerate(ordered, start=1)
    ]
