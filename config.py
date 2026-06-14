"""Конфигурация: переменные окружения, зоны клуба, параметры модели."""
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

# ─── Время клуба ────────────────────────────────────────────────
# Усть-Каменогорск работает по времени Казахстана (UTC+5, без перехода на лето).
# Единый источник KZ-времени для всего приложения (ai, booking, db).
KZ_TZ = timezone(timedelta(hours=5))


def now_kz() -> datetime:
    """Текущие дата и время в часовом поясе клуба (UTC+5)."""
    return datetime.now(KZ_TZ)

# ─── Админ-панель и бонусы ──────────────────────────────────────
# Пароль входа в /admin. Меняйте через переменную окружения ADMIN_PASSWORD.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "choka2026")
# Секрет для подписи токена сессии админа (HMAC). По умолчанию — на основе пароля.
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "choka-admin-secret-" + ADMIN_PASSWORD)
# Кэшбэк бонусами: процент от суммы брони начисляется на номер телефона клиента.
BONUS_CASHBACK_PCT = int(os.environ.get("BONUS_CASHBACK_PCT", "5"))
# Окно (в часах) для ручного начисления кэшбэка по номеру: ищем последнюю бронь
# клиента, созданную за это время (см. bonus.accrue_from_last_booking).
BONUS_ACCRUE_WINDOW_HOURS = int(os.environ.get("BONUS_ACCRUE_WINDOW_HOURS", "3"))

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
# Не печатаем сам ключ в логи — только факт наличия.
print(f"DEBUG: GROQ_API_KEY {'задан' if GROQ_API_KEY else 'НЕ задан'}")

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "choko_verify_2024")
OWNER_PHONE = os.environ.get("OWNER_PHONE", "")
GREEN_API_URL = os.environ.get("GREEN_API_URL", "https://api.greenapi.com").rstrip('"').rstrip("/")
GREEN_API_ID = os.environ.get("GREEN_API_ID_INSTANCE", "").strip('"')
GREEN_API_TOKEN = os.environ.get("GREEN_API_TOKEN_INSTANCE", "")

# Имена как в .env (MONGODB_URL / MONGODB_DB_NAME), со старыми как запасной вариант.
MONGODB_URI = (
    os.environ.get("MONGODB_URL")
    or os.environ.get("MONGODB_URI")
    or "mongodb://localhost:27017"
)
MONGODB_DB = (
    os.environ.get("MONGODB_DB_NAME")
    or os.environ.get("MONGODB_DB")
    or "choko"
)

# ─── Зоны и вместимость (считаем МЕСТА = число одновременных броней) ──
# Меняйте числа здесь при необходимости.
ZONE_CAPACITY = {
    "ps": 4,        # PlayStation 5 — 4 приставки (Протозанова, 35)
    "lounge": 4,    # Lounge — 4 столика (Протозанова, 35)
    "billiard": 2,  # Бильярд — 2 стола (Чехова, 31)
    "vip": 1,       # VIP-зона — 1 зона (Чехова, 31)
}
ZONE_LABELS = {
    "ps": "PlayStation 5",
    "lounge": "Lounge-зона",
    "billiard": "Бильярд (ул. Чехова, 31)",
    "vip": "VIP-зона (ул. Чехова, 31)",
}
# Синонимы из текста для нормализации зоны в ключ.
ZONE_ALIASES = {
    "ps": "ps", "ps5": "ps", "playstation": "ps", "плойка": "ps",
    "плейстейшн": "ps", "приставка": "ps", "консоль": "ps",
    "lounge": "lounge", "лаундж": "lounge", "лаунж": "lounge",
    "billiard": "billiard", "бильярд": "billiard", "biliard": "billiard",
    "vip": "vip", "вип": "vip", "вип-комната": "vip", "vip-room": "vip",
}

# ─── Тарифы по зонам ────────────────────────────────────────────
# Подробная логика расчёта — в pricing.py. Здесь только параметры.
#
# PS5 (Протозанова, 35): спец-цены за полные часы, далее +1500 ₸ за каждый час.
PS_FULL_HOUR_PRICES = {1: 1500, 2: 2400, 3: 2900}  # 3 часа = акция «2+1»
PS_EXTRA_HOUR = 1500
PS_EXTRA_JOYSTICK = 400  # доп. джойстик (в сумму брони не входит, для справки)
#
# Зоны-депозиты: фикс. сумма за блок времени (до 6 персон), заказ по меню на эту сумму.
#   Lounge (Протозанова, 35) и VIP-зона (Чехова, 31) — 2900 ₸ за 2 часа.
DEPOSIT_ZONES = {
    "lounge": {"block_minutes": 120, "block_price": 2900},
    "vip": {"block_minutes": 120, "block_price": 2900},
}
#
# Бильярд (Чехова, 31): 2500 ₸/час. Акция «2+1» (при оплате 2 часов — 1 час в
# подарок, т.е. каждый 3-й час бесплатно) действует для броней, начатых 12:00–18:00.
BILLIARD_HOUR_PRICE = 2500
BILLIARD_PROMO_START_MIN = 12 * 60   # 12:00
BILLIARD_PROMO_END_MIN = 18 * 60     # 18:00

# ─── Витрина для сайта (юзер-часть) ─────────────────────────────
# Контакты и часы работы клуба.
CLUB_INFO = {
    "name": "Choka Club",
    "city": "Усть-Каменогорск",
    "address": "Протозанова, 35 (PS И Lounge) / Чехова, 31 (Бильярд и VIP)",
    "phone": "+7-771-189-07-98",
    "hours": "Пн–Вс: 12:00 — 05:00",
}

# Тарифы для отображения на сайте (расчёт цен — в pricing.py, это витрина).
TARIFFS = [
    {"label": "PlayStation 5 — 1 час", "price": 1500},
    {"label": "PlayStation 5 — 2 часа", "price": 2400},
    {"label": "PlayStation 5 — 3 часа (акция 2+1)", "price": 2900},
    {"label": "Доп. джойстик", "price": 400},
    {"label": "Lounge-зона (депозит, до 6 чел, 2ч)", "price": 2900},
    {"label": "VIP-зона (депозит, до 6 чел, 2ч)", "price": 2900},
    {"label": "Бильярд — 1 час", "price": 2500},
]

# Меню кухни/бара по категориям.
MENU = [
    {"category": "Чайная карта", "items": [
        {"name": "Ташкентский чай (1 л)", "price": 900},
        {"name": "Ягодный чай (1 л)", "price": 900},
        {"name": "Имбирный чай (1 л)", "price": 1200},
        {"name": "Кедровый чай (1 л)", "price": 1200},
    ]},
    {"category": "Десерты", "items": [
        {"name": "Медовик с орешками", "price": 800},
        {"name": "Пахлава (3 шт)", "price": 900},
    ]},
    {"category": "Дымный коктейль", "items": [
        {"name": "Лёгкий", "price": 2900},
        {"name": "Средний", "price": 3900},
        {"name": "Крепкий", "price": 4900},
    ]},
    {
        "category": "Закуски",
        "items": [
            {"name": "Круасан с курицей", "price": 900},
        ]
    }
]

# ─── Параметры модели Groq ──────────────────────────────────────
# Qwen3-32b лучше справляется с казахским. Модель может добавлять служебные
# <think>…</think> блоки — их вырезаем на стороне сервера (см. ai.py).
GROQ_MODEL = "qwen/qwen3-32b"
MAX_TOKENS = 512  # reasoning отключён через /no_think, ответу хватает с запасом
# Низкая температура: бронь — это структурное извлечение, а не творчество.
# Дату/время всё равно парсит сервер (datetime_parse.py), но и от модели нужна стабильность.
TEMPERATURE = 0.1
MAX_HISTORY = 40  # сколько сообщений диалога держим в контексте
# История диалога подтягивается из базы только за последние N часов — окно
# свежести: старое «завтра» из вчерашнего диалога не подмешивается в новый.
HISTORY_TTL_HOURS = 12
