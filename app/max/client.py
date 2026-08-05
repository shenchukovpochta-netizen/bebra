"""Тонкий клиент Bot API MAX (dev.max.ru).

API MAX - наследник платформы TamTam: long polling через GET /updates,
отправка POST /messages, файлы в два шага (получить upload-URL, залить),
inline-клавиатуры вложением типа inline_keyboard. Все обращения к сети
собраны здесь, чтобы расхождение с живым API чинилось в одном файле.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://botapi.max.ru"

# Файл после загрузки обрабатывается на стороне MAX не мгновенно: отправка
# с ещё не готовым токеном отвечает ошибкой attachment.not.ready, и её
# положено переигрывать, а не считать провалом.
NOT_READY_RETRIES = 5
NOT_READY_DELAY = 1.0


class MaxAPIError(Exception):
    """Ответ API с ошибкой. Текст несёт код и тело - для лога достаточно."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class MaxClient:
    def __init__(self, token: str, base: str = DEFAULT_BASE) -> None:
        self.token = token
        self.base = base.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _call(self, method: str, path: str, *, params: dict | None = None,
                    json: dict | None = None) -> dict:
        query = {"access_token": self.token, **(params or {})}
        sess = await self.session()
        async with sess.request(method, self.base + path, params=query,
                                json=json) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise MaxAPIError(resp.status, body)
            return await resp.json(content_type=None)

    # ─────────────────────────── основное ───────────────────────────

    async def me(self) -> dict:
        return await self._call("GET", "/me")

    async def updates(self, marker: int | None, timeout: int = 30) -> dict:
        params: dict[str, Any] = {"timeout": timeout, "limit": 100}
        if marker is not None:
            params["marker"] = marker
        return await self._call("GET", "/updates", params=params)

    async def send(self, *, user_id: int | None = None, chat_id: int | None = None,
                   text: str, keyboard: list | None = None,
                   attachments: list | None = None, fmt: str | None = "html",
                   reply_to_mid: str | None = None) -> dict:
        """Отправка сообщения пользователю или в чат.

        Возвращает тело ответа (в нём message.body.mid - нужен карточкам).
        Повторяет отправку при attachment.not.ready: свежезалитый файл
        MAX отдаёт в сообщение не сразу.
        """
        payload: dict[str, Any] = {"text": text}
        if fmt:
            payload["format"] = fmt
        atts = list(attachments or [])
        if keyboard:
            atts.append({"type": "inline_keyboard",
                         "payload": {"buttons": keyboard}})
        if atts:
            payload["attachments"] = atts
        if reply_to_mid:
            payload["link"] = {"type": "reply", "mid": reply_to_mid}
        params = {"user_id": user_id} if user_id else {"chat_id": chat_id}

        for attempt in range(NOT_READY_RETRIES):
            try:
                return await self._call("POST", "/messages", params=params,
                                        json=payload)
            except MaxAPIError as exc:
                if "not.ready" in exc.body and attempt < NOT_READY_RETRIES - 1:
                    await asyncio.sleep(NOT_READY_DELAY)
                    continue
                raise
        raise MaxAPIError(0, "attachment.not.ready после всех попыток")

    async def answer_callback(self, callback_id: str,
                              notification: str | None = None) -> None:
        """Ответ на нажатие кнопки - иначе клиент MAX крутит ожидание."""
        body: dict[str, Any] = {}
        if notification:
            body["notification"] = notification
        try:
            await self._call("POST", "/answers",
                             params={"callback_id": callback_id}, json=body)
        except MaxAPIError as exc:
            # Просроченный callback - не повод ронять обработку нажатия.
            log.warning("ответ на callback не принят: %s", exc)

    # ─────────────────────────── файлы ───────────────────────────

    async def upload_file(self, filename: str, data: bytes) -> dict:
        """Загрузка документа. Возвращает payload для вложения типа file."""
        target = await self._call("POST", "/uploads", params={"type": "file"})
        form = aiohttp.FormData()
        form.add_field("data", data, filename=filename,
                       content_type="application/octet-stream")
        sess = await self.session()
        async with sess.post(target["url"], data=form) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise MaxAPIError(resp.status, body)
            uploaded = await resp.json(content_type=None)
        # Ответ несёт токен файла; какие-то развёртывания оборачивают его
        # в {"file": {...}} - берём то, что есть.
        payload = uploaded.get("file", uploaded)
        return {"type": "file", "payload": payload}

    async def download(self, url: str, max_bytes: int) -> bytes:
        """Скачивание присланного файла по url из вложения."""
        sess = await self.session()
        async with sess.get(url) as resp:
            if resp.status >= 400:
                raise MaxAPIError(resp.status, await resp.text())
            data = await resp.read()
        if len(data) > max_bytes:
            raise MaxAPIError(0, f"файл больше лимита: {len(data)} байт")
        return data

    # ─────────────────────────── подписка ───────────────────────────

    async def is_member(self, chat_id: int, user_id: int) -> bool:
        """Состоит ли пользователь в канале. Бот обязан быть администратором.

        При ошибке API гейт закрывается, а не открывается: недоступность
        проверки не должна становиться способом её обойти.
        """
        try:
            data = await self._call("GET", f"/chats/{chat_id}/members",
                                    params={"user_ids": str(user_id)})
        except MaxAPIError as exc:
            log.warning("проверка подписки %s не удалась: %s", user_id, exc)
            return False
        return any(m.get("user_id") == user_id for m in data.get("members", []))
