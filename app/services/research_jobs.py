from __future__ import annotations

import json
import time
from datetime import date

from sqlalchemy.orm import Session

from app.models import JobRun
from app.services.backtest import run_backtest
from app.services.calendar import today_in_timezone
from app.services.data_sources import AkshareClient
from app.services.report_service import report_to_dict
from app.services.signals import SIGNAL_LABELS, compute_signal_scores, compute_signal_scores_v2
from app.services.storage import (
    finish_job_run, get_backfill_progress, latest_report, list_primary_fund_assets,
    replace_fund_holdings, save_fund_metadata, upsert_quotes,
)
from app.config import settings


class ResearchService:
    def __init__(self, session: Session, client: AkshareClient | None = None, batch_size: int = 20) -> None:
        self.session = session
        self.client = client or AkshareClient()
        self.batch_size = batch_size

    def run(self, job_name: str, run: JobRun, as_of: date | None = None) -> dict:
        trade_date = as_of or today_in_timezone(settings.timezone)
        if job_name == "fund_history_backfill":
            return self._history_batch(run, trade_date)
        if job_name == "fund_metadata_refresh":
            return self._metadata_batch(run, trade_date)
        if job_name == "fund_holdings_refresh":
            return self._holdings_batch(run, trade_date)
        if job_name == "signal_compute":
            return self._signals(run, trade_date)
        if job_name == "backtest":
            return self._backtest(run)
        raise ValueError(f"未知研究任务: {job_name}")

    def _history_batch(self, run: JobRun, trade_date: date) -> dict:
        progress = get_backfill_progress(self.session, "fund_history_backfill")
        assets = list_primary_fund_assets(self.session, progress.cursor, self.batch_size)
        imported = failed = 0
        errors: list[str] = []
        for asset in assets:
            try:
                rows = self.client.fetch_fund_history(asset.code, asset.name, asset.asset_type, trade_date)
                upsert_quotes(self.session, rows)
                imported += len(rows)
            except Exception as exc:
                failed += 1
                errors.append(f"{asset.code}: {str(exc)[:80]}")
            progress.cursor = asset.code
            progress.completed += 1
            time.sleep(0.15)
        self._finish_progress(progress, assets, errors)
        message = f"本批处理 {len(assets)} 只，写入历史 {imported} 条，失败 {failed}；总进度 {progress.completed}/{progress.total}"
        finish_job_run(self.session, run, "success" if assets else "skipped", message)
        return {"status": run.status, "message": message}

    def _metadata_batch(self, run: JobRun, trade_date: date) -> dict:
        progress = get_backfill_progress(self.session, "fund_metadata_refresh")
        assets = list_primary_fund_assets(self.session, progress.cursor, self.batch_size)
        failed = 0
        errors: list[str] = []
        for asset in assets:
            try:
                save_fund_metadata(self.session, self.client.fetch_fund_metadata(asset.code, trade_date))
            except Exception as exc:
                failed += 1
                errors.append(f"{asset.code}: {str(exc)[:80]}")
            progress.cursor = asset.code
            progress.completed += 1
            time.sleep(0.15)
        self._finish_progress(progress, assets, errors)
        message = f"本批元数据 {len(assets)} 只，失败 {failed}；总进度 {progress.completed}/{progress.total}"
        finish_job_run(self.session, run, "success" if assets else "skipped", message)
        return {"status": run.status, "message": message}

    def _holdings_batch(self, run: JobRun, trade_date: date) -> dict:
        progress = get_backfill_progress(self.session, "fund_holdings_refresh")
        candidates = list_primary_fund_assets(self.session, progress.cursor, self.batch_size * 3)
        assets = [row for row in candidates if row.asset_type == "fund"][: self.batch_size]
        failed = 0
        errors: list[str] = []
        processed_codes = {row.code for row in assets}
        for candidate in candidates:
            if candidate.asset_type == "etf":
                progress.cursor = candidate.code
                progress.completed += 1
                continue
            if candidate.code not in processed_codes:
                break
            try:
                report_date, rows = self.client.fetch_fund_holdings(candidate.code, trade_date)
                replace_fund_holdings(self.session, candidate.code, report_date, rows)
            except Exception as exc:
                failed += 1
                errors.append(f"{candidate.code}: {str(exc)[:80]}")
            progress.cursor = candidate.code
            progress.completed += 1
            time.sleep(0.15)
        self._finish_progress(progress, candidates if assets else [], errors)
        message = f"本批持仓披露 {len(assets)} 只，失败 {failed}；总进度 {progress.completed}/{progress.total}"
        finish_job_run(self.session, run, "success" if assets else "skipped", message)
        return {"status": run.status, "message": message}
    def _signals(self, run: JobRun, trade_date: date) -> dict:
        report = latest_report(self.session)
        if report:
            trade_date = min(trade_date, report.trade_date)
            news = report_to_dict(report).get("summary", {}).get("news", [])
        else:
            news = []
        # v1 legacy computation (also writes signal_scores)
        rows = compute_signal_scores(self.session, trade_date, news)
        eligible = sum(1 for row in rows if row["status"] == "experimental")
        # v2 computation (writes fund_features, fund_sector_exposure, signal_events)
        v2_result = compute_signal_scores_v2(self.session, trade_date, news)
        label_summary = ", ".join(
            f"{SIGNAL_LABELS.get(label, label)}: {count}"
            for label, count in v2_result["labels"].items()
            if count > 0
        )
        message = (
            f"已计算 {len(rows)} 只基金，实验候选 {eligible} 只，"
            f"低置信度 {len(rows) - eligible} 只；"
            f"v2 信号 {v2_result['total']} 条，标签分布：{label_summary or '无'}"
        )
        finish_job_run(self.session, run, "success", message)
        return {"status": "success", "message": message}

    def _backtest(self, run: JobRun) -> dict:
        result = run_backtest(self.session)
        message = result.message or result.status
        finish_job_run(self.session, run, result.status, message)
        return {"status": result.status, "message": message, "backtest_id": result.id}

    @staticmethod
    def _finish_progress(progress, assets, errors: list[str]) -> None:
        progress.state = "complete" if not assets else "running"
        progress.message = "；".join(errors[:3]) if errors else None
        progress.updated_at = __import__("datetime").datetime.utcnow()
