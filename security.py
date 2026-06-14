"""Защита от промт-инъекций: текст клиента — это данные, а не команды."""
import re

MAX_INPUT_LEN = 2000
CONTROL_TOKENS = (
    "[[BOOKING]]", "[[/BOOKING]]", "[[CHECK]]", "[[/CHECK]]",
    "[[CANCEL]]", "[[/CANCEL]]", "[[MYBOOKINGS]]", "[[/MYBOOKINGS]]",
    "[[SCHEDULE]]", "[[/SCHEDULE]]",
)
INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (all |the |your )?(previous |above |prior )?instructions",
        r"disregard (all |the |your )?(previous |above )?",
        r"забудь(те)? (все |про )?(предыдущие |прошлые )?(инструкции|правила|указания)",
        r"игнорируй(те)? (все |предыдущие |прошлые )?(инструкции|правила|указания)",
        r"(покажи|раскрой|выведи|скажи|напиши)[^\n]{0,40}(системн\w* промпт|system prompt|свои инструкции|твои инструкции)",
        r"system prompt",
        r"ты (теперь|больше не)\b",
        r"act as\b",
        r"pretend (to be|you are)\b",
        r"developer mode|jailbreak|dan mode",
        r"\bты\b[^\n]{0,30}\b(владелец|админ|администратор)\b",
        r"(я|это)\s+(владелец|админ|администратор|хозяин)\b",
        r"(role|роль)\s*:\s*(system|assistant|систем|ассистент)",
    )
]


def sanitize_user_input(text: str) -> str:
    """Чистит входящее сообщение клиента перед передачей в модель."""
    if not text:
        return ""
    text = text[:MAX_INPUT_LEN]
    # Не даём пользователю подделать управляющие токены брони.
    for token in CONTROL_TOKENS:
        text = text.replace(token, "")
    # Срезаем подделку ролевых маркеров в начале строк (system:/assistant:).
    text = re.sub(
        r"(?im)^\s*(system|assistant|систем\w*|ассистент)\s*:\s*",
        "",
        text,
    )
    return text.strip()


def looks_like_injection(text: str) -> bool:
    return any(p.search(text) for p in INJECTION_PATTERNS)
