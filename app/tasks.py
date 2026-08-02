"""Ретеншен: удаление сканов и договоров по сроку, чистка журнала апдейтов."""

from __future__ import annotations

import asyncio
import logging

from . import logic
from .config import Config
from .db import Database
from .services import files

log = logging.getLogger(__name__)

INTERVAL_SECONDS = 6 * 3600

# Ссылки на живые фоновые задачи. Без них сборщик мусора вправе уничтожить
# задачу на середине: событийный цикл держит только слабую ссылку. Симптом -
# скан не сохранился, договор не собрался, и ни строчки в логах.
_background: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def drain(timeout: float = 10.0) -> None:
    """Дать фоновым задачам доработать при остановке."""
    if not _background:
        return
    log.info("жду завершения фоновых задач: %s", len(_background))
    await asyncio.wait(set(_background), timeout=timeout)


async def purge_once(db: Database, cfg: Config) -> tuple[int, int]:
    """Возвращает (удалено файлов, удалено записей журнала)."""
    removed = 0
    for row in await db.rows_to_purge():
        paths = [p for p in (row["doc_path"], row["contract_path"]) if p]
        # Путь пришёл из своей же базы, но перед удалением всё равно сверяется
        # с шаблоном: одна опечатка в запросе - и rm уедет не туда.
        unsafe = [p for p in paths if not logic.is_safe_store_path(p, cfg.storage_dir)]
        if unsafe:
            log.error("подозрительный путь у %s: %s - пропускаю", row["tg_id"], unsafe)
            continue
        if not all(files.remove(p) for p in paths):
            # Не удалилось - ссылку в базе не трогаем, иначе файл останется
            # на диске навсегда и без следа. Строка уедет на следующий прогон.
            continue
        await db.clear_files(row["tg_id"])
        await db.log_event(row["tg_id"], "files_purged", {"count": len(paths)})
        removed += len(paths)

    pruned = await db.prune_updates_log(cfg.updates_log_days)
    return removed, pruned


async def retention_loop(db: Database, cfg: Config) -> None:
    while True:
        try:
            removed, pruned = await purge_once(db, cfg)
            if removed or pruned:
                log.info("ретеншен: удалено файлов %s, записей журнала %s", removed, pruned)
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("прогон ретеншена не удался")
        await asyncio.sleep(INTERVAL_SECONDS)
