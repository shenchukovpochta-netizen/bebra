"""Сотрудник и его Telegram: привязка по одноразовому коду и наряды.

Техник получает свои наряды в боте, а не ходит за ними в панель. Панель
выдаёт код, сотрудник отправляет боту «/staff КОД» - и связь закреплена.
Пароль от панели в переписку не отдают, одноразовый код можно: он гаснет
при первом применении.

Роутер подключается раньше регистрации: «/staff КОД» не должен стать
ответом на вопрос анкеты.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .. import texts
from ..crm import logic as crm_logic

log = logging.getLogger(__name__)
router = Router(name="staff")


@router.message(Command("staff"))
async def cmd_staff(message: Message, command: CommandObject | None = None,
                    bot: Bot | None = None, crm: Any = None) -> None:
    del bot
    if crm is None:
        await message.answer(texts.STAFF_LINK_NO_CRM)
        return
    code = crm_logic.clean_link_code(command.args if command else "")
    if not code:
        await message.answer(texts.STAFF_LINK_USAGE)
        return
    person = await crm.staff_by_link_code(code)
    if person is None or not person.get("active"):
        await message.answer(texts.STAFF_LINK_BAD)
        return
    user = message.from_user
    linked = await crm.link_staff_tg(person["id"], user.id if user else 0,
                                     user.username if user else None)
    if not linked:
        await message.answer(texts.STAFF_LINK_TAKEN)
        return
    await message.answer(texts.STAFF_LINKED.format(name=person.get("name")
                                                   or person["login"]))
