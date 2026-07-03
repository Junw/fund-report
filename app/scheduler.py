from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import session_scope
from app.services.report_service import ReportService
from app.services.research_jobs import ResearchService
from app.services.storage import create_job_run


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    scheduler.add_job(
        _run_close_report,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=30, timezone=settings.timezone),
        id="close_report",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        _run_fund_refresh,
        CronTrigger(day_of_week="mon-fri", hour=21, minute=30, timezone=settings.timezone),
        id="fund_refresh",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        _run_signal_compute,
        CronTrigger(day_of_week="mon-fri", hour=22, minute=0, timezone=settings.timezone),
        id="signal_compute",
        replace_existing=True,
        max_instances=1,
    )
    return scheduler


def _run_close_report() -> None:
    with session_scope() as session:
        ReportService(session).run_close_report()


def _run_fund_refresh() -> None:
    with session_scope() as session:
        ReportService(session).run_fund_refresh()



def _run_signal_compute() -> None:
    with session_scope() as session:
        run = create_job_run(session, "signal_compute")
        ResearchService(session).run("signal_compute", run)