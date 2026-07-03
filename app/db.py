from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _ensure_sqlite_parent(url: str) -> None:
    if not url.startswith("sqlite:///"):
        return
    db_path = url.removeprefix("sqlite:///")
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_parent(settings.db_url)
engine = create_engine(
    settings.db_url,
    connect_args={"check_same_thread": False, "timeout": 30} if settings.db_url.startswith("sqlite") else {},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    from app import models  # noqa: F401

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    _run_migrations()
    if settings.db_url.startswith("sqlite"):
        with engine.connect() as connection:
            connection.execute(text("PRAGMA journal_mode=WAL"))
            connection.execute(text("PRAGMA synchronous=NORMAL"))


def _run_migrations() -> None:
    if not settings.db_url.startswith("sqlite"):
        return
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at DATETIME NOT NULL)"))
        applied = {row[0] for row in connection.execute(text("SELECT version FROM schema_migrations"))}
        for version, migration in _MIGRATIONS:
            if version in applied:
                continue
            migration(connection)
            connection.execute(
                text("INSERT INTO schema_migrations(version, applied_at) VALUES (:version, CURRENT_TIMESTAMP)"),
                {"version": version},
            )


def _add_column_if_missing(connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in connection.execute(text(f"PRAGMA table_info({table})"))}
    if column not in columns:
        connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))


def _migration_1(connection) -> None:
    _add_column_if_missing(connection, "portfolio_holdings", "return_rate", "FLOAT")


def _migration_2(connection) -> None:
    _add_column_if_missing(connection, "assets", "family_key", "VARCHAR(160)")
    _add_column_if_missing(connection, "assets", "share_class", "VARCHAR(16)")
    _add_column_if_missing(connection, "assets", "is_primary", "INTEGER NOT NULL DEFAULT 1")
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_assets_family_key ON assets(family_key)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_assets_is_primary ON assets(is_primary)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_daily_quotes_type_code_date ON daily_quotes(asset_type, code, trade_date)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_signal_scores_date_status_score ON signal_scores(trade_date, status, total_score)"))


def _migration_3(connection) -> None:
    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS fund_features (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date DATE NOT NULL,
            asset_type VARCHAR(24) NOT NULL,
            code VARCHAR(32) NOT NULL,
            name VARCHAR(128) NOT NULL,
            category VARCHAR(64),
            return_20d FLOAT,
            return_60d FLOAT,
            return_120d FLOAT,
            volatility_120d FLOAT,
            downside_volatility_120d FLOAT,
            max_drawdown_120d FLOAT,
            sharpe_120d FLOAT,
            sortino_120d FLOAT,
            amount FLOAT,
            turnover_rate FLOAT,
            premium FLOAT,
            liquidity_score FLOAT,
            quality_score FLOAT,
            data_status VARCHAR(32) DEFAULT 'ok' NOT NULL,
            feature_json TEXT DEFAULT '{}' NOT NULL,
            created_at DATETIME NOT NULL
        )
    """))
    connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_fund_features ON fund_features(trade_date, asset_type, code)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_fund_features_trade_date ON fund_features(trade_date)"))
    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS fund_sector_exposure (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date DATE NOT NULL,
            asset_type VARCHAR(24) NOT NULL,
            code VARCHAR(32) NOT NULL,
            name VARCHAR(128) NOT NULL,
            sector_code VARCHAR(32),
            sector_name VARCHAR(128) NOT NULL,
            source VARCHAR(64) DEFAULT 'holding',
            confidence FLOAT,
            coverage FLOAT,
            created_at DATETIME NOT NULL
        )
    """))
    connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_fund_sector_exposure ON fund_sector_exposure(trade_date, asset_type, code, sector_name)"))
    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS signal_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date DATE NOT NULL,
            model_version VARCHAR(64) NOT NULL,
            asset_type VARCHAR(24) NOT NULL,
            code VARCHAR(32) NOT NULL,
            name VARCHAR(128) NOT NULL,
            action VARCHAR(32) NOT NULL,
            score FLOAT,
            confidence FLOAT DEFAULT 0 NOT NULL,
            risk_level VARCHAR(24),
            status VARCHAR(24) DEFAULT 'active' NOT NULL,
            reason_json TEXT DEFAULT '{}' NOT NULL,
            risk_json TEXT DEFAULT '{}' NOT NULL,
            invalid_json TEXT DEFAULT '{}' NOT NULL,
            feature_json TEXT DEFAULT '{}' NOT NULL,
            created_at DATETIME NOT NULL
        )
    """))
    connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_signal_event ON signal_events(trade_date, model_version, asset_type, code)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_signal_events_date_action ON signal_events(trade_date, action)"))
    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS signal_model_versions (
            model_version VARCHAR(64) PRIMARY KEY,
            status VARCHAR(24) DEFAULT 'draft' NOT NULL,
            weights_json TEXT DEFAULT '{}' NOT NULL,
            backtest_json TEXT DEFAULT '{}' NOT NULL,
            notes TEXT,
            updated_at DATETIME NOT NULL
        )
    """))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_signal_model_versions_status ON signal_model_versions(status)"))


_MIGRATIONS = ((1, _migration_1), (2, _migration_2), (3, _migration_3))


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()