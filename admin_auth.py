"""Простая авторизация админа для /admin: пароль -> токен (HMAC), без БД.

Токен = hex(HMAC_SHA256(ADMIN_SECRET, "admin")). Он стабилен между рестартами
и проверяется без хранения состояния. Достаточно для одного админ-доступа;
для нескольких ролей нужна полноценная сессионная модель.
"""
import hashlib
import hmac

from fastapi import Header, HTTPException

from config import ADMIN_PASSWORD, ADMIN_SECRET


def _token() -> str:
    return hmac.new(ADMIN_SECRET.encode(), b"admin", hashlib.sha256).hexdigest()


def login(password: str) -> str:
    """Сверяет пароль и возвращает токен. Бросает HTTPException 401 при ошибке."""
    if not password or not hmac.compare_digest(password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="Неверный пароль")
    return _token()


def require_admin(x_admin_token: str = Header(default="")) -> bool:
    """Зависимость FastAPI: пускает только с валидным токеном админа."""
    if not x_admin_token or not hmac.compare_digest(x_admin_token, _token()):
        raise HTTPException(status_code=401, detail="Требуется авторизация админа")
    return True
