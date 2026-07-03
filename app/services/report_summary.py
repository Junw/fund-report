from __future__ import annotations

from dataclasses import asdict
from datetime import date

from app.schemas import QuoteRecord
from app.services.calculations import RankingItem
from app.services.sector_heat import SectorHeatItem, heat_action_label, heat_status_label


ASSET_TYPES = ("fund", "etf", "industry", "concept")


def build_report_summary(
    trade_date: date,
    quotes: list[QuoteRecord],
    rankings: list[RankingItem],
    metrics: dict[str, float | str | None],
    data_gaps: list[str],
    news: list[dict] | None = None,
    sector_heat: list[SectorHeatItem] | None = None,
) -> dict:
    latest_quotes = [quote for quote in quotes if quote.trade_date == trade_date]
    counts = {asset_type: sum(1 for quote in latest_quotes if quote.asset_type == asset_type) for asset_type in ASSET_TYPES}
    warnings = list(data_gaps)
    for asset_type, label in {
        "fund": "权益基金",
        "etf": "ETF",
        "industry": "行业板块",
        "concept": "概念题材",
    }.items():
        if counts[asset_type] == 0:
            warnings.append(f"{label}当天数据为空")
    for metric, label in {
        "advancers": "上涨家数",
        "decliners": "下跌家数",
        "total_amount": "成交额",
    }.items():
        if metrics.get(metric) is None:
            warnings.append(f"市场指标缺失：{label}")
    advancers = _to_float(metrics.get("advancers")) or 0
    decliners = _to_float(metrics.get("decliners")) or 0
    total_amount = _to_float(metrics.get("total_amount")) or 0
    if advancers + decliners <= 0:
        warnings.append("A股涨跌家数为0，股票实时行情可能缺失")
    if total_amount <= 0:
        warnings.append("市场成交额为0，股票成交数据可能缺失")
    warnings = list(dict.fromkeys(warnings))

    top_map: dict[str, list[dict]] = {}
    bottom_map: dict[str, list[dict]] = {}
    for asset_type in ASSET_TYPES:
        top_map[asset_type] = [
            asdict(item)
            for item in rankings
            if item.asset_type == asset_type and item.window == "1d" and item.rank_type == "gain" and item.rank <= 10
        ]
        bottom_map[asset_type] = [
            asdict(item)
            for item in rankings
            if item.asset_type == asset_type and item.window == "1d" and item.rank_type == "loss" and item.rank <= 10
        ]

    sector_heat_summary = {
        asset_type: _heat_summary_for_asset(sector_heat or [], asset_type)
        for asset_type in ("industry", "concept")
    }

    return {
        "counts": counts,
        "top": top_map,
        "bottom": bottom_map,
        "sector_heat": sector_heat_summary["industry"],
        "sector_heat_by_asset": sector_heat_summary,
        "market_state": _market_state(metrics),
        "data_quality": _data_quality(warnings, counts, metrics),
        "metrics": metrics,
        "news": news or [],
        "warnings": warnings,
        "disclaimer": "仅供个人研究记录，不构成投资建议。",
    }


def _heat_summary_for_asset(items: list[SectorHeatItem], asset_type: str) -> dict:
    heat_items = [item for item in items if item.asset_type == asset_type and item.heat_score is not None]
    heat_top = sorted(heat_items, key=lambda item: item.heat_score or 0, reverse=True)[:10]
    heat_cooling = sorted(heat_items, key=lambda item: item.heat_score or 0)[:10]
    status = "ok" if any(item.data_status == "ok" for item in heat_items) else "partial" if heat_items else "history_insufficient"
    return {
        "top": [_heat_to_dict(item) for item in heat_top],
        "cooling": [_heat_to_dict(item) for item in heat_cooling],
        "status": status,
        "status_label": heat_status_label(status),
    }


def _heat_to_dict(item: SectorHeatItem) -> dict:
    payload = asdict(item)
    payload["trade_date"] = item.trade_date.isoformat()
    payload["action_label"] = heat_action_label(item)
    payload["status_label"] = heat_status_label(item.data_status)
    return payload


def _market_state(metrics: dict[str, float | str | None]) -> dict:
    advancers = _to_float(metrics.get("advancers"))
    decliners = _to_float(metrics.get("decliners"))
    limit_up = _to_float(metrics.get("limit_up")) or 0
    limit_down = _to_float(metrics.get("limit_down")) or 0
    amount_delta = _to_float(metrics.get("total_amount_delta"))

    score = 50.0
    if advancers is not None and decliners is not None and advancers + decliners > 0:
        breadth = advancers / (advancers + decliners)
        score += (breadth - 0.5) * 70
    if limit_up + limit_down > 0:
        score += min(12, (limit_up - limit_down) / max(limit_up + limit_down, 1) * 12)
    if amount_delta is not None:
        score += 6 if amount_delta > 0 else -6 if amount_delta < 0 else 0
    score = round(max(0, min(100, score)), 1)

    if score >= 70:
        label = "偏强"
    elif score >= 55:
        label = "温和偏强"
    elif score >= 45:
        label = "震荡"
    elif score >= 30:
        label = "偏弱"
    else:
        label = "风险升高"
    return {"score": score, "label": label}


def _data_quality(warnings: list[str], counts: dict[str, int], metrics: dict[str, float | str | None]) -> dict:
    critical = [
        item
        for item in warnings
        if "当天数据为空" in item or "涨跌家数为0" in item or "成交额为0" in item or "市场指标缺失" in item
    ]
    if critical:
        status = "partial"
        label = "部分缺口"
    elif warnings:
        status = "warning"
        label = "有警告"
    else:
        status = "ok"
        label = "完整"
    return {
        "status": status,
        "label": label,
        "warning_count": len(warnings),
        "critical_count": len(critical),
        "counts": counts,
        "metric_keys": sorted(key for key, value in metrics.items() if value is not None),
    }


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
