"""Точка входа: FastAPI-приложение, роуты и обработчик вебхука Green API."""
import os
import traceback
from contextlib import asynccontextmanager
from typing import Any, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from config import (
    VERIFY_TOKEN, GROQ_MODEL, CLUB_INFO, TARIFFS, MENU, BONUS_CASHBACK_PCT,
    GREEN_API_ID, OWNER_PHONE,
)
from db import (
    init_db, get_stats, get_owner_metrics, list_bookings,
    list_conversations, conversation_messages, save_manual_message,
)
from ai import get_ai_reply
from whatsapp import (
    send_message_to_whatsapp, is_owner_phone, extract_incoming_text,
    to_chat_id, digits_only,
)

import admin_auth
import bonus as bonus_svc
import tournaments as tour_svc
from booking import try_create_booking, check_availability, zones_occupancy


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    port = int(os.environ.get("PORT", 8080))
    print(f"Choko Bot запущен на порту {port}")
    yield


app = FastAPI(title="Choko Bot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def process_incoming_webhook(data: dict[str, Any]) -> None:
    webhook_type = data.get("typeWebhook", "unknown")
    print(f"DEBUG WEBHOOK: type={webhook_type}")

    if webhook_type == "quotaExceeded":
        quota = data.get("quotaData", {})
        print("!!! ЛИМИТ GREEN API ИСЧЕРПАН !!!")
        print(f"Использовано чатов: {quota.get('used')}/{quota.get('total')}")
        print(f"Статус: {quota.get('status')}")
        print(f"Описание: {quota.get('description')}")
        return

    if webhook_type != "incomingMessageReceived":
        return

    sender_data = data.get("senderData") or {}
    chat_id = sender_data.get("chatId")
    if not chat_id:
        print("DEBUG: в вебхуке нет senderData.chatId")
        print(f"DEBUG: полный вебхук: {data}")
        return

    user_message = extract_incoming_text(data)
    print(f"DEBUG: Получено сообщение от {chat_id}: {user_message!r}")

    if not user_message or not user_message.strip():
        send_message_to_whatsapp(
            chat_id,
            "Не удалось прочитать сообщение. Отправьте, пожалуйста, обычный текст ещё раз.",
        )
        return

    if "{{SWE001}}" in user_message or "{{SWE999}}" in user_message:
        send_message_to_whatsapp(
            chat_id,
            "Не удалось прочитать сообщение. Отправьте, пожалуйста, его ещё раз.",
        )
        return

    ai_reply = get_ai_reply(chat_id, user_message, is_owner_phone(chat_id))
    send_message_to_whatsapp(chat_id, ai_reply)
    # Уведомление владельцу о новой брони отправляется внутри try_create_booking,
    # только когда бронь реально сохранена в базу.


# ─── Маршруты ───────────────────────────────────────────────────
@app.get("/webhook")
def verify_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        print("Webhook verified")
        return PlainTextResponse(hub_challenge or "")
    raise HTTPException(status_code=403, detail="Forbidden")


@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    try:
        process_incoming_webhook(data or {})
    except Exception as e:
        print(f"WEBHOOK ERROR: {e}")
        traceback.print_exc()

    return PlainTextResponse("OK")


@app.get("/stats")
def stats():
    today_b, today_r, today_d = get_stats("today")
    week_b, week_r, _ = get_stats("week")
    month_b, month_r, _ = get_stats("month")
    return {
        "today": {"bookings": today_b, "revenue": today_r, "dialogs": today_d},
        "week": {"bookings": week_b, "revenue": week_r},
        "month": {"bookings": month_b, "revenue": month_r},
    }


# ─── Pydantic-схемы запросов ────────────────────────────────────
class LoginIn(BaseModel):
    password: str


class BookingIn(BaseModel):
    phone: str
    name: str = "Гость"
    zone: str
    date: str
    time_from: str
    time_to: str
    persons: int = 1


class CheckIn(BaseModel):
    zone: str = "ps"
    date: str
    time_from: str
    time_to: Optional[str] = None


class TournamentIn(BaseModel):
    name: str
    game: str = "PS5"
    prize: int = 0
    players: list[str]


class MatchIn(BaseModel):
    match_id: str
    score1: int
    score2: int


class BonusOp(BaseModel):
    phone: str
    amount: int
    reason: Optional[str] = None
    name: Optional[str] = None


class ChatSend(BaseModel):
    text: str


def _bad_request(e: Exception):
    return HTTPException(status_code=400, detail=str(e))


# ─── Публичные эндпоинты (юзер-часть, без авторизации) ──────────
@app.get("/api/info")
def api_info():
    wa_digits = digits_only(CLUB_INFO.get("phone", ""))
    return {
        **CLUB_INFO,
        "cashback_pct": BONUS_CASHBACK_PCT,
        "whatsapp_url": f"https://wa.me/{wa_digits}" if wa_digits else "",
    }


@app.get("/api/tariffs")
def api_tariffs():
    return {"tariffs": TARIFFS}


@app.get("/api/menu")
def api_menu():
    return {"menu": MENU}


@app.get("/api/tournaments")
def api_tournaments():
    return {"tournaments": tour_svc.list_tournaments()}


@app.get("/api/tournaments/{tid}")
def api_tournament(tid: str):
    t = tour_svc.get_tournament(tid)
    if not t:
        raise HTTPException(status_code=404, detail="Турнир не найден")
    return t


@app.get("/api/occupancy")
def api_occupancy(
    date: Optional[str] = Query(None),
    time_from: Optional[str] = Query(None),
    time_to: Optional[str] = Query(None),
):
    """Занятость зон (места: занято/свободно). Без параметров — на текущий момент."""
    return zones_occupancy(date, time_from, time_to)


@app.post("/api/booking/check")
def api_booking_check(body: CheckIn):
    return {"message": check_availability(body.model_dump())}


@app.post("/api/booking")
def api_booking_create(body: BookingIn):
    """Заявка на бронь с сайта. Телефон используется как идентификатор клиента."""
    phone = bonus_svc.normalize_phone(body.phone)
    if not phone:
        raise HTTPException(status_code=400, detail="Укажите корректный номер телефона")
    raw = body.model_dump()
    raw.pop("phone", None)
    message = try_create_booking(phone, raw)
    return {"message": message}


# ─── Админ: авторизация ─────────────────────────────────────────
@app.post("/admin/login")
def admin_login(body: LoginIn):
    return {"token": admin_auth.login(body.password)}


# ─── Админ: метрики и брони ─────────────────────────────────────
@app.get("/admin/metrics")
def admin_metrics(_: bool = Depends(admin_auth.require_admin)):
    return get_owner_metrics()


@app.get("/admin/bookings")
def admin_bookings(
    upcoming: bool = Query(False),
    _: bool = Depends(admin_auth.require_admin),
):
    return {"bookings": list_bookings(only_upcoming=upcoming)}


# ─── Админ: турниры ─────────────────────────────────────────────
@app.post("/admin/tournaments")
def admin_create_tournament(
    body: TournamentIn, _: bool = Depends(admin_auth.require_admin)
):
    try:
        return tour_svc.create_tournament(body.name, body.game, body.prize, body.players)
    except ValueError as e:
        raise _bad_request(e)


@app.post("/admin/tournaments/{tid}/match")
def admin_report_match(
    tid: str, body: MatchIn, _: bool = Depends(admin_auth.require_admin)
):
    try:
        return tour_svc.report_match(tid, body.match_id, body.score1, body.score2)
    except ValueError as e:
        raise _bad_request(e)


@app.delete("/admin/tournaments/{tid}")
def admin_delete_tournament(tid: str, _: bool = Depends(admin_auth.require_admin)):
    if not tour_svc.delete_tournament(tid):
        raise HTTPException(status_code=404, detail="Турнир не найден")
    return {"ok": True}


# ─── Админ: бонусы по номеру телефона ───────────────────────────
# ─── Админ: чаты WhatsApp (реальный коннект с ботом) ────────────
@app.get("/admin/whatsapp/status")
def admin_whatsapp_status(_: bool = Depends(admin_auth.require_admin)):
    """Подключён ли WhatsApp-бот (есть ли ключи Green API)."""
    return {
        "connected": bool(GREEN_API_ID),
        "instance": GREEN_API_ID or None,
        "owner_phone": OWNER_PHONE or None,
    }


@app.get("/admin/chats")
def admin_chats(_: bool = Depends(admin_auth.require_admin)):
    return {"chats": list_conversations()}


@app.get("/admin/chats/{phone}")
def admin_chat_messages(phone: str, _: bool = Depends(admin_auth.require_admin)):
    return {"phone": phone, "messages": conversation_messages(phone)}


@app.post("/admin/chats/{phone}/send")
def admin_chat_send(
    phone: str, body: ChatSend, _: bool = Depends(admin_auth.require_admin)
):
    """Отправляет сообщение клиенту в WhatsApp через Green API от имени владельца."""
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Пустое сообщение")
    if not GREEN_API_ID:
        raise HTTPException(status_code=503, detail="WhatsApp-бот не подключён (нет ключей Green API)")
    try:
        resp = send_message_to_whatsapp(to_chat_id(phone), text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка отправки в WhatsApp: {e}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Green API вернул {resp.status_code}: {resp.text}")
    save_manual_message(digits_only(phone), text)
    return {"ok": True}


@app.get("/admin/bonus/{phone}")
def admin_bonus_lookup(phone: str, _: bool = Depends(admin_auth.require_admin)):
    return bonus_svc.get_bonus(phone)


@app.post("/admin/bonus/add")
def admin_bonus_add(body: BonusOp, _: bool = Depends(admin_auth.require_admin)):
    try:
        return bonus_svc.add_bonus(
            body.phone, body.amount, body.reason or "Начисление вручную", body.name or ""
        )
    except ValueError as e:
        raise _bad_request(e)


@app.post("/admin/bonus/redeem")
def admin_bonus_redeem(body: BonusOp, _: bool = Depends(admin_auth.require_admin)):
    try:
        return bonus_svc.redeem_bonus(
            body.phone, body.amount, body.reason or "Списание бонусов"
        )
    except ValueError as e:
        raise _bad_request(e)


@app.get("/")
def health():
    return {"status": "running", "club": "Choko", "model": GROQ_MODEL}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
