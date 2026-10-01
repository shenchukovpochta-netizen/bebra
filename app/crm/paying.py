"""Счета эквайринга: опрос статуса и автосписание.

Опрос живёт в процессе бота - по той же причине, что выписка и трекеры:
здесь расписание и бот для сообщений, а веб-процессов может быть
несколько, и каждый спрашивал бы банк об одном и том же.

Почему опрос, а не webhook: панель наружу не смотрит, порт для банка
открывать нечем, а счёт живёт сутки - минутного опроса хватает с
запасом. Появится белый адрес - webhook ляжет поверх, не ломая таблицу:
он будет звать те же `service.check_pay_order`.

Автосписание закрывает уже начисленный долг и ничего не берёт вперёд.
Выключено по умолчанию: это чужая карта, и включать её списание должен
владелец руками.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any

from . import logic, notices, notify, service

log = logging.getLogger(__name__)

# Минута: клиент оплачивает ссылку при операторе, и тот ждёт зелёной
# отметки, чтобы выдать велосипед. Пятиминутная пауза здесь - это
# пятиминутная очередь на точке.
POLL_SECONDS = 60


async def poll_once(crm: Any, acquiring: Any, *, limit: int = 100) -> dict:
    """Спросить банк про все открытые счета. Протухшие - закрыть.

    Сначала банк, потом часы: ссылку, оплаченную за минуту до конца
    суток, опрос мог увидеть уже после них, и закрытие «по времени»
    без вопроса банку оставляло деньги клиента мимо журнала.
    """
    now = datetime.now().astimezone()
    orders = await crm.open_pay_orders(limit=limit)
    paid: list[dict] = []
    twice: list[dict] = []
    failed = expired = 0
    for order in orders:
        state = await service.check_pay_order(crm, order, acquiring=acquiring)
        if state == "paid":
            paid.append(order)
            continue
        if state == "paid_twice":
            # Закрыт руками, а клиент оплатил и ссылку. Отметка - до
            # сообщения и ровно одна: следующий круг этот счёт уже не видит.
            if await crm.mark_pay_twice(order["id"], error=logic.PAY_TWICE_NOTE):
                log.warning("счёт %s оплачен дважды: руками и по ссылке",
                            order.get("no"))
                twice.append(order)
            continue
        if state == "repair_twice":
            # Второй счёт за уже оплаченный ремонт: счёт закрыт с отметкой
            # этим же вызовом, и сказать команде надо ровно сейчас.
            log.warning("ремонт по счёту %s оплачен дважды", order.get("no"))
            twice.append(order)
            continue
        if state == "paid_before":
            continue                 # закрыл другой: сообщает тоже он
        if order.get("status") not in logic.PAY_OPEN:
            continue                 # перепроверка закрытого: ответ прежний
        if state == "failed":
            failed += 1
        elif logic.pay_expired(order, now=now):
            await crm.mark_pay_failed(
                order["id"], error="ссылка просрочена, оплата не поступила")
            expired += 1
    return {"seen": len(orders), "paid": paid, "twice": twice, "failed": failed,
            "expired": expired}


async def report_paid(bot: Any, crm: Any, cfg: Any, order: dict) -> bool:
    """Сказать в служебный чат, что деньги пришли.

    Оператор стоит рядом с клиентом и ждёт именно этого сообщения:
    обновлять список счетов в панели, пока клиент держит телефон, ему
    некогда.
    """
    text = (f"💳 Оплачен счёт {order.get('no')} — "
            f"{logic.money(order.get('amount'))}\n"
            + logic.html.escape(f"{order.get('full_name') or 'клиент'} · "
                                f"{order.get('purpose') or ''}", quote=False))
    # send_team, а не прямой send: получателя этого уведомления владелец
    # задаёт в панели, и отправка мимо него сделала бы настройку пустой.
    return await notices.send_team(crm, bot, "pay_paid", text.strip(),
                                   cfg.contract_chat_id,
                                   client_id=order.get("client_id"))


async def report_twice(bot: Any, crm: Any, cfg: Any, order: dict) -> bool:
    """Счёт закрыли руками, а клиент оплатил и ссылку: деньги дважды.

    В журнал второй раз не пишем - решает человек: вернуть клиенту или
    зачесть. Молчать нельзя: иначе вторые деньги лежат на счёте ничьими.
    """
    why = ("Ремонт по наряду уже был оплачен — другим счётом или на месте."
           if order.get("work_order_id") else
           "Закрыт наличными или переводом, а клиент оплатил и ссылку.")
    text = (f"⚠️ Счёт {order.get('no')} оплачен дважды — "
            f"{logic.money(order.get('amount'))}\n"
            f"{why} Второй раз не зачислено: верните деньги или зачтите руками.\n"
            + logic.html.escape(f"{order.get('full_name') or 'клиент'} · "
                                f"{order.get('purpose') or ''}", quote=False))
    return await notices.send_team(crm, bot, "pay_twice", text.strip(),
                                   cfg.contract_chat_id,
                                   client_id=order.get("client_id"))


async def report_unknown(bot: Any, crm: Any, cfg: Any, order: dict) -> bool:
    """Банк не ответил на списание с карты: прошло ли оно - неизвестно."""
    text = (f"❓ Автосписание {order.get('no')} — {logic.money(order.get('amount'))}: "
            "банк не ответил, прошло ли списание, неизвестно.\n"
            "Сверьте операцию в Точке и закройте счёт в панели. До этого клиенту "
            "больше не списываем.\n"
            + logic.html.escape(f"{order.get('full_name') or 'клиент'}", quote=False))
    return await notices.send_team(crm, bot, "autocharge_unknown", text.strip(),
                                   cfg.contract_chat_id,
                                   client_id=order.get("client_id"))


async def autocharge_daily(crm: Any, acquiring: Any, *, bot: Any = None,
                           today: date | None = None) -> dict:
    """Дневной проход автосписания. Час проверяет вызывающий."""
    return await service.autocharge_once(crm, acquiring=acquiring, bot=bot,
                                         today=today)


async def tell_paid(bot: Any, db: Any, crm: Any, cfg: Any, order: dict) -> None:
    """Оплаченный счёт: команде карточка, клиенту зачисление, агенту бонус.

    Клиенту говорим только про аренду: счёт за ремонт в журнал не идёт,
    и «на балансе» про него - неправда. Бонус за друга - только за
    настоящий платёж в журнале, по той же причине.
    """
    await report_paid(bot, crm, cfg, order)
    if order.get("work_order_id") is not None or order.get("ledger_id") is None:
        return
    client = await crm.client(order["client_id"])
    if client is None:
        return
    await notices.send_client(
        crm, "pay_credited", client["id"],
        lambda: notify.payment_credited(bot, db, crm, client, order["amount"]))
    try:
        await nudge_card(bot, db, crm, client, order)
    except Exception:                                    # noqa: BLE001
        log.exception("предложение привязать карту клиенту %s не собрано", client["id"])
    try:
        bonus = await service.ref_paid(crm, client, logic.to_money(order["amount"]),
                                       by="эквайринг")
    except Exception:                                    # noqa: BLE001
        log.exception("реферальный бонус за счёт %s не начислен", order.get("no"))
        return
    if bonus:
        await notify.referral_bonus(bot, db, bonus["agent"], client, bonus["bonus"])


async def nudge_card(bot: Any, db: Any, crm: Any, client: dict, order: dict) -> bool:
    """Клиент заплатил по ссылке, а карты у нас нет - объяснить, зачем её
    привязать. True - сообщение ушло.

    Только по ссылке (автосписанию карта уже известна), только когда
    автосписание включил владелец и банк уже присылал карты
    (logic.card_nudge_ready) - иначе «спишем сами» было бы неправдой - и
    не чаще раза в срок: отметка на карточке ставится до отправки, как у
    напоминаний, чтобы недоставка не превращалась в повтор на каждой оплате.
    Только арендатору: автосписание берёт долг идущих аренд, и должнику,
    который уже сдал велосипед, «спишем новый период» - неправда, а срок
    предложения съелся бы до следующей аренды.
    """
    if order.get("kind") != "link" or not client.get("tg_id"):
        return False
    settings = await crm.settings()
    if not logic.card_nudge_ready(settings, await crm.cards_seen()):
        return False
    if await crm.card_of(client["id"]) is not None:
        return False                     # банк отдал токен этой же оплатой
    if await crm.active_rental_of(client["id"]) is None:
        return False
    state = await notices.settings(crm)
    if not state.get("card_nudge", {}).get("enabled", True):
        return False
    days = logic.notice_param(state.get("card_nudge"), "every_days",
                              logic.CARD_NUDGE_DAYS)
    if not await crm.claim_card_nudge(client["id"], days=days):
        return False
    hour = logic.pay_settings(settings)["autocharge_hour"]
    return await notices.send_client(
        crm, "card_nudge", client["id"],
        lambda: notify.card_nudge(bot, db, client, hour=hour))


async def paying_loop(bot: Any, crm: Any, cfg: Any, acquiring: Any, *,
                      interval: int = POLL_SECONDS, db: Any = None) -> None:
    """Фоновый опрос счетов. Сбой круга не останавливает следующие."""
    if acquiring is None or not getattr(acquiring, "token", ""):
        log.info("эквайринг Точки не настроен, счета не опрашиваются")
        return
    while True:
        try:
            result = await poll_once(crm, acquiring)
            for order in result["paid"]:
                # Свежая строка: в ней уже есть ledger_id, по нему видно,
                # платёж это или счёт за ремонт.
                fresh = await crm.pay_order(order["id"]) or order
                await tell_paid(bot, db, crm, cfg, fresh)
            for order in result.get("twice") or []:
                await report_twice(bot, crm, cfg, order)
            now = datetime.now()
            if logic.autocharge_time(await crm.settings(), now):
                # Отметка ставится в finally: одна попытка в сутки при любом
                # исходе. Ставить её до прохода нельзя - сбой базы отменял бы
                # списание молча; не ставить вовсе тоже нельзя - при сбое
                # банка круг повторялся бы каждую минуту до полуночи. Живёт
                # она в crm.settings, а не в памяти: перезапуск бота после
                # часа списания иначе прогонял бы проход второй раз.
                try:
                    charge = await autocharge_daily(crm, acquiring, bot=bot,
                                                    today=now.date())
                    if (charge.get("charged") or charge.get("failed")
                            or charge.get("pending") or charge.get("unknown")):
                        log.info("автосписание: списано %s, отказов %s, "
                                 "ждут банк %s, без ответа банка %s",
                                 charge["charged"], charge["failed"],
                                 charge.get("pending", 0), len(charge.get("unknown") or []))
                    for order in charge.get("unknown") or []:
                        await report_unknown(bot, crm, cfg, order)
                finally:
                    await crm.set_setting("autocharge_done_on", now.date().isoformat(),
                                          by="автосписание")
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("опрос счетов не удался, повтор через %s с", interval)
        await asyncio.sleep(interval)
