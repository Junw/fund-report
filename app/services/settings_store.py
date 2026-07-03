from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import AppSetting


LLM_BASE_URL = "llm_base_url"
LLM_API_KEY = "llm_api_key"
LLM_MODEL = "llm_model"


def get_app_setting(session: Session, key: str) -> str | None:
    row = session.execute(select(AppSetting).where(AppSetting.key == key)).scalar_one_or_none()
    return row.value if row else None


def set_app_setting(session: Session, key: str, value: str | None) -> None:
    row = session.execute(select(AppSetting).where(AppSetting.key == key)).scalar_one_or_none()
    if row is None:
        session.add(AppSetting(key=key, value=value, updated_at=datetime.utcnow()))
    else:
        row.value = value
        row.updated_at = datetime.utcnow()


def get_llm_config(session: Session | None = None) -> dict[str, str | None]:
    if session is not None:
        return {
            "base_url": get_app_setting(session, LLM_BASE_URL) or settings.llm_base_url,
            "api_key": get_app_setting(session, LLM_API_KEY) or settings.llm_api_key,
            "model": get_app_setting(session, LLM_MODEL) or settings.llm_model,
        }
    with SessionLocal() as local_session:
        return get_llm_config(local_session)


def save_llm_config(session: Session, base_url: str | None, api_key: str | None, model: str | None) -> None:
    set_app_setting(session, LLM_BASE_URL, _clean(base_url))
    if api_key and api_key.strip():
        set_app_setting(session, LLM_API_KEY, api_key.strip())
    set_app_setting(session, LLM_MODEL, _clean(model))


def masked_llm_config(session: Session) -> dict[str, str | bool | None]:
    config = get_llm_config(session)
    api_key = config.get("api_key")
    return {
        "base_url": config.get("base_url") or "",
        "model": config.get("model") or "",
        "api_key_masked": mask_secret(api_key),
        "configured": bool(config.get("base_url") and api_key and config.get("model")),
    }


def mask_secret(value: str | None) -> str:
    if not value:
        return "未配置"
    if len(value) <= 8:
        return value[0:1] + "******" + value[-1:]
    return value[:4] + "******" + value[-4:]


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
