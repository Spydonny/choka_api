"""Серверный разбор русской даты/времени из текста клиента.

Модель (LLM) ненадёжно считает «завтра» и читает «22:00», поэтому конкретные
числа извлекаем здесь детерминированно и накладываем поверх блока [[BOOKING]].
Цель — не доверять модели арифметику дат, а делать её кодом.
"""
from datetime import date, datetime, timedelta
from typing import Optional, TypedDict

# Месяцы в именительном и родительном падеже (и сокращения).
_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "май": 5,
    "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
    "ноябр": 11, "декабр": 12,
}
_WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3,
    "пятниц": 4, "суббот": 5, "воскресень": 6,
}


class ParsedDateTime(TypedDict, total=False):
    date: str        # YYYY-MM-DD
    time_from: str   # HH:MM
    time_to: str     # HH:MM


def _fmt_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _fmt_time(h: int, mi: int) -> str:
    return f"{h:02d}:{mi:02d}"


def _parse_date(text: str, today: date) -> Optional[str]:
    """Достаёт дату из текста. Возвращает YYYY-MM-DD или None."""
    import re

    t = text.lower()

    # 1) Относительные слова — самый частый и самый «ломкий» для модели случай.
    if "послезавтра" in t:
        return _fmt_date(today + timedelta(days=2))
    if "завтра" in t:
        return _fmt_date(today + timedelta(days=1))
    if "сегодня" in t or "сейчас" in t or "вечером" in t:
        return _fmt_date(today)

    # 2) Числовой формат: 13.06, 13.06.2026, 13/06, 13-06.
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", t)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = today.year
        if m.group(3):
            year = int(m.group(3))
            if year < 100:
                year += 2000
        d = _safe_date(year, month, day)
        if d:
            # Без указанного года и дата уже в прошлом — значит следующий год.
            if not m.group(3) and d < today:
                d = _safe_date(year + 1, month, day) or d
            return _fmt_date(d)

    # 3) «13 июня», «13 июнь».
    m = re.search(r"\b(\d{1,2})\s+([а-я]+)", t)
    if m:
        day = int(m.group(1))
        word = m.group(2)
        for stem, month in _MONTHS.items():
            if word.startswith(stem):
                d = _safe_date(today.year, month, day)
                if d:
                    if d < today:
                        d = _safe_date(today.year + 1, month, day) or d
                    return _fmt_date(d)
                break

    # 4) День недели: «в субботу».
    for stem, wd in _WEEKDAYS.items():
        if stem in t:
            days_ahead = (wd - today.weekday()) % 7
            return _fmt_date(today + timedelta(days=days_ahead))

    return None


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_time(text: str) -> ParsedDateTime:
    """Достаёт время начала/конца. Возможные ключи: time_from, time_to."""
    import re

    t = text.lower()
    result: ParsedDateTime = {}

    def hm(h: str, mi: Optional[str]) -> Optional[str]:
        hh = int(h)
        mm = int(mi) if mi else 0
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return _fmt_time(hh, mm)
        return None

    # 1) Диапазон по ключевому слову: «с 18:00 до 20:00», «с 18 до 20», «18 по 20».
    m = re.search(
        r"(?:с\s*)?(\d{1,2})(?:[:.](\d{2}))?\s*(?:до|по)\s*(\d{1,2})(?:[:.](\d{2}))?",
        t,
    )
    if not m:
        # Диапазон через дефис только если обе стороны с минутами — иначе путаем с датой.
        m = re.search(r"(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})", t)
    if m:
        tf = hm(m.group(1), m.group(2))
        tt = hm(m.group(3), m.group(4))
        if tf:
            result["time_from"] = tf
        if tt:
            result["time_to"] = tt
        if tf or tt:
            return result

    # 2) Длительность — ТОЛЬКО при явном слове «час» (иначе «на 2 человека» = 2 часа).
    dur = None
    m = re.search(r"\bна\s+(\d{1,2})\s*час", t)
    if m:
        dur = int(m.group(1))
    elif re.search(r"\bна\s+час", t):
        dur = 1

    # 3) Время начала: «в 22:00», «к 22», «в 22 час», «22:00».
    m = re.search(r"\b(?:в|к)\s*(\d{1,2})(?:[:.](\d{2}))?(?:\s*час)?\b", t)
    if not m:
        m = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
    if m:
        tf = hm(m.group(1), m.group(2))
        if tf:
            result["time_from"] = tf
            if dur:
                start_h = int(tf[:2])
                end_h = (start_h + dur) % 24
                result["time_to"] = _fmt_time(end_h, int(tf[3:]))

    return result


def _parse_relative(text: str, now: datetime) -> ParsedDateTime:
    """Относительное смещение от текущего момента: «через час», «через 2 часа»,
    «через 30 минут», «через полчаса». Возвращает date+time_from или {}.

    Такую арифметику модель путает (видели «через час» -> 04:00 вместо 02:58),
    поэтому считаем её кодом от реального времени.
    """
    import re

    t = text.lower()
    delta = None
    if "через полчаса" in t:
        delta = 30
    else:
        m = re.search(r"через\s+(\d{1,3})\s*(?:час\w*|ч)\b", t)
        if m:
            delta = int(m.group(1)) * 60
        elif re.search(r"\bчерез\s+час\w*\b", t):
            delta = 60
        else:
            m = re.search(r"через\s+(\d{1,3})\s*(?:минут\w*|мин)\b", t)
            if m:
                delta = int(m.group(1))
    if delta is None:
        return {}

    target = now + timedelta(minutes=delta)
    return {"date": _fmt_date(target.date()), "time_from": _fmt_time(target.hour, target.minute)}


def parse_user_datetime(messages: list[str], now: datetime) -> ParsedDateTime:
    """Разбирает дату/время из сообщений клиента (от новых к старым).

    Для каждого поля берём первое (самое свежее) уверенное совпадение.
    `messages` — тексты реплик клиента по возрастанию времени; `now` — текущее
    KZ-время (нужно для относительных «через час» и относительных дат).
    """
    today = now.date()
    result: ParsedDateTime = {}
    for text in reversed(messages):
        if "time_from" not in result:
            # Относительное «через ...» задаёт сразу дату и время начала.
            rel = _parse_relative(text, now)
            if rel:
                result.setdefault("date", rel["date"])
                result["time_from"] = rel["time_from"]
        if "date" not in result:
            d = _parse_date(text, today)
            if d:
                result["date"] = d
        if "time_from" not in result:
            t = _parse_time(text)
            if "time_from" in t:
                result["time_from"] = t["time_from"]
                if "time_to" in t:
                    result["time_to"] = t["time_to"]
        if "date" in result and "time_from" in result:
            break
    return result
