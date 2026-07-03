from __future__ import annotations

import json
from datetime import date

from sqlalchemy.orm import Session

from app.config import settings
from app.schemas import QuoteRecord
from app.services.calculations import RankingItem, calculate_rankings
from app.services.calendar import format_local_datetime, is_probable_trading_day, today_in_timezone
from app.services.data_sources import AkshareClient, MarketSnapshot
from app.services.llm_client import LlmClient
from app.services.recommender import build_advice
from app.services.report_summary import ASSET_TYPES, build_report_summary
from app.services.sector_heat import SectorHeatItem, calculate_sector_heat
from app.services.settings_store import get_llm_config
from app.services.storage import (
    create_job_run,
    finish_job_run,
    load_metrics_dict,
    load_previous_metric,
    load_quotes,
    replace_rankings,
    replace_sector_heat,
    upsert_metrics,
    upsert_quotes,
    upsert_report,
)


class ReportService:
    def __init__(self, session: Session, client: AkshareClient | None = None) -> None:
        self.session = session
        self._client = client

    @property
    def client(self) -> AkshareClient:
        if self._client is None:
            self._client = AkshareClient()
        return self._client

    def run_close_report(self, as_of: date | None = None, run=None) -> dict:
        trade_date = as_of or today_in_timezone(settings.timezone)
        return self._run_job("close", trade_date, lambda: self.client.fetch_close_snapshot(trade_date), run=run)

    def run_fund_refresh(self, as_of: date | None = None, run=None) -> dict:
        trade_date = as_of or today_in_timezone(settings.timezone)
        return self._run_job("fund_refresh", trade_date, lambda: self.client.fetch_fund_refresh(trade_date), run=run)

    def run_historical_report(self, as_of: date, run=None) -> dict:
        run = run or create_job_run(self.session, "historical_report")
        self.session.flush()

        if not is_probable_trading_day(as_of):
            message = f"{as_of.isoformat()} 不是交易日，已跳过"
            finish_job_run(self.session, run, "skipped", message)
            return {"status": "skipped", "message": message}

        try:
            generated = self.generate_report(as_of)
            message = f"{generated['message']}；历史日报基于本地已保存行情重建"
            finish_job_run(self.session, run, generated["status"], message)
            generated["message"] = message
            return generated
        except Exception as exc:
            finish_job_run(self.session, run, "failed", str(exc))
            raise

    def run_sector_backfill(self, as_of: date | None = None, run=None) -> dict:
        trade_date = as_of or today_in_timezone(settings.timezone)
        run = run or create_job_run(self.session, "sector_backfill")
        self.session.flush()
        try:
            snapshot = self.client.fetch_sector_backfill(trade_date)
            upsert_quotes(self.session, snapshot.quotes)
            self.session.flush()
            generated = self.generate_report(trade_date, snapshot.data_gaps, snapshot.one_month_overrides, snapshot.news)
            message = f"{generated['message']}；回填行业历史 {len(snapshot.quotes)} 条"
            finish_job_run(self.session, run, generated["status"], message)
            generated["message"] = message
            generated["backfilled_quotes"] = len(snapshot.quotes)
            return generated
        except Exception as exc:
            finish_job_run(self.session, run, "failed", str(exc))
            raise

    def _run_job(self, job_name: str, trade_date: date, fetch_snapshot, run=None) -> dict:
        run = run or create_job_run(self.session, job_name)
        self.session.flush()

        if not is_probable_trading_day(trade_date):
            message = f"{trade_date.isoformat()} 不是交易日，已跳过"
            finish_job_run(self.session, run, "skipped", message)
            return {"status": "skipped", "message": message}

        try:
            snapshot: MarketSnapshot = fetch_snapshot()
            upsert_quotes(self.session, snapshot.quotes)
            upsert_metrics(self.session, snapshot.metrics)
            self.session.flush()
            generated = self.generate_report(trade_date, snapshot.data_gaps, snapshot.one_month_overrides, snapshot.news)
            finish_job_run(self.session, run, generated["status"], generated["message"])
            return generated
        except Exception as exc:
            finish_job_run(self.session, run, "failed", str(exc))
            raise

    def generate_report(
        self,
        trade_date: date,
        data_gaps: list[str] | None = None,
        one_month_overrides: dict[str, dict[str, float]] | None = None,
        news: list[dict] | None = None,
    ) -> dict:
        quotes = load_quotes(self.session, ASSET_TYPES, trade_date, lookback_days=220)
        current_quotes = [quote for quote in quotes if quote.trade_date == trade_date]
        data_gaps_for_summary = list(data_gaps or [])
        if not current_quotes:
            data_gaps_for_summary.append("当天没有可用行情数据")
        all_rankings: list[RankingItem] = []
        overrides = one_month_overrides or {}

        for asset_type in ASSET_TYPES:
            asset_quotes = [quote for quote in quotes if quote.asset_type == asset_type]
            if asset_type == "fund":
                fund_overrides = _fund_month_overrides(asset_quotes, trade_date)
            else:
                fund_overrides = overrides.get(asset_type, {})
            all_rankings.extend(
                calculate_rankings(
                    quotes,
                    asset_type,
                    trade_date,
                    limit=settings.ranking_limit,
                    one_month_overrides=fund_overrides,
                )
            )

        replace_rankings(self.session, trade_date, all_rankings)
        metrics = load_metrics_dict(self.session, trade_date)
        _attach_total_amount_delta(self.session, trade_date, metrics)
        sector_heat = _calculate_all_sector_heat(quotes, trade_date)
        replace_sector_heat(self.session, trade_date, sector_heat)
        advice = build_advice(all_rankings, metrics, news or [], sector_heat)
        summary = build_report_summary(trade_date, quotes, all_rankings, metrics, data_gaps_for_summary, news or [], sector_heat)
        summary["ai_advice"] = _build_ai_advice(self.session, trade_date, summary, advice)
        completeness = "partial" if summary["warnings"] else "complete"
        if not current_quotes:
            completeness = "empty"

        upsert_report(self.session, trade_date, summary, advice, completeness)
        return {
            "status": "success" if completeness != "empty" else "partial",
            "message": f"{trade_date.isoformat()} 报表已生成，完整性: {completeness}",
            "trade_date": trade_date.isoformat(),
            "completeness": completeness,
            "data_gaps": data_gaps or [],
        }


def _fund_month_overrides(quotes: list[QuoteRecord], trade_date: date) -> dict[str, float]:
    result: dict[str, float] = {}
    for quote in quotes:
        if quote.trade_date != trade_date:
            continue
        value = quote.extra.get("last_month")
        if value is not None:
            try:
                result[quote.code] = float(value)
            except (TypeError, ValueError):
                pass
    return result


def _calculate_all_sector_heat(quotes: list[QuoteRecord], trade_date: date) -> list[SectorHeatItem]:
    items: list[SectorHeatItem] = []
    for asset_type in ("industry", "concept"):
        items.extend(calculate_sector_heat(quotes, asset_type, trade_date))
    return items


def _attach_total_amount_delta(session: Session, trade_date: date, metrics: dict[str, float | str | None]) -> None:
    current_amount = _to_float(metrics.get("total_amount"))
    if current_amount is None:
        return
    previous = load_previous_metric(session, trade_date, "total_amount")
    if previous is None:
        return
    previous_amount = _to_float(previous.value)
    if previous_amount is None:
        return
    metrics["total_amount_previous"] = previous_amount
    metrics["total_amount_delta"] = current_amount - previous_amount
    metrics["total_amount_previous_date"] = previous.trade_date.isoformat()


def _build_ai_advice(session: Session, trade_date: date, summary: dict, advice: list) -> dict:
    config = get_llm_config(session)
    client = LlmClient(
        base_url=config.get("base_url"),
        api_key=config.get("api_key"),
        model=config.get("model"),
        load_saved=False,
    )
    if not client.configured:
        return {
            "status": "not_configured",
            "content": "",
            "message": "未配置大模型，前往设置页填写模型地址、模型名和密钥后，重新生成日报即可生成AI建议。",
        }

    context = {
        "trade_date": trade_date.isoformat(),
        "market_metrics": _compact_metrics(summary.get("metrics", {})),
        "industry_gainers": _compact_rows(summary.get("top", {}).get("industry", []), limit=8),
        "industry_losers": _compact_rows(summary.get("bottom", {}).get("industry", []), limit=8),
        "fund_gainers": _compact_rows(summary.get("top", {}).get("fund", []), limit=5),
        "etf_gainers": _compact_rows(summary.get("top", {}).get("etf", []), limit=5),
        "sector_heat_top": _compact_heat_rows(summary.get("sector_heat", {}).get("top", []), limit=5),
        "rule_advice": [
            {"level": item.level, "title": item.title, "body": item.body}
            for item in advice[:6]
        ],
        "news": [
            {
                "title": str(item.get("title", ""))[:120],
                "published_at": item.get("published_at"),
            }
            for item in (summary.get("news") or [])[:8]
        ],
        "required_output": "3到5条中文要点；仅供个人研究记录；不得给出硬性买卖指令或收益承诺。",
    }
    try:
        content = client.summarize_market_report(context)
    except Exception as exc:  # defensive: never let LLM failure break report generation
        return {"status": "failed", "content": "", "message": f"AI建议生成失败: {exc}"}

    if not content:
        return {
            "status": "failed",
            "content": "",
            "message": client.last_error or "AI建议生成失败：模型未返回内容。",
        }
    return {
        "status": "success",
        "content": content,
        "message": "",
        "model": client.model,
    }


def _compact_rows(rows: list[dict], limit: int) -> list[dict]:
    result: list[dict] = []
    for row in rows[:limit]:
        result.append(
            {
                "rank": row.get("rank"),
                "name": row.get("name"),
                "code": row.get("code"),
                "change_pct": _round(row.get("value")),
            }
        )
    return result


def _compact_heat_rows(rows: list[dict], limit: int) -> list[dict]:
    result: list[dict] = []
    for row in rows[:limit]:
        result.append(
            {
                "rank": row.get("heat_rank") or row.get("rank"),
                "name": row.get("name"),
                "heat_score": _round(row.get("heat_score")),
                "return_7d": _round(row.get("return_7d")),
                "return_1m": _round(row.get("return_1m")),
            }
        )
    return result


def _compact_metrics(metrics: dict) -> dict:
    keys = (
        "advancers",
        "decliners",
        "limit_up",
        "limit_down",
        "total_amount",
        "total_amount_delta",
        "total_amount_previous_date",
        "advance_decline_ratio",
        "market_congestion",
        "equity_bond_spread",
    )
    return {key: _round(metrics.get(key)) for key in keys if key in metrics}


def _round(value):
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return value


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def report_to_dict(report) -> dict:
    return {
        "trade_date": report.trade_date.isoformat(),
        "title": report.title,
        "summary": json.loads(report.summary_json or "{}"),
        "advice": json.loads(report.advice_json or "[]"),
        "completeness": report.completeness,
        "generated_at": format_local_datetime(report.generated_at),
    }
