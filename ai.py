"""Слой ИИ: история диалога, сборка промпта, ответ Groq, обработка брони."""
import re
from datetime import datetime

from groq import Groq

from config import (
    GROQ_API_KEY, GROQ_MODEL, MAX_TOKENS, TEMPERATURE, MAX_HISTORY,
    HISTORY_TTL_HOURS, now_kz,
)
from db import get_owner_metrics, load_history, save_session
from prompts import CLIENT_PROMPT, OWNER_PROMPT
from security import sanitize_user_input, looks_like_injection
from booking import (
    extract_booking_block, extract_check_block, extract_cancel_block,
    extract_mybookings_block, extract_schedule_block, try_create_booking,
    check_availability, cancel_booking, list_bookings, week_schedule,
)
from datetime_parse import parse_user_datetime

client = Groq(api_key=GROQ_API_KEY)


_WEEKDAYS_RU = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]


def _now_kz() -> datetime:
    """Текущие дата и время в часовом поясе клуба (обёртка над config.now_kz)."""
    return now_kz()


def _is_open_now(now: datetime) -> bool:
    """Клуб работает с 12:00 до 05:00 — закрыт в окне 05:00–12:00."""
    return not (5 <= now.hour < 12)


def build_time_context() -> str:
    """Строка с реальными датой/временем — чтобы бот не был оторван от реальности."""
    now = _now_kz()
    weekday = _WEEKDAYS_RU[now.weekday()]
    is_weekend = now.weekday() >= 5
    day_type = "выходной" if is_weekend else "будний день"
    open_state = "сейчас клуб открыт" if _is_open_now(now) else "сейчас клуб закрыт (откроемся в 12:00)"
    return (
        f"\n\nРЕАЛЬНОЕ ВРЕМЯ (Усть-Каменогорск, UTC+5):\n"
        f"Сейчас {now.strftime('%H:%M')}, {now.strftime('%Y-%m-%d')}, {weekday} ({day_type}). "
        f"{open_state}.\n"
        "Используй это время при ответах: «сегодня вечером», «до скольки работаете», "
        "будни/выходные тарифы и т.п. Относительные даты (сегодня/завтра) переводи в формат YYYY-MM-DD."
    )


# Директива Qwen3: отключает пошаговые рассуждения (<think>) — быстрее, дешевле,
# нет риска обрезать ответ. Пустой <think></think> всё равно вырезается на сервере.
_NO_THINK = " /no_think"


def build_system_prompt(is_owner: bool) -> str:
    """Собирает системный промпт с реальными датой/временем (и статистикой — для владельца)."""
    date_context = build_time_context()
    if not is_owner:
        return CLIENT_PROMPT + date_context + _NO_THINK

    m = get_owner_metrics()
    if m["week_delta_pct"] is None:
        week_cmp = "нет данных за прошлую неделю для сравнения"
    else:
        sign = "+" if m["week_delta"] >= 0 else ""
        week_cmp = (
            f"{sign}{m['week_delta']} тг к прошлой неделе "
            f"({sign}{m['week_delta_pct']}%, было {m['prev_week_revenue']} тг)"
        )
    stats_context = f"""
ДАННЫЕ ИЗ БАЗЫ (все цифры уже посчитаны, НЕ пересчитывай):
Сегодня: {m['today_bookings']} броней | выручка {m['today_revenue']} тг | диалогов {m['today_dialogs']}
Неделя:  {m['week_bookings']} броней | выручка {m['week_revenue']} тг | в среднем {m['revenue_per_day_week']} тг/день
Месяц:   {m['month_bookings']} броней | выручка {m['month_revenue']} тг | средний чек {m['avg_check']} тг
Динамика недели: {week_cmp}
"""
    return OWNER_PROMPT + stats_context + date_context + _NO_THINK


def _strip_reasoning(reply: str) -> str:
    """Убирает служебные <think>…</think> рассуждения qwen, чтобы они не попали
    клиенту и не сломали разбор управляющих блоков.
    """
    if not reply:
        return ""
    # Закрытые блоки рассуждений.
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL | re.IGNORECASE)
    # Незакрытый <think> (ответ обрезан по лимиту токенов) — режем до конца.
    reply = re.sub(r"<think>.*$", "", reply, flags=re.DOTALL | re.IGNORECASE)
    return reply.strip()


def _has_broken_control_block(reply: str) -> bool:
    """Маркер управляющего блока в ответе есть, но валидным блоком не распознался."""
    has_marker = any(t in reply for t in ("BOOKING", "CHECK", "CANCEL", "MYBOOKINGS", "SCHEDULE"))
    parsed = (
        extract_booking_block(reply)[1] is not None
        or extract_check_block(reply)[1] is not None
        or extract_cancel_block(reply)[1] is not None
        or extract_mybookings_block(reply)[1] is not None
        or extract_schedule_block(reply)[1] is not None
    )
    return has_marker and not parsed


def _apply_server_datetime(user_texts: list[str], booking: dict) -> None:
    """Перекрывает date/time_from/time_to в блоке значениями, разобранными из
    текста клиента на сервере. Модель оставляет зону, кол-во человек и имя.

    user_texts — реплики клиента в хронологическом порядке (включая текущую).
    """
    parsed = parse_user_datetime(user_texts, _now_kz())

    if parsed.get("date"):
        booking["date"] = parsed["date"]
    if parsed.get("time_from"):
        # Бронь всегда по целым часам — округляем уже здесь, чтобы это было видно
        # и в брони, и в проверке наличия (18:30 -> 19:00).
        booking["time_from"] = _round_hhmm(parsed["time_from"])
        # Начало распознали, а конец — нет: НЕ выдумываем (это и был баг),
        # обнуляем конец, чтобы сервер вежливо переспросил время окончания.
        tt = parsed.get("time_to")
        booking["time_to"] = _round_hhmm(tt) if tt else ""

    if parsed:
        print(f"DEBUG: серверный разбор даты/времени: {parsed}")


def _round_hhmm(t: str) -> str:
    """Округляет «HH:MM» к ближайшему целому часу (round half up). Не уходим за
    23:00, чтобы не плодить некорректные «24:00» (брони через полночь не ведём)."""
    total = (int(t[:2]) * 60 + int(t[3:5]) + 30) // 60 * 60
    total = min(total, 23 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def get_ai_reply(user_id: str, user_message: str, is_owner: bool = False) -> str:
    # Защита от промт-инъекций: входящий текст — это данные клиента.
    clean_message = sanitize_user_input(user_message)
    if looks_like_injection(clean_message):
        print(f"DEBUG: подозрение на промт-инъекцию от {user_id}: {clean_message!r}")

    # История диалога — из базы (переживает рестарт), окно по свежести.
    history = load_history(user_id, MAX_HISTORY, HISTORY_TTL_HOURS)

    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": build_system_prompt(is_owner)},
                *history,
                {"role": "user", "content": clean_message},
            ],
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
        )
        reply = _strip_reasoning(response.choices[0].message.content)
    except Exception as e:
        print(f"Groq error: {e}")
        return "Извините, техническая ошибка. Попробуйте ещё раз."

    # Все реплики клиента по порядку (прошлые + текущая) — для серверного разбора даты/времени.
    user_texts = [m["content"] for m in history if m["role"] == "user"]
    user_texts.append(clean_message)

    # Управляющие блоки (бронь / проверка / отмена / список) обрабатывает сервер.
    if not is_owner:
        visible, booking = extract_booking_block(reply)
        visible, check = extract_check_block(visible)
        visible, cancel = extract_cancel_block(visible)
        visible, mybookings = extract_mybookings_block(visible)
        visible, schedule = extract_schedule_block(visible)

        if booking is not None:
            # Дату/время не доверяем модели — парсим из текста клиента на сервере
            # и накладываем поверх блока (модель часто ошибается в «завтра»/«22:00»).
            _apply_server_datetime(user_texts, booking)
            # Авторитетный ответ (подтверждение/отказ) формирует сервер.
            result = try_create_booking(user_id, booking)
            visible = f"{visible}\n{result}".strip() if visible else result
        elif check is not None:
            # Наличие мест — реальный ответ из базы, а не «да/нет» наугад.
            _apply_server_datetime(user_texts, check)
            result = check_availability(check)
            visible = f"{visible}\n{result}".strip() if visible else result
        elif cancel is not None:
            # Отмена — по телефону клиента; дату/время уточняем серверным разбором.
            _apply_server_datetime(user_texts, cancel)
            result = cancel_booking(user_id, cancel)
            visible = f"{visible}\n{result}".strip() if visible else result
        elif mybookings is not None:
            # Список своих броней по номеру телефона.
            result = list_bookings(user_id)
            visible = f"{visible}\n{result}".strip() if visible else result
        elif schedule is not None:
            # Занятость зоны на неделю — реальное расписание из базы (нужна только зона).
            result = week_schedule(schedule)
            visible = f"{visible}\n{result}".strip() if visible else result
        elif _has_broken_control_block(reply):
            # Маркер брони/проверки был, но JSON не распарсился — не делаем вид,
            # что что-то обрабатываем; просим повторить детали.
            visible = (
                "Кажется, я не до конца разобрал детали. Подскажите ещё раз: "
                "зона, дата, время и количество человек?"
            )
    else:
        visible = reply

    if not visible:
        visible = "Извините, не расслышал. Повторите, пожалуйста."

    # Сохраняем обмен в базу — это и лог, и источник истории для след. запроса.
    save_session(user_id, clean_message, visible)
    return visible
