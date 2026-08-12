"""Проверка подлинности Telegram Mini App: разбор и подпись initData.

Mini App присылает initData - строку запроса, которую Telegram подписал
HMAC-SHA256 с ключом, выведенным из токена бота. Проверка обязана жить
на сервере: заголовок с tg_id может написать кто угодно, а initData
подделать нельзя - без токена не собрать подпись.

Алгоритм ровно из документации Telegram: пары ключ=значение (кроме hash)
сортируются, склеиваются через перевод строки, secret = HMAC(key="WebAppData",
msg=токен), подпись сверяется constant-time сравнением.

Модуль намеренно на голом stdlib, как app/logic.py: граница доверия
обязана тестироваться без единой зависимости.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

# initData старше суток не принимается: перехваченная однажды строка
# не должна оставаться пропуском навсегда. Сутки, а не минуты: Mini App
# живёт открытой вкладкой, и Telegram не перевыпускает initData на лету.
MAX_AGE_SECONDS = 24 * 3600


def parse_init_data(init_data: str | None, bot_token: str,
                    *, now: float | None = None) -> dict | None:
    """Пользователь из initData либо None. Без исключений: это граница
    доверия, и любой мусор снаружи - это None, а не трейсбек."""
    try:
        pairs = parse_qsl(init_data or "", strict_parsing=True)
    except ValueError:
        return None
    data = dict(pairs)
    received = data.pop("hash", "")
    if not received or not bot_token:
        return None

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received, expected):
        return None

    try:
        auth_date = int(data.get("auth_date", ""))
    except ValueError:
        return None
    if (time.time() if now is None else now) - auth_date > MAX_AGE_SECONDS:
        return None

    try:
        user = json.loads(data.get("user", ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user


def sign_init_data(data: dict[str, str], bot_token: str) -> str:
    """Собрать подписанную строку initData - для тестов и только для них.

    Живёт рядом с проверкой намеренно: если алгоритм подписи разъедется
    с алгоритмом проверки, тест упадёт первым.
    """
    from urllib.parse import urlencode

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**data, "hash": signature})
