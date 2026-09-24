"""Рабочая группа точек: сверка сообщений с базой.

Раньше группу читал сценарий n8n и переписывал формы в Google-таблицу
«Действующие арендаторы». Таблица расходилась с CRM с первого дня: одна
и та же аренда жила в двух местах, и верным было то, что обновили
последним. Теперь источник один - база, а сообщение в группе проверяется
на совпадение с ней:

- фиксация выдачи: клиент не в стоп-листе, велосипед в парке, аренда на
  нём идёт и у того же человека;
- «ЗАМЕНА»: проводится в CRM той же заменой, что и из карточки аренды -
  аренда та же, меняется велосипед;
- сдача: аренда в CRM закрыта, долг виден сразу;
- итоги дня сервиса: сохраняются в журнал, панель показывает их списком;
- долг и GPS: ответ по номеру рамы, мотора или телефону.

Денег отсюда в журнал не пишется ничего: «оплатил 1 500» в группе - это
слова, платёж проводит человек в панели. Модуль не знает про Telegram -
он возвращает реакцию и текст, отправляет их обработчик бота.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from . import logic, service

log = logging.getLogger(__name__)

OK, BAD = "👍", "👎"
# Сдача, которую CRM закрыла больше недели назад, - это не та сдача:
# скорее велосипед выдавали мимо базы, и отчёт в группе ей не соответствует.
RETURN_FRESH_DAYS = 7


@dataclass
class Outcome:
    reaction: str | None = None
    reply: str | None = None


def _esc(value: Any) -> str:
    return logic.html.escape(str(value or "—"), quote=False)


async def _save(crm: Any, meta: dict, kind: str, data: dict, *, ok: bool,
                note: str | None = None, bike: dict | None = None,
                rental: dict | None = None, client_id: int | None = None) -> None:
    await crm.save_ops_report(
        kind=kind, chat_id=meta["chat_id"], message_id=meta["message_id"],
        thread_id=meta.get("thread_id"), author_tg=meta.get("author_tg"),
        author=meta.get("author"), bike_id=bike["id"] if bike else None,
        rental_id=rental["id"] if rental else None,
        client_id=client_id if client_id is not None
        else (rental["client_id"] if rental else None),
        data=data, ok=ok, note=note)


async def _bike(crm: Any, *numbers: Any) -> dict | None:
    for number in numbers:
        if logic.vin_key(number):
            bike = await crm.bike_by_vin(str(number))
            if bike is not None:
                return bike
    return None


def _numbers(*numbers: Any) -> str:
    shown = [str(n) for n in numbers if logic.vin_key(n)]
    return " / ".join(_esc(n) for n in shown) or "—"


async def _fail(crm: Any, meta: dict, kind: str, data: dict, note: str, *,
                reply: str | None = None, **links: Any) -> Outcome:
    await _save(crm, meta, kind, data, ok=False, note=note, **links)
    return Outcome(BAD, reply or note[:1].upper() + note[1:] + ".")


async def fixation(crm: Any, text: str, meta: dict) -> Outcome:
    """Форма «1. ФИО: …» из темы фиксации выдачи."""
    data, err = logic.parse_ops_fix(text)
    if data is None:
        return Outcome(BAD, err)
    phones = data.pop("phones", [])
    hit = logic.blacklist_hit(phones, data["fio"], await crm.flagged_clients())
    if hit is not None:
        client, matched_by = hit
        return await _fail(crm, meta, "fix", data, f"стоп-лист CRM, совпадение по: {matched_by}",
                           reply=logic.ops_blacklist_text(client, matched_by),
                           client_id=client["id"])
    bike = await _bike(crm, data["vin_motor"], data["vin_frame"])
    if bike is None:
        return await _fail(
            crm, meta, "fix", data, "велосипед не найден в парке",
            reply=(f"Велосипед {_numbers(data['vin_motor'], data['vin_frame'])} "
                   "не найден в парке CRM. Проверьте номер или заведите "
                   "велосипед в панели."))
    frame_key = logic.vin_key(data["vin_frame"])
    if frame_key and logic.vin_key(bike.get("frame_no")) not in ("", frame_key):
        return await _fail(
            crm, meta, "fix", data, "рама в форме не совпадает с карточкой", bike=bike,
            reply=(f"У велосипеда {_esc(bike['code'])} в CRM рама "
                   f"{_esc(bike.get('frame_no'))}, в форме - {_esc(data['vin_frame'])}. "
                   "Проверьте, какой велосипед выдали."))
    motor_key = logic.vin_key(data["vin_motor"])
    if motor_key and logic.vin_key(bike.get("motor_no")) not in ("", motor_key):
        # Велосипед нашёлся по раме, а мотор в карточке другой: колесо
        # меняли в ремонте, а номер в карточке остался старым.
        return await _fail(
            crm, meta, "fix", data, "мотор в форме не совпадает с карточкой", bike=bike,
            reply=(f"У велосипеда {_esc(bike['code'])} в CRM мотор "
                   f"{_esc(bike.get('motor_no'))}, в форме - {_esc(data['vin_motor'])}. "
                   "Если мотор-колесо меняли, обновите номер в карточке велосипеда."))
    rental = await crm.active_rental_of_bike(bike["id"])
    if rental is None:
        return await _fail(
            crm, meta, "fix", data, "в CRM нет идущей аренды на велосипед", bike=bike,
            reply=(f"На велосипед {_esc(bike['code'])} в CRM нет идущей аренды. "
                   "Оформите выдачу ботом или мастером «Быстрая выдача» в "
                   "панели - иначе не будет ни начислений, ни напоминаний."))
    client = await crm.client(rental["client_id"]) or {}
    if not logic.ops_client_matches(phones, data["fio"], client):
        return await _fail(
            crm, meta, "fix", data, "аренда в CRM у другого клиента", bike=bike,
            rental=rental,
            reply=(f"Велосипед {_esc(bike['code'])} в CRM выдан другому клиенту: "
                   f"{_esc(client.get('full_name'))}. Ни телефон, ни ФИО из формы "
                   "с карточкой не совпали."))
    await _save(crm, meta, "fix", data, ok=True, bike=bike, rental=rental)
    return Outcome(OK)


async def swap(crm: Any, text: str, meta: dict, *, allowed: bool, by: str) -> Outcome:
    """«ЗАМЕНА»: была рама/мотор, «стало:» - новые. Проводится в CRM."""
    if not allowed:
        return Outcome(BAD, "Замену в CRM проводит сотрудник с правом менять аренды. "
                            "Привяжите Telegram в панели: «Сотрудники» → код → "
                            "/staff КОД в личке боту.")
    data, err = logic.parse_ops_swap(text)
    if data is None:
        return Outcome(BAD, err)
    old = await _bike(crm, data["old_motor"], data["old_frame"])
    new = await _bike(crm, data["new_motor"], data["new_frame"])
    if old is None:
        shown = _numbers(data["old_motor"], data["old_frame"])
        return await _fail(crm, meta, "swap", data, "снятый велосипед не найден в парке",
                           reply=f"Снятый велосипед {shown} не найден в парке CRM.")
    if new is None:
        shown = _numbers(data["new_motor"], data["new_frame"])
        return await _fail(crm, meta, "swap", data, "новый велосипед не найден в парке",
                           bike=old, reply=f"Новый велосипед {shown} не найден в парке CRM.")
    rental = await crm.active_rental_of_bike(old["id"])
    if rental is None:
        done = await crm.active_rental_of_bike(new["id"])
        if done is not None and (not data["fio"]
                                 or logic.same_person(data["fio"], done.get("full_name"))):
            # Замену уже провели из карточки аренды - сообщение её только
            # подтверждает, второй раз менять нечего.
            await _save(crm, meta, "swap", data, ok=True, note="замена уже в CRM",
                        bike=new, rental=done)
            return Outcome(OK)
        return await _fail(crm, meta, "swap", data, "на снятом велосипеде нет идущей аренды",
                           bike=old,
                           reply=f"На велосипеде {_esc(old['code'])} в CRM нет идущей аренды.")
    if data["fio"] and not logic.same_person(data["fio"], rental.get("full_name")):
        return await _fail(crm, meta, "swap", data, "аренда в CRM у другого клиента",
                           bike=old, rental=rental,
                           reply=(f"Велосипед {_esc(old['code'])} в CRM у другого клиента: "
                                  f"{_esc(rental.get('full_name'))}."))
    try:
        result = await service.swap_bike(
            crm, rental, new, reason=data["reason"], mileage_old=data["mileage_old"],
            mileage_new=data["mileage_new"], by=by)
    except service.ServiceError as exc:
        return await _fail(crm, meta, "swap", data, str(exc).rstrip("."), bike=old,
                           rental=rental, reply=f"Замена не проведена: {_esc(exc)}")
    await _save(crm, meta, "swap", data, ok=True, bike=new, rental=rental)
    status = logic.BIKE_STATUSES.get(result["old_status"], result["old_status"])
    return Outcome(OK, (f"Замена проведена в CRM: {_esc(old['code'])} → {_esc(new['code'])}"
                        f" ({logic.SWAP_REASONS[data['reason']].lower()}). "
                        f"Снятый велосипед - «{status}»."))


async def handover(crm: Any, text: str, meta: dict, *, today: date) -> Outcome:
    """Отчёт о сдаче: аренда в CRM закрыта, и закрыта недавно."""
    data, err = logic.parse_ops_return(text)
    if data is None:
        return Outcome(BAD, err) if logic.looks_like_return(text) else Outcome()
    bike = await _bike(crm, data["vin_motor"], data["vin_frame"])
    if bike is None:
        return await _fail(crm, meta, "return", data, "велосипед не найден в парке",
                           reply=(f"Велосипед {_numbers(data['vin_motor'], data['vin_frame'])}"
                                  " не найден в парке CRM."))
    active = await crm.active_rental_of_bike(bike["id"])
    if active is not None:
        return await _fail(
            crm, meta, "return", data, "аренда в CRM ещё идёт", bike=bike, rental=active,
            reply=(f"Аренда {_esc(active.get('full_name'))} на велосипеде "
                   f"{_esc(bike['code'])} в CRM ещё открыта. Закройте её - формой "
                   "закрытия в боте или кнопкой в карточке аренды, - иначе "
                   "начисления продолжатся."))
    last = await crm.last_rental_of_bike(bike["id"])
    if last is None:
        return await _fail(crm, meta, "return", data, "по велосипеду нет ни одной аренды",
                           bike=bike,
                           reply=f"По велосипеду {_esc(bike['code'])} в CRM нет ни одной аренды.")
    closed = last.get("closed_on")
    if closed is not None and (today - closed).days > RETURN_FRESH_DAYS:
        return await _fail(
            crm, meta, "return", data, "последняя аренда закрыта давно", bike=bike,
            rental=last,
            reply=(f"Последняя аренда велосипеда {_esc(bike['code'])} в CRM закрыта "
                   f"{closed.strftime('%d.%m.%Y')} - эта сдача в CRM не отражена."))
    if data["fio"] and not logic.same_person(data["fio"], last.get("full_name")):
        return await _fail(crm, meta, "return", data, "последняя аренда у другого клиента",
                           bike=bike, rental=last,
                           reply=(f"Последняя аренда велосипеда {_esc(bike['code'])} - у "
                                  f"{_esc(last.get('full_name'))}, а не у "
                                  f"{_esc(data['fio'])}."))
    await _save(crm, meta, "return", data, ok=True, bike=bike, rental=last)
    bal = logic.to_money(await crm.client_balance(last["client_id"]))
    if bal < 0:
        return Outcome(OK, (f"Аренда закрыта, но за клиентом долг {logic.money(-bal)}. "
                            f"В отчёте оплачено долгов: {_esc(data['debt_paid'] or '0')}. "
                            "Если деньги приняли - проведите платёж в панели."))
    return Outcome(OK)


async def daily(crm: Any, text: str, meta: dict) -> Outcome:
    """Итоги дня сервиса. Не похоже на отчёт - это разговор, молчим."""
    data, _ = logic.parse_ops_daily(text)
    if data is None:
        return Outcome()
    await _save(crm, meta, "daily", data, ok=True)
    return Outcome(OK)


async def _find(crm: Any, query: str) -> tuple[dict | None, dict | None]:
    """(велосипед, идущая аренда) по номеру или телефону клиента."""
    phone = logic.bot_logic.normalize_phone(query)
    if phone:
        client = await crm.client_by_phone(phone)
        rental = await crm.active_rental_of(client["id"]) if client else None
        if rental is not None and rental.get("bike_id"):
            return await crm.bike(rental["bike_id"]), rental
    bike = await crm.bike_by_vin(query)
    if bike is None:
        return None, None
    return bike, await crm.active_rental_of_bike(bike["id"])


def _not_found(query: str) -> str | None:
    """Ответ «не нашёл» - только на то, что похоже на номер: в теме
    переговариваются, и отвечать на каждое «ок» незачем."""
    key = logic.vin_key(query)
    if len(key) >= 6 and any(ch.isdigit() for ch in key):
        return f"Не нашёл в парке велосипед «{_esc(query)}»."
    return None


async def debt(crm: Any, text: str, *, today: date) -> Outcome:
    query = logic.ops_query(text)
    if query is None:
        return Outcome()
    bike, rental = await _find(crm, query)
    if bike is None:
        return Outcome(reply=_not_found(query))
    client = await crm.client(rental["client_id"]) if rental else None
    bal = rental.get("balance") if rental else 0
    return Outcome(reply=logic.ops_debt_text(bike, rental, client, bal, today=today))


async def gps(crm: Any, text: str, *, now: datetime) -> Outcome:
    query = logic.ops_query(text)
    if query is None:
        return Outcome()
    bike, _ = await _find(crm, query)
    if bike is None:
        return Outcome(reply=_not_found(query))
    tracker = await crm.tracker_of_bike(bike["id"])
    return Outcome(reply=logic.ops_gps_text(bike, tracker, now=now))


async def handle(crm: Any, topic: str, text: str, meta: dict, *, today: date,
                 now: datetime, swap_allowed: bool = False, by: str = "") -> Outcome:
    """Сообщение из темы группы -> реакция и ответ."""
    if topic == "fix":
        if logic.is_ops_swap(text):
            return await swap(crm, text, meta, allowed=swap_allowed, by=by)
        if logic.is_ops_fix(text):
            return await fixation(crm, text, meta)
        return Outcome()
    if topic == "return":
        return await handover(crm, text, meta, today=today)
    if topic == "daily":
        return await daily(crm, text, meta)
    if topic == "debt":
        return await debt(crm, text, today=today)
    if topic == "gps":
        return await gps(crm, text, now=now)
    return Outcome()
