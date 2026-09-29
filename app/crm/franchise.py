"""Опрос франчайзи: раз в сутки забрать агрегаты и сложить в базу.

Живёт в процессе бота, как опросы банка и трекеров: веб-процессов может
быть несколько, и каждый ходил бы к франчайзи сам. Панель зовёт отсюда
ровно refresh_one - по кнопке «Обновить сейчас», один запрос к одному
франчайзи при владельце (так же, как ссылку на оплату просят при клиенте).

Сбой одного франчайзи не останавливает остальных: у каждого свой сервер,
и лежащий в Самаре не повод слепнуть по Уфе. Причина неудачи ложится в
карточку, прежние цифры остаются - по ok_at видно, насколько они старые.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from ..services import franchise as franchise_http
from . import logic, notices, service

log = logging.getLogger(__name__)

# Круг - десять минут: кому пора, решает logic.franchise_due (раз в сутки
# после принятого ответа, неудачу - не чаще раза в час).
POLL_SECONDS = 600
# Снимки держим с запасом на год: для роялти хватает crm.franchise_months,
# а снимок нужен, чтобы показать, что именно прислал франчайзи.
SNAPSHOT_DAYS = 400
ROOT = Path(__file__).resolve().parent.parent.parent


@functools.lru_cache(maxsize=4)
def code_stamp(root: Path = ROOT) -> str:
    """Версия копии системы для франчайзера: отпечаток кода и схемы.

    Номера версии у проекта нет, а франчайзеру важно одно - та же ли
    сборка стоит у франчайзи, что у него. Поэтому по содержимому, а не по
    времени правки: одинаковый код на двух серверах даёт одну метку.
    Код в процессе не меняется, поэтому считается один раз на процесс.
    """
    digest = hashlib.sha256()
    for item in sorted([*(root / "app").rglob("*.py"), root / "schema.sql"]):
        if item.is_file():
            digest.update(item.relative_to(root).as_posix().encode())
            digest.update(item.read_bytes())
    return digest.hexdigest()[:12]


async def refresh_one(crm: Any, vault: Any, row: dict, *, fetch: Any = None,
                      now: datetime | None = None) -> str | None:
    """Опросить одного франчайзи. None - ответ принят и записан, иначе
    причина (она же легла в карточку)."""
    now = now or datetime.now().astimezone()
    fetch = fetch or franchise_http.fetch_metrics
    token = service.franchise_token(vault, row)
    if not token:
        error = ("токен не задан или не читается ключом secrets/franchise_key - "
                 "введите его в карточке заново")
    else:
        try:
            body = await fetch(logic.metrics_url(row["base_url"]), token)
        except franchise_http.MetricsError as exc:
            error = str(exc)
        else:
            try:
                parsed = logic.parse_metrics_bytes(body, now=now)
            except Exception as exc:                    # noqa: BLE001
                # Порча, которую проверка не предусмотрела, - та же причина
                # в карточке, а не 500 на кнопке «Обновить сейчас».
                log.exception("ответ франчайзи %s не разобран", row.get("id"))
                parsed = logic.Check(False, error=f"сбой разбора ({type(exc).__name__})")
            if parsed.ok:
                await service.franchise_store(crm, int(row["id"]), parsed.value,
                                              today=now.date())
                return None
            error = f"ответ не прошёл проверку: {parsed.error}"
    await crm.franchise_failed(int(row["id"]), error[:300])
    return error


async def poll_once(crm: Any, vault: Any, *, fetch: Any = None,
                    now: datetime | None = None) -> dict[str, int]:
    """Круг опроса: всех, кому пора, по одному. Итог - сколько принято и
    сколько нет."""
    now = now or datetime.now().astimezone()
    done = failed = 0
    for row in await crm.franchisees():
        if not logic.franchise_due(row, now):
            continue
        try:
            error = await refresh_one(crm, vault, row, fetch=fetch, now=now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                        # noqa: BLE001
            log.exception("опрос франчайзи %s не удался", row.get("id"))
            error = f"сбой опроса: {type(exc).__name__}"
            try:
                await crm.franchise_failed(int(row["id"]), error)
            except Exception:                           # noqa: BLE001
                log.exception("итог опроса франчайзи %s не записан", row.get("id"))
        if error:
            failed += 1
            log.warning("франчайзи %s: %s", row.get("id"), error)
        else:
            done += 1
    try:
        await crm.purge_franchise_snapshots(SNAPSHOT_DAYS)
    except Exception:                                   # noqa: BLE001
        log.exception("старые снимки франчайзи не удалены")
    return {"done": done, "failed": failed}


async def franchise_loop(crm: Any, cfg: Any, *, interval: int = POLL_SECONDS,
                         fetch: Any = None) -> None:
    """Фоновый опрос франчайзи. Без ключа токенов читать нечем - выходим."""
    vault = service.franchise_vault(getattr(cfg, "franchise_key", ""))
    if vault is None:
        log.info("ключ secrets/franchise_key не задан - франчайзи не опрашиваются")
        return
    while True:
        try:
            result = await poll_once(crm, vault, fetch=fetch)
            if result["done"] or result["failed"]:
                log.info("франчайзи: принято %s, не ответили %s",
                         result["done"], result["failed"])
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("опрос франчайзи не удался, повтор через %s с", interval)
        await asyncio.sleep(interval)


async def report_stale(bot: Any, crm: Any, cfg: Any, *, now: datetime) -> int:
    """Сигнал «франчайзи без свежих данных» в служебный чат. Молчит, когда
    франчайзи нет или все отвечают. Возвращает, скольких нет.
    Дневной проход живёт в местном времени без пояса - здесь он нужен."""
    now = now.astimezone() if now.tzinfo is None else now
    rows = [r for r in await crm.franchisees() if r.get("active")]
    stale = sum(1 for r in rows if logic.franchise_stale(r, now))
    if stale:
        await notices.send_team(crm, bot, "franchise_stale",
                                logic.franchise_stale_text(stale, len(rows)),
                                getattr(cfg, "contract_chat_id", None))
    return stale
