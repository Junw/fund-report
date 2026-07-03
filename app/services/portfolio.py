from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DailyQuote, PortfolioHolding, SignalEvent
from app.services.calculations import pct_change
from app.services.portfolio_rules import score_level, score_returns
from app.services.storage import latest_signal_event_date


@dataclass(frozen=True)
class HoldingAnalysis:
    holding: PortfolioHolding
    latest_date: date | None
    latest_close: float | None
    market_value: float | None
    weight: float | None
    returns: dict[str, float | None]
    score: int
    level: str
    signals: list[str]


def analyze_holdings(session: Session, holdings: list[PortfolioHolding], as_of: date | None = None) -> list[HoldingAnalysis]:
    analyses = [_analyze_one(session, holding, as_of) for holding in holdings]
    total_value = sum(item.market_value or 0 for item in analyses)
    adjusted: list[HoldingAnalysis] = []
    for item in analyses:
        weight = (item.market_value or 0) / total_value * 100 if total_value > 0 and item.market_value is not None else None
        score = item.score
        signals = list(item.signals)
        if weight is not None and weight >= 40:
            score -= 8
            signals.append("单只持仓权重偏高，组合波动可能集中")
        elif weight is not None and weight >= 25:
            score -= 4
            signals.append("单只持仓权重较高，注意分散度")
        score = max(0, min(100, score))
        adjusted.append(
            HoldingAnalysis(
                holding=item.holding,
                latest_date=item.latest_date,
                latest_close=item.latest_close,
                market_value=item.market_value,
                weight=weight,
                returns=item.returns,
                score=score,
                level=score_level(score),
                signals=signals,
            )
        )
    return adjusted


def portfolio_context(analyses: list[HoldingAnalysis]) -> dict:
    total_value = sum(item.market_value or 0 for item in analyses)
    weighted_score = (
        sum((item.market_value or 0) * item.score for item in analyses) / total_value if total_value > 0 else None
    )
    return {
        "holding_count": len(analyses),
        "total_market_value": total_value if total_value > 0 else None,
        "weighted_score": round(weighted_score, 1) if weighted_score is not None else None,
        "holdings": [
            {
                "code": item.holding.code,
                "name": item.holding.name,
                "asset_type": item.holding.asset_type,
                "shares": item.holding.shares,
                "market_value": item.market_value,
                "weight": item.weight,
                "returns": item.returns,
                "score": item.score,
                "level": item.level,
                "signals": item.signals,
            }
            for item in analyses
        ],
    }


def _analyze_one(session: Session, holding: PortfolioHolding, as_of: date | None) -> HoldingAnalysis:
    stmt = select(DailyQuote).where(
        DailyQuote.asset_type == holding.asset_type,
        DailyQuote.code == holding.code,
    )
    if as_of is not None:
        stmt = stmt.where(DailyQuote.trade_date <= as_of)
    quotes = list(session.execute(stmt.order_by(DailyQuote.trade_date)).scalars())
    latest = quotes[-1] if quotes else None
    returns = {
        "1d": latest.change_pct if latest else None,
        "3d": _window_return(quotes, 3),
        "7d": _window_return(quotes, 7),
        "1m": _window_return(quotes, 21),
    }
    score, signals = score_returns(returns)
    close = latest.close if latest else None
    market_value = close * holding.shares if close is not None and holding.shares > 0 else holding.cost_amount
    if holding.shares <= 0 and holding.cost_amount is not None:
        signals.append("未识别持有份额，暂用持仓金额估算权重")
    return HoldingAnalysis(
        holding=holding,
        latest_date=latest.trade_date if latest else None,
        latest_close=close,
        market_value=market_value,
        weight=None,
        returns=returns,
        score=score,
        level=score_level(score),
        signals=signals,
    )


def _window_return(quotes: list[DailyQuote], length: int) -> float | None:
    if len(quotes) < length:
        return None
    current = quotes[-1]
    previous = quotes[-length]
    return pct_change(current.close, previous.close)


# ---------------------------------------------------------------------------
# portfolio signal summary — aggregates v2 signal events for holdings
# ---------------------------------------------------------------------------

SIGNAL_LABEL_RESEARCH_MAP = {
    "insufficient_data": "历史不足",
    "strong_attention": "建仓观察",
    "worthy_attention": "趋势确认",
    "neutral_watch": "持有观察",
    "cautious_watch": "减仓观察",
    "no_attention": "风险升高",
    "pullback_watch": "等待修复",
}


def portfolio_signal_summary(
    session: Session,
    holdings: list[PortfolioHolding],
) -> dict:
    """Summarize v2 signal events for each portfolio holding using
    SignalEvent.action values and the exact research labels.

    Returns a dict with per-holding signals and aggregate research context.
    """
    event_date = latest_signal_event_date(session)
    if event_date is None:
        return {
            "event_date": None,
            "holdings": [],
            "label_distribution": {},
            "weighted_attention_score": None,
        }

    events = list(
        session.execute(
            select(SignalEvent).where(SignalEvent.trade_date == event_date)
        ).scalars()
    )
    event_map: dict[tuple[str, str], SignalEvent] = {}
    for e in events:
        event_map[(e.asset_type, e.code)] = e

    # compute market values
    holding_signals: list[dict] = []
    total_market_value = 0.0
    for holding in holdings:
        quote = session.execute(
            select(DailyQuote)
            .where(
                DailyQuote.asset_type == holding.asset_type,
                DailyQuote.code == holding.code,
            )
            .order_by(DailyQuote.trade_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        close = quote.close if quote else None
        market_value = (
            close * holding.shares
            if close is not None and holding.shares > 0
            else holding.cost_amount
        )
        total_market_value += market_value or 0
        sig = event_map.get((holding.asset_type, holding.code))
        holding_signals.append({
            "code": holding.code,
            "name": holding.name,
            "asset_type": holding.asset_type,
            "shares": holding.shares,
            "market_value": market_value,
            "action": sig.action if sig else "insufficient_data",
            "action_text": (
                SIGNAL_LABEL_RESEARCH_MAP.get(sig.action, sig.action)
                if sig
                else "历史不足"
            ),
            "score": sig.score if sig else None,
            "confidence": sig.confidence if sig else 0,
            "risk_level": sig.risk_level if sig else None,
        })

    # label distribution
    label_distribution: dict[str, int] = {}
    for hs in holding_signals:
        label = hs["action"]
        label_distribution[label] = label_distribution.get(label, 0) + 1

    # weighted attention score (higher = more attention-worthy holdings)
    attention_score_map = {
        "strong_attention": 100,
        "worthy_attention": 75,
        "neutral_watch": 50,
        "cautious_watch": 30,
        "pullback_watch": 20,
        "no_attention": 10,
        "insufficient_data": 0,
    }
    weighted_attention_score = None
    if total_market_value > 0:
        scored_values = [
            (attention_score_map.get(hs["action"], 0), hs["market_value"] or 0)
            for hs in holding_signals
        ]
        if scored_values:
            weighted_attention_score = round(
                sum(s * v for s, v in scored_values)
                / max(sum(v for _, v in scored_values), 1),
                1,
            )

    return {
        "event_date": event_date.isoformat(),
        "holdings": holding_signals,
        "label_distribution": {
            SIGNAL_LABEL_RESEARCH_MAP.get(k, k): v
            for k, v in label_distribution.items()
        },
        "weighted_attention_score": weighted_attention_score,
    }
