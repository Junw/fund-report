from __future__ import annotations

import json
import math
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean, median, pstdev

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Asset, BacktestRun, DailyQuote, SignalScore
from app.services.signals import MODEL_VERSION


BACKTEST_CONFIG = {
    "horizons": [20, 40, 60],
    "rebalance": "monthly",
    "minimum_oos_periods": 24,
    "etf_round_trip_cost": 0.002,
    "fund_default_round_trip_cost": 0.0075,
    "fund_cost_sensitivity": [0.005, 0.01],
    "benchmark": "eligible_universe_median",
}


def run_backtest(session: Session) -> BacktestRun:
    run = BacktestRun(model_version=MODEL_VERSION, status="running", config_json=json.dumps(BACKTEST_CONFIG), started_at=datetime.utcnow())
    session.add(run)
    session.flush()
    scores = list(session.execute(
        select(SignalScore).where(SignalScore.model_version == MODEL_VERSION, SignalScore.status == "experimental")
        .order_by(SignalScore.trade_date, SignalScore.total_score.desc())
    ).scalars())
    by_date: dict = defaultdict(list)
    for row in scores:
        by_date[row.trade_date].append(row)
    month_dates = []
    for row_date in sorted(by_date):
        month = (row_date.year, row_date.month)
        if month_dates and (month_dates[-1].year, month_dates[-1].month) == month:
            month_dates[-1] = row_date
        else:
            month_dates.append(row_date)
    if len(month_dates) < BACKTEST_CONFIG["minimum_oos_periods"]:
        derived = _derive_price_history_backtest(session)
        if derived.get("periods", 0) < BACKTEST_CONFIG["minimum_oos_periods"]:
            return _finish_insufficient(run, f"历史月度样本仅 {derived.get('periods', 0)} 期，需要先完成三年净值回填")
        run.status = "success"
        run.finished_at = datetime.utcnow()
        run.metrics_json = json.dumps(derived, ensure_ascii=False)
        run.production_eligible = 0
        run.message = "价格核心因子回测已完成；缺少历史行业披露，不能升级生产模型"
        return run

    min_date, max_date = month_dates[0], month_dates[-1] + timedelta(days=120)
    codes = {row.code for rows in by_date.values() for row in rows}
    quote_rows = session.execute(
        select(DailyQuote).where(DailyQuote.code.in_(codes), DailyQuote.trade_date >= min_date, DailyQuote.trade_date <= max_date)
        .order_by(DailyQuote.asset_type, DailyQuote.code, DailyQuote.trade_date)
    ).scalars()
    prices: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for quote in quote_rows:
        if quote.close:
            prices[(quote.asset_type, quote.code)].append((quote.trade_date, quote.close))

    period_returns: list[dict] = []
    sector_counts: dict[str, int] = defaultdict(int)
    for signal_date in month_dates:
        universe = by_date[signal_date]
        selected = universe[:max(1, math.ceil(len(universe) * 0.2))]
        horizons: dict[int, list[float]] = {20: [], 40: [], 60: []}
        benchmark: dict[int, list[float]] = {20: [], 40: [], 60: []}
        for row in universe:
            for horizon in (20, 40, 60):
                value = _forward_return(prices.get((row.asset_type, row.code), []), signal_date, horizon)
                if value is not None:
                    benchmark[horizon].append(value)
        for row in selected:
            cost = 0.002 if row.asset_type == "etf" else 0.0075
            for horizon in (20, 40, 60):
                value = _forward_return(prices.get((row.asset_type, row.code), []), signal_date, horizon)
                if value is not None:
                    horizons[horizon].append(value - cost * 100)
            for evidence in json.loads(row.evidence_json or "[]"):
                if evidence.startswith("关联行业 "):
                    sector_counts[evidence.split("，", 1)[0].removeprefix("关联行业 ")] += 1
        if horizons[20] and benchmark[20]:
            period_returns.append({
                "date": signal_date.isoformat(),
                "portfolio_20d": mean(horizons[20]),
                "benchmark_20d": median(benchmark[20]),
                "portfolio_40d": mean(horizons[40]) if horizons[40] else None,
                "portfolio_60d": mean(horizons[60]) if horizons[60] else None,
                "selected": len(selected),
            })

    if len(period_returns) < BACKTEST_CONFIG["minimum_oos_periods"]:
        return _finish_insufficient(run, f"可计算前瞻收益仅 {len(period_returns)} 期，需要至少 24 期")
    portfolio = [row["portfolio_20d"] for row in period_returns]
    benchmark_values = [row["benchmark_20d"] for row in period_returns]
    excess = [left - right for left, right in zip(portfolio, benchmark_values)]
    sector_total = sum(sector_counts.values())
    concentration = max(sector_counts.values()) / sector_total if sector_total else None
    metrics = {
        "periods": len(period_returns),
        "annualized_excess_return": round(mean(excess) * 12, 2),
        "information_ratio": round(mean(excess) / pstdev(excess) * math.sqrt(12), 3) if len(excess) > 1 and pstdev(excess) else None,
        "positive_excess_month_ratio": round(sum(1 for value in excess if value > 0) / len(excess), 3),
        "portfolio_max_drawdown": round(_return_drawdown(portfolio), 2),
        "benchmark_max_drawdown": round(_return_drawdown(benchmark_values), 2),
        "max_sector_contribution": round(concentration, 3) if concentration is not None else None,
        "average_40d_return": round(mean([row["portfolio_40d"] for row in period_returns if row["portfolio_40d"] is not None]), 2),
        "average_60d_return": round(mean([row["portfolio_60d"] for row in period_returns if row["portfolio_60d"] is not None]), 2),
        "period_returns": period_returns,
    }
    eligible = (
        metrics["annualized_excess_return"] > 2
        and (metrics["information_ratio"] or -99) > 0.3
        and metrics["positive_excess_month_ratio"] >= 0.55
        and metrics["portfolio_max_drawdown"] <= metrics["benchmark_max_drawdown"] + 5
        and concentration is not None and concentration <= 0.4
    )
    run.status = "success"
    run.finished_at = datetime.utcnow()
    run.metrics_json = json.dumps(metrics, ensure_ascii=False)
    run.production_eligible = 1 if eligible else 0
    run.message = "达到生产门槛" if eligible else "未达到生产门槛，继续保持实验状态"
    return run


def _derive_price_history_backtest(session: Session) -> dict:
    primary_keys = set(session.execute(
        select(Asset.asset_type, Asset.code).where(Asset.asset_type.in_(("fund", "etf")), Asset.is_primary == 1)
    ).all())
    latest_date = session.execute(select(DailyQuote.trade_date).order_by(DailyQuote.trade_date.desc()).limit(1)).scalar_one_or_none()
    if latest_date is None:
        return {"periods": 0, "mode": "price_history"}
    cutoff = latest_date - timedelta(days=1100)
    quote_rows = session.execute(
        select(DailyQuote.asset_type, DailyQuote.code, DailyQuote.trade_date, DailyQuote.close)
        .where(DailyQuote.asset_type.in_(("fund", "etf")), DailyQuote.trade_date >= cutoff, DailyQuote.close.is_not(None))
        .order_by(DailyQuote.asset_type, DailyQuote.code, DailyQuote.trade_date)
    )
    prices: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    market_dates: set = set()
    for asset_type, code, row_date, close in quote_rows:
        key = (asset_type, code)
        if key not in primary_keys:
            continue
        prices[key].append((row_date, close))
        market_dates.add(row_date)
    ordered_dates = sorted(market_dates)
    if len(ordered_dates) < 180:
        return {"periods": 0, "mode": "price_history"}
    month_ends = []
    for row_date in ordered_dates:
        if month_ends and (month_ends[-1].year, month_ends[-1].month) == (row_date.year, row_date.month):
            month_ends[-1] = row_date
        else:
            month_ends.append(row_date)
    period_returns: list[dict] = []
    for signal_date in month_ends:
        if signal_date < ordered_dates[120] or signal_date > ordered_dates[-61]:
            continue
        raw_scores: list[tuple[float, tuple[str, str]]] = []
        forward: dict[tuple[str, str], dict[int, float]] = {}
        for key, rows in prices.items():
            dates = [item[0] for item in rows]
            index = bisect_right(dates, signal_date) - 1
            if index < 120 or index + 60 >= len(rows):
                continue
            closes = [item[1] for item in rows[index - 120:index + 61]]
            past = closes[:121]
            returns = [(past[-1] / past[-length - 1] - 1) * 100 for length in (20, 60, 120) if past[-length - 1]]
            daily = [past[pos] / past[pos - 1] - 1 for pos in range(1, len(past)) if past[pos - 1]]
            volatility = pstdev(daily) * math.sqrt(250) * 100 if len(daily) >= 20 else None
            if not returns or not volatility:
                continue
            score = mean(returns) / max(volatility, 0.01)
            raw_scores.append((score, key))
            forward[key] = {horizon: (closes[120 + horizon] / closes[120] - 1) * 100 for horizon in (20, 40, 60)}
        if len(raw_scores) < 20:
            continue
        raw_scores.sort(reverse=True)
        selected = raw_scores[:max(1, math.ceil(len(raw_scores) * 0.2))]
        portfolio = {h: [] for h in (20, 40, 60)}
        benchmark = {h: [] for h in (20, 40, 60)}
        for _, key in raw_scores:
            for horizon in benchmark:
                benchmark[horizon].append(forward[key][horizon])
        for _, key in selected:
            cost = 0.2 if key[0] == "etf" else 0.75
            for horizon in portfolio:
                portfolio[horizon].append(forward[key][horizon] - cost)
        period_returns.append({
            "date": signal_date.isoformat(),
            "portfolio_20d": mean(portfolio[20]),
            "benchmark_20d": median(benchmark[20]),
            "portfolio_40d": mean(portfolio[40]),
            "portfolio_60d": mean(portfolio[60]),
            "selected": len(selected),
        })
    if not period_returns:
        return {"periods": 0, "mode": "price_history"}
    portfolio_values = [row["portfolio_20d"] for row in period_returns]
    benchmark_values = [row["benchmark_20d"] for row in period_returns]
    excess = [left - right for left, right in zip(portfolio_values, benchmark_values)]
    return {
        "periods": len(period_returns),
        "mode": "price_history_no_lookahead",
        "annualized_excess_return": round(mean(excess) * 12, 2),
        "information_ratio": round(mean(excess) / pstdev(excess) * math.sqrt(12), 3) if len(excess) > 1 and pstdev(excess) else None,
        "positive_excess_month_ratio": round(sum(value > 0 for value in excess) / len(excess), 3),
        "portfolio_max_drawdown": round(_return_drawdown(portfolio_values), 2),
        "benchmark_max_drawdown": round(_return_drawdown(benchmark_values), 2),
        "max_sector_contribution": None,
        "average_40d_return": round(mean(row["portfolio_40d"] for row in period_returns), 2),
        "average_60d_return": round(mean(row["portfolio_60d"] for row in period_returns), 2),
        "period_returns": period_returns,
    }

def _forward_return(rows: list[tuple], signal_date, horizon: int) -> float | None:
    start_index = next((index for index, (row_date, _) in enumerate(rows) if row_date >= signal_date), None)
    if start_index is None or start_index + horizon >= len(rows):
        return None
    start, end = rows[start_index][1], rows[start_index + horizon][1]
    return (end / start - 1) * 100 if start else None


def _return_drawdown(returns: list[float]) -> float:
    value = peak = 1.0
    worst = 0.0
    for item in returns:
        value *= 1 + item / 100
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return abs(worst) * 100


def _finish_insufficient(run: BacktestRun, message: str) -> BacktestRun:
    run.status = "insufficient"
    run.finished_at = datetime.utcnow()
    run.metrics_json = json.dumps({"periods": 0}, ensure_ascii=False)
    run.message = message
    run.production_eligible = 0
    return run