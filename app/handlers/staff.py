"""Сотрудник и его Telegram: привязка по одноразовому коду, наряды и
панель CRM внутри Telegram.

Техник получает свои наряды в боте, а не ходит за ними в панель. Панель
выдаёт код, сотрудник отправляет боту «/staff КОД» - и связь закреплена.
Пароль от панели в переписку не отдают, одноразовый код можно: он гаснет
при первом применении.

«/crm» открывает саму панель как Telegram Mini App. Вход - логин и
пароль сотрудника в форме панели по https: бот пароля не видит, и в
переписке он не остаётся.

Роутер подключается раньше регистрации: «/staff КОД» и «/crm» не должны
стать ответом на вопрос анкеты.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import MenuButtonWebApp, Message, WebAppInfo

from .. import keyboards as kb
from .. import logic, texts
from ..crm import logic as crm_logic

log = logging.getLogger(__name__)
router = Router(name="staff")


async def set_crm_menu(bot: Bot, chat_id: int, url: str) -> bool:
    """Кнопка «CRM» слева от поля ввода - в личке сотрудника, а не всем:
    у клиентов там остаётся обычное меню команд. Сбой Telegram (например,
    личка с ботом ещё не начата) ответа не срывает - есть кнопка в
    сообщении и команда /crm. False - кнопка не поставлена."""
    try:
        await bot.set_chat_menu_button(
            chat_id=chat_id,
            menu_button=MenuButtonWebApp(text=texts.CRM_MENU_BUTTON,
                                         web_app=WebAppInfo(url=url)))
    except TelegramAPIError:
        log.warning("кнопка CRM в меню чата %s не поставлена", chat_id, exc_info=True)
        return False
    return True


@router.message(Command("crm"))
async def cmd_crm(message: Message, bot: Bot | None = None, crm: Any = None,
                  cfg: Any = None) -> None:
    """Панель CRM в Telegram: кнопка открывает её как Mini App.

    Права - роль сотрудника в панели, как в браузере, поэтому кнопка не
    секрет: без логина и пароля за ней только страница входа. Привязанному
    сотруднику (staff.tg_id) кнопка «CRM» ставится ещё и в меню чата.
    Кнопка Mini App в группе не работает (Telegram её не примет), поэтому
    там - подсказка написать в личку.
    """
    if message.chat.type != "private":
        await message.answer(texts.CRM_APP_PRIVATE)
        return
    url = crm_logic.panel_app_url(getattr(cfg, "crm_domain", ""))
    if url is None:
        await message.answer(texts.CRM_APP_NO_DOMAIN)
        return
    await message.answer(texts.CRM_APP, reply_markup=kb.crm_app(url))
    user = message.from_user
    if bot is None or crm is None or user is None:
        return
    try:
        person = await crm.staff_by_tg(user.id)
    except Exception:                                    # noqa: BLE001
        log.warning("CRM: сотрудник по Telegram %s не прочитан", user.id, exc_info=True)
        return
    if person is not None and person.get("active"):
        await set_crm_menu(bot, message.chat.id, url)


@router.message(Command("staff"))
async def cmd_staff(message: Message, command: CommandObject | None = None,
                    bot: Bot | None = None, crm: Any = None, cfg: Any = None) -> None:
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
    # Имя заводят в панели, а ответ уходит с parse_mode=HTML.
    text = texts.STAFF_LINKED.format(name=logic.esc(person.get("name") or person["login"]))
    url = crm_logic.panel_app_url(getattr(cfg, "crm_domain", ""))
    if url is not None and bot is not None and user is not None \
            and await set_crm_menu(bot, user.id, url):
        text += texts.STAFF_LINKED_CRM
    await message.answer(text)
