from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import mean, pstdev

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Asset, DailyQuote, FundHolding, FundMetadata, MarketMetric, SectorHeat
from app.services.fund_filters import primary_fund_codes
from app.services.storage import (
    replace_fund_features, replace_fund_sector_exposures,
    replace_signal_events, replace_signal_scores, upsert_signal_model_version,
)

MODEL_VERSION = "medium_term_v1"
MODEL_VERSION_V2 = "medium_term_v2"
MIN_CONFIDENCE = 70.0
FACTOR_WEIGHTS = {
    "momentum": 25.0,
    "risk": 20.0,
    "sector": 20.0,
    "market": 15.0,
    "quality": 10.0,
    "context": 10.0,
}


def compute_signal_scores(session: Session, as_of: date, news: list[dict] | None = None) -> list[dict]:
    start = as_of - timedelta(days=550)
    assets = list(session.execute(
        select(Asset).where(Asset.asset_type.in_(("fund", "etf")))
    ).scalars())
    primary = primary_fund_codes([(row.code, row.name) for row in assets if row.asset_type == "fund"])
    assets = [row for row in assets if row.asset_type == "etf" or row.code in primary]
    asset_map = {(row.asset_type, row.code): row for row in assets}
    quotes = list(session.execute(
        select(DailyQuote).where(
            DailyQuote.asset_type.in_(("fund", "etf")),
            DailyQuote.trade_date >= start,
            DailyQuote.trade_date <= as_of,
        ).order_by(DailyQuote.asset_type, DailyQuote.code, DailyQuote.trade_date)
    ).scalars())
    series: dict[tuple[str, str], list[DailyQuote]] = defaultdict(list)
    for quote in quotes:
        key = (quote.asset_type, quote.code)
        if key in asset_map:
            series[key].append(quote)

    metadata = {row.code: row for row in session.execute(select(FundMetadata)).scalars()}
    heat = list(session.execute(
        select(SectorHeat).where(SectorHeat.trade_date == as_of, SectorHeat.asset_type == "industry", SectorHeat.data_status == "ok")
    ).scalars())
    holding_rows = list(session.execute(select(FundHolding).order_by(FundHolding.fund_code, FundHolding.report_date.desc())).scalars())
    holding_exposure: dict[str, tuple[date, dict[str, float]]] = {}
    for holding in holding_rows:
        if not holding.industry:
            continue
        current = holding_exposure.get(holding.fund_code)
        if current is None or holding.report_date > current[0]:
            holding_exposure[holding.fund_code] = (holding.report_date, {})
            current = holding_exposure[holding.fund_code]
        if holding.report_date == current[0]:
            current[1][holding.industry] = current[1].get(holding.industry, 0) + (holding.weight or 0)
    metrics = {
        row.metric: row.value for row in session.execute(select(MarketMetric).where(MarketMetric.trade_date == as_of)).scalars()
    }
    raw: dict[tuple[str, str], dict] = {}
    for key, rows in series.items():
        current = next((row for row in reversed(rows) if row.trade_date == as_of), None)
        if current is None:
            continue
        closes = [row.close for row in rows if row.close and row.trade_date <= as_of]
        returns = {length: _period_return(closes, length) for length in (20, 60, 120)}
        daily = [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes)) if closes[i - 1]]
        recent = daily[-120:]
        volatility = pstdev(recent) * math.sqrt(250) if len(recent) >= 20 else None
        downside_values = [value for value in recent if value < 0]
        downside = pstdev(downside_values) * math.sqrt(250) if len(downside_values) >= 10 else None
        max_drawdown = _max_drawdown(closes[-120:]) if len(closes) >= 20 else None
        risk_adjusted = None
        if returns[60] is not None and volatility:
            risk_adjusted = returns[60] / max(volatility * 100, 0.01) - (max_drawdown or 0) / 20
        asset = asset_map[key]
        meta = metadata.get(asset.code)
        sector, sector_source, sector_coverage = _match_sector(asset.name, heat, meta, holding_exposure.get(asset.code))
        quality = _quality_raw(asset, current, meta, as_of)
        raw[key] = {
            "asset": asset,
            "current": current,
            "returns": returns,
            "momentum_raw": _mean_available([returns[20], returns[60], returns[120]]),
            "risk_raw": risk_adjusted,
            "volatility": volatility,
            "downside": downside,
            "max_drawdown": max_drawdown,
            "sector": sector,
            "sector_source": sector_source,
            "sector_coverage": sector_coverage,
            "quality_raw": quality,
        }

    momentum_pct = _percentiles({key: row["momentum_raw"] for key, row in raw.items()})
    risk_pct = _percentiles({key: row["risk_raw"] for key, row in raw.items()})
    quality_pct = _percentiles({key: row["quality_raw"] for key, row in raw.items()})
    market_score = _market_score(metrics)
    news_rows = news or []
    payloads: list[dict] = []
    for key, item in raw.items():
        components: dict[str, float | None] = {name: None for name in FACTOR_WEIGHTS}
        evidence: list[str] = []
        if key in momentum_pct:
            components["momentum"] = momentum_pct[key] * FACTOR_WEIGHTS["momentum"]
            evidence.append(_momentum_evidence(item["returns"]))
        if key in risk_pct:
            components["risk"] = risk_pct[key] * FACTOR_WEIGHTS["risk"]
            evidence.append(f"120日最大回撤 {item['max_drawdown']:.2f}%" if item["max_drawdown"] is not None else "风险历史不足")
        if item["sector"] is not None:
            components["sector"] = (item["sector"].heat_score or 0) / 100 * FACTOR_WEIGHTS["sector"]
            evidence.append(f"关联行业 {item['sector'].name}，热度 {item['sector'].heat_score or 0:.1f}，来源 {item['sector_source']}，覆盖 {item['sector_coverage']:.1f}%")
        if market_score is not None:
            components["market"] = market_score * FACTOR_WEIGHTS["market"]
            evidence.append("市场环境因子已纳入")
        if key in quality_pct:
            components["quality"] = quality_pct[key] * FACTOR_WEIGHTS["quality"]
            evidence.append("基金规模/费率或 ETF 流动性已纳入")
        context = _context_score(item["sector"].name if item["sector"] else "", news_rows)
        if context is not None:
            components["context"] = context * FACTOR_WEIGHTS["context"]
            evidence.append("相关政策新闻使用时间衰减计分")
        available = sum(FACTOR_WEIGHTS[name] for name, value in components.items() if value is not None)
        confidence = round(available, 1)
        total = round(sum(value or 0 for value in components.values()) / available * 100, 1) if available else None
        status = "experimental" if confidence >= MIN_CONFIDENCE else "insufficient"
        asset = item["asset"]
        payloads.append({
            "trade_date": as_of,
            "model_version": MODEL_VERSION,
            "asset_type": asset.asset_type,
            "code": asset.code,
            "name": asset.name,
            "total_score": total,
            "confidence": confidence,
            "status": status,
            "components_json": json.dumps(components, ensure_ascii=False),
            "evidence_json": json.dumps(evidence, ensure_ascii=False),
            "data_date": item["current"].trade_date,
            "created_at": datetime.utcnow(),
        })
    replace_signal_scores(session, as_of, MODEL_VERSION, payloads)
    return payloads


def _period_return(closes: list[float], length: int) -> float | None:
    if len(closes) <= length or not closes[-length - 1]:
        return None
    return (closes[-1] / closes[-length - 1] - 1) * 100


def _max_drawdown(closes: list[float]) -> float | None:
    if not closes:
        return None
    peak = closes[0]
    worst = 0.0
    for value in closes:
        peak = max(peak, value)
        worst = min(worst, (value / peak - 1) * 100)
    return abs(worst)


def _percentiles(values: dict) -> dict:
    valid = sorted((value, key) for key, value in values.items() if value is not None)
    if not valid:
        return {}
    denominator = max(1, len(valid) - 1)
    return {key: index / denominator for index, (_, key) in enumerate(valid)}


def _mean_available(values: list[float | None]) -> float | None:
    valid = [value for value in values if value is not None]
    return mean(valid) if valid else None


def _match_sector(
    name: str,
    heat: list[SectorHeat],
    metadata: FundMetadata | None,
    exposure: tuple[date, dict[str, float]] | None,
) -> tuple[SectorHeat | None, str, float]:
    if exposure:
        _, industries = exposure
        for industry, weight in sorted(industries.items(), key=lambda item: item[1], reverse=True):
            match = next((row for row in heat if row.name == industry or row.name in industry or industry in row.name), None)
            if match:
                return match, "季度持仓", min(100.0, sum(industries.values()))
    if metadata and metadata.tracking_index:
        match = next((row for row in heat if row.name and row.name in metadata.tracking_index), None)
        if match:
            return match, "跟踪指数", 100.0
    normalized = name.upper()
    matches = [row for row in heat if row.name and row.name.upper() in normalized]
    return (max(matches, key=lambda row: row.heat_score or 0), "名称关键词", 30.0) if matches else (None, "无映射", 0.0)

def _quality_raw(asset: Asset, quote: DailyQuote, meta: FundMetadata | None, as_of: date) -> float | None:
    if asset.asset_type == "etf":
        return math.log10(max(quote.amount or 0, 1)) if quote.amount else None
    values: list[float] = []
    if meta and meta.fund_size:
        values.append(math.log10(max(meta.fund_size, 1)))
    if meta and meta.inception_date:
        values.append(min((as_of - meta.inception_date).days / 365, 10))
    if meta and meta.management_fee is not None:
        values.append(max(0, 3 - meta.management_fee))
    return mean(values) if values else None


def _market_score(metrics: dict[str, float | None]) -> float | None:
    up, down = metrics.get("advancers"), metrics.get("decliners")
    if up is None or down is None or up + down == 0:
        return None
    breadth = up / (up + down)
    score = breadth
    congestion = metrics.get("market_congestion")
    if congestion is not None and congestion > 70:
        score -= 0.15
    return max(0.0, min(1.0, score))


def _context_score(sector: str, news: list[dict]) -> float | None:
    if not sector or not news:
        return None
    score = 0.5
    hits = 0
    for index, row in enumerate(news[:30]):
        text = f"{row.get('title', '')} {row.get('summary', '')}"
        if sector not in text:
            continue
        hits += 1
        decay = math.exp(-index / 12)
        if any(word in text for word in ("政策", "规划", "支持", "增长", "突破")):
            score += 0.15 * decay
        if any(word in text for word in ("风险", "处罚", "下调", "亏损", "限制")):
            score -= 0.15 * decay
    return max(0.0, min(1.0, score)) if hits else 0.5


def _momentum_evidence(returns: dict[int, float | None]) -> str:
    parts = [f"{length}日 {value:.2f}%" for length, value in returns.items() if value is not None]
    return "收益：" + " / ".join(parts) if parts else "收益历史不足"


# ---------------------------------------------------------------------------
# v2 signal computation — writes fund_features, fund_sector_exposure,
# signal_events, and still persists legacy signal_scores
# ---------------------------------------------------------------------------

SIGNAL_LABELS = {
    "insufficient_data": "历史不足",
    "strong_attention": "建仓观察",
    "worthy_attention": "趋势确认",
    "neutral_watch": "持有观察",
    "cautious_watch": "减仓观察",
    "no_attention": "风险升高",
    "pullback_watch": "等待修复",
}

V2_FACTOR_WEIGHTS = {
    "momentum": 25.0,
    "risk": 20.0,
    "sector": 20.0,
    "market": 15.0,
    "quality": 10.0,
    "context": 10.0,
}


def compute_signal_scores_v2(
    session: Session, as_of: date, news: list[dict] | None = None,
) -> dict:
    """Compute v2 deterministic features/signals, persist new tables, and
    also write legacy signal_scores for backward compatibility.

    Returns a dict with counts for reporting.
    """
    start = as_of - timedelta(days=550)
    assets = list(session.execute(
        select(Asset).where(Asset.asset_type.in_(("fund", "etf")))
    ).scalars())
    primary = primary_fund_codes(
        [(row.code, row.name) for row in assets if row.asset_type == "fund"]
    )
    assets = [row for row in assets if row.asset_type == "etf" or row.code in primary]
    asset_map = {(row.asset_type, row.code): row for row in assets}

    quotes = list(session.execute(
        select(DailyQuote).where(
            DailyQuote.asset_type.in_(("fund", "etf")),
            DailyQuote.trade_date >= start,
            DailyQuote.trade_date <= as_of,
        ).order_by(DailyQuote.asset_type, DailyQuote.code, DailyQuote.trade_date)
    ).scalars())
    series: dict[tuple[str, str], list[DailyQuote]] = defaultdict(list)
    for quote in quotes:
        key = (quote.asset_type, quote.code)
        if key in asset_map:
            series[key].append(quote)

    metadata = {row.code: row for row in session.execute(select(FundMetadata)).scalars()}
    heat = list(session.execute(
        select(SectorHeat).where(
            SectorHeat.trade_date == as_of,
            SectorHeat.asset_type == "industry",
            SectorHeat.data_status == "ok",
        )
    ).scalars())
    holding_rows = list(session.execute(
        select(FundHolding).order_by(FundHolding.fund_code, FundHolding.report_date.desc())
    ).scalars())
    holding_exposure: dict[str, tuple[date, dict[str, float]]] = {}
    for holding in holding_rows:
        if not holding.industry:
            continue
        current = holding_exposure.get(holding.fund_code)
        if current is None or holding.report_date > current[0]:
            holding_exposure[holding.fund_code] = (holding.report_date, {})
            current = holding_exposure[holding.fund_code]
        if holding.report_date == current[0]:
            current[1][holding.industry] = current[1].get(holding.industry, 0) + (holding.weight or 0)

    metrics = {
        row.metric: row.value
        for row in session.execute(
            select(MarketMetric).where(MarketMetric.trade_date == as_of)
        ).scalars()
    }

    # --- build raw features per asset ---
    raw: dict[tuple[str, str], dict] = {}
    for key, rows in series.items():
        current = next((row for row in reversed(rows) if row.trade_date == as_of), None)
        if current is None:
            continue
        closes = [row.close for row in rows if row.close and row.trade_date <= as_of]
        returns = {length: _period_return(closes, length) for length in (20, 60, 120)}
        daily = [
            (closes[i] / closes[i - 1] - 1)
            for i in range(1, len(closes))
            if closes[i - 1]
        ]
        recent = daily[-120:]
        volatility = (
            pstdev(recent) * math.sqrt(250) if len(recent) >= 20 else None
        )
        downside_values = [value for value in recent if value < 0]
        downside = (
            pstdev(downside_values) * math.sqrt(250)
            if len(downside_values) >= 10
            else None
        )
        max_drawdown = _max_drawdown(closes[-120:]) if len(closes) >= 20 else None
        risk_adjusted = None
        if returns[60] is not None and volatility:
            risk_adjusted = (
                returns[60] / max(volatility * 100, 0.01) - (max_drawdown or 0) / 20
            )
        asset = asset_map[key]
        meta = metadata.get(asset.code)
        sector, sector_source, sector_coverage = _match_sector(
            asset.name, heat, meta, holding_exposure.get(asset.code),
        )
        quality = _quality_raw(asset, current, meta, as_of)
        raw[key] = {
            "asset": asset,
            "current": current,
            "returns": returns,
            "momentum_raw": _mean_available([returns[20], returns[60], returns[120]]),
            "risk_raw": risk_adjusted,
            "volatility": volatility,
            "downside": downside,
            "max_drawdown": max_drawdown,
            "sector": sector,
            "sector_source": sector_source,
            "sector_coverage": sector_coverage,
            "quality_raw": quality,
            "_daily": daily,
        }

    # --- compute additional metrics for signal logic ---
    market_congestion = metrics.get("market_congestion")
    market_congestion_high = market_congestion is not None and market_congestion > 70
    etf_premium_map: dict[str, bool] = {}
    sector_7d_return_map: dict[str, float] = {
        row.name: row.return_7d or 0 for row in heat
    }

    # --- persist fund_features ---
    feature_payloads: list[dict] = []
    for key, item in raw.items():
        asset = item["asset"]
        meta = metadata.get(asset.code)
        rets = item["returns"]
        vol = item["volatility"]
        dvol = item["downside"]
        md = item["max_drawdown"]
        # sharpe and sortino
        sharpe = None
        sortino = None
        rf_annual = 0.025  # 2.5% risk-free proxy
        rf_daily = rf_annual / 250
        daily_vals = item.get("_daily")  # computed below
        if daily_vals and len(daily_vals) >= 20:
            excess = [d - rf_daily for d in daily_vals[-120:]]
            if vol and vol > 0:
                sharpe = round((_mean_available([rets.get(60)]) or 0) / 100 / (vol / math.sqrt(250)) if vol > 0 else None, 4) if _mean_available([rets.get(60)]) else None
            if dvol and dvol > 0 and len([x for x in excess if x < 0]) >= 10:
                neg_excess = [x for x in excess if x < 0]
                sortino = round(_mean_available([rets.get(60)]) / 100 / dvol if _mean_available([rets.get(60)]) and dvol > 0 else None, 4)
        # etf premium detection
        premium = None
        premium_high = False
        if asset.asset_type == "etf" and item["current"].extra_json:
            extra = json.loads(item["current"].extra_json or "{}")
            nav = extra.get("nav") or extra.get("unit_nav")
            if nav and item["current"].close:
                premium = round((item["current"].close / nav - 1) * 100, 2)
                premium_high = abs(premium) > 2.0
        etf_premium_map[asset.code] = premium_high
        # liquidity score
        liquidity_score = None
        if item["current"].amount and item["current"].amount > 0:
            liquidity_score = round(math.log10(max(item["current"].amount, 1)), 2)
        feature_payloads.append({
            "trade_date": as_of,
            "asset_type": asset.asset_type,
            "code": asset.code,
            "name": asset.name,
            "category": asset.category,
            "return_20d": rets.get(20),
            "return_60d": rets.get(60),
            "return_120d": rets.get(120),
            "volatility_120d": vol,
            "downside_volatility_120d": dvol,
            "max_drawdown_120d": md,
            "sharpe_120d": sharpe,
            "sortino_120d": sortino,
            "amount": item["current"].amount,
            "turnover_rate": item["current"].turnover_rate,
            "premium": premium,
            "liquidity_score": liquidity_score,
            "quality_score": item["quality_raw"],
            "data_status": "ok",
            "feature_json": json.dumps(_feature_extra(item, meta, as_of), ensure_ascii=False),
            "created_at": datetime.utcnow(),
        })
    replace_fund_features(session, as_of, feature_payloads)

    # --- persist fund_sector_exposure ---
    exposure_payloads: list[dict] = []
    for key, item in raw.items():
        asset = item["asset"]
        sector = item["sector"]
        if sector is None:
            continue
        exposure_payloads.append({
            "trade_date": as_of,
            "asset_type": asset.asset_type,
            "code": asset.code,
            "name": asset.name,
            "sector_code": sector.code,
            "sector_name": sector.name,
            "source": item["sector_source"],
            "confidence": min(100.0, item["sector_coverage"]),
            "coverage": item["sector_coverage"],
            "created_at": datetime.utcnow(),
        })
    replace_fund_sector_exposures(session, as_of, exposure_payloads)

    # --- compute percentiles for scoring ---
    momentum_pct = _percentiles({key: item["momentum_raw"] for key, item in raw.items()})
    risk_pct = _percentiles({key: item["risk_raw"] for key, item in raw.items()})
    quality_pct = _percentiles({key: item["quality_raw"] for key, item in raw.items()})
    market_score = _market_score(metrics)
    news_rows = news or []

    # --- build signal_events and legacy signal_scores ---
    signal_payloads: list[dict] = []
    legacy_payloads: list[dict] = []
    for key, item in raw.items():
        components: dict[str, float | None] = {name: None for name in V2_FACTOR_WEIGHTS}
        evidence: list[str] = []
        if key in momentum_pct:
            components["momentum"] = momentum_pct[key] * V2_FACTOR_WEIGHTS["momentum"]
            evidence.append(_momentum_evidence(item["returns"]))
        if key in risk_pct:
            components["risk"] = risk_pct[key] * V2_FACTOR_WEIGHTS["risk"]
            evidence.append(
                f"120日最大回撤 {item['max_drawdown']:.2f}%"
                if item["max_drawdown"] is not None
                else "风险历史不足"
            )
        if item["sector"] is not None:
            components["sector"] = (
                (item["sector"].heat_score or 0) / 100 * V2_FACTOR_WEIGHTS["sector"]
            )
            evidence.append(
                f"关联行业 {item['sector'].name}，热度 {item['sector'].heat_score or 0:.1f}，"
                f"来源 {item['sector_source']}，覆盖 {item['sector_coverage']:.1f}%"
            )
        if market_score is not None:
            components["market"] = market_score * V2_FACTOR_WEIGHTS["market"]
            evidence.append("市场环境因子已纳入")
        if key in quality_pct:
            components["quality"] = quality_pct[key] * V2_FACTOR_WEIGHTS["quality"]
            evidence.append("基金规模/费率或 ETF 流动性已纳入")
        context = _context_score(
            item["sector"].name if item["sector"] else "", news_rows,
        )
        if context is not None:
            components["context"] = context * V2_FACTOR_WEIGHTS["context"]
            evidence.append("相关政策新闻使用时间衰减计分")

        available = sum(
            V2_FACTOR_WEIGHTS[name]
            for name, value in components.items()
            if value is not None
        )
        confidence = round(available, 1)
        total = (
            round(
                sum(value or 0 for value in components.values()) / available * 100, 1,
            )
            if available
            else None
        )
        legacy_status = "experimental" if confidence >= MIN_CONFIDENCE else "insufficient"
        asset = item["asset"]

        # --- detect overheat / sector weakening / premium ---
        is_overheated = (
            item["sector"] is not None
            and (item["sector"].heat_score or 0) >= 85
        )
        sector_name = item["sector"].name if item["sector"] else ""
        sector_7d = sector_7d_return_map.get(sector_name, 0)
        sector_weakening = sector_7d < 0 and item["sector"] is not None and (item["sector"].return_1m or 0) > 0
        premium_high = etf_premium_map.get(asset.code, False)

        # --- research-only action label ---
        action_label, risk_dict, invalid_dict = _determine_signal_label(
            total, confidence, components,
            returns=item["returns"],
            max_drawdown=item["max_drawdown"],
            is_overheated=is_overheated,
            premium_high=premium_high,
            market_congestion_high=market_congestion_high,
            sector_weakening=sector_weakening,
        )

        # risk level
        risk_level = "high" if risk_dict else ("medium" if confidence < 85 else "low")

        # feature_json for signal event
        feat_json = {
            "return_20d": item["returns"].get(20),
            "return_60d": item["returns"].get(60),
            "return_120d": item["returns"].get(120),
            "volatility_120d": item["volatility"],
            "max_drawdown_120d": item["max_drawdown"],
            "sector_heat": item["sector"].heat_score if item["sector"] else None,
            "is_overheated": is_overheated,
            "sector_weakening": sector_weakening,
        }

        # reason_json
        reason_data = {
            "evidence": evidence,
            "components": components,
        }

        # build signal event
        signal_payloads.append({
            "trade_date": as_of,
            "model_version": MODEL_VERSION_V2,
            "asset_type": asset.asset_type,
            "code": asset.code,
            "name": asset.name,
            "action": action_label,
            "score": total,
            "confidence": confidence,
            "risk_level": risk_level,
            "status": "active",
            "reason_json": json.dumps(reason_data, ensure_ascii=False),
            "risk_json": json.dumps(risk_dict, ensure_ascii=False),
            "invalid_json": json.dumps(invalid_dict, ensure_ascii=False),
            "feature_json": json.dumps(feat_json, ensure_ascii=False),
            "created_at": datetime.utcnow(),
        })

        # legacy payload (backward compat)
        legacy_payloads.append({
            "trade_date": as_of,
            "model_version": MODEL_VERSION,
            "asset_type": asset.asset_type,
            "code": asset.code,
            "name": asset.name,
            "total_score": total,
            "confidence": confidence,
            "status": legacy_status,
            "components_json": json.dumps(components, ensure_ascii=False),
            "evidence_json": json.dumps(evidence, ensure_ascii=False),
            "data_date": item["current"].trade_date,
            "created_at": datetime.utcnow(),
        })

    # write signal_events
    replace_signal_events(session, as_of, MODEL_VERSION_V2, signal_payloads)

    # write legacy signal_scores
    replace_signal_scores(session, as_of, MODEL_VERSION, legacy_payloads)

    # record model version
    upsert_signal_model_version(session, MODEL_VERSION_V2, {
        "status": "experimental",
        "notes": "v2 中期选基信号 — 包含因子特征、行业暴露、研究标签",
        "weights_json": json.dumps(
            {"weights": V2_FACTOR_WEIGHTS, "min_confidence": MIN_CONFIDENCE},
            ensure_ascii=False,
        ),
        "backtest_json": "{}",
    })

    eligible = sum(1 for p in signal_payloads if p["action"] not in (
        "insufficient_data",
    ))
    return {
        "total": len(signal_payloads),
        "eligible": eligible,
        "labels": {
            label: sum(1 for p in signal_payloads if p["action"] == label)
            for label in SIGNAL_LABELS
        },
    }


def _determine_signal_label(
    total_score: float | None,
    confidence: float,
    components: dict[str, float | None],
    returns: dict[int, float | None] | None = None,
    max_drawdown: float | None = None,
    is_overheated: bool = False,
    premium_high: bool = False,
    market_congestion_high: bool = False,
    sector_weakening: bool = False,
) -> tuple[str, dict, dict]:
    """Map computed scores to research-only action labels using deterministic
    rules.  Returns (label, risk_dict, invalid_dict).

    These are NOT buy/sell recommendations — attention-level indicators only.
    """
    risk: dict[str, bool | float | str] = {}
    invalid: dict[str, bool | float | str] = {}

    # --- guard: insufficient data ---
    if confidence < MIN_CONFIDENCE or total_score is None:
        invalid["insufficient_confidence"] = confidence < MIN_CONFIDENCE
        invalid["missing_score"] = total_score is None
        return "insufficient_data", risk, invalid

    ret = returns or {}
    r20 = ret.get(20)
    r60 = ret.get(60)
    r120 = ret.get(120)

    # --- detect risk triggers ---
    if max_drawdown is not None and max_drawdown > 25:
        risk["high_max_drawdown"] = round(max_drawdown, 2)
    if is_overheated:
        risk["overheated"] = True
    if premium_high:
        risk["etf_premium_high"] = True
    if market_congestion_high:
        risk["market_congestion_high"] = True
    if sector_weakening:
        risk["sector_short_term_weakening"] = True

    # --- pullback: mid/long trend positive but 20d negative ---
    mid_long_positive = (
        (r60 is not None and r60 > 0) or (r120 is not None and r120 > 0)
    )
    short_negative = r20 is not None and r20 < 0
    if (mid_long_positive and short_negative) or (is_overheated and short_negative):
        risk["pullback_after_heat"] = True
        return "pullback_watch", risk, invalid

    # --- strong_attention: score >= 80, confidence >= 70, mid/long positive, not overheated ---
    if total_score >= 80 and confidence >= 70:
        if mid_long_positive and not is_overheated:
            return "strong_attention", risk, invalid
        if is_overheated:
            risk["strong_downgraded_by_overheat"] = True

    # --- worthy_attention: score >= 70, 20/60/120 returns mostly positive, drawdown controlled ---
    if total_score >= 70:
        ret_vals = [v for v in [r20, r60, r120] if v is not None]
        mostly_positive = sum(1 for v in ret_vals if v > 0) >= len(ret_vals) * 0.5 if ret_vals else False
        drawdown_ok = max_drawdown is not None and max_drawdown <= 20
        if mostly_positive and (drawdown_ok or max_drawdown is None):
            return "worthy_attention", risk, invalid

    # --- cautious_watch: score 40-55, or overheat/premium/sector weakening ---
    if 40 <= total_score <= 55 or is_overheated or premium_high or sector_weakening:
        return "cautious_watch", risk, invalid

    # --- neutral_watch: score >= 55 and no major risk trigger ---
    if total_score >= 55 and not risk:
        return "neutral_watch", risk, invalid

    # --- no_attention: score < 40, or high drawdown, or market congestion ---
    if total_score < 40 or (max_drawdown is not None and max_drawdown > 25) or market_congestion_high:
        return "no_attention", risk, invalid

    # --- fallback ---
    if total_score >= 55:
        return "neutral_watch", risk, invalid
    if total_score >= 40:
        return "cautious_watch", risk, invalid
    return "no_attention", risk, invalid


def _feature_extra(item: dict, meta, as_of: date) -> dict:
    """Build extra metadata for fund_features.feature_json payload."""
    return {
        "fund_size": meta.fund_size if meta else None,
        "inception_days": (
            (as_of - meta.inception_date).days
            if meta and meta.inception_date
            else None
        ),
        "management_fee": meta.management_fee if meta else None,
        "close": item["current"].close,
        "change_pct": item["current"].change_pct,
    }
