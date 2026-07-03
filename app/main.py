from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from threading import Thread
from urllib.parse import quote_plus

import uvicorn
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, init_db
from app.models import Asset, BackfillProgress, DailyQuote, JobRun
from app.scheduler import create_scheduler
from app.services.llm_client import LlmClient
from app.services.calendar import format_local_datetime, today_in_timezone
from app.services.portfolio import analyze_holdings, portfolio_context, portfolio_signal_summary
from app.services.report_service import ReportService, report_to_dict
from app.services.research_jobs import ResearchService
from app.services.sector_recommendation import SectorRecommendationEngine, clear_sector_recommendation_cache
from app.services.sector_heat import heat_action_label, heat_status_label
from app.services.settings_store import masked_llm_config, save_llm_config
from app.services.storage import (
    create_job_run,
    delete_holding,
    finish_job_run,
    get_fund_features,
    get_fund_sector_exposure,
    get_rankings,
    get_report,
    get_sector_heat,
    get_signal_model_versions,
    latest_report,
    latest_asset_quote,
    latest_signal_event_date,
    list_holdings,
    list_job_runs,
    list_reports,
    list_signal_events,
    list_signal_scores,
    list_watchlist,
    latest_backtest,
    latest_signal_date,
    normalize_fund_assets,
    search_funds,
    running_job,
    upsert_watchlist,
    delete_watchlist,
    upsert_holding,
)


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

JOB_PATTERN = "^(close|fund_refresh|sector_backfill|historical_report|fund_history_backfill|fund_metadata_refresh|fund_holdings_refresh|signal_compute|backtest)$"


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@app.on_event("startup")
def startup() -> None:
    init_db()
    with SessionLocal() as session:
        normalize_fund_assets(session)
        stale = session.query(JobRun).filter(JobRun.status == "running").all()
        for run in stale:
            finish_job_run(session, run, "failed", "应用重启，原后台任务已中断")
        session.commit()
    Thread(target=_warm_sector_recommendation_cache, daemon=True).start()
    if settings.enable_scheduler:
        scheduler = create_scheduler()
        scheduler.start()
        app.state.scheduler = scheduler


@app.on_event("shutdown")
def shutdown() -> None:
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler:
        scheduler.shutdown(wait=False)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)):
    report = latest_report(session)
    report_data = report_to_dict(report) if report else None
    if report and report_data:
        _enrich_sector_recommendations(session, report_data, report.trade_date)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "report": report_data,
            "reports": list_reports(session, limit=20),
            "asset_labels": ASSET_LABELS,
            "window_labels": WINDOW_LABELS,
            "sector_url": sector_url,
            "sector_funds_url": sector_funds_url,
        },
    )


@app.get("/reports/{trade_date}", response_class=HTMLResponse)
def report_page(request: Request, trade_date: date, session: Session = Depends(get_session)):
    report = get_report(session, trade_date)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    report_data = report_to_dict(report)
    _enrich_sector_recommendations(session, report_data, report.trade_date)
    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "report": report_data,
            "reports": list_reports(session, limit=20),
            "asset_labels": ASSET_LABELS,
            "window_labels": WINDOW_LABELS,
            "sector_url": sector_url,
            "sector_funds_url": sector_funds_url,
        },
    )


@app.get("/rankings", response_class=HTMLResponse)
def rankings_page(
    request: Request,
    date_: date | None = Query(default=None, alias="date"),
    asset: str = "fund",
    window: str = "1d",
    session: Session = Depends(get_session),
):
    report = get_report(session, date_) if date_ else latest_report(session)
    if report is None:
        rows = []
        selected_date = date_
    else:
        selected_date = report.trade_date
        rows = get_rankings(session, selected_date, asset, window, limit=50)
    return templates.TemplateResponse(
        request,
        "rankings.html",
        {
            "rows": rows,
            "selected_date": selected_date,
            "asset": asset,
            "window": window,
            "asset_labels": ASSET_LABELS,
            "window_labels": WINDOW_LABELS,
            "reports": list_reports(session, limit=20),
        },
    )


@app.get("/funds", response_class=HTMLResponse)
def funds_page(request: Request):
    return templates.TemplateResponse(request, "funds.html", {})


@app.get("/holdings", response_class=HTMLResponse)
def holdings_page(request: Request, session: Session = Depends(get_session)):
    report = latest_report(session)
    holdings = list_holdings(session)
    analyses = analyze_holdings(session, holdings, report.trade_date if report else None)
    context = portfolio_context(analyses)
    signal_summary = portfolio_signal_summary(session, holdings)
    llm = LlmClient()
    return templates.TemplateResponse(
        request,
        "holdings.html",
        {
            "holdings": holdings,
            "analyses": analyses,
            "summary": context,
            "signal_summary": signal_summary,
            "llm_text": None,
            "llm_configured": llm.configured,
            "message": None,
            "import_debug": None,
        },
    )


@app.post("/holdings", response_class=HTMLResponse)
def save_holding(
    asset_type: str = Form(pattern="^(fund|etf)$"),
    code: str = Form(min_length=1),
    name: str = Form(min_length=1),
    shares: float | None = Form(default=None, ge=0),
    cost_amount: float | None = Form(default=None),
    return_rate: float | None = Form(default=None),
    note: str | None = Form(default=None),
    session: Session = Depends(get_session),
):
    normalized_code = code.strip()
    if shares is None and cost_amount is not None:
        quote = latest_asset_quote(session, asset_type, normalized_code)
        shares = round(cost_amount / quote.close, 4) if quote and quote.close else 0.0
        note = note or "按持仓金额和最新净值估算份额"
    upsert_holding(session, asset_type, normalized_code, name.strip(), shares or 0.0, cost_amount, return_rate, note)
    session.commit()
    return RedirectResponse("/holdings", status_code=303)


@app.post("/holdings/{holding_id}/delete")
def remove_holding(holding_id: int, session: Session = Depends(get_session)):
    delete_holding(session, holding_id)
    session.commit()
    return RedirectResponse("/holdings", status_code=303)


@app.post("/holdings/import-image", response_class=HTMLResponse)
async def import_holdings_image(
    request: Request,
    images: list[UploadFile] = File(...),
    session: Session = Depends(get_session),
):
    image_payloads: list[tuple[bytes, str]] = []
    for image in images:
        content = await image.read()
        if content:
            image_payloads.append((content, image.content_type or "image/png"))
    llm = LlmClient()
    imported = 0
    skipped: list[str] = []
    debug_rows: list[dict] = []
    message = "未配置大模型，截图无法自动解析。请使用手工录入，或到设置页配置模型。"
    if llm.configured:
        parsed = llm.parse_holdings_images(image_payloads)
        for index, item in enumerate(parsed, start=1):
            normalized = _normalize_imported_holding(session, item)
            code = normalized["code"]
            name = normalized["name"]
            shares = normalized["shares"]
            cost_amount = normalized["cost_amount"]
            return_rate = normalized["return_rate"]
            asset_type = normalized["asset_type"]
            debug_rows.append(
                {
                    "index": index,
                    "raw": json.dumps(item, ensure_ascii=False, indent=2),
                    "normalized": json.dumps(normalized, ensure_ascii=False, indent=2),
                }
            )
            if code and name and (shares is not None or cost_amount is not None):
                note = "截图导入"
                if shares is None:
                    if cost_amount is not None:
                        latest_quote = latest_asset_quote(session, asset_type, code)
                        if latest_quote and latest_quote.close:
                            shares = round(cost_amount / latest_quote.close, 4)
                            note = "截图导入：按持仓金额和最新净值估算份额"
                    if shares is None:
                        shares = 0.0
                        note = "截图导入：未识别份额，使用持仓金额估算"
                    skipped.append(f"第 {index} 条未识别份额，已按持仓金额导入")
                upsert_holding(session, asset_type, code, name, shares or 0.0, cost_amount, return_rate, note)
                imported += 1
            else:
                missing = []
                if not code:
                    missing.append("代码")
                if not name:
                    missing.append("名称")
                if shares is None and cost_amount is None:
                    missing.append("份额或金额")
                skipped.append(f"第 {index} 条缺少{'/'.join(missing)}，字段：{_item_preview(item)}")
        session.commit()
        if imported:
            message = f"截图解析完成，模型返回 {llm.last_parsed_count} 条，已导入 {imported} 条。"
            if skipped:
                message += " 跳过：" + "；".join(skipped[:5])
        elif llm.last_error:
            image_hint = f"；压缩后图片约 {llm.last_image_bytes // 1024} KB" if llm.last_image_bytes else ""
            message = f"截图未导入：{llm.last_error}{image_hint}"
        elif parsed:
            message = "截图已解析，但没有可导入记录。" + (" 跳过：" + "；".join(skipped[:5]) if skipped else "")
        else:
            message = "截图未导入：模型没有返回持仓 JSON 数组。"

    report = latest_report(session)
    holdings = list_holdings(session)
    analyses = analyze_holdings(session, holdings, report.trade_date if report else None)
    context = portfolio_context(analyses)
    import_debug = {
        "raw_response": llm.last_content or "模型没有返回文本内容",
        "rows": debug_rows,
        "error": llm.last_error,
    }
    return templates.TemplateResponse(
        request,
        "holdings.html",
        {
            "holdings": holdings,
            "analyses": analyses,
            "summary": context,
            "llm_text": None,
            "llm_configured": llm.configured,
            "message": message,
            "import_debug": import_debug,
        },
    )


@app.get("/heat", response_class=HTMLResponse)
def heat_page(
    request: Request,
    date_: date | None = Query(default=None, alias="date"),
    asset: str = "industry",
    session: Session = Depends(get_session),
):
    report = get_report(session, date_) if date_ else latest_report(session)
    selected_date = report.trade_date if report else date_
    rows = get_sector_heat(session, selected_date, asset, limit=100) if selected_date else []
    report_data = report_to_dict(report) if report else {"summary": {}}
    news = report_data.get("summary", {}).get("news", [])
    engine = SectorRecommendationEngine(session, selected_date, news) if selected_date else None
    rows_json = [
        {
            "rank": row.heat_rank,
            "code": row.code,
            "name": row.name,
            "return_7d": row.return_7d,
            "return_1m": row.return_1m,
            "heat_score": row.heat_score,
            "heat_level": row.heat_level,
            "data_status": row.data_status,
            "action_label": heat_action_label(row),
            "status_label": heat_status_label(row.data_status),
            "url": sector_url(row.code, row.name),
            "funds_url": sector_funds_url(row.name),
            "recommendation": engine.score(row) if engine and row.heat_score is not None else None,
        }
        for row in rows
    ]
    ok_rows_json = [row for row in rows_json if row["heat_score"] is not None]
    return templates.TemplateResponse(
        request,
        "heat.html",
        {
            "rows": rows_json,
            "rows_json": rows_json,
            "ok_rows_json": ok_rows_json,
            "selected_date": selected_date,
            "asset": asset,
            "reports": list_reports(session, limit=20),
            "asset_labels": HEAT_ASSET_LABELS,
            "sector_url": sector_url,
            "sector_funds_url": sector_funds_url,
        },
    )


@app.get("/sector-funds", response_class=HTMLResponse)
def sector_funds_page(
    request: Request,
    sector: str = Query(min_length=1),
    session: Session = Depends(get_session),
):
    report = latest_report(session)
    trade_date = report.trade_date if report else date.today()
    engine = SectorRecommendationEngine(session, trade_date)
    funds = engine.related_funds(sector, limit=100)
    return templates.TemplateResponse(
        request,
        "sector_funds.html",
        {"sector": sector, "funds": funds, "trade_date": trade_date},
    )


@app.get("/research", response_class=HTMLResponse)
def research_page(request: Request, session: Session = Depends(get_session)):
    signal_date = latest_signal_date(session)
    signals = [_signal_to_dict(row) for row in list_signal_scores(session, signal_date, limit=100)]
    backtest = _backtest_to_dict(latest_backtest(session))
    progress = list(session.query(BackfillProgress).order_by(BackfillProgress.job_name).all())
    # v2 model versions for research context
    model_versions = get_signal_model_versions(session)
    return templates.TemplateResponse(request, "research.html", {
        "signal_date": signal_date,
        "signals": signals,
        "backtest": backtest,
        "progress": progress,
        "model_versions": [
            {
                "model_version": mv.model_version,
                "status": mv.status,
                "notes": mv.notes,
            }
            for mv in model_versions
        ],
    })


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request, session: Session = Depends(get_session)):
    signal_rows = list_signal_scores(session, latest_signal_date(session), limit=10000)
    signal_map = {(row.asset_type, row.code): _signal_to_dict(row) for row in signal_rows}
    rows = [{"item": item, "signal": signal_map.get((item.asset_type, item.code))} for item in list_watchlist(session)]
    return templates.TemplateResponse(request, "watchlist.html", {"rows": rows})


@app.post("/watchlist")
def add_watchlist(
    asset_type: str = Form(pattern="^(fund|etf)$"),
    code: str = Form(min_length=1),
    name: str = Form(min_length=1),
    reason: str | None = Form(default=None),
    session: Session = Depends(get_session),
):
    upsert_watchlist(session, asset_type, code.strip(), name.strip(), reason)
    session.commit()
    return RedirectResponse("/watchlist", status_code=303)


@app.post("/watchlist/{item_id}/delete")
def remove_watchlist(item_id: int, session: Session = Depends(get_session)):
    delete_watchlist(session, item_id)
    session.commit()
    return RedirectResponse("/watchlist", status_code=303)

@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, session: Session = Depends(get_session)):
    today = today_in_timezone(settings.timezone)
    historical_dates = [today - timedelta(days=offset) for offset in range(30)]
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": list_job_runs(session),
            "format_datetime": format_local_datetime,
            "historical_dates": historical_dates,
        },
    )


@app.post("/jobs/run")
def run_job_from_form(
    job: str = Query(pattern=JOB_PATTERN),
    date_: date | None = Query(default=None, alias="date"),
):
    _start_report_job(job, date_)
    return RedirectResponse("/jobs", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "settings.html", {"llm": masked_llm_config(session), "saved": False})


@app.post("/settings", response_class=HTMLResponse)
def save_settings(
    request: Request,
    llm_base_url: str | None = Form(default=None),
    llm_api_key: str | None = Form(default=None),
    llm_model: str | None = Form(default=None),
    session: Session = Depends(get_session),
):
    save_llm_config(session, llm_base_url, llm_api_key, llm_model)
    session.commit()
    return templates.TemplateResponse(request, "settings.html", {"llm": masked_llm_config(session), "saved": True})


@app.get("/api/reports/latest")
def api_latest_report(session: Session = Depends(get_session)):
    report = latest_report(session)
    if report is None:
        raise HTTPException(status_code=404, detail="No report generated")
    return report_to_dict(report)


@app.get("/api/reports/{trade_date}")
def api_report(trade_date: date, session: Session = Depends(get_session)):
    report = get_report(session, trade_date)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return report_to_dict(report)


@app.get("/api/rankings")
def api_rankings(
    date_: date = Query(alias="date"),
    asset: str = Query(pattern="^(fund|etf|industry|concept)$"),
    window: str = Query(pattern="^(1d|3d|7d|1m)$"),
    session: Session = Depends(get_session),
):
    rows = get_rankings(session, date_, asset, window, limit=50)
    return [
        {
            "rank": row.rank,
            "code": row.code,
            "name": row.name,
            "value": row.value,
            "close": row.close,
            "amount": row.amount,
            "note": row.note,
        }
        for row in rows
    ]


@app.get("/api/funds/search")
def api_fund_search(q: str = Query(min_length=1), session: Session = Depends(get_session)):
    rows = search_funds(session, q, limit=20)
    return [
        {"asset_type": row.asset_type, "code": row.code, "name": row.name, "category": row.category}
        for row in rows
    ]


@app.get("/api/portfolio/summary")
def api_portfolio_summary(session: Session = Depends(get_session)):
    report = latest_report(session)
    analyses = analyze_holdings(session, list_holdings(session), report.trade_date if report else None)
    return portfolio_context(analyses)


@app.post("/api/jobs/run")
def api_run_job(
    job: str = Query(pattern=JOB_PATTERN),
    date_: date | None = Query(default=None, alias="date"),
):
    if job == "historical_report":
        try:
            _validate_historical_report_date(date_)
        except ValueError as exc:
            return JSONResponse({"status": "invalid", "job": job, "message": str(exc)}, status_code=400)
    accepted = _start_report_job(job, date_)
    if not accepted:
        return JSONResponse({"status": "running", "job": job, "message": "同名任务已在运行"}, status_code=409)
    return JSONResponse({"status": "accepted", "job": job, "date": date_.isoformat() if date_ else None, "message": "任务已进入后台执行"})


@app.get("/api/jobs/status")
def api_job_status(session: Session = Depends(get_session)):
    jobs = list_job_runs(session, limit=20)
    return [
        {
            "id": job.id,
            "job_name": job.job_name,
            "status": job.status,
            "started_at": format_local_datetime(job.started_at),
            "finished_at": format_local_datetime(job.finished_at),
            "message": job.message,
        }
        for job in jobs
    ]


@app.get("/api/signals")
def api_signals(
    date_: date | None = Query(default=None, alias="date"),
    asset: str | None = Query(default=None, pattern="^(fund|etf)$"),
    action: str | None = Query(default=None),
    min_confidence: float | None = Query(default=None, ge=0, le=100),
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
):
    # return v2 signal_events when present; fall back to legacy scores
    event_date = date_ or latest_signal_event_date(session)
    if event_date:
        events = list_signal_events(
            session, event_date, action=action, limit=10000,
        )
        if events:
            # filter by asset and min_confidence in Python
            result = events
            if asset:
                result = [e for e in result if e.asset_type == asset]
            if min_confidence is not None:
                result = [e for e in result if e.confidence >= min_confidence]
            return [_signal_event_to_dict(e) for e in result[:limit]]
    # legacy fallback
    return [_signal_to_dict(row) for row in list_signal_scores(session, date_, limit=limit)]


@app.get("/api/backtests/latest")
def api_latest_backtest(session: Session = Depends(get_session)):
    row = latest_backtest(session)
    if row is None:
        raise HTTPException(status_code=404, detail="No backtest generated")
    return _backtest_to_dict(row)


@app.get("/api/watchlist")
def api_watchlist(session: Session = Depends(get_session)):
    return [{"id": row.id, "asset_type": row.asset_type, "code": row.code, "name": row.name, "reason": row.reason, "state": row.state} for row in list_watchlist(session)]

# ---------------------------------------------------------------------------
# v2 signal routes
# ---------------------------------------------------------------------------


@app.get("/signals", response_class=HTMLResponse)
def signals_page(
    request: Request,
    label: str | None = Query(default=None),
    session: Session = Depends(get_session),
):
    """HTML page listing v2 signal events with research labels."""
    event_date = latest_signal_event_date(session)
    events = [
        _signal_event_to_dict(row)
        for row in list_signal_events(session, event_date, action=label, limit=200)
    ]
    model_versions = get_signal_model_versions(session)
    label_counts: dict[str, int] = {}
    if event_date:
        all_events = list_signal_events(session, event_date, limit=10000)
        for row in all_events:
            label_counts[row.action] = label_counts.get(row.action, 0) + 1
    return templates.TemplateResponse(
        request,
        "signals.html",
        {
            "event_date": event_date,
            "events": events,
            "model_versions": [
                {
                    "model_version": mv.model_version,
                    "status": mv.status,
                    "notes": mv.notes,
                    "updated_at": mv.updated_at.isoformat() if mv.updated_at else None,
                }
                for mv in model_versions
            ],
            "selected_label": label,
            "label_counts": label_counts,
            "signal_labels": {
                "insufficient_data": "历史不足",
                "strong_attention": "建仓观察",
                "worthy_attention": "趋势确认",
                "neutral_watch": "持有观察",
                "cautious_watch": "减仓观察",
                "no_attention": "风险升高",
                "pullback_watch": "等待修复",
            },
        },
    )


@app.get("/funds/{code}", response_class=HTMLResponse)
def fund_detail_page(
    request: Request,
    code: str,
    session: Session = Depends(get_session),
):
    """Fund detail page with signals and features."""
    # look up asset
    asset = session.execute(
        select(Asset).where(Asset.asset_type.in_(("fund", "etf")), Asset.code == code)
    ).scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=404, detail="Fund not found")

    # latest features
    features = get_fund_features(session, code=code)
    feature = features[0] if features else None

    # sector exposures
    exposures = get_fund_sector_exposure(session, code=code)

    # latest signal event
    event_date = latest_signal_event_date(session)
    signal_event = None
    if event_date:
        events = list_signal_events(session, event_date, limit=10000)
        signal_event = next(
            (e for e in events if e.code == code and e.asset_type == asset.asset_type),
            None,
        )

    # latest quote
    quote = latest_asset_quote(session, asset.asset_type, asset.code)

    return templates.TemplateResponse(
        request,
        "fund_detail.html",
        {
            "asset": {
                "asset_type": asset.asset_type,
                "code": asset.code,
                "name": asset.name,
                "category": asset.category,
            },
            "feature": _fund_feature_to_dict(feature) if feature else None,
            "exposures": [
                {
                    "sector_name": e.sector_name,
                    "sector_code": e.sector_code,
                    "coverage": e.coverage,
                    "confidence": e.confidence,
                    "source": e.source,
                }
                for e in exposures
            ],
            "signal": _signal_event_to_dict(signal_event) if signal_event else None,
            "quote": {
                "trade_date": quote.trade_date.isoformat() if quote and quote.trade_date else None,
                "close": quote.close,
                "change_pct": quote.change_pct,
                "amount": quote.amount,
                "volume": quote.volume,
            } if quote else None,
            "signal_labels": {
                "insufficient_data": "历史不足",
                "strong_attention": "建仓观察",
                "worthy_attention": "趋势确认",
                "neutral_watch": "持有观察",
                "cautious_watch": "减仓观察",
                "no_attention": "风险升高",
                "pullback_watch": "等待修复",
            },
        },
    )


@app.get("/api/funds/{code}/signal")
def api_fund_signal(
    code: str,
    session: Session = Depends(get_session),
):
    """API endpoint returning signal, features, exposures, and quote history."""
    event_date = latest_signal_event_date(session)
    events = list_signal_events(session, event_date, limit=10000) if event_date else []
    signal_event = next((e for e in events if e.code == code), None)
    features = get_fund_features(session, code=code)
    exposures = get_fund_sector_exposure(session, code=code)
    # quote history (last 30 days)
    quote_rows = session.execute(
        select(DailyQuote)
        .where(DailyQuote.code == code)
        .order_by(desc(DailyQuote.trade_date))
        .limit(30)
    ).scalars()
    quotes = [
        {
            "trade_date": q.trade_date.isoformat(),
            "close": q.close,
            "change_pct": q.change_pct,
            "amount": q.amount,
        }
        for q in quote_rows
    ]
    return {
        "code": code,
        "event_date": event_date.isoformat() if event_date else None,
        "signal": _signal_event_to_dict(signal_event) if signal_event else None,
        "feature": _fund_feature_to_dict(features[0]) if features else None,
        "exposures": [
            {
                "sector_name": e.sector_name,
                "sector_code": e.sector_code,
                "confidence": e.confidence,
                "coverage": e.coverage,
                "source": e.source,
            }
            for e in exposures
        ],
        "quotes": quotes,
    }


@app.get("/api/portfolio/signals")
def api_portfolio_signals(session: Session = Depends(get_session)):
    """API returning signal summary for all portfolio holdings using latest
    SignalEvent.action values and the exact research labels."""
    event_date = latest_signal_event_date(session)
    events = list_signal_events(session, event_date, limit=10000) if event_date else []
    event_map: dict[tuple[str, str], dict] = {}
    for e in events:
        event_map[(e.asset_type, e.code)] = _signal_event_to_dict(e)

    holdings = list_holdings(session)

    holding_signals: list[dict] = []
    total_market_value = 0.0
    for holding in holdings:
        quote = latest_asset_quote(session, holding.asset_type, holding.code)
        close = quote.close if quote else None
        market_value = (
            close * holding.shares
            if close is not None and holding.shares > 0
            else holding.cost_amount
        )
        total_market_value += market_value or 0
        holding_key = (holding.asset_type, holding.code)
        sig = event_map.get(holding_key)
        holding_signals.append({
            "code": holding.code,
            "name": holding.name,
            "asset_type": holding.asset_type,
            "shares": holding.shares,
            "market_value": market_value,
            "signal": sig,
        })

    # compute weighted score from SignalEvent.score values
    weighted_score = None
    if total_market_value > 0:
        scored = [
            (h["signal"].get("score") or 0, h["market_value"] or 0)
            for h in holding_signals
            if h["signal"] and h["signal"].get("score") is not None
        ]
        if scored:
            weighted_score = round(
                sum(s * v for s, v in scored) / sum(v for _, v in scored), 1,
            )

    return {
        "event_date": event_date.isoformat() if event_date else None,
        "holdings": holding_signals,
        "total_market_value": total_market_value if total_market_value > 0 else None,
        "weighted_signal_score": weighted_score,
    }


@app.get("/api/sector-heat")
def api_sector_heat(
    date_: date = Query(alias="date"),
    asset: str = Query(default="industry", pattern="^(industry|concept)$"),
    session: Session = Depends(get_session),
):
    rows = get_sector_heat(session, date_, asset)
    return [
        {
            "rank": row.heat_rank,
            "code": row.code,
            "name": row.name,
            "return_7d": row.return_7d,
            "return_1m": row.return_1m,
            "heat_score": row.heat_score,
            "heat_level": row.heat_level,
            "data_status": row.data_status,
            "url": sector_url(row.code, row.name),
        }
        for row in rows
    ]


ASSET_LABELS = {
    "fund": "权益基金",
    "etf": "ETF",
    "industry": "行业板块",
    "concept": "概念题材",
}
HEAT_ASSET_LABELS = {"industry": "行业板块", "concept": "概念题材"}
WINDOW_LABELS = {"1d": "当天", "3d": "近 3 日", "7d": "近 7 日", "1m": "近 1 个月"}


def _start_report_job(job: str, target_date: date | None = None) -> bool:
    with SessionLocal() as session:
        if running_job(session, job) is not None:
            return False
        run = create_job_run(session, job)
        if job == "historical_report" and target_date is not None:
            run.message = f"准备补充/重建 {target_date.isoformat()} 历史日报"
        session.commit()
        run_id = run.id
    Thread(target=_run_report_job_background, args=(job, run_id, target_date), daemon=True).start()
    return True


def _validate_historical_report_date(target_date: date | None) -> None:
    if target_date is None:
        raise ValueError("历史日报任务需要 date=YYYY-MM-DD")
    today = today_in_timezone(settings.timezone)
    start = today - timedelta(days=29)
    if target_date > today or target_date < start:
        raise ValueError(f"只能补充最近 30 天内的日报（{start.isoformat()} 至 {today.isoformat()}）")


def _warm_sector_recommendation_cache() -> None:
    with SessionLocal() as session:
        report = latest_report(session)
        if report is not None:
            SectorRecommendationEngine(session, report.trade_date)


def _run_report_job_background(job: str, run_id: int, target_date: date | None = None) -> None:
    with SessionLocal() as session:
        run = session.get(JobRun, run_id)
        if run is None:
            return
        try:
            if job in {"close", "fund_refresh", "sector_backfill", "historical_report"}:
                service = ReportService(session)
                if job == "close":
                    service.run_close_report(run=run)
                elif job == "fund_refresh":
                    service.run_fund_refresh(run=run)
                elif job == "sector_backfill":
                    service.run_sector_backfill(run=run)
                else:
                    if target_date is None:
                        raise ValueError("历史日报任务缺少目标日期")
                    service.run_historical_report(target_date, run=run)
            else:
                ResearchService(session).run(job, run)
            session.commit()
            clear_sector_recommendation_cache()
        except Exception as exc:
            finish_job_run(session, run, "failed", str(exc))
            session.commit()


def sector_url(code: str | None, name: str | None = None) -> str:
    if code and code.startswith("BK"):
        return f"https://quote.eastmoney.com/bk/90.{code}.html"
    keyword = name or code or ""
    return f"https://so.eastmoney.com/web/s?keyword={keyword}"


def sector_funds_url(name: str | None) -> str:
    return f"/sector-funds?sector={quote_plus(name or '')}"


def _enrich_sector_recommendations(session: Session, report_data: dict, trade_date: date) -> None:
    summary = report_data.get("summary", {})
    sector_heat_by_asset = summary.get("sector_heat_by_asset")
    if sector_heat_by_asset:
        rows = []
        for heat in sector_heat_by_asset.values():
            rows.extend(heat.get("top", []))
            rows.extend(heat.get("cooling", []))
    else:
        sector_heat = summary.get("sector_heat", {})
        rows = [*sector_heat.get("top", []), *sector_heat.get("cooling", [])]
    if not rows:
        return
    engine = SectorRecommendationEngine(session, trade_date, summary.get("news", []))
    cache: dict[str, dict] = {}
    for row in rows:
        name = row.get("name", "")
        recommendation = cache.setdefault(name, engine.score(row))
        row["recommendation_score"] = recommendation["score"]
        row["recommendation_level"] = recommendation["level"]
        row["related_count"] = recommendation["related_count"]
        row["funds_url"] = sector_funds_url(name)


def _signal_to_dict(row) -> dict:
    return {
        "trade_date": row.trade_date.isoformat(),
        "model_version": row.model_version,
        "asset_type": row.asset_type,
        "code": row.code,
        "name": row.name,
        "total_score": row.total_score,
        "confidence": row.confidence,
        "status": row.status,
        "components": json.loads(row.components_json or "{}"),
        "evidence": json.loads(row.evidence_json or "[]"),
        "data_date": row.data_date.isoformat() if row.data_date else None,
    }


def _backtest_to_dict(row) -> dict | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "model_version": row.model_version,
        "status": row.status,
        "started_at": format_local_datetime(row.started_at),
        "finished_at": format_local_datetime(row.finished_at),
        "config": json.loads(row.config_json or "{}"),
        "metrics": json.loads(row.metrics_json or "{}"),
        "message": row.message,
        "production_eligible": bool(row.production_eligible),
    }

def _normalize_imported_holding(session: Session, item: dict) -> dict:
    code = _extract_code(_first_field(item, ("code", "fund_code", "基金代码", "代码", "产品代码")))
    name = _clean_text(_first_field(item, ("name", "fund_name", "基金名称", "基金简称", "名称", "产品名称")))
    if not code:
        code = _extract_code(name)

    shares = _to_float(
        _first_field(
            item,
            (
                "shares",
                "share",
                "holding_shares",
                "holding_share",
                "quantity",
                "amount_share",
                "持有份额",
                "持仓份额",
                "可用份额",
                "基金份额",
                "份额",
                "数量",
                "持有数量",
                "持仓数量",
            ),
        )
    )
    cost_amount = _to_float(
        _first_field(
            item,
            (
                "cost_amount",
                "market_value",
                "holding_amount",
                "amount",
                "value",
                "cost",
                "持仓金额",
                "持有金额",
                "持仓市值",
                "持有市值",
                "参考市值",
                "最新市值",
                "资产市值",
                "当前市值",
                "市值",
                "金额",
                "资产",
                "成本",
                "本金",
            ),
        )
    )
    return_rate = _to_percent(
        _first_field(
            item,
            (
                "return_rate",
                "holding_return_rate",
                "profit_rate",
                "yield_rate",
                "持有收益率",
                "持仓收益率",
                "收益率",
                "累计收益率",
            ),
        )
    )
    asset_type = "etf" if "ETF" in (name or "").upper() else "fund"

    lookup_key = code or name
    if lookup_key:
        matches = search_funds(session, lookup_key, limit=1)
        if matches:
            code = code or matches[0].code
            name = name or matches[0].name
            asset_type = matches[0].asset_type

    return {
        "code": code or "",
        "name": name or "",
        "shares": shares,
        "cost_amount": cost_amount,
        "return_rate": return_rate,
        "asset_type": asset_type,
    }


def _first_field(item: dict, keys: tuple[str, ...]):
    normalized = {_normalize_key(key): value for key, value in item.items()}
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
        value = normalized.get(_normalize_key(key))
        if value not in (None, ""):
            return value
    return None


def _normalize_key(value: str) -> str:
    return re.sub(r"[\s_\-：:（）()\[\]【】/／]+", "", str(value)).lower()


def _item_preview(item: dict) -> str:
    parts = []
    for key, value in list(item.items())[:8]:
        text = str(value).replace("\n", " ").strip()
        if len(text) > 24:
            text = text[:24] + "..."
        parts.append(f"{key}={text}")
    return "，".join(parts) or "空记录"


def _clean_text(value) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()


def _extract_code(value) -> str:
    text = _clean_text(value)
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    return match.group(1) if match else ""


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("value", "amount", "number", "text"):
            parsed = _to_float(value.get(key))
            if parsed is not None:
                return parsed
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "nan", "--", "-"} or "%" in text:
        return None
    multiplier = 1.0
    if "亿" in text:
        multiplier = 100000000.0
    elif "万" in text:
        multiplier = 10000.0
    text = (
        text.replace(",", "")
        .replace("，", "")
        .replace("人民币", "")
        .replace("元", "")
        .replace("份", "")
        .replace("约", "")
        .replace("万", "")
        .replace("亿", "")
        .strip()
    )
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0)) * multiplier
    except ValueError:
        return None


def _to_percent(value) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).replace("％", "%").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return None
    number = float(match.group(0))
    if "%" not in text and abs(number) <= 1:
        number *= 100
    return number


def _signal_event_to_dict(row) -> dict:
    return {
        "trade_date": row.trade_date.isoformat(),
        "model_version": row.model_version,
        "asset_type": row.asset_type,
        "code": row.code,
        "name": row.name,
        "action": row.action,
        "score": row.score,
        "confidence": row.confidence,
        "risk_level": row.risk_level,
        "status": row.status,
        "reason_json": json.loads(row.reason_json or "{}"),
        "risk_json": json.loads(row.risk_json or "{}"),
        "invalid_json": json.loads(row.invalid_json or "{}"),
        "feature_json": json.loads(row.feature_json or "{}"),
    }


def _fund_feature_to_dict(row) -> dict:
    return {
        "trade_date": row.trade_date.isoformat(),
        "asset_type": row.asset_type,
        "code": row.code,
        "name": row.name,
        "category": row.category,
        "return_20d": row.return_20d,
        "return_60d": row.return_60d,
        "return_120d": row.return_120d,
        "volatility_120d": row.volatility_120d,
        "downside_volatility_120d": row.downside_volatility_120d,
        "max_drawdown_120d": row.max_drawdown_120d,
        "sharpe_120d": row.sharpe_120d,
        "sortino_120d": row.sortino_120d,
        "amount": row.amount,
        "turnover_rate": row.turnover_rate,
        "premium": row.premium,
        "liquidity_score": row.liquidity_score,
        "quality_score": row.quality_score,
        "data_status": row.data_status,
        "feature_json": json.loads(row.feature_json or "{}"),
    }


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)

