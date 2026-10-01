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
from collections import OrderedDict
from datetime import date
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, MenuButtonWebApp, Message, WebAppInfo

from .. import keyboards as kb
from .. import logic, texts
from ..crm import logic as crm_logic
from ..crm import quickforms, service

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
    if quickforms.may(person, "repair") or quickforms.may(person, "issue"):
        text += texts.STAFF_LINKED_FORMS
    await message.answer(text)


# ─────────────── быстрые формы: сторонний ремонт и выдача ───────────────
#
# Форма текстом -> предпросмотр ответом на неё -> кнопка. Состояния диалога
# нет: подтверждение перечитывает форму из сообщения, на которое ответил
# предпросмотр, и проверяет права ещё раз - между формой и кнопкой
# сотрудника могли отключить.

# Двойное нажатие: Telegram доставляет оба, пока кнопки ещё не сняты, а
# второй наряд на те же деньги никому не нужен. Сделанное помнится в
# процессе бота - этого хватает: кнопку жмут секунды спустя, не дни.
_BUSY: set[tuple[int, int]] = set()
_DONE: OrderedDict[tuple[int, int], None] = OrderedDict()
_DONE_KEEP = 2000


def _done(key: tuple[int, int]) -> None:
    _DONE[key] = None
    while len(_DONE) > _DONE_KEEP:
        _DONE.popitem(last=False)


class QuickForm(BaseFilter):
    """Форма сотрудника в личке: вид формы - в обработчик."""

    async def __call__(self, message: Message) -> bool | dict[str, Any]:
        if message.chat.type != "private" or not message.text:
            return False
        kind = crm_logic.quick_form_kind(message.text)
        return {"form_kind": kind} if kind else False


async def _quick_staff(crm: Any, user_id: int, kind: str) -> tuple[dict | None, str]:
    """Сотрудник с правом на эту форму - или текст отказа."""
    if crm is None:
        return None, texts.STAFF_LINK_NO_CRM
    staff = await quickforms.staff_for(crm, user_id)
    if staff is None:
        return None, texts.QF_NOT_STAFF
    if not quickforms.may(staff, kind):
        return None, texts.QF_NO_RIGHTS.format(what=texts.QF_WHAT[kind])
    return staff, ""


async def _send_template(message: Message, crm: Any, kind: str) -> None:
    user = message.from_user
    staff, problem = await _quick_staff(crm, user.id if user else 0, kind)
    if staff is None:
        await message.answer(problem)
        return
    today = f"{date.today():%d.%m}"
    if kind == "repair":
        form = crm_logic.QUICK_REPAIR_TEMPLATE.format(
            today=today, who=staff.get("name") or staff["login"])
        await message.answer(texts.QF_REPAIR_TEMPLATE.format(form=logic.esc(form)))
    else:
        form = crm_logic.QUICK_ISSUE_TEMPLATE.format(today=today)
        await message.answer(texts.QF_ISSUE_TEMPLATE.format(form=logic.esc(form)))


@router.message(Command("remont"))
async def cmd_remont(message: Message, crm: Any = None) -> None:
    await _send_template(message, crm, "repair")


@router.message(Command("vydacha"))
async def cmd_vydacha(message: Message, crm: Any = None) -> None:
    await _send_template(message, crm, "issue")


async def _target_order(crm: Any, message: Message) -> dict | None:
    """Наряд, который форма дополняет: ответ на карточку бота с РЕМ-номером.
    В личке бот один, поэтому достаточно «написал бот»."""
    reply = message.reply_to_message
    if reply is None or reply.from_user is None or not reply.from_user.is_bot:
        return None
    no = crm_logic.order_no_in(reply.text or reply.caption)
    return await crm.work_order_by_no(no) if no else None


@router.message(QuickForm())
async def quick_form(message: Message, form_kind: str, crm: Any = None) -> None:
    user = message.from_user
    staff, problem = await _quick_staff(crm, user.id if user else 0, form_kind)
    if staff is None:
        await message.reply(problem)
        return
    today = date.today()
    if form_kind == "repair":
        order = await _target_order(crm, message)
        if order is not None and (order.get("payer") != "client" or order.get("bike_id")):
            await message.reply(f"⚠️ {logic.esc(order['no'])} — не сторонний ремонт: "
                                "его ведут в панели.")
            return
        preview = await quickforms.preview_repair(crm, staff, message.text or "",
                                                  today=today, order=order)
    else:
        preview = await quickforms.preview_issue(crm, staff, message.text or "", today=today)
    await message.reply(preview.text, reply_markup=(
        kb.quick_confirm(form_kind, preview.target) if preview.ok else None))


@router.callback_query(F.data.regexp(r"^qf:(x|i|r:\d{1,18})$"))
async def cb_quick(callback: CallbackQuery, bot: Bot, crm: Any = None, cfg: Any = None,
                   db: Any = None) -> None:
    shown = callback.message
    if not isinstance(shown, Message):
        await callback.answer(texts.QF_STALE, show_alert=True)
        return
    key = (shown.chat.id, shown.message_id)
    parts = (callback.data or "").split(":")
    if parts[1] == "x":
        _done(key)
        try:
            await shown.edit_text(texts.QF_CANCELLED, reply_markup=None)
        except TelegramAPIError:
            pass
        await callback.answer()
        return
    if key in _DONE:
        await callback.answer(texts.QF_DONE)
        return
    if key in _BUSY:
        await callback.answer(texts.QF_BUSY)
        return
    form = shown.reply_to_message
    if form is None or not form.text or form.from_user is None \
            or form.from_user.id != callback.from_user.id:
        await callback.answer(texts.QF_STALE, show_alert=True)
        return
    kind = "issue" if parts[1] == "i" else "repair"
    staff, problem = await _quick_staff(crm, callback.from_user.id, kind)
    if staff is None:
        await callback.answer(problem, show_alert=True)
        return
    _BUSY.add(key)
    try:
        try:
            await shown.edit_reply_markup(reply_markup=None)
        except TelegramAPIError:
            pass
        today = date.today()
        if kind == "issue":
            text = await quickforms.apply_issue(
                crm, staff, form.text, today=today, bot=bot, db=db,
                panel_url=crm_logic.panel_app_url(getattr(cfg, "crm_domain", "")))
        else:
            text = await quickforms.apply_repair(crm, staff, form.text, today=today,
                                                 order_id=int(parts[2]), bot=bot)
        _done(key)
    except quickforms.FormError as exc:
        text = str(exc)
    except service.ServiceError as exc:
        text = f"⚠️ {logic.esc(exc)}"
    finally:
        _BUSY.discard(key)
    try:
        await shown.edit_text(text, reply_markup=None)
    except TelegramAPIError:
        await shown.answer(text)
    await callback.answer()
