"""Бронирование: разбор блока модели, валидация, проверка занятости, сохранение."""
import json
import re
from datetime import timedelta
from typing import Any, Optional

from config import ZONE_CAPACITY, ZONE_LABELS, ZONE_ALIASES, now_kz
from db import bookings_col, save_booking, find_active_bookings, cancel_booking_doc, span_iso
from pricing import booking_amount
from whatsapp import notify_owner
from bonus import accrue_for_booking


def _parse_hm(value: str) -> Optional[int]:
    """'18:30' -> минуты от полуночи; иначе None."""
    if not isinstance(value, str):
        return None
    m = re.match(r"^\s*(\d{1,2})[:.\-](\d{2})\s*$", value)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h * 60 + mi


def _round_to_hour(minutes: int) -> int:
    """Округляет минуты от полуночи к ближайшему целому часу (round half up)."""
    return (minutes + 30) // 60 * 60


def _intervals_overlap(a_from, a_to, b_from, b_to) -> bool:
    return a_from < b_to and b_from < a_to


def normalize_zone(value: str) -> Optional[str]:
    if not value:
        return None
    key = value.strip().lower()
    if key in ZONE_CAPACITY:
        return key
    return ZONE_ALIASES.get(key)


def count_overlapping(zone_key: str, date: str, t_from: int, t_to: int) -> int:
    """Сколько активных броней зоны пересекается с интервалом [t_from, t_to).

    Проверка «span of time» делается прямым запросом в Mongo по нормализованным
    границам start_at/end_at: интервалы пересекаются, если start_at < конца
    запроса И end_at > начала запроса. Работает и для броней через полночь.
    """
    q_start = span_iso(date, t_from)
    q_end = span_iso(date, t_to)
    if not q_start or not q_end:
        return 0
    return bookings_col.count_documents({
        "zone": zone_key,
        "status": {"$ne": "cancelled"},
        "start_at": {"$lt": q_end},
        "end_at": {"$gt": q_start},
    })


def zones_occupancy(
    date: Optional[str] = None,
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
) -> dict[str, Any]:
    """Занятость всех зон на момент/слот — для визуализации мест.

    Без параметров считает «прямо сейчас». Для каждой зоны возвращает места
    (квадраты) со статусом занято/свободно. Конкретные брони к конкретным местам
    не привязаны, поэтому первые `busy` мест считаем занятыми, остальные — свободными.
    """
    now = now_kz()
    date = date or now.strftime("%Y-%m-%d")

    t_from = _parse_hm(time_from) if time_from else None
    if t_from is None:
        t_from = now.hour * 60 + now.minute
    t_to = _parse_hm(time_to) if time_to else None
    if t_to is None or t_to <= t_from:
        t_to = t_from + 1  # окно «момента времени»

    zones = []
    for key, capacity in ZONE_CAPACITY.items():
        busy = min(count_overlapping(key, date, t_from, t_to), capacity)
        zones.append({
            "key": key,
            "label": ZONE_LABELS[key],
            "capacity": capacity,
            "busy": busy,
            "free": capacity - busy,
            "spots": [{"index": i + 1, "occupied": i < busy} for i in range(capacity)],
        })

    return {
        "date": date,
        "time_from": f"{t_from // 60:02d}:{t_from % 60:02d}",
        "time_to": f"{t_to // 60:02d}:{t_to % 60:02d}",
        "zones": zones,
    }


def validate_booking(booking: dict[str, Any]) -> tuple[bool, str, Optional[dict]]:
    """Проверяет поля брони. Возвращает (ok, сообщение_об_ошибке, нормализованная_бронь)."""
    zone = normalize_zone(str(booking.get("zone", "")))
    if not zone:
        return False, "Уточните, что бронируем: PS5, Lounge, Бильярд или VIP-комната?", None

    date = str(booking.get("date", "")).strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return False, "Уточните, пожалуйста, дату брони (например, 2026-06-12).", None

    # Дата в прошлом почти всегда означает, что модель ошиблась с «завтра» —
    # лучше переспросить, чем подтвердить бронь на вчера.
    today_kz = now_kz().strftime("%Y-%m-%d")
    if date < today_kz:
        return False, "Кажется, эта дата уже прошла. На какую дату забронировать?", None

    t_from = _parse_hm(str(booking.get("time_from", "")))
    t_to = _parse_hm(str(booking.get("time_to", "")))
    if t_from is None or t_to is None:
        return False, "С какого и до какого времени бронируем? Напишите, например: с 18:00 до 20:00.", None
    if t_to <= t_from:
        return False, "Время окончания должно быть позже начала. С какого и до какого времени?", None

    # Бронь всегда по целым часам: округляем к ближайшему часу (2:07–3:50 → 2:00–4:00).
    t_from = _round_to_hour(t_from)
    t_to = min(_round_to_hour(t_to), 24 * 60)
    if t_to <= t_from:
        t_to = t_from + 60  # минимум 1 час

    try:
        persons = int(booking.get("persons", 0))
    except (TypeError, ValueError):
        persons = 0
    if persons < 1:
        return False, "Сколько человек будет? Уточните количество.", None

    name = str(booking.get("name", "")).strip() or "Гость"

    # Сумму НЕ берём у модели — считаем в коде по длительности, зоне и времени
    # начала (нужно для акции бильярда 12:00–18:00). См. pricing.py.
    amount = booking_amount(t_to - t_from, zone, start_minute=t_from)

    normalized = {
        "zone": zone,
        "date": date,
        "time_from": f"{t_from // 60:02d}:{t_from % 60:02d}",
        "time_to": f"{t_to // 60:02d}:{t_to % 60:02d}",
        "_t_from": t_from,
        "_t_to": t_to,
        "persons": persons,
        "name": name,
        "amount": amount,
    }
    return True, "", normalized


def try_create_booking(chat_id: str, raw_booking: dict[str, Any]) -> str:
    """Валидирует, проверяет занятость и сохраняет бронь. Возвращает текст клиенту."""
    ok, err, b = validate_booking(raw_booking)
    if not ok:
        return err

    capacity = ZONE_CAPACITY[b["zone"]]
    busy = count_overlapping(b["zone"], b["date"], b["_t_from"], b["_t_to"])
    label = ZONE_LABELS[b["zone"]]

    if busy >= capacity:
        return (
            f"К сожалению, на {b['date']} с {b['time_from']} до {b['time_to']} "
            f"зона «{label}» уже занята (все {capacity} мест). "
            "Выберите, пожалуйста, другое время или дату."
        )

    phone = chat_id.split("@")[0]
    doc = save_booking(
        phone=phone,
        name=b["name"],
        zone_key=b["zone"],
        date=b["date"],
        time_from=b["time_from"],
        time_to=b["time_to"],
        persons=b["persons"],
        amount=b["amount"],
        start_at=span_iso(b["date"], b["_t_from"]),
        end_at=span_iso(b["date"], b["_t_to"]),
    )

    # Кэшбэк бонусами на номер клиента (см. bonus.py / BONUS_CASHBACK_PCT).
    accrued = accrue_for_booking(phone, b["amount"], b["name"])

    notify_owner(
        f"Зона: {label}\n"
        f"Дата: {b['date']}\n"
        f"Время: {b['time_from']}–{b['time_to']}\n"
        f"Человек: {b['persons']}\n"
        f"Сумма: {b['amount']} ₸\n"
        f"Имя: {b['name']}\n"
        f"Телефон: {phone}\n"
        f"Свободно мест после брони: {capacity - busy - 1}/{capacity}"
    )
    print(f"DEBUG: бронь сохранена: {doc}")

    bonus_line = f" Начислено {accrued} бонусов." if accrued else ""
    return (
        f"Бронь принята! {label}, {b['date']}, с {b['time_from']} до {b['time_to']}, "
        f"{b['persons']} чел. Стоимость: {b['amount']} ₸.{bonus_line} "
        f"Ждём вас! Если что-то изменится — напишите нам."
    )


def check_availability(raw: dict[str, Any]) -> str:
    """Достоверный ответ о наличии мест: смотрит занятость в базе, а не «из головы».

    Зону по умолчанию считаем PS5. Дата обязательна (её подставляет серверный
    разбор). Если конца времени нет — проверяем часовое окно от начала.
    """
    zone = normalize_zone(str(raw.get("zone", ""))) or "ps"
    label = ZONE_LABELS[zone]

    date = str(raw.get("date", "")).strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return "Подскажите дату — на какой день проверить наличие мест?"
    if date < now_kz().strftime("%Y-%m-%d"):
        return "Эта дата уже прошла. На какую дату проверить наличие?"

    t_from = _parse_hm(str(raw.get("time_from", "")))
    if t_from is None:
        return "На какое время проверить? Напишите, например: на 20:00."
    t_to = _parse_hm(str(raw.get("time_to", "")))
    if t_to is None or t_to <= t_from:
        t_to = min(t_from + 60, 24 * 60)  # нет конца — смотрим часовое окно

    capacity = ZONE_CAPACITY[zone]
    busy = count_overlapping(zone, date, t_from, t_to)
    free = capacity - busy
    tf = f"{t_from // 60:02d}:{t_from % 60:02d}"
    tt = f"{t_to // 60:02d}:{t_to % 60:02d}"

    if free <= 0:
        return (
            f"На {date} с {tf} зона «{label}» занята (все {capacity} мест). "
            "Подскажите другое время или дату — подберём."
        )
    return (
        f"На {date} с {tf} в зоне «{label}» есть свободные места "
        f"({free} из {capacity}). Бронируем? Подскажите имя и сколько человек."
    )


_WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _open_hour_starts() -> list[int]:
    """Часы работы клуба (12:00–05:00) в минутах от полуночи — старты часовых слотов."""
    return [h * 60 for h in range(24) if h < 5 or h >= 12]


def _merge_consecutive_hours(starts: list[int]) -> list[tuple[int, int]]:
    """Сливает соседние часовые слоты в интервалы: [18:00,19:00,20:00] -> 18:00–21:00."""
    ranges: list[tuple[int, int]] = []
    for s in sorted(set(starts)):
        if ranges and ranges[-1][1] == s:
            ranges[-1] = (ranges[-1][0], s + 60)
        else:
            ranges.append((s, s + 60))
    return ranges


def week_schedule(raw: dict[str, Any], days: int = 7) -> str:
    """Занятость зоны на ближайшие N дней: по дням — часы, где есть брони.

    Отвечает на вопрос «когда занято на неделе». Один запрос в базу по интервалу
    недели (start_at/end_at), занятость по часам считаем в коде.
    """
    zone = normalize_zone(str(raw.get("zone", ""))) or "ps"
    label = ZONE_LABELS[zone]
    capacity = ZONE_CAPACITY[zone]
    now = now_kz()
    now_min = now.hour * 60 + now.minute

    start_iso = span_iso(now.strftime("%Y-%m-%d"), 0)
    end_iso = span_iso((now + timedelta(days=days)).strftime("%Y-%m-%d"), 0)

    # Все активные брони зоны за окно недели — одним запросом.
    day_intervals: dict[str, list[tuple[int, int]]] = {}
    cursor = bookings_col.find({
        "zone": zone,
        "status": {"$ne": "cancelled"},
        "start_at": {"$lt": end_iso},
        "end_at": {"$gt": start_iso},
    })
    for b in cursor:
        bf = _parse_hm(b.get("time_from", ""))
        bt = _parse_hm(b.get("time_to", ""))
        if bf is None or bt is None:
            continue
        day_intervals.setdefault(b.get("date", ""), []).append((bf, bt))

    lines = []
    for i in range(days):
        d = now + timedelta(days=i)
        date = d.strftime("%Y-%m-%d")
        intervals = day_intervals.get(date, [])
        busy_starts, full_starts = [], []
        for hs in _open_hour_starts():
            if i == 0 and hs + 60 <= now_min:
                continue  # уже прошедшие часы сегодня не показываем
            busy = sum(1 for bf, bt in intervals if bf < hs + 60 and hs < bt)
            if busy > 0:
                busy_starts.append(hs)
            if busy >= capacity:
                full_starts.append(hs)
        wd = _WEEKDAYS_SHORT[d.weekday()]
        full = set(full_starts)
        if busy_starts:
            parts = []
            for a, b in _merge_consecutive_hours(busy_starts):
                tag = " (нет мест)" if all(h in full for h in range(a, b, 60)) else ""
                parts.append(f"{a // 60:02d}:00–{b // 60:02d}:00{tag}")
            lines.append(f"{d.strftime('%d.%m')} ({wd}): {', '.join(parts)}")
        else:
            lines.append(f"{d.strftime('%d.%m')} ({wd}): свободно весь день")

    return (
        f"Занятость «{label}» на {days} дней (часы, где уже есть брони; "
        f"всего мест: {capacity}):\n"
        + "\n".join(lines)
        + "\nВ остальное время места свободны. Назовите день и время — забронирую."
    )


def _format_booking(b: dict) -> str:
    """Короткое человекочитаемое описание брони для списков/подтверждений."""
    label = ZONE_LABELS.get(b.get("zone", ""), b.get("zone", ""))
    return (
        f"{label}, {b.get('date', '?')}, с {b.get('time_from', '?')} "
        f"до {b.get('time_to', '?')}, {b.get('persons', '?')} чел."
    )


def list_bookings(chat_id: str) -> str:
    """Показывает клиенту его активные брони (по номеру телефона из chat_id)."""
    phone = chat_id.split("@")[0]
    active = find_active_bookings(phone)
    if not active:
        return "У вас нет активных броней."
    lines = "\n".join(f"{i}. {_format_booking(b)}" for i, b in enumerate(active, 1))
    return f"Ваши активные брони:\n{lines}\nЧтобы отменить — напишите, какую именно."


def cancel_booking(chat_id: str, raw: dict[str, Any]) -> str:
    """Отменяет бронь клиента по его телефону. Сужает выбор по дате/зоне/времени.

    Клиент может отменить ТОЛЬКО свои брони — телефон берётся из chat_id, не из текста.
    """
    phone = chat_id.split("@")[0]
    active = find_active_bookings(phone)
    if not active:
        return "У вас нет активных броней для отмены."

    matches = active
    date = str(raw.get("date", "")).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        matches = [b for b in matches if b.get("date") == date]
    zone = normalize_zone(str(raw.get("zone", "")))
    if zone:
        matches = [b for b in matches if b.get("zone") == zone]
    tf = _parse_hm(str(raw.get("time_from", "")))
    if tf is not None:
        matches = [b for b in matches if _parse_hm(b.get("time_from", "")) == tf]

    if len(matches) == 0:
        lst = "\n".join(f"- {_format_booking(b)}" for b in active)
        return f"Не нашёл такую бронь. Вот ваши активные брони:\n{lst}\nКакую отменить?"
    if len(matches) > 1:
        lst = "\n".join(f"- {_format_booking(b)}" for b in matches)
        return f"Под запрос подходит несколько броней:\n{lst}\nУточните дату и время — какую отменить?"

    b = matches[0]
    if not cancel_booking_doc(b["_id"]):
        return "Эта бронь уже отменена."
    notify_owner(f"ОТМЕНА брони:\n{_format_booking(b)}\nТелефон: {phone}")
    return f"Бронь отменена: {_format_booking(b)} Если передумаете — напишите нам."


def _extract_block(reply: str, tag: str, lenient: bool = False) -> tuple[str, Optional[dict]]:
    """Достаёт [[TAG]]{...}[[/TAG]] из ответа модели.

    Возвращает (видимый_текст_без_блока, данные_или_None). При lenient=True блок
    без валидного JSON всё равно срабатывает как пустой словарь {} (для действий
    без обязательных параметров — список/отмена своих броней).
    """
    match = re.search(rf"\[\[{tag}\]\](.*?)\[\[/{tag}\]\]", reply, re.DOTALL)
    if not match:
        return reply, None

    visible = (reply[: match.start()] + reply[match.end():]).strip()
    payload = match.group(1).strip()
    try:
        data = json.loads(payload) if payload else {}
        if not isinstance(data, dict):
            data = {} if lenient else None
    except json.JSONDecodeError:
        print(f"DEBUG: не удалось распарсить блок {tag}: {payload!r}")
        data = {} if lenient else None
    return visible, data


def extract_booking_block(reply: str) -> tuple[str, Optional[dict]]:
    """Достаёт [[BOOKING]]{...}[[/BOOKING]] из ответа модели."""
    return _extract_block(reply, "BOOKING")


def extract_check_block(reply: str) -> tuple[str, Optional[dict]]:
    """Достаёт [[CHECK]]{...}[[/CHECK]] из ответа модели."""
    return _extract_block(reply, "CHECK")


def extract_cancel_block(reply: str) -> tuple[str, Optional[dict]]:
    """Достаёт [[CANCEL]]{...}[[/CANCEL]] — отмена брони (параметры необязательны)."""
    return _extract_block(reply, "CANCEL", lenient=True)


def extract_mybookings_block(reply: str) -> tuple[str, Optional[dict]]:
    """Достаёт [[MYBOOKINGS]] — показать брони клиента (параметры не нужны)."""
    return _extract_block(reply, "MYBOOKINGS", lenient=True)


def extract_schedule_block(reply: str) -> tuple[str, Optional[dict]]:
    """Достаёт [[SCHEDULE]] — занятость зоны на неделю (зона необязательна)."""
    return _extract_block(reply, "SCHEDULE", lenient=True)
