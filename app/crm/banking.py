"""Выписка Точки: из банка в базу, а дальше решает человек.

Опрос живёт в процессе бота - по той же причине, что и опрос трекеров:
там расписание и бот для сообщений, а веб-процессов может быть
несколько.

На счёт падает не только аренда: выручка чужого ремонта, возвраты
поставщиков, личные переводы владельца. Поэтому строка выписки сама по
себе платежом не становится - оператор подтверждает зачисление в панели.
Автозачисление есть, но выключено по умолчанию и срабатывает только на
точное совпадение номера договора в назначении: угадывать по сумме -
это чужие деньги на чужом балансе.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any

from aiogram.exceptions import TelegramAPIError

from . import logic, notices, service

log = logging.getLogger(__name__)

# Полчаса: клиент, оплативший утром, не должен ждать выдачи до вечера,
# а чаще дёргать банк незачем - выписка собирается не мгновенно.
POLL_SECONDS = 1800
# За сколько дней тянуть выписку каждый раз. Три - с запасом на выходные
# и на операции, которые банк проводит задним числом; повторы отсекает
# уникальный номер операции.
STATEMENT_DAYS = 3


async def import_once(crm: Any, client: Any, *, today: date | None = None,
                      days: int = STATEMENT_DAYS) -> dict:
    """Забрать выписку и сложить в базу. Зачисления - отдельным шагом."""
    today = today or date.today()
    statement = await client.statement(since=today - timedelta(days=days),
                                       until=today)
    if not statement.get("ready"):
        # Банк ещё собирает документ: номер сохранять негде и незачем -
        # следующий проход закажет заново, это дешёвая операция.
        return {"seen": 0, "saved": 0, "credited": 0}
    result = await service.import_statement(crm, statement["rows"])
    credited = await auto_credit(crm)
    return {**result, "credited": credited}


async def auto_credit(crm: Any, *, by: str = "bank") -> int:
    """Зачислить то, в чём нет сомнений: номер договора в назначении.

    Выключено, пока владелец не включит `bank_auto_credit` в настройках:
    по умолчанию деньги на баланс кладёт человек, и это осознанно.
    """
    settings = await crm.settings()
    if not logic.bank_settings(settings)["auto_credit"]:
        return 0
    rows = logic.bank_rows(await crm.bank_txns(status="new", limit=200),
                           await crm.clients(limit=10000))
    done = 0
    for row in rows:
        if not row.get("sure"):
            continue
        try:
            await service.credit_bank_txn(crm, row, row["guess"]["client"], by=by)
            done += 1
        except service.ServiceError as exc:
            log.info("автозачисление %s пропущено: %s", row.get("txn_id"), exc)
    return done


async def report_unmatched(bot: Any, crm: Any, cfg: Any, limit: int = 10) -> int:
    """Напомнить оператору о неразобранных поступлениях."""
    if not await notices.allowed(crm, "bank_unmatched"):
        return 0
    rows = [r for r in await crm.bank_txns(status="new", limit=100)
            if r["direction"] == "credit"]
    if not rows:
        return 0
    total = logic.to_money(sum(logic.to_money(r["amount"]) for r in rows))
    lines = [f"🏦 Выписка: не разобрано {len(rows)} поступлений на {logic.money(total)}"]
    for row in rows[:limit]:
        who = row.get("payer_name") or "плательщик не указан"
        lines.append(f"• {logic.money(row['amount'])} — {who}")
    if len(rows) > limit:
        lines.append(f"…и ещё {len(rows) - limit}")
    try:
        await bot.send_message(cfg.contract_chat_id, "\n".join(lines))
    except TelegramAPIError as exc:
        log.exception("сводка по выписке не доставлена")
        await notices.record(crm, "bank_unmatched", status="failed",
                             detail=str(exc))
        return 0
    await notices.record(crm, "bank_unmatched", status="sent")
    return len(rows)


async def banking_loop(bot: Any, crm: Any, cfg: Any, client: Any, *,
                       interval: int = POLL_SECONDS) -> None:
    """Фоновый разбор выписки. Сбой круга не останавливает следующие."""
    if client is None or not getattr(client, "ready", False):
        log.info("счёт в Точке не настроен, выписка не тянется")
        return
    del bot, cfg
    while True:
        try:
            result = await import_once(crm, client)
            if result["saved"] or result["credited"]:
                log.info("выписка: новых строк %s, зачислено %s",
                         result["saved"], result["credited"])
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("разбор выписки не удался, повтор через %s с", interval)
        await asyncio.sleep(interval)
