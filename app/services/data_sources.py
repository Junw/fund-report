from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

import json
import re
from datetime import datetime

import pandas as pd

from app.schemas import MetricRecord, QuoteRecord
from app.services.fund_filters import is_domestic_etf, is_equity_fund


@dataclass
class MarketSnapshot:
    trade_date: date
    quotes: list[QuoteRecord] = field(default_factory=list)
    metrics: list[MetricRecord] = field(default_factory=list)
    news: list[dict[str, Any]] = field(default_factory=list)
    one_month_overrides: dict[str, dict[str, float]] = field(default_factory=dict)
    data_gaps: list[str] = field(default_factory=list)


class AkshareClient:
    def __init__(self, akshare_module: Any | None = None) -> None:
        if akshare_module is None:
            import akshare as akshare_module

        self.ak = akshare_module

    def fetch_close_snapshot(self, trade_date: date) -> MarketSnapshot:
        snapshot = MarketSnapshot(trade_date=trade_date)
        self._safe_extend(snapshot, "A 股实时行情", lambda: self._fetch_stock_spot(trade_date))
        self._safe_extend(snapshot, "ETF 实时行情", lambda: self._fetch_etf_spot(trade_date))
        self._safe_extend(snapshot, "行业板块", lambda: self._fetch_board_spot(trade_date, "industry"))
        self._safe_extend(snapshot, "概念板块", lambda: self._fetch_board_spot(trade_date, "concept"))
        self._safe_metrics(snapshot, "市场指标", lambda: self._fetch_market_metrics(trade_date, snapshot.quotes))
        self._safe_news(snapshot, "热点新闻", self._fetch_market_news)
        self._safe_overrides(snapshot, "ETF 近 1 月排行", lambda: self._fetch_exchange_fund_month_returns())
        return snapshot

    def fetch_fund_refresh(self, trade_date: date) -> MarketSnapshot:
        snapshot = MarketSnapshot(trade_date=trade_date)
        self._safe_extend(snapshot, "开放式权益基金排行", lambda: self._fetch_open_funds(trade_date))
        self._safe_news(snapshot, "热点新闻", self._fetch_market_news)
        return snapshot

    def fetch_sector_backfill(self, trade_date: date) -> MarketSnapshot:
        snapshot = MarketSnapshot(trade_date=trade_date)
        self._safe_extend(snapshot, "行业板块历史回填", lambda: self._fetch_industry_history_ths(trade_date))
        self._safe_news(snapshot, "热点新闻", self._fetch_market_news)
        return snapshot

    def fetch_fund_history(self, code: str, name: str, asset_type: str, trade_date: date) -> list[QuoteRecord]:
        start_date = (trade_date - timedelta(days=1100)).strftime("%Y%m%d")
        end_date = trade_date.strftime("%Y%m%d")
        if asset_type == "etf":
            df = self.ak.fund_etf_hist_em(symbol=code, period="daily", start_date=start_date, end_date=end_date, adjust="")
            date_names = ("日期", "净值日期")
            close_names = ("收盘", "单位净值", "累计净值")
            change_names = ("涨跌幅", "日增长率")
        else:
            df = self.ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
            date_names = ("净值日期", "日期", "x")
            close_names = ("单位净值", "y", "净值")
            change_names = ("日增长率", "涨跌幅")
        rows: list[QuoteRecord] = []
        previous: float | None = None
        for _, row in df.iterrows():
            row_date = _parse_date(_first_str(row, date_names))
            close = _first_number(row, close_names)
            if row_date is None or close is None or row_date > trade_date or row_date < trade_date - timedelta(days=1100):
                continue
            change = _first_number(row, change_names)
            if change is None and previous:
                change = (close / previous - 1) * 100
            rows.append(QuoteRecord(row_date, asset_type, code, name, close=close, change_pct=change, category="ETF" if asset_type == "etf" else "权益基金", extra={"history_backfill": True}))
            previous = close
        return rows

    def fetch_fund_metadata(self, code: str, trade_date: date) -> dict[str, Any]:
        df = self.ak.fund_individual_basic_info_xq(symbol=code)
        values: dict[str, str] = {}
        for _, row in df.iterrows():
            key = _first_str(row, ("item", "项目", "字段", "名称"))
            value = _first_str(row, ("value", "值", "数据", "内容"))
            if key:
                values[key] = value
        return {
            "code": code,
            "fund_size": _text_number(_pick_mapping(values, ("基金规模", "最新规模", "资产规模"))),
            "inception_date": _parse_date(_pick_mapping(values, ("成立时间", "成立日期"))),
            "manager_name": _pick_mapping(values, ("基金经理", "现任基金经理")) or None,
            "manager_start_date": _parse_date(_pick_mapping(values, ("任职时间", "经理任职日期"))),
            "management_fee": _text_number(_pick_mapping(values, ("管理费率", "管理费"))),
            "tracking_index": _pick_mapping(values, ("跟踪标的", "业绩比较基准", "跟踪指数")) or None,
            "metadata_json": json.dumps(values, ensure_ascii=False),
            "source": "akshare",
            "data_date": trade_date,
            "updated_at": datetime.utcnow(),
        }

    def fetch_fund_holdings(self, code: str, trade_date: date) -> tuple[date, list[dict[str, Any]]]:
        df = self.ak.fund_portfolio_hold_em(symbol=code, date=str(trade_date.year))
        report_date = trade_date
        rows: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            stock_code = _first_str(row, ("股票代码", "代码"))
            stock_name = _first_str(row, ("股票名称", "名称"))
            if not stock_code or not stock_name:
                continue
            quarter = _first_str(row, ("季度", "报告期"))
            parsed_report_date = _quarter_end(quarter)
            if parsed_report_date and parsed_report_date <= trade_date:
                report_date = max(report_date if report_date != trade_date else parsed_report_date, parsed_report_date)
            rows.append({
                "fund_code": code,
                "report_date": report_date,
                "stock_code": stock_code,
                "stock_name": stock_name,
                "weight": _first_number(row, ("占净值比例", "持仓占比", "占净值比例（%）")),
                "industry": _first_str(row, ("行业", "所属行业")) or None,
                "source": "akshare",
            })
        for row in rows:
            row["report_date"] = report_date
        return report_date, rows
    def _safe_extend(self, snapshot: MarketSnapshot, label: str, fn: Callable[[], list[QuoteRecord]]) -> None:
        try:
            rows = fn()
            if not rows:
                snapshot.data_gaps.append(f"{label}返回为空")
            snapshot.quotes.extend(rows)
        except Exception as exc:
            snapshot.data_gaps.append(f"{label}失败: {exc}")

    def _safe_metrics(self, snapshot: MarketSnapshot, label: str, fn: Callable[[], list[MetricRecord]]) -> None:
        try:
            snapshot.metrics.extend(fn())
        except Exception as exc:
            snapshot.data_gaps.append(f"{label}失败: {exc}")

    def _safe_overrides(self, snapshot: MarketSnapshot, label: str, fn: Callable[[], dict[str, float]]) -> None:
        try:
            values = fn()
            if values:
                snapshot.one_month_overrides["etf"] = values
        except Exception as exc:
            snapshot.data_gaps.append(f"{label}失败: {exc}")

    def _safe_news(self, snapshot: MarketSnapshot, label: str, fn: Callable[[], list[dict[str, Any]]]) -> None:
        try:
            snapshot.news = fn()
            if not snapshot.news:
                snapshot.data_gaps.append(f"{label}返回为空")
        except Exception as exc:
            snapshot.data_gaps.append(f"{label}失败: {exc}")

    def _fetch_stock_spot(self, trade_date: date) -> list[QuoteRecord]:
        try:
            df = self.ak.stock_zh_a_spot_em()
            source = "eastmoney"
        except Exception:
            df = self.ak.stock_zh_a_spot()
            source = "sina"
        return [
            QuoteRecord(
                trade_date=trade_date,
                asset_type="stock",
                code=_normalize_stock_code(_str(row, "代码")),
                name=_str(row, "名称"),
                close=_number(row, "最新价"),
                change_pct=_number(row, "涨跌幅"),
                turnover_rate=_number(row, "换手率"),
                volume=_number(row, "成交量"),
                amount=_number(row, "成交额"),
                extra={"source": source, "market": _str(row, "市场"), "amplitude": _number(row, "振幅")},
            )
            for _, row in df.iterrows()
            if _str(row, "代码") and _str(row, "名称")
        ]

    def _fetch_etf_spot(self, trade_date: date) -> list[QuoteRecord]:
        df = self.ak.fund_etf_spot_em()
        rows: list[QuoteRecord] = []
        for _, row in df.iterrows():
            name = _str(row, "名称")
            if not is_domestic_etf(name):
                continue
            rows.append(
                QuoteRecord(
                    trade_date=trade_date,
                    asset_type="etf",
                    code=_str(row, "代码"),
                    name=name,
                    close=_number(row, "最新价"),
                    change_pct=_number(row, "涨跌幅"),
                    turnover_rate=_number(row, "换手率"),
                    volume=_number(row, "成交量"),
                    amount=_number(row, "成交额"),
                    category="ETF",
                    extra={"premium": _number(row, "溢价率"), "amplitude": _number(row, "振幅")},
                )
            )
        return rows

    def _fetch_open_funds(self, trade_date: date) -> list[QuoteRecord]:
        rows: list[QuoteRecord] = []
        month_returns: dict[str, float] = {}
        for fund_type in ("股票型", "混合型", "指数型"):
            df = self.ak.fund_open_fund_rank_em(symbol=fund_type)
            for _, row in df.iterrows():
                name = _first_str(row, ("基金简称", "简称", "名称"))
                code = _first_str(row, ("基金代码", "代码"))
                if not code or not name or not is_equity_fund(name, fund_type):
                    continue
                month_value = _first_number(row, ("近1月", "近一月", "近1月涨幅"))
                if month_value is not None:
                    month_returns[code] = month_value
                rows.append(
                    QuoteRecord(
                        trade_date=trade_date,
                        asset_type="fund",
                        code=code,
                        name=name,
                        close=_first_number(row, ("单位净值", "最新净值")),
                        change_pct=_first_number(row, ("日增长率", "日涨幅", "近1日")),
                        category=fund_type,
                        extra={
                            "last_week": _first_number(row, ("近1周", "近一周")),
                            "last_month": month_value,
                            "last_three_months": _first_number(row, ("近3月", "近三月")),
                            "last_year": _first_number(row, ("近1年", "近一年")),
                        },
                    )
                )
        return rows

    def _fetch_board_spot(self, trade_date: date, asset_type: str) -> list[QuoteRecord]:
        if asset_type == "concept":
            return self._fetch_concept_spot_multi_source(trade_date)

        try:
            df = self.ak.stock_board_industry_name_em()
        except Exception:
            return self._fetch_industry_spot_ths(trade_date)
        rows: list[QuoteRecord] = []
        for _, row in df.iterrows():
            code = _first_str(row, ("板块代码", "代码"))
            name = _first_str(row, ("板块名称", "名称"))
            if not code or not name:
                continue
            rows.append(
                QuoteRecord(
                    trade_date=trade_date,
                    asset_type=asset_type,
                    code=code,
                    name=name,
                    close=_first_number(row, ("最新价", "涨跌幅")),
                    change_pct=_first_number(row, ("涨跌幅",)),
                    turnover_rate=_first_number(row, ("换手率",)),
                    amount=_first_number(row, ("成交额",)),
                    category="行业",
                    extra={"leader": _first_str(row, ("领涨股票", "领涨股")), "source": "eastmoney_board"},
                )
            )
        return rows

    def _fetch_concept_spot_multi_source(self, trade_date: date) -> list[QuoteRecord]:
        rows_by_key: dict[str, QuoteRecord] = {}
        errors: list[str] = []
        ths_names = self._fetch_ths_concept_name_map()

        try:
            for row in self._fetch_concept_change_em(trade_date, ths_names):
                key = _concept_key(row.name)
                existing = rows_by_key.get(key)
                if existing is None:
                    rows_by_key[key] = row
                    continue
                extra = dict(existing.extra)
                extra["cross_source"] = row.extra.get("source")
                extra["cross_change_pct"] = row.change_pct
                extra["cross_delta_pct"] = (
                    round((existing.change_pct or 0) - (row.change_pct or 0), 4)
                    if existing.change_pct is not None and row.change_pct is not None
                    else None
                )
                extra["verified_by_ths"] = extra.get("verified_by_ths") or row.extra.get("verified_by_ths")
                rows_by_key[key] = QuoteRecord(
                    trade_date=existing.trade_date,
                    asset_type=existing.asset_type,
                    code=existing.code,
                    name=existing.name,
                    close=existing.close,
                    change_pct=existing.change_pct,
                    category=existing.category,
                    turnover=existing.turnover,
                    turnover_rate=existing.turnover_rate,
                    volume=existing.volume,
                    amount=existing.amount,
                    extra=extra,
                )
        except Exception as exc:
            errors.append(f"eastmoney_board_change: {exc}")

        rows = list(rows_by_key.values())
        if not rows:
            raise RuntimeError("；".join(errors) if errors else "概念板块多源均返回为空")
        return rows

    def _fetch_concept_spot_em(self, trade_date: date) -> list[QuoteRecord]:
        df = self.ak.stock_board_concept_name_em()
        rows: list[QuoteRecord] = []
        for _, row in df.iterrows():
            code = _first_str(row, ("板块代码", "代码"))
            name = _first_str(row, ("板块名称", "名称"))
            if not code or not name:
                continue
            rows.append(
                QuoteRecord(
                    trade_date=trade_date,
                    asset_type="concept",
                    code=code,
                    name=name,
                    close=_first_number(row, ("最新价", "涨跌幅")),
                    change_pct=_first_number(row, ("涨跌幅",)),
                    turnover_rate=_first_number(row, ("换手率",)),
                    amount=_first_number(row, ("成交额",)),
                    category="概念",
                    extra={"leader": _first_str(row, ("领涨股票", "领涨股")), "source": "eastmoney_concept_spot"},
                )
            )
        return rows

    def _fetch_concept_change_em(self, trade_date: date, ths_names: dict[str, str]) -> list[QuoteRecord]:
        df = self.ak.stock_board_change_em()
        rows: list[QuoteRecord] = []
        for _, row in df.iterrows():
            name = _first_str(row, ("板块名称", "名称"))
            if not name:
                continue
            key = _concept_key(name)
            ths_name = ths_names.get(key) or ths_names.get(_concept_alias_key(name))
            rows.append(
                QuoteRecord(
                    trade_date=trade_date,
                    asset_type="concept",
                    code=f"emchg:{key}",
                    name=name,
                    close=None,
                    change_pct=_first_number(row, ("涨跌幅",)),
                    amount=None,
                    category="概念",
                    extra={
                        "source": "eastmoney_board_change",
                        "net_inflow": _first_number(row, ("主力净流入",)),
                        "change_count": _first_number(row, ("板块异动总次数",)),
                        "leader_code": _first_str(row, ("板块异动最频繁个股及所属类型-股票代码",)),
                        "leader": _first_str(row, ("板块异动最频繁个股及所属类型-股票名称",)),
                        "leader_direction": _first_str(row, ("板块异动最频繁个股及所属类型-买卖方向",)),
                        "verified_by_ths": bool(ths_name),
                        "ths_name": ths_name,
                    },
                )
            )
        return rows

    def _fetch_ths_concept_name_map(self) -> dict[str, str]:
        try:
            df = self.ak.stock_board_concept_name_ths()
        except Exception:
            return {}
        result: dict[str, str] = {}
        for _, row in df.iterrows():
            name = _first_str(row, ("name", "概念名称", "名称"))
            if not name:
                continue
            result[_concept_key(name)] = name
            alias = _concept_alias_key(name)
            if alias:
                result[alias] = name
        return result

    def _fetch_industry_spot_ths(self, trade_date: date) -> list[QuoteRecord]:
        df = self.ak.stock_board_industry_summary_ths()
        rows: list[QuoteRecord] = []
        for _, row in df.iterrows():
            name = _first_str(row, ("板块", "name", "名称"))
            if not name:
                continue
            rows.append(
                QuoteRecord(
                    trade_date=trade_date,
                    asset_type="industry",
                    code=f"ths:{name}",
                    name=name,
                    close=None,
                    change_pct=_first_number(row, ("涨跌幅",)),
                    volume=_first_number(row, ("总成交量",)),
                    amount=_amount_yi_to_yuan(_first_number(row, ("总成交额",))),
                    category="行业",
                    extra={
                        "source": "ths",
                        "net_inflow_yi": _first_number(row, ("净流入",)),
                        "advancers": _first_number(row, ("上涨家数",)),
                        "decliners": _first_number(row, ("下跌家数",)),
                        "leader": _first_str(row, ("领涨股",)),
                        "leader_change_pct": _first_number(row, ("领涨股-涨跌幅",)),
                    },
                )
            )
        return rows

    def _fetch_industry_history_ths(self, trade_date: date) -> list[QuoteRecord]:
        names_df = self.ak.stock_board_industry_name_ths()
        start_date = (trade_date - timedelta(days=75)).strftime("%Y%m%d")
        end_date = trade_date.strftime("%Y%m%d")
        rows: list[QuoteRecord] = []
        for _, board in names_df.iterrows():
            name = _first_str(board, ("name", "板块", "名称"))
            if not name:
                continue
            try:
                hist_df = self.ak.stock_board_industry_index_ths(symbol=name, start_date=start_date, end_date=end_date)
            except Exception:
                continue
            previous_close: float | None = None
            for _, row in hist_df.tail(35).iterrows():
                row_date = _parse_date(_first_str(row, ("日期", "date")))
                close = _first_number(row, ("收盘价", "收盘", "close"))
                if row_date is None or close is None or row_date > trade_date:
                    continue
                rows.append(
                    QuoteRecord(
                        trade_date=row_date,
                        asset_type="industry",
                        code=f"ths:{name}",
                        name=name,
                        close=close,
                        change_pct=((close / previous_close - 1) * 100) if previous_close else None,
                        volume=_first_number(row, ("成交量", "volume")),
                        amount=_first_number(row, ("成交额", "amount")),
                        category="行业",
                        extra={"source": "ths_history"},
                    )
                )
                previous_close = close
        return rows

    def _fetch_exchange_fund_month_returns(self) -> dict[str, float]:
        df = self.ak.fund_exchange_rank_em()
        result: dict[str, float] = {}
        for _, row in df.iterrows():
            code = _first_str(row, ("基金代码", "代码"))
            name = _first_str(row, ("基金简称", "简称", "名称"))
            value = _first_number(row, ("近1月", "近一月", "近1月涨幅"))
            if code and name and is_domestic_etf(name) and value is not None:
                result[code] = value
        return result

    def _fetch_market_metrics(self, trade_date: date, quotes: list[QuoteRecord]) -> list[MetricRecord]:
        stock_quotes = [quote for quote in quotes if quote.asset_type == "stock"]
        metrics = _metrics_from_stock_quotes(trade_date, stock_quotes)

        congestion = _last_value(self.ak.stock_a_congestion_lg(), ("close", "拥挤度", "value", "数值"))
        if congestion is not None:
            metrics.append(MetricRecord(trade_date, "market_congestion", value=congestion))

        spread = _last_value(self.ak.stock_ebs_lg(), ("close", "股债利差", "value", "数值"))
        if spread is not None:
            metrics.append(MetricRecord(trade_date, "equity_bond_spread", value=spread))

        dividend = _last_value(self.ak.stock_a_gxl_lg(symbol="上证A股"), ("close", "股息率", "value", "数值"))
        if dividend is not None:
            metrics.append(MetricRecord(trade_date, "dividend_yield", value=dividend))
        return metrics

    def _fetch_market_news(self) -> list[dict[str, Any]]:
        df = self.ak.stock_info_global_em()
        rows: list[dict[str, Any]] = []
        for _, row in df.head(30).iterrows():
            title = _first_str(row, ("标题", "title"))
            if not title:
                continue
            rows.append(
                {
                    "title": title,
                    "summary": _first_str(row, ("摘要", "summary")),
                    "published_at": _first_str(row, ("发布时间", "time", "date")),
                    "url": _first_str(row, ("链接", "url")),
                    "source": "eastmoney",
                }
            )
        return rows


def _metrics_from_stock_quotes(trade_date: date, quotes: list[QuoteRecord]) -> list[MetricRecord]:
    up = sum(1 for quote in quotes if (quote.change_pct or 0) > 0)
    down = sum(1 for quote in quotes if (quote.change_pct or 0) < 0)
    flat = len(quotes) - up - down
    limit_up = sum(1 for quote in quotes if (quote.change_pct or 0) >= 9.8)
    limit_down = sum(1 for quote in quotes if (quote.change_pct or 0) <= -9.8)
    total_amount = sum(quote.amount or 0 for quote in quotes)
    ratio = up / down if down else float(up)
    return [
        MetricRecord(trade_date, "advancers", value=float(up)),
        MetricRecord(trade_date, "decliners", value=float(down)),
        MetricRecord(trade_date, "flat", value=float(flat)),
        MetricRecord(trade_date, "limit_up", value=float(limit_up)),
        MetricRecord(trade_date, "limit_down", value=float(limit_down)),
        MetricRecord(trade_date, "total_amount", value=total_amount),
        MetricRecord(trade_date, "advance_decline_ratio", value=ratio),
    ]


def _first_str(row: pd.Series, names: tuple[str, ...]) -> str:
    for name in names:
        value = _str(row, name)
        if value:
            return value
    return ""


def _first_number(row: pd.Series, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = _number(row, name)
        if value is not None:
            return value
    return None


def _str(row: pd.Series, name: str) -> str:
    if name not in row:
        return ""
    value = row[name]
    if pd.isna(value):
        return ""
    return str(value).strip()


def _number(row: pd.Series, name: str) -> float | None:
    if name not in row:
        return None
    value = row[name]
    if pd.isna(value):
        return None
    if isinstance(value, str):
        value = value.replace("%", "").replace(",", "").strip()
        if not value or value in {"-", "--"}:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _last_value(df: pd.DataFrame, names: tuple[str, ...]) -> float | None:
    if df is None or df.empty:
        return None
    row = df.iloc[-1]
    return _first_number(row, names)


def _normalize_stock_code(code: str) -> str:
    return code.removeprefix("sh").removeprefix("sz").removeprefix("bj")


def _concept_key(name: str) -> str:
    text = re.sub(r"\s+", "", name or "").upper()
    text = text.replace("（", "(").replace("）", ")")
    for suffix in ("概念", "板块", "题材"):
        text = text.removesuffix(suffix.upper())
    return text


def _concept_alias_key(name: str) -> str:
    text = _concept_key(name)
    if "CPO" in text or "共封装光学" in text:
        return "CPO"
    if "光通信" in text:
        return "光通信"
    if "光模块" in text:
        return "光模块"
    return text


def _amount_yi_to_yuan(value: float | None) -> float | None:
    if value is None:
        return None
    return value * 100_000_000


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return pd.to_datetime(value).date()
    except Exception:
        return None


def _pick_mapping(values: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        for key, value in values.items():
            if name in key and value:
                return value
    return ""


def _text_number(value: str) -> float | None:
    if not value:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
    if not match:
        return None
    number = float(match.group(0))
    if "亿" in value:
        number *= 100000000
    elif "万" in value:
        number *= 10000
    return number


def _quarter_end(value: str) -> date | None:
    match = re.search(r"(20\d{2}).*?([1-4])", value or "")
    if not match:
        return None
    year, quarter = int(match.group(1)), int(match.group(2))
    return date(year, quarter * 3, 31 if quarter in (1, 4) else 30)
