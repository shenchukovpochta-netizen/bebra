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

from . import logic, notices, service

log = logging.getLogger(__name__)

# Минута: клиент оплачивает ссылку при операторе, и тот ждёт зелёной
# отметки, чтобы выдать велосипед. Пятиминутная пауза здесь - это
# пятиминутная очередь на точке.
POLL_SECONDS = 60


async def poll_once(crm: Any, acquiring: Any, *, limit: int = 100) -> dict:
    """Спросить банк про все открытые счета. Протухшие - закрыть."""
    now = datetime.now().astimezone()
    orders = await crm.open_pay_orders(limit=limit)
    paid: list[dict] = []
    failed = expired = 0
    for order in orders:
        if logic.pay_expired(order, now=now):
            await crm.mark_pay_failed(
                order["id"], error="ссылка просрочена, оплата не поступила")
            expired += 1
            continue
        state = await service.check_pay_order(crm, order, acquiring=acquiring)
        if state == "paid":
            paid.append(order)
        elif state == "failed":
            failed += 1
    return {"seen": len(orders), "paid": paid, "failed": failed,
            "expired": expired}


async def report_paid(bot: Any, crm: Any, cfg: Any, order: dict) -> bool:
    """Сказать в служебный чат, что деньги пришли.

    Оператор стоит рядом с клиентом и ждёт именно этого сообщения:
    обновлять список счетов в панели, пока клиент держит телефон, ему
    некогда.
    """
    text = (f"💳 Оплачен счёт {order.get('no')} — "
            f"{logic.money(order.get('amount'))}\n"
            f"{order.get('full_name') or 'клиент'} · {order.get('purpose') or ''}")
    # send_team, а не прямой send: получателя этого уведомления владелец
    # задаёт в панели, и отправка мимо него сделала бы настройку пустой.
    return await notices.send_team(crm, bot, "pay_paid", text.strip(),
                                   cfg.contract_chat_id,
                                   client_id=order.get("client_id"))


async def autocharge_daily(crm: Any, acquiring: Any, *, bot: Any = None,
                           today: date | None = None) -> dict:
    """Дневной проход автосписания. Час проверяет вызывающий."""
    return await service.autocharge_once(crm, acquiring=acquiring, bot=bot,
                                         today=today)


async def paying_loop(bot: Any, crm: Any, cfg: Any, acquiring: Any, *,
                      interval: int = POLL_SECONDS) -> None:
    """Фоновый опрос счетов. Сбой круга не останавливает следующие."""
    if acquiring is None or not getattr(acquiring, "token", ""):
        log.info("эквайринг Точки не настроен, счета не опрашиваются")
        return
    charged_on: date | None = None
    while True:
        try:
            result = await poll_once(crm, acquiring)
            for order in result["paid"]:
                await report_paid(bot, crm, cfg, order)
            today = date.today()
            hour = logic.pay_settings(await crm.settings())["autocharge_hour"]
            if charged_on != today and datetime.now().hour >= hour:
                # Отметка ставится в finally: одна попытка в сутки при любом
                # исходе. Ставить её до прохода нельзя - сбой базы отменял бы
                # списание молча; не ставить вовсе тоже нельзя - при сбое
                # банка круг повторялся бы каждую минуту до полуночи.
                try:
                    charge = await autocharge_daily(crm, acquiring, bot=bot,
                                                    today=today)
                    if (charge.get("charged") or charge.get("failed")
                            or charge.get("pending")):
                        log.info("автосписание: списано %s, отказов %s, "
                                 "ждут банк %s", charge["charged"],
                                 charge["failed"], charge.get("pending", 0))
                finally:
                    charged_on = today
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("опрос счетов не удался, повтор через %s с", interval)
        await asyncio.sleep(interval)
