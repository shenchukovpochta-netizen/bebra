"""Односторонняя синхронизация бот -> CRM.

Бот ведёт документы (анкета, договор, акты), CRM - деньги и парк. Чтобы
оператор не заводил клиента дважды, ключевые события бота отражаются
в CRM: подписан договор - есть карточка клиента, подписан Акт приёма -
идёт аренда, «Оплата получена» - платёж в журнале, подписан Акт
возврата - аренда закрыта.

Каждая функция - best effort: любое исключение ловится и пишется в лог.
Сбой CRM (нет схемы, нет связи) не должен остановить выдачу договора
или акта - это цикл бота, и он важнее учёта.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from .. import logic as bot_logic
from . import logic, service

log = logging.getLogger(__name__)


def _price_of(user: dict) -> Any:
    """Сумма из данных выдачи бота («3000 qr» -> 3000). None - не число."""
    return logic.first_amount((user.get("issue_data") or {}).get("rent_price"))


async def client_from_bot(crm: Any, user: dict) -> dict | None:
    """Карточка клиента по строке bot.users: найти или завести.

    Поиск сначала по tg_id, затем по телефону: клиента могли завести
    в панели руками до того, как он дошёл до бота, - тогда карточка
    получает привязку к Telegram, а не дубль.
    """
    tg_id = user["tg_id"]
    client = await crm.client_by_tg(tg_id)
    phone = bot_logic.normalize_phone(user.get("phone"))
    if client is None and phone:
        client = await crm.client_by_phone(phone)
        if client is not None and client.get("tg_id") not in (None, tg_id):
            log.warning("телефон %s уже привязан к другому Telegram (клиент %s)",
                        phone, client["id"])
            return None
        if client is not None:
            await crm.link_client_tg(client["id"], tg_id, user.get("username"))
            client = {**client, "tg_id": tg_id}
    if client is None:
        if not phone:
            return None          # без телефона карточку не завести
        client_id = await crm.create_client(
            full_name=user.get("full_name") or "Без имени", phone=phone,
            tg_id=tg_id, username=user.get("username"), source="bot",
            contract_no=user.get("contract_no"))
        client = await crm.client(client_id)
        # Друг по приглашению: переход уже записан на /start, теперь у него
        # есть карточка - связываем, иначе бонус платить будет некому.
        # Кабинет зовёт эту функцию напрямую, без своей обёртки: сбой
        # программы приглашений не должен закрывать человеку кабинет.
        try:
            await service.ref_signed(crm, client or {})
        except Exception:                                # noqa: BLE001
            log.exception("CRM: приглашение клиента %s не связано", client_id)
        return await crm.client(client_id)

    # Номер договора и ФИО у бота свежее: договор подписан только что.
    patch: dict[str, Any] = {}
    if user.get("contract_no") and user["contract_no"] != client.get("contract_no"):
        patch["contract_no"] = user["contract_no"]
    if user.get("full_name") and client.get("source") == "bot" \
            and user["full_name"] != client.get("full_name"):
        patch["full_name"] = user["full_name"]
    if user.get("username") and user["username"] != client.get("username"):
        patch["username"] = user["username"]
    if patch:
        await crm.update_client(client["id"], **patch)
        client = {**client, **patch}
    return client


async def on_contract_signed(crm: Any, user: dict) -> None:
    try:
        await client_from_bot(crm, user)
    except Exception:                                    # noqa: BLE001
        log.exception("CRM: клиент по договору %s не синхронизирован",
                      user.get("contract_no"))


async def on_payment_confirmed(crm: Any, user: dict, *, by: str) -> None:
    """«Оплата получена» в боте: платёж в журнал CRM. Сумма - из данных
    выдачи; если оператор написал её словами, платёж не заводится и
    в логе остаётся след - добавит руками в панели."""
    try:
        client = await client_from_bot(crm, user)
        if client is None:
            return
        amount = _price_of(user)
        if not amount:
            log.warning("CRM: сумма оплаты по договору %s не распознана - "
                        "платёж не заведён", user.get("contract_no"))
            return
        await crm.add_ledger(client_id=client["id"], kind="payment", amount=amount,
                             method="sbp", created_by=by,
                             note=f"Оплата по договору № {user.get('contract_no') or '—'} "
                                  f"(подтверждена в боте)")
        await service.ref_paid(crm, client, amount, by=by)
    except Exception:                                    # noqa: BLE001
        log.exception("CRM: платёж по договору %s не записан", user.get("contract_no"))


async def _bike_for(crm: Any, spec: dict) -> int | None:
    """Велосипед из формы выдачи: найти по номеру рамы или завести.

    Заводится, а не пропускается: парк в панели должен совпадать с тем,
    что реально выдано, даже если оператор ещё не занёс велосипед руками.
    Занятый другой арендой велосипед не трогаем - это ошибка оператора,
    и её должно быть видно в панели, а не спрятано в тихом переносе.
    """
    frame = spec.get("frame_no")
    if frame:
        bike = await crm.bike_by_frame(frame)
        if bike is not None:
            if bike.get("status") == "rented":
                log.warning("CRM: велосипед с рамой %s уже в аренде", frame)
                return None
            return bike["id"]
    code = logic.bike_code_from_frame(frame, spec.get("bike_model"))
    if await crm.bike_by_code(code) is not None:
        code = f"{code}-{date.today().strftime('%d%m')}"
    return await crm.create_bike(by="bot", code=code, model=spec.get("bike_model") or "—",
                                 frame_no=frame, motor_no=spec.get("motor_no"),
                                 note="Заведён ботом из формы выдачи")


async def on_rental_started(crm: Any, user: dict, *, today: date) -> None:
    """Акт приёма подписан: в CRM появляется аренда с первым начислением."""
    try:
        client = await client_from_bot(crm, user)
        if client is None:
            return
        if await crm.active_rental_of(client["id"]) is not None:
            return          # оформлена в панели раньше - не дублируем
        spec = logic.rental_from_issue(user.get("issue_data"), user.get("rent_from"),
                                       user.get("rent_until"), today=today)
        bike_id = await _bike_for(crm, spec)
        period_to = spec["started_on"] + timedelta(days=spec["period_days"])
        # Аренда и её первое начисление - одной транзакцией: порознь сбой
        # между ними оставлял аренду без начисления навсегда, а клиента -
        # с лишним периодом на балансе.
        await crm.start_rental_charged(
            client_id=client["id"], bike_id=bike_id,
            tariff_name=spec["tariff_name"], period_days=spec["period_days"],
            price=spec["price"], billing=spec["billing"],
            started_on=spec["started_on"], period_to=period_to,
            contract_no=user.get("contract_no"),
            note=f"Аренда по договору № {user.get('contract_no') or '—'}: "
                 f"{logic.period_label(spec['started_on'], period_to)}",
            created_by="bot")
        # Шаг воронки приглашений: аренду из бота оформляет не open_rental,
        # и без этого друг перепрыгивал бы «взял велосипед» - а это
        # основной путь выдачи, через договор и акт в боте.
        await service.ref_rented(crm, client)
    except Exception:                                    # noqa: BLE001
        log.exception("CRM: аренда по договору %s не заведена", user.get("contract_no"))


async def on_rental_extended(crm: Any, user: dict, *, until: date, by: str) -> None:
    """Продление оплачено: платёж и начисление за новый срок одной парой.

    Аренда, заведённая ботом, начисляется вручную - по событиям, а не по
    календарю, поэтому продление само добавляет свой период.
    """
    try:
        client = await client_from_bot(crm, user)
        if client is None:
            return
        amount = _price_of(user)
        rental = await crm.active_rental_of(client["id"])
        manual = rental is not None and rental.get("billing") == "manual"
        start = rental["billed_until"] if rental else None
        if manual and start is not None and until > start:
            if not amount:
                log.warning("CRM: продление %s до %s - цена «%s» не разобрана, "
                            "начислено 0; поправьте в панели",
                            user.get("contract_no"), until,
                            (user.get("issue_data") or {}).get("rent_price"))
            # Платёж и начисление - одной транзакцией: порознь сбой между
            # ними уводил клиента в плюс на целый период.
            await crm.extend_rental_paid(
                rental["id"], client["id"], amount=amount or logic.to_money(0),
                period_from=start, period_to=until, method="sbp", created_by=by,
                pay_note=f"Продление по договору № "
                         f"{user.get('contract_no') or '—'} "
                         f"до {until.strftime('%d.%m.%Y')}",
                charge_note=f"Продление: {logic.period_label(start, until)}")
        elif amount:
            # Аренда начисляется по календарю или продление уже начислено:
            # остаётся один платёж.
            await crm.add_ledger(client_id=client["id"], kind="payment", amount=amount,
                                 method="sbp", created_by=by,
                                 note=f"Продление по договору № "
                                      f"{user.get('contract_no') or '—'} "
                                      f"до {until.strftime('%d.%m.%Y')}")
        if amount:
            await service.ref_paid(crm, client, amount, by=by)
    except Exception:                                    # noqa: BLE001
        log.exception("CRM: продление по договору %s не записано", user.get("contract_no"))


async def _return_point(crm: Any, user: dict) -> str | None:
    """Точка возврата по строке «адрес» формы сдачи («Адоратского 15»).

    Не сопоставилась со справочником или справочник недоступен - None:
    точку не угадываем, велосипед остаётся на точке аренды. Сбой здесь
    не вправе сорвать закрытие аренды - оно важнее точки.
    """
    data = user.get("return_data")
    address = data.get("return_address") if isinstance(data, dict) else None
    if not address:
        return None
    try:
        return logic.match_location(address, await crm.locations())
    except Exception:                                    # noqa: BLE001
        log.exception("CRM: точка возврата по адресу не определена")
        return None


async def on_rental_closed(crm: Any, user: dict, *, today: date) -> None:
    try:
        client = await crm.client_by_tg(user["tg_id"])
        if client is None:
            return
        rental = await crm.active_rental_of(client["id"])
        if rental is None:
            return
        # Через сервис, а не напрямую в базу: закрытие обязано вернуть и
        # батареи. Прямой вызов `crm.close_rental` оставлял их «у клиента»
        # навсегда - по две штуки на каждой аренде, закрытой из бота.
        # Выкупленный велосипед в парк не возвращается: он теперь чужой.
        bought = bool(user.get("buyout_signed_at"))
        await service.close_rental(crm, rental, closed_on=today, by="bot",
                                   bike_status="sold" if bought else "available",
                                   note="Акт возврата подписан в боте",
                                   return_location=await _return_point(crm, user))
    except Exception:                                    # noqa: BLE001
        log.exception("CRM: аренда по договору %s не закрыта", user.get("contract_no"))


async def on_buyout_signed(crm: Any, user: dict, *, today: date) -> None:
    """Акт выкупа подписан: велосипед - собственность клиента.

    Бот после выкупа замолкает, а CRM без этого хука продолжала бы
    аренду: начисляла периоды, слала «долг, сдайте велосипед», звала в
    розыск и, закрыв аренду возвратом, выпускала бы проданный велосипед
    в «свободные». Аккумуляторы и прочее оборудование в выкуп не входят
    и по акту возвращены - поэтому батареи уходят в «свободна».
    """
    try:
        client = await crm.client_by_tg(user["tg_id"])
        if client is None:
            return
        rental = await crm.active_rental_of(client["id"])
        if rental is None:
            return
        await service.close_rental(crm, rental, closed_on=today, by="bot",
                                   bike_status="sold",
                                   note="Выкуп: акт о переходе права собственности подписан")
    except Exception:                                    # noqa: BLE001
        log.exception("CRM: выкуп по договору %s не отражён", user.get("contract_no"))


async def card_flag(crm: Any, user: dict) -> tuple[str, str] | None:
    """(статус, заметка) для карточки модерации, если заявитель в CRM
    числится с закрытым статусом. None - всё в порядке или CRM недоступна.

    Ищется по tg_id и по телефону из анкеты: человек из чёрного списка
    заводит новый Telegram-аккаунт, а номер оставляет прежним.
    """
    try:
        client = await crm.client_by_tg(user["tg_id"])
        phone = bot_logic.normalize_phone(user.get("phone"))
        if client is None and phone:
            client = await crm.client_by_phone(phone)
        if client is None or client.get("status") == "active":
            return None
        return (logic.CLIENT_STATUSES.get(client["status"], client["status"]),
                client.get("note") or "")
    except Exception:                                    # noqa: BLE001
        log.exception("CRM: проверка чёрного списка для %s не удалась", user.get("tg_id"))
        return None
