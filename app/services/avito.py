"""Клиент API Авито: сообщения (Messenger API).

Опрашивает процесс бота (`app/crm/inbox.py`) - панель в интернет не ходит.
Ключи - «Для профессионалов → API» основного аккаунта компании: ключ
сотрудника не видит чатов по объявлениям компании. Без подписки с
доступом к API сообщений Авито отвечает 402 - это не сбой, а настройка,
и панель показывает её плашкой.

Особенности API, на которых легко споткнуться:
- токен живёт сутки, просроченный даёт 403 (не 401): новый токен и один
  повтор;
- версии путей разные: чаты v2, сообщения v3 (со слэшем на конце), отправка v1;
- ответ списка сообщений бывает и списком, и {"messages": [...]};
- свои сообщения приходят в тех же списках (author_id - наш аккаунт);
- системные сообщения, автоответы и заглушки «перейдите на подписку» -
  не обращения.

Разбор ответов (`parse_chat`, `parse_message`) отделён от сети ради тестов.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

API_URL = "https://api.avito.ru"
TIMEOUT = 20
# Предел длины текста у Авито. Длиннее - отказ до сети, а не 400 от API.
MESSAGE_LIMIT = 1000
# Запас до конца жизни токена: обновляем заранее, а не на отказе.
TOKEN_MARGIN = 120
# Заглушки и служебные фразы Авито в ленте чата - не слова клиента.
NOISE_PHRASES = ("перейдите на подписку", "api мессенджера", "получить доступ к чатам",
                 "пользователь создал чат", "напишите первыми")
_KINDS = {"text": "text", "link": "text", "image": "image", "voice": "voice",
          "call": "call", "file": "file"}


class AvitoError(Exception):
    """Авито ответил ошибкой. status - код ответа, если он был."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _moment(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), UTC) if value not in (None, "") else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_chat(raw: dict, own_id: int | None) -> dict:
    """Чат из списка - в плоский словарь: собеседник, объявление, последнее."""
    context = (raw.get("context") or {}).get("value") or {}
    other = next((u for u in raw.get("users") or []
                  if own_id is None or int(u.get("id") or 0) != int(own_id)), {})
    last = raw.get("last_message") or {}
    return {
        "id": str(raw.get("id") or ""),
        "updated": _moment(raw.get("updated")),
        "last_id": str(last.get("id") or "") or None,
        "name": str(other.get("name") or "").strip() or None,
        "subject": str(context.get("title") or "").strip() or None,
        "url": str(context.get("url") or "").strip() or None,
    }


def parse_message(raw: dict, own_id: int | None) -> dict:
    """Сообщение чата. noise - не показывать: системное, удалённое,
    автоответ чат-бота Авито или заглушка про подписку."""
    kind_raw = str(raw.get("type") or "")
    content = raw.get("content") or {}
    text = content.get("text")
    if kind_raw == "link":
        link = content.get("link") or {}
        text = " ".join(x for x in (link.get("text"), link.get("url")) if x)
    elif kind_raw == "item":
        item = content.get("item") or {}
        text = item.get("title")
    elif kind_raw == "location":
        location = content.get("location") or {}
        text = location.get("text") or location.get("title")
    text = str(text).strip() if text else None
    lowered = (text or "").lower()
    noise = (kind_raw in ("system", "deleted") or bool(content.get("flow_id"))
             or any(phrase in lowered for phrase in NOISE_PHRASES))
    author = raw.get("author_id")
    return {
        "id": str(raw.get("id") or ""),
        "author_id": int(author) if str(author or "").isdigit() else author,
        "created": _moment(raw.get("created")),
        "kind": _KINDS.get(kind_raw, "other"),
        "text": text,
        "noise": noise,
        "own": own_id is not None and str(author) == str(own_id),
    }


@dataclass
class AvitoClient:
    """Клиент Messenger API. `session_factory` подменяется в тестах."""

    client_id: str
    client_secret: str
    api_url: str = API_URL
    session_factory: Any = None
    _token: str | None = field(default=None, repr=False)
    _token_until: float = 0.0
    _self_id: int | None = None
    _lock: asyncio.Lock | None = field(default=None, repr=False)

    @property
    def ready(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _session(self):
        if self.session_factory is not None:
            return self.session_factory()
        import aiohttp
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT))

    async def _get_token(self, session) -> str:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._token and time.monotonic() < self._token_until:
                return self._token
            # Ключи - в теле формы, а не в адресе: адреса попадают в логи.
            response = await session.request(
                "POST", f"{self.api_url}/token/",
                data={"grant_type": "client_credentials", "client_id": self.client_id,
                      "client_secret": self.client_secret})
            data = await _json(response)
            status = getattr(response, "status", 200)
            token = str((data or {}).get("access_token") or "")
            if status >= 400 or not token:
                raise AvitoError(f"токен не выдан: Авито ответил {status}", status)
            lifetime = int((data or {}).get("expires_in") or 3600)
            self._token = token
            self._token_until = time.monotonic() + max(lifetime - TOKEN_MARGIN, 60)
            return token

    async def _call(self, method: str, path: str, *, retry: bool = True,
                    **kwargs: Any) -> Any:
        async with self._session() as session:
            token = await self._get_token(session)
            response = await session.request(
                method, f"{self.api_url}/{path}",
                headers={"Authorization": f"Bearer {token}"}, **kwargs)
            status = getattr(response, "status", 200)
            # Тело читается внутри сессии: после её закрытия оно недоступно.
            data = await _json(response)
        if status == 403 and retry:
            # Просроченный токен у Авито - 403: новый и один повтор.
            self._token = None
            return await self._call(method, path, retry=False, **kwargs)
        if status == 402:
            raise AvitoError("402 — нет доступа к API сообщений: нужен тариф "
                             "Авито с Messenger API", 402)
        if status == 429:
            raise AvitoError("429 — Авито просит реже", 429)
        if status >= 400:
            raise AvitoError(f"{path.split('?')[0]}: Авито ответил {status}", status)
        return data

    async def self_id(self) -> int:
        """Id аккаунта: нужен в каждом пути Messenger API."""
        if self._self_id is None:
            data = await self._call("GET", "core/v1/accounts/self")
            if not isinstance(data, dict) or not str(data.get("id") or "").isdigit():
                raise AvitoError("не удалось узнать id аккаунта Авито")
            self._self_id = int(data["id"])
        return self._self_id

    async def chats(self, *, limit: int = 100) -> list[dict]:
        own = await self.self_id()
        data = await self._call(
            "GET", f"messenger/v2/accounts/{own}/chats",
            params={"chat_types": "u2i,u2u", "limit": str(limit)})
        raw = (data or {}).get("chats") if isinstance(data, dict) else None
        return [parse_chat(c, own) for c in raw or [] if isinstance(c, dict) and c.get("id")]

    async def messages(self, chat_id: str, *, limit: int = 100) -> list[dict]:
        own = await self.self_id()
        # Слэш на конце - часть пути v3: без него Авито отвечает 404.
        data = await self._call(
            "GET", f"messenger/v3/accounts/{own}/chats/{chat_id}/messages/",
            params={"limit": str(limit)})
        raw = data if isinstance(data, list) else (data or {}).get("messages") or []
        return [parse_message(m, own) for m in raw if isinstance(m, dict) and m.get("id")]

    async def send_text(self, chat_id: str, text: str) -> dict:
        text = str(text or "").strip()
        if not text:
            raise AvitoError("пустой ответ")
        if len(text) > MESSAGE_LIMIT:
            raise AvitoError(f"ответ длиннее {MESSAGE_LIMIT} знаков")
        own = await self.self_id()
        data = await self._call(
            "POST", f"messenger/v1/accounts/{own}/chats/{chat_id}/messages",
            json={"message": {"text": text}, "type": "text"})
        return data if isinstance(data, dict) else {}


async def _json(response: Any) -> Any:
    try:
        return await response.json(content_type=None)
    except Exception:                                   # noqa: BLE001
        return None
