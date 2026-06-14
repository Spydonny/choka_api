"""Редактируемые из админки настройки клуба.

Хранятся одним документом в Mongo и перекрывают значения по умолчанию из config.
Значения берутся из базы в рантайме, поэтому правки применяются без перезапуска.
"""
from config import (
    CLUB_INFO, BONUS_CASHBACK_PCT, BONUS_ACCRUE_WINDOW_HOURS,
    REMINDER_LEAD_MINUTES, REVIEW_MAX_AGE_HOURS,
)
from db import settings_col

# Значения по умолчанию (если в базе ещё ничего не сохранено).
DEFAULTS = {
    "club_name": CLUB_INFO["name"],
    "club_phone": CLUB_INFO["phone"],
    "club_address": CLUB_INFO["address"],
    "club_hours": CLUB_INFO["hours"],
    "cashback_pct": BONUS_CASHBACK_PCT,
    "bonus_accrue_window_hours": BONUS_ACCRUE_WINDOW_HOURS,
    "reminder_lead_minutes": REMINDER_LEAD_MINUTES,
    "review_max_age_hours": REVIEW_MAX_AGE_HOURS,
}
_INT_KEYS = {
    "cashback_pct", "bonus_accrue_window_hours",
    "reminder_lead_minutes", "review_max_age_hours",
}
_STR_KEYS = {"club_name", "club_phone", "club_address", "club_hours"}


def get_all() -> dict:
    """Текущие настройки: дефолты, перекрытые сохранёнными значениями."""
    doc = settings_col.find_one({"_id": "app"}) or {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = doc.get(k)
        if v is not None:
            out[k] = v
    return out


def get(key: str):
    """Одно значение настройки (с дефолтом)."""
    return get_all().get(key, DEFAULTS.get(key))


def update(data: dict) -> dict:
    """Сохраняет переданные поля (только известные ключи), возвращает все настройки."""
    upd: dict = {}
    for k, v in (data or {}).items():
        if k in _INT_KEYS:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"Поле «{k}» должно быть числом")
            if iv < 0:
                raise ValueError(f"Поле «{k}» не может быть отрицательным")
            upd[k] = iv
        elif k in _STR_KEYS:
            s = str(v).strip()
            if not s:
                raise ValueError("Поля контактов не могут быть пустыми")
            upd[k] = s
    if not upd:
        raise ValueError("Нет корректных полей для сохранения")
    settings_col.update_one({"_id": "app"}, {"$set": upd}, upsert=True)
    return get_all()
