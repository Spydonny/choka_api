"""Green API: отправка сообщений, телефоны, разбор входящего вебхука."""
import re
from typing import Any, Optional

import requests

from config import GREEN_API_URL, GREEN_API_ID, GREEN_API_TOKEN, OWNER_PHONE


def digits_only(phone: str) -> str:
    """Оставляет только цифры номера: '+7 776-294-90-09' -> '77762949009'."""
    return re.sub(r"\D", "", phone or "")


def to_chat_id(phone: str) -> str:
    """Номер -> chatId Green API ('7776...@c.us'); готовый chatId не трогаем."""
    return phone if "@" in phone else f"{digits_only(phone)}@c.us"


def is_owner_phone(chat_id: str) -> bool:
    owner = digits_only(OWNER_PHONE)
    return bool(owner) and digits_only(chat_id.split("@")[0]) == owner


def send_message_to_whatsapp(chat_id: str, text: str):
    url = f"{GREEN_API_URL}/waInstance{GREEN_API_ID}/sendMessage/{GREEN_API_TOKEN}"
    payload = {
        "chatId": chat_id,
        "message": text,
    }
    print(f"DEBUG: Пытаюсь отправить ответ в {chat_id}...")
    response = requests.post(url, json=payload)
    print(f"DEBUG: Статус API: {response.status_code}")
    print(f"DEBUG: Ответ API: {response.text}")
    if response.status_code != 200:
        print(f"Green API error: {response.text}")
    return response


def notify_owner(booking_info: str):
    # Уведомление владельца не должно ронять основной поток (например, веб-бронь
    # при отсутствующих ключах Green API). Ошибки только логируем.
    if not OWNER_PHONE:
        return
    try:
        send_message_to_whatsapp(to_chat_id(OWNER_PHONE), f"Новая бронь:\n{booking_info}")
    except Exception as e:
        print(f"notify_owner error: {e}")


def extract_incoming_text(data: dict[str, Any]) -> Optional[str]:
    message_data = data.get("messageData") or {}
    msg_type = message_data.get("typeMessage", "")

    if msg_type == "textMessage":
        return message_data.get("textMessageData", {}).get("textMessage")
    if msg_type == "extendedTextMessage":
        return message_data.get("extendedTextMessageData", {}).get("text")
    if msg_type == "quotedMessage":
        quoted = message_data.get("quotedMessage", {})
        return quoted.get("textMessage") or quoted.get("extendedTextMessage", {}).get("text")

    print(f"DEBUG: неподдерживаемый тип сообщения: {msg_type}")
    return None
