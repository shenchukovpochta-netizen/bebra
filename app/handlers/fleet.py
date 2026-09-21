"""Парк из служебного чата: карточка велосипеда, статус кнопкой, ремонт
по узлу ответом на карточку.

Это полевой ввод для механика и оператора на точке: без панели, с телефона,
в том же чате, где живут карточки модерации. Работает только в служебных
чатах и только для операторов из ADMINS. Все изменения идут через CRM,
поэтому журнал статусов и отчёт по ремонтам получают их так же, как из
панели.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from .. import logic, texts
from ..config import Config
from ..crm import logic as crm_logic
from ..filters import ServiceChatReply, is_operator, is_service_chat

log = logging.getLogger(__name__)
router = Router(name="fleet")


class FleetCommand(BaseFilter):
    """/bike в служебном чате."""

    async def __call__(self, message: Message, cfg: Any = None) -> bool:
        if cfg is None or not logic.is_fleet_command(message.text):
            return False
        return is_service_chat(cfg, message.chat.id)


class BikeCardReply(BaseFilter):
    """Ответ на карточку велосипеда - форма ремонта."""

    async def __call__(self, message: Message, cfg: Any = None) -> bool:
        replied = message.reply_to_message
        if replied is None or not await ServiceChatReply()(message, cfg):
            return False
        # Только карточка самого бота: оператор, скопировавший её текст
        # в свой пост, не должен превращать чужие ответы в ремонты.
        if not (replied.from_user and replied.from_user.is_bot):
            return False
        return logic.bike_code_from_card(replied.text) is not None


def _is_admin(user_id: int, cfg: Config) -> bool:
    return is_operator(cfg, user_id)


def _who(user: Any) -> str:
    """@username, а без него - имя: «@123456» выглядело бы сломанной ссылкой."""
    return f"@{user.username}" if user.username else (user.first_name or str(user.id))


async def _find_bike(crm: Any, query: str) -> dict | None:
    q = query.strip()
    if not q:
        return None
    # Номера хранятся в верхнем регистре (check_code), с телефона приходят
    # как набрали; рама и мотор ищутся без учёта регистра в самой базе.
    for finder, query in ((crm.bike_by_code, q.upper()), (crm.bike_by_frame, q),
                          (crm.bike_by_motor, q)):
        bike = await finder(query)
        if bike is not None:
            return await crm.bike(bike["id"])
    return None


async def card_text(crm: Any, bike: dict) -> str:
    repairs = await crm.bike_log(bike["id"], limit=3, kind="repair")
    items = "; ".join(
        f"{x['created_at'].strftime('%d.%m')} {logic.esc((x.get('note') or '')[:40])}"
        f"{' ' + crm_logic.money(x['cost']) if x.get('cost') else ''}" for x in repairs)
    renter = (texts.FLEET_CARD_RENTER.format(name=logic.esc(bike.get("full_name")),
                                             rental_id=bike["rental_id"])
              if bike.get("rental_id") else texts.FLEET_CARD_FREE)
    return texts.FLEET_CARD.format(
        code=logic.esc(bike["code"]), model=logic.esc(bike["model"]),
        status=crm_logic.BIKE_STATUSES.get(bike["status"], bike["status"]),
        location=f" · {logic.esc(bike['location'])}" if bike.get("location") else "",
        renter=renter,
        repairs=(texts.FLEET_CARD_REPAIRS.format(items=items) if items
                 else texts.FLEET_CARD_NO_REPAIRS))


@router.message(FleetCommand())
async def cmd_bike(message: Message, cfg: Config, crm: Any = None) -> None:
    if not _is_admin(message.from_user.id, cfg):
        return
    if crm is None:
        await message.reply(texts.FLEET_NO_CRM)
        return
    query = logic.fleet_command_arg(message.text)
    if not query:
        await message.reply(texts.FLEET_USAGE)
        return
    bike = await _find_bike(crm, query)
    if bike is None:
        await message.reply(texts.FLEET_NOT_FOUND.format(query=logic.esc(query)))
        return
    await message.reply(await card_text(crm, bike),
                        reply_markup=kb.fleet_card(bike["id"], bike["status"],
                                                   bool(bike.get("rental_id"))))


@router.callback_query(F.data.regexp(r"^bk:\d+:[a-z_]+$"))
async def cb_status(callback: CallbackQuery, bot: Bot, cfg: Config, crm: Any = None) -> None:
    if not _is_admin(callback.from_user.id, cfg):
        await callback.answer(texts.FLEET_NO_RIGHTS, show_alert=True)
        return
    if crm is None:
        await callback.answer(texts.FLEET_NO_CRM, show_alert=True)
        return
    _, bike_id, status = (callback.data or "").split(":")
    bike = await crm.bike(int(bike_id))
    if bike is None or status not in crm_logic.BIKE_MANUAL_STATUSES:
        await callback.answer(texts.MOD_BROKEN_BUTTON, show_alert=True)
        return
    if bike.get("rental_id"):
        await callback.answer(texts.FLEET_RENTED_LOCK, show_alert=True)
        return
    # «На сборке» снимает только ввод в эксплуатацию: иначе кнопка
    # «Свободен» из чата выпускала бы технику мимо сверки - ровно то,
    # что сверка и должна ловить. В панели этот запрет уже стоит.
    if bike.get("status") == "new":
        await callback.answer(texts.FLEET_NEW_LOCK, show_alert=True)
        return
    label = crm_logic.BIKE_STATUSES[status]
    if bike["status"] == status:
        await callback.answer(texts.FLEET_SAME_STATUS.format(status=label))
        return
    who = _who(callback.from_user)
    await crm.update_bike(bike["id"], by=f"tg:{callback.from_user.id}", status=status)
    await crm.add_bike_log(bike["id"], "status", f"{label} (из чата)", None,
                           f"tg:{callback.from_user.id}")
    await callback.answer(label)
    bike = await crm.bike(bike["id"])
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                await card_text(crm, bike),
                reply_markup=kb.fleet_card(bike["id"], bike["status"], False))
        except TelegramAPIError:
            pass
    await bot.send_message(callback.message.chat.id,
                           texts.FLEET_STATUS_SET.format(code=logic.esc(bike["code"]),
                                                         status=label, who=logic.esc(who)))


@router.message(BikeCardReply())
async def repair_reply(message: Message, cfg: Config, crm: Any = None) -> None:
    if not _is_admin(message.from_user.id, cfg):
        return
    if crm is None:
        await message.reply(texts.FLEET_NO_CRM)
        return
    code = logic.bike_code_from_card(message.reply_to_message.text)
    bike = await crm.bike_by_code(code or "")
    if bike is None:
        await message.reply(texts.FLEET_NOT_FOUND.format(query=logic.esc(code or "")))
        return
    parsed, err = logic.parse_repair_form(message.text or message.caption,
                                          crm_logic.REPAIR_NODES)
    if parsed is None:
        await message.reply(logic.esc(err))
        return
    who = _who(message.from_user)
    node_title = crm_logic.REPAIR_NODES[parsed["node"]]
    await crm.create_repair(
        bike["id"], items=[parsed],
        note=node_title + (f": {parsed['note']}" if parsed["note"] else "") + " (из чата)",
        created_by=f"tg:{message.from_user.id}")
    total = parsed["parts_cost"] + parsed["labor_cost"]
    await message.reply(texts.FLEET_REPAIR_SAVED.format(
        code=logic.esc(bike["code"]), node=node_title, total=crm_logic.money(total),
        parts=crm_logic.money(parsed["parts_cost"]), labor=crm_logic.money(parsed["labor_cost"]),
        who=logic.esc(who)))
