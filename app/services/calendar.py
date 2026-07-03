from __future__ import annotations

from datetime import date, datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo


def today_in_timezone(timezone_name: str) -> date:
    return datetime.now(ZoneInfo(timezone_name)).date()


def is_probable_trading_day(day: date) -> bool:
    if day.weekday() >= 5:
        return False
    dates = _exchange_trade_dates()
    return day in dates if dates else True


@lru_cache(maxsize=1)
def _exchange_trade_dates() -> frozenset[date]:
    try:
        import akshare as ak

        frame = ak.tool_trade_date_hist_sina()
        column = "trade_date" if "trade_date" in frame.columns else frame.columns[0]
        return frozenset(value.date() if hasattr(value, "date") else date.fromisoformat(str(value)[:10]) for value in frame[column])
    except Exception:
        return frozenset()


def format_local_datetime(value: datetime | None, timezone_name: str = "Asia/Shanghai") -> str | None:
    if value is None:
        return None
    source = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return source.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S")