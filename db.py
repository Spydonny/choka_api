"""Доступ к MongoDB: брони, сессии (история диалога), статистика."""
import re
from datetime import datetime, timedelta

from bson import ObjectId
from pymongo import MongoClient, ReturnDocument

from config import MONGODB_URI, MONGODB_DB, ZONE_LABELS, KZ_TZ, now_kz
from phones import normalize_phone

mongo_client = MongoClient(MONGODB_URI)
db = mongo_client[MONGODB_DB]
bookings_col = db["bookings"]
sessions_col = db["sessions"]
tournaments_col = db["tournaments"]
bonuses_col = db["bonuses"]
blocked_col = db["blocked"]


def init_db():
    # MongoDB создаёт коллекции автоматически; задаём индексы для запросов.
    bookings_col.create_index("date")
    bookings_col.create_index("created_at")
    # Интервал брони: запрос на пересечение времени бьёт по этому индексу.
    bookings_col.create_index([("zone", 1), ("start_at", 1), ("end_at", 1)])
    sessions_col.create_index("created_at")
    # История диалога грузится по телефону за последние часы.
    sessions_col.create_index([("phone", 1), ("created_at", 1)])
    tournaments_col.create_index("created_at")
    # Бонусный баланс — один документ на телефон.
    bonuses_col.create_index("phone", unique=True)
    # Заблокированные клиенты — один документ на телефон.
    blocked_col.create_index("phone", unique=True)
    # Доставляем интервал старым броням, у которых его ещё нет.
    backfill_booking_spans()
    # Сводим раздвоенные диалоги (chatId '@c.us' vs. чистые цифры) в один тред.
    normalize_session_phones()
    # Старый статус "new" → "pending" (ожидается оплата); пустой статус тоже.
    backfill_booking_status()
    # Чтобы при старте не разослать напоминания/отзывы за уже прошедшие брони.
    backfill_notification_flags()


def backfill_booking_status():
    """Приводит старые брони к статусам оплаты: 'new'/отсутствует → 'pending'."""
    bookings_col.update_many(
        {"$or": [{"status": "new"}, {"status": {"$exists": False}}]},
        {"$set": {"status": "pending"}},
    )


def span_iso(date_str, total_minutes):
    """('2026-06-14', 1080) -> '2026-06-14T18:00:00+05:00' (минуты от полуночи, KZ).

    Минуты ≥ 1440 (например, конец «24:00») корректно переходят на следующий день.
    """
    try:
        base = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=KZ_TZ)
    except (TypeError, ValueError):
        return None
    return (base + timedelta(minutes=int(total_minutes))).isoformat()


def _hm_to_min(hm):
    """'18:30' / '24:00' -> минуты от полуночи; иначе None."""
    m = re.match(r"^\s*(\d{1,2})[:.\-](\d{2})\s*$", hm or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def backfill_booking_spans():
    """Заполняет start_at/end_at у старых броней из строковых time_from/time_to."""
    for b in bookings_col.find({"start_at": {"$exists": False}}):
        sa = span_iso(b.get("date"), _hm_to_min(b.get("time_from")) or 0)
        em = _hm_to_min(b.get("time_to"))
        ea = span_iso(b.get("date"), em) if em is not None else None
        if sa and ea:
            bookings_col.update_one(
                {"_id": b["_id"]}, {"$set": {"start_at": sa, "end_at": ea}}
            )


def save_booking(phone, name, zone_key, date, time_from, time_to, persons,
                 amount=0, start_at=None, end_at=None):
    doc = {
        "phone": phone,
        "name": name,
        "zone": zone_key,
        "zone_label": ZONE_LABELS.get(zone_key, zone_key),
        "date": date,
        "time_from": time_from,
        "time_to": time_to,
        # Нормализованные границы интервала (KZ ISO) — для проверки пересечений.
        "start_at": start_at,
        "end_at": end_at,
        "persons": persons,
        "amount": amount,
        # Статус оплаты: pending (ожидается) → paid (оплачен) / cancelled (отменён).
        "status": "pending",
        "created_at": now_kz().isoformat(),
    }
    bookings_col.insert_one(doc)
    return doc


# Допустимые статусы оплаты брони.
BOOKING_STATUSES = ("pending", "paid", "cancelled")


def set_booking_status(booking_id, status):
    """Меняет статус оплаты брони. Возвращает обновлённый документ (или None, если
    бронь не найдена) — чтобы вызывающий мог уведомить клиента. Принимает _id строкой."""
    if status not in BOOKING_STATUSES:
        raise ValueError("Недопустимый статус брони")
    if isinstance(booking_id, str):
        if not ObjectId.is_valid(booking_id):
            return None
        booking_id = ObjectId(booking_id)
    upd = {"status": status}
    if status == "cancelled":
        upd["cancelled_at"] = now_kz().isoformat()
    if status == "paid":
        upd["paid_at"] = now_kz().isoformat()
    return bookings_col.find_one_and_update(
        {"_id": booking_id}, {"$set": upd}, return_document=ReturnDocument.AFTER
    )


def list_bookings(limit=100, only_upcoming=False):
    """Список броней для админ-панели (свежие сверху)."""
    query = {}
    if only_upcoming:
        today = now_kz().strftime("%Y-%m-%d")
        query = {"status": {"$ne": "cancelled"}, "date": {"$gte": today}}
    cursor = bookings_col.find(query).sort("created_at", -1).limit(limit)
    out = []
    for b in cursor:
        b = dict(b)
        b["id"] = str(b.pop("_id"))
        out.append(b)
    return out


def bookings_in_range(date_from, date_to, zone=None):
    """Активные брони в диапазоне дат [date_from; date_to] (включительно).

    Для недельного календаря. `date` хранится как 'YYYY-MM-DD' — для ISO-дат
    лексикографическое сравнение совпадает с хронологическим. Опц. фильтр по зоне.
    """
    query = {
        "status": {"$ne": "cancelled"},
        "date": {"$gte": date_from, "$lte": date_to},
    }
    if zone:
        query["zone"] = zone
    out = []
    for b in bookings_col.find(query).sort([("date", 1), ("time_from", 1)]):
        b = dict(b)
        b["id"] = str(b.pop("_id"))
        out.append(b)
    return out


def bookings_pending_reminder(before_iso):
    """Брони (ожидается/оплачено) без отправленного напоминания, начинающиеся не
    позднее before_iso (now + lead). Точную проверку времени делает вызывающий."""
    return list(bookings_col.find({
        "status": {"$in": ["pending", "paid"]},
        "reminder_sent_at": {"$exists": False},
        "start_at": {"$exists": True, "$lte": before_iso},
    }))


def bookings_pending_review(before_iso):
    """Неотменённые брони без отправленной просьбы об отзыве, закончившиеся к before_iso."""
    return list(bookings_col.find({
        "status": {"$ne": "cancelled"},
        "review_sent_at": {"$exists": False},
        "end_at": {"$exists": True, "$lte": before_iso},
    }))


def mark_reminder_sent(booking_id):
    bookings_col.update_one({"_id": booking_id}, {"$set": {"reminder_sent_at": now_kz().isoformat()}})


def mark_review_sent(booking_id):
    bookings_col.update_one({"_id": booking_id}, {"$set": {"review_sent_at": now_kz().isoformat()}})


def backfill_notification_flags():
    """Гасим уведомления для уже прошедших событий — чтобы при первом запуске не
    разослать пачку напоминаний/отзывов за старые брони. Будущие брони не трогаем."""
    now = now_kz().isoformat()
    bookings_col.update_many(
        {"start_at": {"$lte": now}, "reminder_sent_at": {"$exists": False}},
        {"$set": {"reminder_sent_at": "skip"}},
    )
    bookings_col.update_many(
        {"end_at": {"$lte": now}, "review_sent_at": {"$exists": False}},
        {"$set": {"review_sent_at": "skip"}},
    )


def find_bookings_by_phone(phone, limit=5):
    """Последние брони клиента (не отменённые), свежие сверху — для истории посещений."""
    cursor = bookings_col.find({
        "phone": _digits(phone),
        "status": {"$ne": "cancelled"},
    }).sort([("date", -1), ("time_from", -1)]).limit(limit)
    return list(cursor)


def find_active_bookings(phone):
    """Активные (не отменённые, на сегодня и позже) брони клиента по телефону."""
    today = now_kz().strftime("%Y-%m-%d")
    cursor = bookings_col.find({
        "phone": phone,
        "status": {"$ne": "cancelled"},
        "date": {"$gte": today},
    }).sort([("date", 1), ("time_from", 1)])
    return list(cursor)


def find_last_booking_within(phone, hours):
    """Последняя НЕотменённая бронь клиента, созданная за последние `hours` часов.

    Возвращает документ брони или None. Телефон сравниваем по цифрам (как он и
    хранится: из чата — '7705...@c.us'.split('@')[0], с сайта — нормализованный).
    """
    since = (now_kz() - timedelta(hours=hours)).isoformat()
    return bookings_col.find_one(
        {
            "phone": _digits(phone),
            "status": {"$ne": "cancelled"},
            "created_at": {"$gte": since},
        },
        sort=[("created_at", -1)],
    )


def mark_booking_accrued(booking_id, bonus):
    """Помечает бронь как «кэшбэк начислен» — чтобы не начислить по ней дважды."""
    bookings_col.update_one(
        {"_id": booking_id},
        {"$set": {
            "bonus_accrued": True,
            "bonus_amount": int(bonus),
            "bonus_accrued_at": now_kz().isoformat(),
        }},
    )


def cancel_booking_doc(booking_id) -> bool:
    """Помечает бронь отменённой по _id. True, если что-то изменилось."""
    res = bookings_col.update_one(
        {"_id": booking_id, "status": {"$ne": "cancelled"}},
        {"$set": {"status": "cancelled", "cancelled_at": now_kz().isoformat()}},
    )
    return res.modified_count > 0


def save_session(phone, message, response):
    # Ключ диалога — всегда чистые цифры (без '@c.us'), чтобы сообщения бота и
    # ручные ответы владельца сходились в один тред, а не раздваивались.
    sessions_col.insert_one({
        "phone": _digits(phone),
        "message": message,
        "response": response,
        "created_at": now_kz().isoformat(),
    })


def save_manual_message(phone, text):
    """Сохраняет ручной ответ владельца в ленту диалога (роль owner)."""
    sessions_col.insert_one({
        "phone": _digits(phone),
        "message": "",
        "response": text,
        "manual": True,
        "created_at": now_kz().isoformat(),
    })


# ─── Блокировка клиентов ────────────────────────────────────────
def _digits(phone):
    """Только цифры номера (с нормализацией 8→7) — единый ключ диалогов/блокировок."""
    return normalize_phone(phone)


def normalize_session_phones():
    """Старые сессии бота хранили телефон как chatId Green API ('7705...@c.us'),
    а ручные ответы владельца — как чистые цифры. Из-за этого один клиент
    раздваивался в списке диалогов. Приводим телефоны всех сессий к цифрам,
    чтобы раздвоенные ветки слились в один тред (разовая миграция при старте)."""
    fixed = 0
    for s in sessions_col.find({"phone": {"$regex": r"\D"}}, {"phone": 1}):
        digits = _digits(s.get("phone"))
        if digits and digits != s.get("phone"):
            sessions_col.update_one({"_id": s["_id"]}, {"$set": {"phone": digits}})
            fixed += 1
    if fixed:
        print(f"DEBUG: нормализовано телефонов в сессиях: {fixed}")


def is_blocked(phone) -> bool:
    """Заблокирован ли клиент (бот игнорирует его сообщения)."""
    key = _digits(phone)
    return bool(key) and blocked_col.count_documents({"phone": key}, limit=1) > 0


def block_phone(phone) -> str:
    """Блокирует клиента по номеру. Возвращает нормализованный номер."""
    key = _digits(phone)
    if not key:
        raise ValueError("Некорректный номер телефона")
    blocked_col.update_one(
        {"phone": key},
        {"$setOnInsert": {"phone": key, "created_at": now_kz().isoformat()}},
        upsert=True,
    )
    return key


def unblock_phone(phone) -> str:
    """Снимает блокировку с клиента. Возвращает нормализованный номер."""
    key = _digits(phone)
    blocked_col.delete_one({"phone": key})
    return key


def list_blocked():
    """Множество заблокированных номеров (только цифры)."""
    return {b["phone"] for b in blocked_col.find({}, {"phone": 1})}


# ─── Чаты WhatsApp для админ-панели ─────────────────────────────
def list_conversations(limit=80):
    """Список диалогов WhatsApp (по телефону) с последним сообщением и временем."""
    pipeline = [
        {"$sort": {"created_at": 1}},
        {"$group": {
            "_id": "$phone",
            "last_message": {"$last": "$message"},
            "last_response": {"$last": "$response"},
            "last_time": {"$last": "$created_at"},
            "count": {"$sum": 1},
        }},
        {"$sort": {"last_time": -1}},
        {"$limit": limit},
    ]
    blocked = list_blocked()
    out = []
    for r in sessions_col.aggregate(pipeline):
        last = r.get("last_response") or r.get("last_message") or ""
        out.append({
            "phone": r["_id"],
            "last": last,
            "time": r.get("last_time"),
            "count": r.get("count", 0),
            "blocked": _digits(r["_id"]) in blocked,
        })
    return out


def conversation_messages(phone, limit=300):
    """Лента сообщений диалога: клиент -> ai/owner, в хронологическом порядке."""
    cursor = sessions_col.find({"phone": _digits(phone)}).sort("created_at", 1).limit(limit)
    msgs = []
    for s in cursor:
        if s.get("message"):
            msgs.append({"role": "client", "text": s["message"], "time": s.get("created_at")})
        if s.get("response"):
            role = "owner" if s.get("manual") else "ai"
            msgs.append({"role": role, "text": s["response"], "time": s.get("created_at")})
    return msgs


# ─── История диалога ───────────────────────────────────────────
def load_history(phone, max_messages, ttl_hours):
    """Контекст диалога из базы (а не из памяти процесса — переживает рестарт).

    Возвращает список реплик [{role, content}] из последних обменов за окно
    ttl_hours, не длиннее max_messages. Каждая сессия = пара user/assistant.
    """
    since = (now_kz() - timedelta(hours=ttl_hours)).isoformat()
    # Берём последние обмены (две реплики на обмен) в пределах окна.
    cursor = (
        sessions_col.find({"phone": _digits(phone), "created_at": {"$gte": since}})
        .sort("created_at", -1)
        .limit(max(1, max_messages // 2))
    )
    sessions = list(cursor)[::-1]  # обратно в хронологический порядок

    history: list[dict] = []
    for s in sessions:
        if s.get("message"):
            history.append({"role": "user", "content": s["message"]})
        if s.get("response"):
            history.append({"role": "assistant", "content": s["response"]})
    return history[-max_messages:]


def _sum_bookings(query):
    pipeline = [
        {"$match": query},
        {"$group": {"_id": None, "count": {"$sum": 1}, "amount": {"$sum": "$amount"}}},
    ]
    result = list(bookings_col.aggregate(pipeline))
    if result:
        return result[0]["count"], result[0]["amount"] or 0
    return 0, 0


def _period_query(period):
    """Фильтр броней по периоду в KZ-времени. Выручка/метрики — ТОЛЬКО по оплаченным
    броням (status='paid'): ожидающие и отменённые в цифры не попадают."""
    now = now_kz()
    if period == "today":
        return {"status": "paid", "date": now.strftime("%Y-%m-%d")}
    if period == "week":
        return {"status": "paid", "created_at": {"$gte": (now - timedelta(days=7)).isoformat()}}
    if period == "month":
        return {"status": "paid", "created_at": {"$gte": (now - timedelta(days=30)).isoformat()}}
    return {"status": "paid"}


def get_stats(period="today"):
    """(кол-во броней, выручка, диалогов за сегодня) — всё по KZ-времени."""
    sessions_count, revenue = _sum_bookings(_period_query(period))
    today_iso_date = now_kz().strftime("%Y-%m-%d")
    dialogs = sessions_col.count_documents({"created_at": {"$gte": today_iso_date}})
    return sessions_count, revenue, dialogs


def get_owner_metrics():
    """Готовые цифры для владельца — ВСЯ арифметика тут, не в модели.

    Средний чек, выручка в день, сравнение с прошлой неделей — посчитаны кодом.
    """
    now = now_kz()
    today_b, today_r, today_d = get_stats("today")
    week_b, week_r, _ = get_stats("week")
    month_b, month_r, _ = get_stats("month")

    # Прошлая неделя: ОПЛАЧЕННЫЕ брони, созданные в окне [14 дней назад; 7 дней назад).
    prev_week_b, prev_week_r = _sum_bookings({
        "status": "paid",
        "created_at": {
            "$gte": (now - timedelta(days=14)).isoformat(),
            "$lt": (now - timedelta(days=7)).isoformat(),
        }
    })

    avg_check = round(month_r / month_b) if month_b else 0
    revenue_per_day_week = round(week_r / 7)
    week_delta = week_r - prev_week_r
    if prev_week_r:
        week_delta_pct = round((week_r - prev_week_r) / prev_week_r * 100)
    else:
        week_delta_pct = None  # сравнивать не с чем

    return {
        "today_bookings": today_b,
        "today_revenue": today_r,
        "today_dialogs": today_d,
        "week_bookings": week_b,
        "week_revenue": week_r,
        "month_bookings": month_b,
        "month_revenue": month_r,
        "avg_check": avg_check,
        "revenue_per_day_week": revenue_per_day_week,
        "prev_week_revenue": prev_week_r,
        "week_delta": week_delta,
        "week_delta_pct": week_delta_pct,
    }
