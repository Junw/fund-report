from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    app_name: str = "A 股收盘日报"
    host: str = os.getenv("STOCK_REPORT_HOST", "127.0.0.1")
    port: int = int(os.getenv("STOCK_REPORT_PORT", "8088"))
    timezone: str = os.getenv("STOCK_REPORT_TZ", os.getenv("TZ", "Asia/Shanghai"))
    data_dir: Path = Path(os.getenv("STOCK_REPORT_DATA_DIR", "./data"))
    database_url: str | None = os.getenv("STOCK_REPORT_DATABASE_URL")
    ranking_limit: int = int(os.getenv("STOCK_REPORT_RANKING_LIMIT", "100"))
    enable_scheduler: bool = os.getenv("STOCK_REPORT_ENABLE_SCHEDULER", "1") != "0"
    llm_base_url: str | None = os.getenv("STOCK_REPORT_LLM_BASE_URL")
    llm_api_key: str | None = os.getenv("STOCK_REPORT_LLM_API_KEY")
    llm_model: str | None = os.getenv("STOCK_REPORT_LLM_MODEL")
    llm_timeout: int = int(os.getenv("STOCK_REPORT_LLM_TIMEOUT", "120"))

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir / 'stock_report.sqlite3'}"


settings = Settings()
