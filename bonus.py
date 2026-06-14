"""Бонусная система по номеру телефона.

У каждого клиента один бонусный счёт, ключ — телефон (только цифры).
Бонусы начисляются автоматически как кэшбэк с брони (BONUS_CASHBACK_PCT)
и вручную админом. Списываются (redeem) при оплате бонусами.
История операций хранится прямо в документе счёта.
"""
import re

from config import BONUS_CASHBACK_PCT, now_kz
from db import bonuses_col


def normalize_phone(phone: str) -> str:
    """Оставляет только цифры: '+7 776-294-90-09' -> '77762949009'."""
    return re.sub(r"\D", "", phone or "")


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
    amount = int(amount)
    if amount <= 0:
        raise ValueError("Сумма начисления должна быть больше нуля")
    return _apply(phone, amount, reason, "manual", name)


def redeem_bonus(phone: str, amount: int, reason: str = "Списание бонусов") -> dict:
    """Списывает бонусы. Нельзя уйти в минус."""
    amount = int(amount)
    if amount <= 0:
        raise ValueError("Сумма списания должна быть больше нуля")
    current = get_bonus(phone)
    if amount > current["balance"]:
        raise ValueError(
            f"Недостаточно бонусов: на счёте {current['balance']}, списать хотите {amount}"
        )
    return _apply(phone, -amount, reason, "redeem")


def accrue_for_booking(phone: str, booking_amount: int, name: str = "") -> int:
    """Кэшбэк с брони. Возвращает сколько бонусов начислено (0 — если выключено)."""
    if BONUS_CASHBACK_PCT <= 0 or booking_amount <= 0:
        return 0
    bonus = round(booking_amount * BONUS_CASHBACK_PCT / 100)
    if bonus <= 0:
        return 0
    try:
        _apply(phone, bonus, f"Кэшбэк {BONUS_CASHBACK_PCT}% с брони", "accrue", name)
    except ValueError:
        return 0
    return bonus
