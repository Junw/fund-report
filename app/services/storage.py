from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Iterable

from sqlalchemy import delete, desc, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import (
    Asset, BackfillProgress, BacktestRun, DailyQuote, FundFeature, FundHolding,
    FundMetadata, FundSectorExposure, JobRun, MarketMetric, PortfolioHolding, Ranking,
    Report, SectorHeat, SignalEvent, SignalModelVersion, SignalScore, WatchlistItem,
)
from app.schemas import MetricRecord, QuoteRecord
from app.services.calculations import RankingItem
from app.services.fund_filters import fund_family_key, fund_share_class, is_domestic_etf, is_equity_fund, primary_fund_codes
from app.services.recommender import Advice
from app.services.sector_heat import SectorHeatItem


def upsert_quotes(session: Session, quotes: Iterable[QuoteRecord]) -> None:
    records = list(quotes)
    if not records:
        return
    now = datetime.utcnow()
    primary_codes = primary_fund_codes([(row.code, row.name) for row in records if row.asset_type == "fund"])
    asset_payloads: dict[tuple[str, str], dict] = {}
    quote_payloads: dict[tuple[date, str, str], dict] = {}
    for quote in records:
        is_fund = quote.asset_type == "fund"
        asset_payloads[(quote.asset_type, quote.code)] = {
            "asset_type": quote.asset_type,
            "code": quote.code,
            "name": quote.name,
            "category": quote.category,
            "family_key": fund_family_key(quote.name) if is_fund else None,
            "share_class": fund_share_class(quote.name) if is_fund else None,
            "is_primary": 1 if not is_fund or quote.code in primary_codes else 0,
            "source": "akshare",
            "updated_at": now,
        }
        quote_payloads[(quote.trade_date, quote.asset_type, quote.code)] = {
            "trade_date": quote.trade_date,
            "asset_type": quote.asset_type,
            "code": quote.code,
            "name": quote.name,
            "close": quote.close,
            "change_pct": quote.change_pct,
            "turnover": quote.turnover,
            "turnover_rate": quote.turnover_rate,
            "volume": quote.volume,
            "amount": quote.amount,
            "extra_json": json.dumps(quote.extra, ensure_ascii=False),
            "created_at": now,
        }
    for payloads in _chunks(list(asset_payloads.values()), 400):
        asset_stmt = sqlite_insert(Asset).values(payloads)
        session.execute(asset_stmt.on_conflict_do_update(
            index_elements=["asset_type", "code"],
            set_={
                "name": asset_stmt.excluded.name,
                "category": func.coalesce(asset_stmt.excluded.category, Asset.category),
                "family_key": asset_stmt.excluded.family_key,
                "share_class": asset_stmt.excluded.share_class,
                "is_primary": asset_stmt.excluded.is_primary,
                "updated_at": asset_stmt.excluded.updated_at,
            },
        ))
    for payloads in _chunks(list(quote_payloads.values()), 400):
        quote_stmt = sqlite_insert(DailyQuote).values(payloads)
        session.execute(quote_stmt.on_conflict_do_update(
            index_elements=["trade_date", "asset_type", "code"],
            set_={
                "name": quote_stmt.excluded.name,
                "close": func.coalesce(quote_stmt.excluded.close, DailyQuote.close),
                "change_pct": func.coalesce(quote_stmt.excluded.change_pct, DailyQuote.change_pct),
                "turnover": quote_stmt.excluded.turnover,
                "turnover_rate": quote_stmt.excluded.turnover_rate,
                "volume": quote_stmt.excluded.volume,
                "amount": quote_stmt.excluded.amount,
                "extra_json": quote_stmt.excluded.extra_json,
            },
        ))


def _chunks(rows: list[dict], size: int):
    for index in range(0, len(rows), size):
        yield rows[index:index + size]

def upsert_metrics(session: Session, metrics: Iterable[MetricRecord]) -> None:
    for metric in metrics:
        existing = session.execute(
            select(MarketMetric).where(
                MarketMetric.trade_date == metric.trade_date,
                MarketMetric.metric == metric.metric,
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                MarketMetric(
                    trade_date=metric.trade_date,
                    metric=metric.metric,
                    value=metric.value,
                    text_value=metric.text_value,
                    source=metric.source,
                )
            )
        else:
            existing.value = metric.value
            existing.text_value = metric.text_value
            existing.source = metric.source


def replace_rankings(session: Session, trade_date: date, rankings: Iterable[RankingItem]) -> None:
    session.execute(delete(Ranking).where(Ranking.trade_date == trade_date))
    for item in rankings:
        session.add(
            Ranking(
                trade_date=trade_date,
                asset_type=item.asset_type,
                window=item.window,
                rank_type=item.rank_type,
                rank=item.rank,
                code=item.code,
                name=item.name,
                value=item.value,
                close=item.close,
                amount=item.amount,
                note=item.note,
            )
        )


def replace_sector_heat(session: Session, trade_date: date, heat_items: Iterable[SectorHeatItem]) -> None:
    session.execute(delete(SectorHeat).where(SectorHeat.trade_date == trade_date))
    for item in heat_items:
        session.add(
            SectorHeat(
                trade_date=item.trade_date,
                asset_type=item.asset_type,
                code=item.code,
                name=item.name,
                return_7d=item.return_7d,
                return_1m=item.return_1m,
                heat_score=item.heat_score,
                heat_rank=item.heat_rank,
                heat_level=item.heat_level,
                data_status=item.data_status,
            )
        )


def get_sector_heat(
    session: Session,
    trade_date: date,
    asset_type: str = "industry",
    limit: int | None = None,
    status: str | None = None,
) -> list[SectorHeat]:
    stmt = select(SectorHeat).where(SectorHeat.trade_date == trade_date, SectorHeat.asset_type == asset_type)
    if status:
        stmt = stmt.where(SectorHeat.data_status == status)
    stmt = stmt.order_by(SectorHeat.heat_rank.is_(None), SectorHeat.heat_rank, desc(SectorHeat.heat_score))
    if limit:
        stmt = stmt.limit(limit)
    return list(session.execute(stmt).scalars())


def search_funds(session: Session, query: str, limit: int = 20) -> list[Asset]:
    keyword = f"%{query.strip()}%"
    if not query.strip():
        return []
    rows = list(
        session.execute(
            select(Asset)
            .where(
                Asset.asset_type.in_(("fund", "etf")),
                or_(Asset.code.like(keyword), Asset.name.like(keyword)),
            )
            .order_by(Asset.asset_type, Asset.code)
            .limit(limit * 5)
        ).scalars()
    )
    filtered = [
        row
        for row in rows
        if (row.asset_type == "etf" and is_domestic_etf(row.name))
        or (row.asset_type == "fund" and is_equity_fund(row.name, row.category))
    ]
    return filtered[:limit]


def list_holdings(session: Session) -> list[PortfolioHolding]:
    return list(session.execute(select(PortfolioHolding).order_by(PortfolioHolding.asset_type, PortfolioHolding.code)).scalars())


def upsert_holding(
    session: Session,
    asset_type: str,
    code: str,
    name: str,
    shares: float,
    cost_amount: float | None = None,
    return_rate: float | None = None,
    note: str | None = None,
) -> PortfolioHolding:
    now = datetime.utcnow()
    existing = session.execute(
        select(PortfolioHolding).where(PortfolioHolding.asset_type == asset_type, PortfolioHolding.code == code)
    ).scalar_one_or_none()
    if existing is None:
        existing = PortfolioHolding(
            asset_type=asset_type,
            code=code,
            name=name,
            shares=shares,
            cost_amount=cost_amount,
            return_rate=return_rate,
            note=note,
            created_at=now,
            updated_at=now,
        )
        session.add(existing)
    else:
        existing.name = name
        existing.shares = shares
        existing.cost_amount = cost_amount
        existing.return_rate = return_rate
        existing.note = note
        existing.updated_at = now
    return existing


def delete_holding(session: Session, holding_id: int) -> None:
    session.execute(delete(PortfolioHolding).where(PortfolioHolding.id == holding_id))


def latest_asset_quote(session: Session, asset_type: str, code: str) -> DailyQuote | None:
    return session.execute(
        select(DailyQuote)
        .where(DailyQuote.asset_type == asset_type, DailyQuote.code == code)
        .order_by(desc(DailyQuote.trade_date))
        .limit(1)
    ).scalar_one_or_none()


def upsert_report(
    session: Session,
    trade_date: date,
    summary: dict,
    advice: list[Advice],
    completeness: str,
) -> Report:
    payload = {
        "title": f"{trade_date.isoformat()} A股收盘日报",
        "summary_json": json.dumps(summary, ensure_ascii=False),
        "advice_json": json.dumps([item.__dict__ for item in advice], ensure_ascii=False),
        "completeness": completeness,
        "generated_at": datetime.utcnow(),
    }
    existing = session.execute(select(Report).where(Report.trade_date == trade_date)).scalar_one_or_none()
    if existing is None:
        existing = Report(trade_date=trade_date, **payload)
        session.add(existing)
    else:
        for key, value in payload.items():
            setattr(existing, key, value)
    return existing


def load_quotes(session: Session, asset_types: Iterable[str], through_date: date, lookback_days: int | None = None) -> list[QuoteRecord]:
    stmt = select(DailyQuote).where(
        DailyQuote.asset_type.in_(list(asset_types)),
        DailyQuote.trade_date <= through_date,
    )
    if lookback_days is not None:
        stmt = stmt.where(DailyQuote.trade_date >= through_date - timedelta(days=lookback_days))
    rows = session.execute(stmt).scalars()
    return [
        QuoteRecord(
            trade_date=row.trade_date,
            asset_type=row.asset_type,
            code=row.code,
            name=row.name,
            close=row.close,
            change_pct=row.change_pct,
            turnover=row.turnover,
            turnover_rate=row.turnover_rate,
            volume=row.volume,
            amount=row.amount,
            extra=json.loads(row.extra_json or "{}"),
        )
        for row in rows
    ]


def load_metrics_dict(session: Session, trade_date: date) -> dict[str, float | str | None]:
    rows = session.execute(select(MarketMetric).where(MarketMetric.trade_date == trade_date)).scalars()
    return {row.metric: row.value if row.value is not None else row.text_value for row in rows}


def load_previous_metric(session: Session, trade_date: date, metric: str) -> MarketMetric | None:
    return session.execute(
        select(MarketMetric)
        .where(
            MarketMetric.trade_date < trade_date,
            MarketMetric.metric == metric,
            MarketMetric.value.is_not(None),
        )
        .order_by(desc(MarketMetric.trade_date))
        .limit(1)
    ).scalar_one_or_none()


def list_reports(session: Session, limit: int = 30) -> list[Report]:
    return list(session.execute(select(Report).order_by(desc(Report.trade_date)).limit(limit)).scalars())


def latest_report(session: Session) -> Report | None:
    return session.execute(select(Report).order_by(desc(Report.trade_date)).limit(1)).scalar_one_or_none()


def get_report(session: Session, trade_date: date) -> Report | None:
    return session.execute(select(Report).where(Report.trade_date == trade_date)).scalar_one_or_none()


def get_rankings(
    session: Session,
    trade_date: date,
    asset_type: str,
    window: str,
    rank_type: str = "gain",
    limit: int = 30,
) -> list[Ranking]:
    return list(
        session.execute(
            select(Ranking)
            .where(
                Ranking.trade_date == trade_date,
                Ranking.asset_type == asset_type,
                Ranking.window == window,
                Ranking.rank_type == rank_type,
            )
            .order_by(Ranking.rank)
            .limit(limit)
        ).scalars()
    )


def create_job_run(session: Session, job_name: str) -> JobRun:
    run = JobRun(job_name=job_name, status="running", started_at=datetime.utcnow())
    session.add(run)
    session.flush()
    return run


def finish_job_run(session: Session, run: JobRun, status: str, message: str | None = None) -> None:
    run.status = status
    run.message = message
    run.finished_at = datetime.utcnow()


def list_job_runs(session: Session, limit: int = 50) -> list[JobRun]:
    return list(session.execute(select(JobRun).order_by(desc(JobRun.started_at)).limit(limit)).scalars())


def list_primary_fund_assets(session: Session, after_code: str | None = None, limit: int = 20) -> list[Asset]:
    stmt = select(Asset).where(Asset.asset_type.in_(("fund", "etf")), Asset.is_primary == 1)
    if after_code:
        stmt = stmt.where(Asset.code > after_code)
    return list(session.execute(stmt.order_by(Asset.code).limit(limit)).scalars())


def get_backfill_progress(session: Session, job_name: str) -> BackfillProgress:
    row = session.get(BackfillProgress, job_name)
    if row is None:
        total = session.execute(
            select(func.count()).select_from(Asset).where(Asset.asset_type.in_(("fund", "etf")), Asset.is_primary == 1)
        ).scalar_one()
        row = BackfillProgress(job_name=job_name, completed=0, total=total, state="pending", updated_at=datetime.utcnow())
        session.add(row)
        session.flush()
    return row


def save_fund_metadata(session: Session, payload: dict) -> None:
    stmt = sqlite_insert(FundMetadata).values(payload)
    session.execute(stmt.on_conflict_do_update(index_elements=["code"], set_={
        key: getattr(stmt.excluded, key) for key in payload if key != "code"
    }))


def replace_fund_holdings(session: Session, fund_code: str, report_date: date, rows: list[dict]) -> None:
    session.execute(delete(FundHolding).where(FundHolding.fund_code == fund_code, FundHolding.report_date == report_date))
    if rows:
        session.execute(sqlite_insert(FundHolding).values(rows))


def replace_signal_scores(session: Session, trade_date: date, model_version: str, rows: list[dict]) -> None:
    session.execute(delete(SignalScore).where(SignalScore.trade_date == trade_date, SignalScore.model_version == model_version))
    for payloads in _chunks(rows, 300):
        session.execute(sqlite_insert(SignalScore).values(payloads))


def list_signal_scores(
    session: Session,
    trade_date: date | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[SignalScore]:
    if trade_date is None:
        trade_date = session.execute(select(func.max(SignalScore.trade_date))).scalar_one_or_none()
    if trade_date is None:
        return []
    stmt = select(SignalScore).where(SignalScore.trade_date == trade_date)
    if status:
        stmt = stmt.where(SignalScore.status == status)
    return list(session.execute(stmt.order_by(desc(SignalScore.total_score)).limit(limit)).scalars())


def latest_signal_date(session: Session) -> date | None:
    return session.execute(select(func.max(SignalScore.trade_date))).scalar_one_or_none()


def latest_backtest(session: Session) -> BacktestRun | None:
    return session.execute(select(BacktestRun).order_by(desc(BacktestRun.id)).limit(1)).scalar_one_or_none()


def list_watchlist(session: Session) -> list[WatchlistItem]:
    return list(session.execute(select(WatchlistItem).order_by(desc(WatchlistItem.updated_at))).scalars())


def upsert_watchlist(session: Session, asset_type: str, code: str, name: str, reason: str | None = None) -> None:
    stmt = sqlite_insert(WatchlistItem).values(
        asset_type=asset_type, code=code, name=name, reason=reason, state="watching",
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    session.execute(stmt.on_conflict_do_update(index_elements=["asset_type", "code"], set_={
        "name": stmt.excluded.name, "reason": stmt.excluded.reason, "state": "watching", "updated_at": stmt.excluded.updated_at,
    }))


def delete_watchlist(session: Session, item_id: int) -> None:
    session.execute(delete(WatchlistItem).where(WatchlistItem.id == item_id))


def running_job(session: Session, job_name: str) -> JobRun | None:
    return session.execute(
        select(JobRun).where(JobRun.job_name == job_name, JobRun.status == "running").order_by(desc(JobRun.started_at)).limit(1)
    ).scalar_one_or_none()

def normalize_fund_assets(session: Session) -> int:
    rows = list(session.execute(select(Asset).where(Asset.asset_type == "fund")).scalars())
    primary_codes = primary_fund_codes([(row.code, row.name) for row in rows])
    changed = 0
    for row in rows:
        family = fund_family_key(row.name)
        share_class = fund_share_class(row.name)
        is_primary = 1 if row.code in primary_codes else 0
        if row.family_key != family or row.share_class != share_class or row.is_primary != is_primary:
            row.family_key = family
            row.share_class = share_class
            row.is_primary = is_primary
            changed += 1
    return changed


# ---------------------------------------------------------------------------
# v2 signal storage helpers
# ---------------------------------------------------------------------------


def replace_fund_features(session: Session, trade_date: date, rows: list[dict]) -> None:
    session.execute(
        delete(FundFeature).where(FundFeature.trade_date == trade_date)
    )
    for payloads in _chunks(rows, 300):
        session.execute(sqlite_insert(FundFeature).values(payloads))


def replace_fund_sector_exposures(
    session: Session, trade_date: date, rows: list[dict],
) -> None:
    session.execute(
        delete(FundSectorExposure).where(FundSectorExposure.trade_date == trade_date)
    )
    for payloads in _chunks(rows, 300):
        session.execute(sqlite_insert(FundSectorExposure).values(payloads))


def replace_signal_events(
    session: Session, trade_date: date, model_version: str, rows: list[dict],
) -> None:
    session.execute(
        delete(SignalEvent).where(
            SignalEvent.trade_date == trade_date,
            SignalEvent.model_version == model_version,
        )
    )
    for payloads in _chunks(rows, 300):
        session.execute(sqlite_insert(SignalEvent).values(payloads))


def upsert_signal_model_version(
    session: Session, model_version: str, payload: dict,
) -> SignalModelVersion:
    existing = session.get(SignalModelVersion, model_version)
    now = datetime.utcnow()
    if existing is None:
        existing = SignalModelVersion(
            model_version=model_version,
            status=payload.get("status", "draft"),
            weights_json=payload.get("weights_json", "{}"),
            backtest_json=payload.get("backtest_json", "{}"),
            notes=payload.get("notes"),
            updated_at=now,
        )
        session.add(existing)
    else:
        for field in ("status", "weights_json", "backtest_json", "notes"):
            if field in payload:
                setattr(existing, field, payload[field])
        existing.updated_at = now
    return existing


def list_signal_events(
    session: Session,
    trade_date: date | None = None,
    model_version: str | None = None,
    action: str | None = None,
    limit: int = 100,
) -> list[SignalEvent]:
    if trade_date is None:
        trade_date = session.execute(
            select(func.max(SignalEvent.trade_date))
        ).scalar_one_or_none()
    if trade_date is None:
        return []
    stmt = select(SignalEvent).where(SignalEvent.trade_date == trade_date)
    if model_version:
        stmt = stmt.where(SignalEvent.model_version == model_version)
    if action:
        stmt = stmt.where(SignalEvent.action == action)
    return list(
        session.execute(
            stmt.order_by(desc(SignalEvent.score)).limit(limit)
        ).scalars()
    )


def latest_signal_event_date(session: Session) -> date | None:
    return session.execute(
        select(func.max(SignalEvent.trade_date))
    ).scalar_one_or_none()


def get_fund_features(
    session: Session,
    trade_date: date | None = None,
    asset_type: str | None = None,
    code: str | None = None,
) -> list[FundFeature]:
    if trade_date is None:
        trade_date = session.execute(
            select(func.max(FundFeature.trade_date))
        ).scalar_one_or_none()
    if trade_date is None:
        return []
    stmt = select(FundFeature).where(FundFeature.trade_date == trade_date)
    if asset_type:
        stmt = stmt.where(FundFeature.asset_type == asset_type)
    if code:
        stmt = stmt.where(FundFeature.code == code)
    return list(session.execute(stmt).scalars())


def get_fund_sector_exposure(
    session: Session,
    trade_date: date | None = None,
    code: str | None = None,
) -> list[FundSectorExposure]:
    if trade_date is None:
        trade_date = session.execute(
            select(func.max(FundSectorExposure.trade_date))
        ).scalar_one_or_none()
    if trade_date is None:
        return []
    stmt = select(FundSectorExposure).where(FundSectorExposure.trade_date == trade_date)
    if code:
        stmt = stmt.where(FundSectorExposure.code == code)
    return list(
        session.execute(
            stmt.order_by(desc(FundSectorExposure.coverage))
        ).scalars()
    )


def get_signal_model_versions(
    session: Session, status: str | None = None,
) -> list[SignalModelVersion]:
    stmt = select(SignalModelVersion)
    if status:
        stmt = stmt.where(SignalModelVersion.status == status)
    return list(
        session.execute(
            stmt.order_by(desc(SignalModelVersion.updated_at))
        ).scalars()
    )
