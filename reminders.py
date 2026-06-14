"""Фоновые уведомления клиентам:
  - напоминание за REMINDER_LEAD_MINUTES минут до начала брони;
  - просьба оставить отзыв после её окончания.

Запускается фоновым циклом из main (lifespan). Идемпотентно: на каждой броне
ставятся метки reminder_sent_at / review_sent_at, повторно не шлём.
"""
from datetime import datetime, timedelta

from config import ZONE_LABELS, GREEN_API_ID, now_kz
from db import (
    bookings_pending_reminder, bookings_pending_review,
    mark_reminder_sent, mark_review_sent,
)
from whatsapp import notify_client
import settings_store


def _parse(iso):
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def _reminder_text(b: dict) -> str:
    label = ZONE_LABELS.get(b.get("zone", ""), b.get("zone", ""))
    return (
        f"Напоминаем о вашей броне в {b.get('time_from', '')}: {label}, {b.get('date', '')}, "
        f"с {b.get('time_from', '')} до {b.get('time_to', '')}. Скоро ждём вас в CHOKA!"
    )


def _review_text(b: dict) -> str:
    return (
        "Спасибо, что были в CHOKA! Как всё прошло? Будем благодарны за короткий "
        "отзыв — просто ответьте сообщением, ваше мнение помогает нам стать лучше."
    )


def process_due_notifications():
    """Один проход: разослать напоминания и просьбы об отзыве, что подошли по времени."""
    # Без ключей Green API отправлять некуда — не помечаем брони, попробуем позже.
    if not GREEN_API_ID:
        return

    now = now_kz()

    # 1) Напоминания: бронь начинается в ближайшие N минут (из настроек).
    horizon = now + timedelta(minutes=settings_store.get("reminder_lead_minutes"))
    for b in bookings_pending_reminder(horizon.isoformat()):
        start = _parse(b.get("start_at"))
        if start and now < start:
            notify_client(b.get("phone", ""), _reminder_text(b))
        # Уже начавшиеся (start <= now) просто помечаем, чтобы не висели.
        mark_reminder_sent(b["_id"])

    # 2) Отзывы: бронь недавно закончилась (не позже N часов назад, из настроек).
    oldest = now - timedelta(hours=settings_store.get("review_max_age_hours"))
    for b in bookings_pending_review(now.isoformat()):
        end = _parse(b.get("end_at"))
        if end and end >= oldest:
            notify_client(b.get("phone", ""), _review_text(b))
        mark_review_sent(b["_id"])
