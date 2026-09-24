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

from . import logic, notices, notify, service

log = logging.getLogger(__name__)

# Полчаса: клиент, оплативший утром, не должен ждать выдачи до вечера,
# а чаще дёргать банк незачем - выписка собирается не мгновенно.
POLL_SECONDS = 1800
# За сколько дней тянуть выписку каждый раз. Три - с запасом на выходные
# и на операции, которые банк проводит задним числом; повторы отсекает
# уникальный номер операции.
STATEMENT_DAYS = 3


async def import_once(crm: Any, client: Any, *, today: date | None = None,
                      days: int = STATEMENT_DAYS,
                      statement_id: str | None = None,
                      bot: Any = None, db: Any = None) -> dict:
    """Забрать выписку и сложить в базу. Зачисления - отдельным шагом.

    Банк собирает документ не мгновенно, поэтому номер заказанной
    выписки возвращается наружу в `pending`: следующий круг читает ЕЁ,
    а не заказывает новую. Заказывать каждый раз новую и читать её тут
    же значит не прочитать выписку никогда.
    """
    today = today or date.today()
    statement = await client.statement(since=today - timedelta(days=days),
                                       until=today, statement_id=statement_id)
    if not statement.get("ready"):
        return {"seen": 0, "saved": 0, "credited": 0,
                "pending": statement.get("statement_id")}
    result = await service.import_statement(crm, statement["rows"])
    credited = await auto_credit(crm, bot=bot, db=db)
    return {**result, "credited": credited, "pending": None}


async def tell_credited(bot: Any, db: Any, crm: Any, client: dict, amount: Any, *,
                        by: str) -> None:
    """Перевод зачислен: клиенту - новая дата «оплачено до», агенту - бонус.

    То же, что после заявки и оплаченного счёта: клиент, заплативший
    переводом, иначе не узнаёт, что деньги дошли, а агент терял бонус за
    друга только потому, что зачислила машина, а не человек.
    """
    amount = logic.to_money(amount)
    if bot is not None:
        await notices.send_client(
            crm, "pay_credited", client["id"],
            lambda: notify.payment_credited(bot, db, crm, client, amount))
    try:
        bonus = await service.ref_paid(crm, client, amount, by=by)
    except Exception:                                    # noqa: BLE001
        log.exception("реферальный бонус за клиента %s не начислен", client.get("id"))
        return
    if bonus and bot is not None:
        await notify.referral_bonus(bot, db, bonus["agent"], client, bonus["bonus"])


async def auto_credit(crm: Any, *, by: str = "bank", bot: Any = None,
                      db: Any = None) -> int:
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
        client = row["guess"]["client"]
        try:
            await service.credit_bank_txn(crm, row, client, by=by)
        except service.ServiceError as exc:
            log.info("автозачисление %s пропущено: %s", row.get("txn_id"), exc)
            continue
        done += 1
        try:
            await tell_credited(bot, db, crm, client, row["amount"], by=by)
        except Exception:                                # noqa: BLE001
            # Деньги уже в журнале: недоставленное сообщение не повод
            # останавливать разбор остальных строк.
            log.exception("о зачислении %s клиенту не сообщено", row.get("txn_id"))
    return done


async def report_unmatched(bot: Any, crm: Any, cfg: Any, limit: int = 10, *,
                           chat_id: Any = None) -> int:
    """Напомнить оператору о неразобранных поступлениях.

    Зовётся дневным проходом (`billing.run_daily`) в свой час. Раньше её
    не звал никто: функция была написана, тумблер в панели показывался,
    а сообщение не уходило никогда.
    """
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
    # Через send_team, а не напрямую в чат: владелец мог назначить этому
    # уведомлению своего получателя в панели, и отправка мимо него
    # означала бы, что настройка ничего не делает.
    if not await notices.send_team(crm, bot, "bank_unmatched", "\n".join(lines),
                                   chat_id or cfg.contract_chat_id):
        return 0
    return len(rows)


async def banking_loop(bot: Any, crm: Any, cfg: Any, client: Any, *,
                       interval: int = POLL_SECONDS, db: Any = None) -> None:
    """Фоновый разбор выписки. Сбой круга не останавливает следующие."""
    if client is None or not getattr(client, "ready", False):
        log.info("счёт в Точке не настроен, выписка не тянется")
        return
    del cfg
    pending: str | None = None
    while True:
        try:
            result = await import_once(crm, client, statement_id=pending,
                                       bot=bot, db=db)
            pending = result.get("pending")
            if result["saved"] or result["credited"]:
                log.info("выписка: новых строк %s, зачислено %s",
                         result["saved"], result["credited"])
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("разбор выписки не удался, повтор через %s с", interval)
        await asyncio.sleep(interval)
