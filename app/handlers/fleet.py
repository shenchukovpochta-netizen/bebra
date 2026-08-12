"""Служебные команды парка: сводка, список, карточка единицы, брони.

Работают только в служебных чатах и только у админов - как кнопки модерации.
Middleware пускает их по служебному пути (без анкеты, рейт-лимита и гейта
подписки): оператор здесь работает, а не регистрируется.

/park                 - сводка: сколько единиц в каком статусе по точкам
/bikes                - список единиц с вин-номерами и арендаторами
/bike + форма         - добавить или поправить единицу (ключ: значение)
/hold + форма         - удержать единицу под клиента с таймером
/unhold N             - снять бронь
/service N [заметка]  - в сервис (живая бронь снимается)
/free N               - вернуть в «свободен»
"""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.types import Message

from .. import texts
from ..config import Config
from ..fleet import logic as fl
from ..fleet.db import FleetDB
from ..logic import chunked, esc

log = logging.getLogger(__name__)
router = Router(name="fleet")

# Сколько единиц в одном сообщении /bikes: карточка занимает 3-5 строк,
# и полсотни единиц одним сообщением упёрлись бы в лимит длины Telegram.
BIKES_PER_MESSAGE = 8


class ServiceChat(BaseFilter):
    """Служебный чат заявок или договоров - как ServiceChatReply, но для
    обычных сообщений: команды парка реплаев не требуют."""

    async def __call__(self, message: Message, cfg: Config = None) -> bool:
        if cfg is None:
            return False
        return message.chat.id in {cfg.admin_chat_id, cfg.contract_chat_id}


@router.message(ServiceChat(), F.text.func(fl.is_fleet_command))
async def fleet_command(message: Message, cfg: Config,
                        fleet: FleetDB | None = None) -> None:
    if fleet is None:
        return                    # парк не подключён (сборка без main.run)
    if message.from_user is None or message.from_user.id not in cfg.admins:
        return                    # молча, как в модерации: команда не наша
    name = fl.command_name(message.text)
    args = fl.command_args(message.text)
    handler = {"park": _park, "bikes": _bikes, "bike": _bike, "hold": _hold,
               "unhold": _unhold, "service": _service, "free": _free}[name]
    await handler(message, fleet, args)


async def _park(message: Message, fleet: FleetDB, _args: str) -> None:
    rows = await fleet.park_counts()
    await message.answer(fl.park_text(
        [(r["point"], r["model"], r["status"], r["count"]) for r in rows]))


async def _bikes(message: Message, fleet: FleetDB, _args: str) -> None:
    rows = await fleet.list_bikes()
    if not rows:
        await message.answer(fl.park_text([]))
        return
    for chunk in chunked([fl.bike_line(dict(r)) for r in rows],
                         BIKES_PER_MESSAGE):
        await message.answer("\n\n".join(chunk))


async def _bike(message: Message, fleet: FleetDB, args: str) -> None:
    if not args.strip():
        await message.answer(
            texts.FLEET_BIKE_HINT.format(form=fl.BIKE_FORM_TEMPLATE))
        return
    data, err = fl.parse_bike_form(args)
    if err:
        await message.reply(err)
        return
    model_id = point_id = None
    if data.get("model"):
        model_id = await fleet.model_id_by_title(str(data["model"]))
        if model_id is None:
            models = ", ".join(m["title"] for m in await fleet.models_with_tariffs())
            await message.reply(texts.FLEET_MODEL_UNKNOWN.format(
                model=esc(data["model"]), models=esc(models or "пусто")))
            return
    if data.get("point"):
        point_id = await fleet.point_id_by_title(str(data["point"]))
        if point_id is None:
            points = ", ".join(p["title"] for p in await fleet.points())
            await message.reply(texts.FLEET_POINT_UNKNOWN.format(
                point=esc(data["point"]), points=esc(points or "пусто")))
            return
    row = await fleet.upsert_bike(
        str(data["vin_frame"]),
        vin_motor=str(data.get("vin_motor") or "") or None,
        model_id=model_id, point_id=point_id,
        battery_count=data.get("battery_count"),
        status=str(data.get("status") or "") or None,
        notes=str(data.get("notes") or "") or None)
    card = await fleet.bike_card(row["id"])
    await message.answer(texts.FLEET_BIKE_SAVED.format(
        bike=fl.bike_line(dict(card or row))))


async def _find(message: Message, fleet: FleetDB, ref: str):
    """Единица по ссылке оператора; None уже отвечен пользователю."""
    bike = await fleet.get_bike(ref)
    if bike is None:
        await message.reply(texts.FLEET_NOT_FOUND.format(ref=esc(ref or "?")))
    return bike


async def _hold(message: Message, fleet: FleetDB, args: str) -> None:
    if not args.strip():
        await message.answer(
            texts.FLEET_HOLD_HINT.format(form=fl.HOLD_FORM_TEMPLATE))
        return
    data, err = fl.parse_hold_form(args, now=datetime.now())
    if err:
        await message.reply(err)
        return
    bike = await _find(message, fleet, str(data["bike_ref"]))
    if bike is None:
        return
    ok = await fleet.hold(bike["id"], int(data["minutes"]), str(data["note"]),
                          message.from_user.id if message.from_user else None)
    if not ok:
        fresh = await fleet.get_bike(str(bike["id"])) or bike
        await message.reply(texts.FLEET_HOLD_BUSY.format(
            id=bike["id"],
            status=fl.STATUS_TITLES.get(fresh["status"], fresh["status"])))
        return
    note = f", {esc(str(data['note']))}" if data["note"] else ""
    await message.answer(texts.FLEET_HOLD_OK.format(
        id=bike["id"], minutes=data["minutes"], note=note))


async def _unhold(message: Message, fleet: FleetDB, args: str) -> None:
    ref, _ = fl.parse_ref_args(args)
    bike = await _find(message, fleet, ref)
    if bike is None:
        return
    if await fleet.unhold(bike["id"]):
        await message.answer(texts.FLEET_UNHOLD_OK.format(id=bike["id"]))
    else:
        await message.reply(texts.FLEET_UNHOLD_NONE.format(id=bike["id"]))


async def _service(message: Message, fleet: FleetDB, args: str) -> None:
    ref, note = fl.parse_ref_args(args)
    bike = await _find(message, fleet, ref)
    if bike is None:
        return
    await fleet.set_status(bike["id"], fl.SERVICE, note or None)
    await message.answer(texts.FLEET_SERVICE_OK.format(
        id=bike["id"], note=f" ({esc(note)})" if note else ""))


async def _free(message: Message, fleet: FleetDB, args: str) -> None:
    ref, _ = fl.parse_ref_args(args)
    bike = await _find(message, fleet, ref)
    if bike is None:
        return
    await fleet.set_status(bike["id"], fl.FREE)
    await message.answer(texts.FLEET_FREE_OK.format(id=bike["id"]))
