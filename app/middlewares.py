"""Общая обвязка: маршрутизация апдейтов, клейм, загрузка пользователя,
рейт-лимит и гейт подписки. Всё, что должно случиться до любого обработчика.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from . import keyboards as kb
from . import logic, texts
from .config import Config
from .db import Database
from .services.subscription import check_subscription

log = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


def _describe(update: Update) -> tuple[int | None, int | None, str, dict]:
    """(user_id, chat_id, kind, безопасный слепок для журнала).

    В журнал не попадают ни телефон, ни ФИО, ни текст сообщений - только
    структура апдейта. Иначе ПДн растекаются по таблицам с другим сроком
    хранения, и удалять их потом неоткуда.
    """
    event = update.event
    if isinstance(event, CallbackQuery):
        chat = event.message.chat if event.message else None
        return (
            event.from_user.id if event.from_user else None,
            chat.id if chat else None,
            "callback",
            {"kind": "callback", "cb_data": event.data,
             "chat_type": chat.type if chat else None},
        )
    if isinstance(event, Message):
        kind = ("contact" if event.contact else "photo" if event.photo
                else "document" if event.document else "text" if event.text else "other")
        return (
            event.from_user.id if event.from_user else None,
            event.chat.id,
            kind,
            {"kind": kind, "chat_type": event.chat.type,
             "has_text": bool(event.text), "text_len": len(event.text or ""),
             "has_contact": bool(event.contact), "has_photo": bool(event.photo),
             "document_mime": event.document.mime_type if event.document else None},
        )
    return None, None, update.event_type, {"kind": update.event_type}


class PipelineMiddleware(BaseMiddleware):
    def __init__(self, db: Database, cfg: Config) -> None:
        self.db = db
        self.cfg = cfg

    async def __call__(self, handler: Handler, event: TelegramObject,
                       data: dict[str, Any]) -> Any:
        # Middleware висит на dp.update, поэтому event и есть Update;
        # data["event_update"] - подстраховка на случай иной регистрации.
        update: Update = event if isinstance(event, Update) else data["event_update"]
        inner = update.event
        user_id, chat_id, kind, payload = _describe(update)
        if not user_id or not chat_id:
            return None

        is_moderation = (
            isinstance(inner, CallbackQuery)
            and logic.parse_moderation_callback(inner.data) is not None
        )
        if not logic.should_process(
            payload.get("chat_type"),
            from_admin_chat=chat_id == self.cfg.admin_chat_id,
            is_moderation_callback=is_moderation,
        ):
            return None

        if not await self.db.claim_update(update.update_id, user_id, kind, payload):
            log.info("апдейт %s уже обработан, пропускаю", update.update_id)
            return None

        try:
            result = await self._dispatch(handler, event, data, inner, user_id, is_moderation)
        except Exception:
            # Клейм намеренно НЕ закрывается: запись остаётся в processing,
            # и повторная доставка того же update_id переиграет его. Пометить
            # упавший апдейт как done - значит потерять его без следа.
            log.exception("обработка апдейта %s не удалась", update.update_id)
            raise
        await self.db.finish_update(update.update_id)
        return result

    async def _dispatch(self, handler: Handler, event: TelegramObject, data: dict[str, Any],
                        inner: Any, user_id: int, is_moderation: bool) -> Any:
        data["db"] = self.db
        data["cfg"] = self.cfg

        # Модерация идёт мимо всего пользовательского конвейера: у админа нет
        # анкеты, рейт-лимит и подписка к нему не относятся.
        if is_moderation:
            return await handler(event, data)

        row = await self.db.upsert_user(
            user_id, inner.from_user.username if inner.from_user else None
        )
        user = dict(row)
        data["user"] = user

        verdict = logic.rate_limit_verdict(
            user["rl_count"], self.cfg.rate_soft, self.cfg.rate_hard
        )
        if verdict == "drop":
            return None      # молча: ответ на флуд сам становится флудом
        if verdict == "warn":
            await self._reply(inner, texts.RATE_LIMITED)
            return None

        # Гейт подписки: без кэша, всегда живой запрос.
        if not await check_subscription(data["bot"], self.cfg.channel_id, user_id):
            await self._reply(
                inner,
                texts.NOT_SUBSCRIBED.format(channel_url=self.cfg.channel_url),
                kb.subscribe(self.cfg.channel_url),
            )
            if isinstance(inner, CallbackQuery):
                await inner.answer(texts.SUB_NOT_FOUND, show_alert=True)
            return None

        return await handler(event, data)

    @staticmethod
    async def _reply(inner: Any, text: str, markup: Any = None) -> None:
        target = inner.message if isinstance(inner, CallbackQuery) else inner
        if target is not None:
            await target.answer(text, reply_markup=markup)
