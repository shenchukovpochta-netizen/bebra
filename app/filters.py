"""Фильтры, общие для нескольких роутеров."""

from __future__ import annotations

from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import Message


def is_service_chat(cfg: Any, chat_id: int | None) -> bool:
    """Служебные чаты: модерация заявок и утверждение договоров. Одно место
    для middleware и всех роутеров, чтобы правило не разъезжалось."""
    return chat_id is not None and chat_id in {cfg.admin_chat_id, cfg.contract_chat_id}


def is_operator(cfg: Any, user_id: int | None) -> bool:
    """Кто жмёт кнопки модерации и командует парком: ADMINS из .env."""
    return user_id is not None and user_id in cfg.admins


class ServiceChatReply(BaseFilter):
    """Ответ на сообщение в служебном чате - и только там.

    Роутер модерации подключается первым, поэтому голый фильтр «любой реплай»
    ловил бы и обычного пользователя, ответившего на сообщение бота в личке:
    aiogram останавливает разбор на первом совпавшем обработчике, и человек
    посреди анкеты не получил бы ничего в ответ.
    """

    async def __call__(self, message: Message, cfg: Any = None) -> bool:
        if cfg is None or message.reply_to_message is None:
            return False
        return is_service_chat(cfg, message.chat.id)


class StateIs(BaseFilter):
    """Шаг пользователя из анкеты, загруженной middleware."""

    def __init__(self, *states: str) -> None:
        self.states = set(states)

    # user приходит из middleware, но для апдейтов модерации его нет:
    # у админа анкеты не заводится. Значение по умолчанию обязательно,
    # иначе фильтр упадёт с TypeError на чужом апдейте.
    async def __call__(self, event: Any, user: dict | None = None) -> bool:
        return bool(user) and user.get("state") in self.states
