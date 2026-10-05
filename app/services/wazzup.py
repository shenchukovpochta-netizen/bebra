"""Клиент API Wazzup (v3): WhatsApp, Telegram (аккаунт) и Авито во «Входящих».

Входящие приходят сами - вебхуком на `/hook/inbox/<токен>` панели. Этот
клиент нужен процессу бота (`app/crm/inbox.py`) для трёх дел: узнать
номера WhatsApp, подключённые в Wazzup (`channels`), подписать хук на
новые сообщения (`set_webhook`) и отправить ответ из панели
(`send_text`). Панель в интернет не ходит.

Особенности, на которых легко споткнуться:
- ключ - «Интеграции → API» в кабинете Wazzup, заголовок
  `Authorization: Bearer <ключ>`;
- вебхук при подключении по своему ключу приходит БЕЗ заголовка
  авторизации: токен хука стоит в самом адресе;
- при подписке Wazzup сразу шлёт на адрес `{"test": true}` и ждёт 200:
  панель должна быть доступна по домену до подписки;
- `crmMessageId` - защита от дубля: повтор с тем же номером Wazzup не
  отправляет, а отвечает 400 `repeatedCrmMessageId` - это «уже ушло».

Разбор ответов (`parse_channels`) отделён от сети ради тестов.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

API_URL = "https://api.wazzup24.com/v3"
TIMEOUT = 20
# WhatsApp принимает длинные сообщения, но ответ из панели короче: предел
# держит logic.INBOX_REPLY_LIMITS, здесь - страховка до сети.
MESSAGE_LIMIT = 4000
# Каналы Wazzup, которые мы читаем как WhatsApp: обычный номер и WABA.
WHATSAPP_TRANSPORTS = ("whatsapp", "wapi")
# Транспорт канала Wazzup -> канал «Входящих»: WhatsApp, личный Telegram
# (аккаунт менеджера, а не бот - отдельный канал tgp) и Авито.
TRANSPORT_KINDS = {"whatsapp": "wa", "wapi": "wa", "tgapi": "tgp", "telegram": "tgp",
                   "avito": "avito"}
# Канал «Входящих» -> chatType в запросе отправки.
CHAT_TYPES = {"wa": "whatsapp", "tgp": "telegram", "avito": "avito"}
_CHANNEL_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_CHAT_ID = re.compile(r"\d{10,15}")
# chatId Telegram и Авито - номер или строка чата Авито («u2i-...»).
_OTHER_CHAT = re.compile(r"[A-Za-z0-9_@.:~+-]{1,128}")


class WazzupError(Exception):
    """Wazzup ответил ошибкой. status - код ответа, если он был."""

    def __init__(self, message: str, status: int | None = None,
                 code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def channel_id_ok(value: Any) -> str | None:
    """Номер канала Wazzup (uuid) из чужого JSON - или None."""
    text = str(value or "").strip()
    return text if _CHANNEL_ID.fullmatch(text) else None


def chat_id(phone: Any) -> str | None:
    """Телефон обращения (+79991234567) - в chatId WhatsApp у Wazzup:
    одни цифры, без плюса."""
    digits = re.sub(r"\D", "", str(phone or ""))
    return digits if _CHAT_ID.fullmatch(digits) else None


def parse_channels(raw: Any) -> list[dict]:
    """Каналы из GET /channels: WhatsApp, Telegram и Авито - id, вид
    («wa», «tgp», «avito»), номер (у WhatsApp и Telegram), живой ли."""
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        cid = channel_id_ok(item.get("channelId"))
        transport = str(item.get("transport") or "").lower()
        kind = TRANSPORT_KINDS.get(transport)
        if cid is None or kind is None:
            continue
        plain = re.sub(r"\D", "", str(item.get("plainId") or ""))[:15]
        state = str(item.get("state") or "")[:40]
        out.append({"id": cid, "kind": kind,
                    "phone": f"+{plain}" if plain and kind != "avito" else None,
                    "state": state, "active": state == "active"})
    return out


def _error_text(data: Any) -> tuple[str, str | None]:
    if isinstance(data, dict):
        code = str(data.get("error") or "")[:60] or None
        text = str(data.get("description") or data.get("message") or code or "")[:200]
        return text, code
    return "", None


@dataclass
class WazzupClient:
    """Клиент Wazzup API v3. `session_factory` подменяется в тестах."""

    api_key: str
    api_url: str = API_URL
    session_factory: Any = None

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    def _session(self):
        if self.session_factory is not None:
            return self.session_factory()
        import aiohttp
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT))

    async def _call(self, method: str, path: str, *, allow_empty: bool = False,
                    **kwargs: Any) -> Any:
        try:
            async with self._session() as session:
                response = await session.request(
                    method, f"{self.api_url}/{path}",
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"}, **kwargs)
                status = getattr(response, "status", 200)
                # Тело читается внутри сессии: после её закрытия оно недоступно.
                data = await _json(response)
        except (OSError, TimeoutError, ValueError) as exc:
            raise WazzupError(f"Wazzup недоступен: {type(exc).__name__}") from exc
        except Exception as exc:                        # noqa: BLE001
            if type(exc).__module__.startswith("aiohttp"):
                raise WazzupError(f"Wazzup недоступен: {type(exc).__name__}") from exc
            raise
        if status in (401, 403):
            raise WazzupError(f"{status} — Wazzup не принял ключ API", status)
        if status == 429:
            raise WazzupError("429 — Wazzup просит реже", status)
        if status >= 400:
            text, code = _error_text(data)
            raise WazzupError(f"Wazzup ответил {status}" + (f": {text}" if text else ""),
                              status, code)
        if data is None and not allow_empty:
            raise WazzupError(f"{path}: ответ Wazzup не разобрать", status)
        return data

    async def channels(self) -> list[dict]:
        """Каналы WhatsApp, Telegram и Авито, подключённые в кабинете Wazzup."""
        return parse_channels(await self._call("GET", "channels"))

    async def set_webhook(self, uri: str) -> None:
        """Подписать адрес на новые сообщения. Wazzup тут же пришлёт на
        него проверку {"test": true} и без 200 подписку не примет."""
        await self._call("PATCH", "webhooks", allow_empty=True, json={
            "webhooksUri": uri,
            "subscriptions": {"messagesAndStatuses": True,
                              "contactsAndDealsCreation": False}})

    async def send_text(self, channel_id: str, phone: str, text: str, *,
                        crm_message_id: str, kind: str = "wa") -> dict:
        """Ответ в WhatsApp, Telegram или Авито (`kind` - канал «Входящих»).
        У WhatsApp адрес - телефон, у остальных - номер чата из вебхука.
        Повтор с тем же crm_message_id Wazzup не отправляет - тогда это
        «уже ушло», а не сбой."""
        text = str(text or "").strip()
        if not text:
            raise WazzupError("пустой ответ")
        if len(text) > MESSAGE_LIMIT:
            raise WazzupError(f"ответ длиннее {MESSAGE_LIMIT} знаков")
        chat_type = CHAT_TYPES.get(kind)
        if chat_type is None:
            raise WazzupError("в этот канал Wazzup не пишет")
        cid = channel_id_ok(channel_id)
        if cid is None:
            raise WazzupError("не выбран канал в Wazzup")
        if kind == "wa":
            chat = chat_id(phone)
            if chat is None:
                raise WazzupError("у обращения нет телефона WhatsApp")
        else:
            raw = str(phone or "").strip()
            chat = raw if _OTHER_CHAT.fullmatch(raw) else None
            if chat is None:
                raise WazzupError("у обращения нет номера чата")
        try:
            data = await self._call("POST", "message", allow_empty=True, json={
                "channelId": cid, "chatType": chat_type, "chatId": chat,
                "text": text, "crmMessageId": crm_message_id})
        except WazzupError as exc:
            if exc.code == "repeatedCrmMessageId":
                return {"repeated": True}
            raise
        return data if isinstance(data, dict) else {}


async def _json(response: Any) -> Any:
    try:
        return await response.json(content_type=None)
    except Exception:                                   # noqa: BLE001
        return None
