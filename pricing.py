"""Расчёт стоимости брони. Вся арифметика цен — здесь, в коде, а не в модели.

Правила:
- PS5 (Протозанова, 35): спец-цены за полные часы — 1ч=1500, 2ч=2400, 3ч=2900
  (3 часа = акция «2+1»). За каждый час сверх 3-х — +1500 ₸.
  Примеры: 4ч = 2900+1500 = 4400; 5ч = 2900+2×1500 = 5900.
- Lounge (Протозанова, 35) и VIP-зона (Чехова, 31): депозит за стол/зону
  (до 6 персон) — 2900 ₸ за 2 часа. Считаем блоками по 2 часа.
- Бильярд (Чехова, 31): 2500 ₸/час. Акция «2+1» (каждый 3-й час бесплатно)
  для броней, начатых с 12:00 до 18:00.

Неполные часы округляем вверх до целого (платится начатый час/блок).
Доп. джойстик (400 ₸) в сумму брони не входит — это отдельная опция на месте.
"""
from math import ceil

from config import (
    PS_FULL_HOUR_PRICES, PS_EXTRA_HOUR, DEPOSIT_ZONES,
    BILLIARD_HOUR_PRICE, BILLIARD_PROMO_START_MIN, BILLIARD_PROMO_END_MIN,
)

_MAX_PS_FULL = max(PS_FULL_HOUR_PRICES)  # 3 часа


def _ps_amount(minutes: int) -> int:
    hours = max(1, ceil(minutes / 60))
    if hours <= _MAX_PS_FULL:
        return PS_FULL_HOUR_PRICES[hours]
    return PS_FULL_HOUR_PRICES[_MAX_PS_FULL] + (hours - _MAX_PS_FULL) * PS_EXTRA_HOUR


def _deposit_amount(minutes: int, cfg: dict) -> int:
    blocks = max(1, ceil(minutes / cfg["block_minutes"]))
    return blocks * cfg["block_price"]


def _billiard_amount(minutes: int, start_minute) -> int:
    hours = max(1, ceil(minutes / 60))
    in_promo = (
        start_minute is not None
        and BILLIARD_PROMO_START_MIN <= start_minute < BILLIARD_PROMO_END_MIN
    )
    # Акция 2+1: каждый 3-й час бесплатно (платим за 2 из каждых 3 часов).
    free = hours // 3 if in_promo else 0
    paid = hours - free
    return paid * BILLIARD_HOUR_PRICE


def booking_amount(minutes: int, zone: str = "ps", start_minute=None) -> int:
    """Стоимость брони в тенге по длительности (минуты), зоне и времени начала.

    start_minute (минуты от полуночи) нужен для акции бильярда 12:00–18:00.
    """
    if minutes <= 0:
        return 0
    if zone == "ps":
        return _ps_amount(minutes)
    if zone in DEPOSIT_ZONES:
        return _deposit_amount(minutes, DEPOSIT_ZONES[zone])
    if zone == "billiard":
        return _billiard_amount(minutes, start_minute)
    # Неизвестная зона — подстраховка по часовой ставке PS5.
    return ceil(minutes / 60) * PS_FULL_HOUR_PRICES[1]
