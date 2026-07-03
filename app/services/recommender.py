from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from app.services.calculations import RankingItem
from app.services.sector_heat import SectorHeatItem


@dataclass(frozen=True)
class Advice:
    level: str
    title: str
    body: str


NEWS_KEYWORDS = {
    "AI": ("AI", "算力", "人工智能", "芯片", "半导体", "数据中心"),
    "新能源": ("新能源", "电池", "储能", "光伏", "锂", "充电"),
    "机器人": ("机器人", "工业母机", "自动化"),
    "医药": ("医药", "创新药", "医疗", "生物"),
    "消费": ("消费", "白酒", "食品", "旅游", "零售"),
    "港股科技": ("港股", "互联网", "恒生科技", "科技"),
    "资源": ("有色", "煤炭", "石油", "黄金", "铜", "资源"),
}


def build_advice(
    rankings: Iterable[RankingItem],
    metrics: dict[str, float | str | None],
    news: list[dict] | None = None,
    sector_heat: list[SectorHeatItem] | None = None,
) -> list[Advice]:
    items = list(rankings)
    advice: list[Advice] = []
    top_by_asset_window: dict[tuple[str, str], set[str]] = defaultdict(set)
    names: dict[str, str] = {}

    for item in items:
        if item.rank_type == "gain" and item.rank <= 10 and item.window in {"3d", "7d", "1m"}:
            top_by_asset_window[(item.asset_type, item.window)].add(item.code)
            names[item.code] = item.name

    for asset_type in ("fund", "etf", "industry", "concept"):
        strong = (
            top_by_asset_window[(asset_type, "3d")]
            & top_by_asset_window[(asset_type, "7d")]
            & top_by_asset_window[(asset_type, "1m")]
        )
        for code in sorted(strong)[:3]:
            advice.append(
                Advice(
                    level="positive",
                    title="趋势较强，可观察回调机会",
                    body=f"{names.get(code, code)} 同时进入近 3 日、7 日和 1 个月强势榜，短中期动量一致。避免盘中追高，优先观察缩量回踩后的承接。",
                )
            )

    hot_1d = [
        item
        for item in items
        if item.rank_type == "gain" and item.window == "1d" and item.value is not None and item.value >= 5
    ]
    if hot_1d:
        joined = "、".join(item.name for item in hot_1d[:5])
        advice.append(
            Advice(
                level="warning",
                title="当天涨幅偏高，谨慎追高",
                body=f"{joined} 当日涨幅较大，短线情绪可能已经集中释放，适合等分歧或回落后再评估。",
            )
        )

    news_advice = _build_news_advice(items, news or [])
    advice.extend(news_advice)
    advice.extend(_build_heat_advice(sector_heat or []))

    congestion = _to_float(metrics.get("market_congestion"))
    equity_bond_spread = _to_float(metrics.get("equity_bond_spread"))
    if congestion is not None and congestion >= 0.42:
        advice.append(
            Advice(
                level="risk",
                title="市场拥挤度偏高",
                body="大盘拥挤度处于偏高区域，热门方向的回撤风险上升，仓位和买点需要更保守。",
            )
        )
    if equity_bond_spread is not None and equity_bond_spread <= 0.03:
        advice.append(
            Advice(
                level="risk",
                title="股债性价比下降",
                body="股债利差偏低时，权益资产风险补偿不足，整体策略应更重视防守和分散。",
            )
        )

    breadth = _to_float(metrics.get("advance_decline_ratio"))
    if breadth is not None and breadth < 0.8:
        advice.append(
            Advice(
                level="neutral",
                title="市场宽度偏弱",
                body="上涨家数相对不足，热点可能集中在少数方向，追逐排行榜时需要确认板块内部扩散程度。",
            )
        )

    opportunity = [
        item
        for item in items
        if item.rank_type == "gain"
        and item.window == "1m"
        and item.asset_type in {"industry", "concept"}
        and item.value is not None
        and item.value > 8
    ]
    if opportunity:
        joined = "、".join(item.name for item in opportunity[:5])
        advice.append(
            Advice(
                level="neutral",
                title="强势主线等待企稳观察",
                body=f"{joined} 近 1 个月表现靠前，若短线回撤未破坏趋势，可作为后续观察池。",
            )
        )

    if not advice:
        advice.append(
            Advice(
                level="neutral",
                title="没有明确高胜率信号",
                body="当前榜单和风险指标未形成一致信号，建议保持观察，等待趋势和市场宽度进一步确认。",
            )
        )

    return advice[:6]


def _build_heat_advice(sector_heat: list[SectorHeatItem]) -> list[Advice]:
    ok_items = [item for item in sector_heat if item.asset_type == "industry" and item.data_status == "ok"]
    if not ok_items:
        return []

    hot = sorted(ok_items, key=lambda item: item.heat_score or 0, reverse=True)[:3]
    cooling_pullback = [
        item
        for item in ok_items
        if (item.return_1m or 0) > 0 and (item.return_7d or 0) < 0 and (item.heat_score or 0) >= 55
    ][:3]
    warming = [
        item
        for item in ok_items
        if (item.return_7d or 0) > 0 and (item.return_1m or 0) <= 0 and (item.heat_score or 0) >= 45
    ][:3]

    result: list[Advice] = []
    if hot:
        names = "、".join(item.name for item in hot)
        result.append(
            Advice(
                level="positive",
                title="板块热度延续",
                body=f"{names} 的 7 日与 1 个月热度排名靠前，说明趋势仍有延续性；若当日涨幅过大，仍应等待分歧后的承接。",
            )
        )
    if cooling_pullback:
        names = "、".join(item.name for item in cooling_pullback)
        result.append(
            Advice(
                level="neutral",
                title="强势主线回调观察",
                body=f"{names} 近 1 个月仍强但近 7 日回落，可纳入观察池，重点看回撤后是否缩量企稳。",
            )
        )
    if warming:
        names = "、".join(item.name for item in warming)
        result.append(
            Advice(
                level="neutral",
                title="新方向初步升温",
                body=f"{names} 近 7 日开始转强但 1 个月趋势尚未完全确认，适合观察持续性，不宜只凭单日表现追入。",
            )
        )
    return result


def _build_news_advice(items: list[RankingItem], news: list[dict]) -> list[Advice]:
    if not news:
        return []

    top_names = [
        item.name
        for item in items
        if item.rank_type == "gain" and item.window == "1d" and item.rank <= 12
    ]
    news_text = " ".join(f"{item.get('title', '')} {item.get('summary', '')}" for item in news[:20])
    matched_themes: list[str] = []

    for theme, keywords in NEWS_KEYWORDS.items():
        keyword_hit = any(keyword in news_text for keyword in keywords)
        market_hit = any(any(keyword in name for keyword in keywords) for name in top_names)
        if keyword_hit and market_hit:
            matched_themes.append(theme)

    if not matched_themes:
        return [
            Advice(
                level="neutral",
                title="新闻与榜单暂未形成强共振",
                body="当前热点新闻与涨幅榜方向匹配度不高，短线交易更应以量价确认和风险控制为主。",
            )
        ]

    themes = "、".join(matched_themes[:3])
    related_news = _pick_related_news(news, NEWS_KEYWORDS[matched_themes[0]])
    suffix = f" 相关新闻：{related_news}" if related_news else ""
    return [
        Advice(
            level="positive",
            title="新闻热点与盘面方向共振",
            body=f"{themes} 同时出现在热点新闻和当日强势榜中，可作为后续观察主线，但若当日涨幅已高，仍应等待分歧后的承接确认。{suffix}",
        )
    ]


def _pick_related_news(news: list[dict], keywords: tuple[str, ...]) -> str:
    for item in news:
        text = f"{item.get('title', '')} {item.get('summary', '')}"
        if any(keyword in text for keyword in keywords):
            return str(item.get("title", ""))[:48]
    return ""


def _to_float(value: float | str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
