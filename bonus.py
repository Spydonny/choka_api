"""Бонусная система по номеру телефона.

У каждого клиента один бонусный счёт, ключ — телефон (только цифры).
Бонусы начисляются автоматически как кэшбэк с брони (BONUS_CASHBACK_PCT)
и вручную админом. Списываются (redeem) при оплате бонусами.
История операций хранится прямо в документе счёта.
"""
from config import BONUS_CASHBACK_PCT, BONUS_ACCRUE_WINDOW_HOURS, now_kz
from db import bonuses_col, find_last_booking_within, mark_booking_accrued
from phones import normalize_phone, is_valid_phone, PHONE_ERROR


def _public(doc: dict | None, phone: str) -> dict:
    """Безопасное представление счёта для ответа API."""
    if not doc:
        return {"phone": phone, "balance": 0, "history": []}
    return {
        "phone": doc.get("phone", phone),
        "name": doc.get("name", ""),
        "balance": doc.get("balance", 0),
        "history": doc.get("history", [])[-50:][::-1],  # свежие сверху
    }


def get_bonus(phone: str) -> dict:
    """Возвращает бонусный счёт по номеру телефона."""
    p = normalize_phone(phone)
    if not p:
        return {"phone": "", "balance": 0, "history": []}
    return _public(bonuses_col.find_one({"phone": p}), p)


def format_bonus_summary(phone: str) -> str:
    """Текст для бота: баланс бонусов клиента + последние операции (по его номеру)."""
    acc = get_bonus(phone)
    bal = acc.get("balance", 0)
    lines = [f"Ваш бонусный баланс: {bal} бонусов."]
    hist = acc.get("history", [])[:5]  # уже свежие сверху
    if hist:
        lines.append("Последние операции:")
        for h in hist:
            delta = h.get("delta", 0)
            sign = "+" if delta >= 0 else ""
            lines.append(f"  {sign}{delta} — {h.get('reason', '')}")
    return "\n".join(lines)


def _apply(phone: str, delta: int, reason: str, kind: str, name: str = "") -> dict:
    """Меняет баланс на delta и пишет запись в историю. Возвращает счёт."""
    p = normalize_phone(phone)
    if not p:
        raise ValueError("Некорректный номер телефона")

    entry = {
        "kind": kind,            # accrue | manual | redeem
        "delta": delta,
        "reason": reason,
        "created_at": now_kz().isoformat(),
    }
    update = {
        "$inc": {"balance": delta},
        "$push": {"history": entry},
        "$setOnInsert": {"phone": p, "created_at": now_kz().isoformat()},
    }
    if name:
        update["$set"] = {"name": name}
    bonuses_col.update_one({"phone": p}, update, upsert=True)
    return get_bonus(p)


def add_bonus(phone: str, amount: int, reason: str = "Начисление вручную", name: str = "") -> dict:
    """Начисляет бонусы вручную (админ)."""
    if not is_valid_phone(phone):
        raise ValueError(PHONE_ERROR)
    amount = int(amount)
    if amount <= 0:
        raise ValueError("Сумма начисления должна быть больше нуля")
    return _apply(phone, amount, reason, "manual", name)


def redeem_bonus(phone: str, amount: int, reason: str = "Списание бонусов") -> dict:
    """Списывает бонусы. Нельзя уйти в минус."""
    if not is_valid_phone(phone):
        raise ValueError(PHONE_ERROR)
    amount = int(amount)
    if amount <= 0:
        raise ValueError("Сумма списания должна быть больше нуля")
    current = get_bonus(phone)
    if amount > current["balance"]:
        raise ValueError(
            f"Недостаточно бонусов: на счёте {current['balance']}, списать хотите {amount}"
        )
    return _apply(phone, -amount, reason, "redeem")


def accrue_from_last_booking(phone: str, hours: int = BONUS_ACCRUE_WINDOW_HOURS) -> dict:
    """Начисляет кэшбэк по ПОСЛЕДНЕЙ броне клиента за последние `hours` часов.

    Сумму бонусов считаем от стоимости этой брони (BONUS_CASHBACK_PCT). Одну и ту
    же бронь повторно не начисляем (флаг bonus_accrued на документе брони).
    Возвращает обновлённый бонусный счёт (как add_bonus/redeem_bonus).
    """
    if not is_valid_phone(phone):
        raise ValueError(PHONE_ERROR)
    p = normalize_phone(phone)
    if BONUS_CASHBACK_PCT <= 0:
        raise ValueError("Начисление кэшбэка отключено")

    booking = find_last_booking_within(p, hours)
    if not booking:
        raise ValueError(f"Нет брони за последние {hours} ч для номера {p}")
    if booking.get("bonus_accrued"):
        raise ValueError("По последней брони бонусы уже начислены")

    amount = int(booking.get("amount") or 0)
    bonus = round(amount * BONUS_CASHBACK_PCT / 100)
    if bonus <= 0:
        raise ValueError("По последней брони не из чего начислить (сумма 0)")

    reason = (
        f"Кэшбэк {BONUS_CASHBACK_PCT}% с брони "
        f"{booking.get('date', '')} {booking.get('time_from', '')} ({amount} ₸)"
    ).strip()
    account = _apply(p, bonus, reason, "accrue", booking.get("name", ""))
    mark_booking_accrued(booking["_id"], bonus)
    return account
