from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class QuoteRecord:
    trade_date: date
    asset_type: str
    code: str
    name: str
    close: float | None = None
    change_pct: float | None = None
    turnover: float | None = None
    turnover_rate: float | None = None
    volume: float | None = None
    amount: float | None = None
    category: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricRecord:
    trade_date: date
    metric: str
    value: float | None = None
    text_value: str | None = None
    source: str = "akshare"

