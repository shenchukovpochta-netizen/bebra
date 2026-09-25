"""Точка входа демо-стенда: python -m app.demo (сервис crm-demo).

Порядок: конфиг демо, проверка, что база своя, сброс и сид, панель без
бота, ночной сброс фоновой задачей того же процесса. ensure_admin не
зовётся: сотрудников демо заводит сид.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ..crm.db import CrmDB
from ..db import Database
from ..web.__main__ import make_server, setup_logging
from ..web.app import create_app
from . import runtime

log = logging.getLogger("crm.demo")


async def run() -> None:
    setup_logging()
    cfg = runtime.load_config()
    db = await Database.connect(cfg.pg)
    task = None
    crm = CrmDB(db.pool)
    day = runtime.DemoDay(crm)
    try:
        await runtime.check_database(db.pool, str(cfg.pg.get("database") or ""))
        try:
            day.start(await runtime.reset(db.pool, cfg))
        except Exception:
            # Вчерашнее демо лучше пустой панели; а без него показывать
            # нечего - пусть compose перезапустит процесс.
            if not await runtime.has_demo(db.pool):
                raise
            log.exception("сброс на старте не удался - показываю прежние данные")
        # bot=None всегда: демо не пишет в Telegram ни при каких настройках.
        app = create_app(crm=crm, db=db, cfg=cfg, bot=None)
        task = asyncio.create_task(runtime.nightly(app, db.pool, cfg, day=day))
        log.info("демо-стенд слушает порт %s, сброс каждый день в %s по Москве",
                 cfg.port, runtime.RESET_AT.strftime("%H:%M"))
        await make_server(app, cfg).serve()
    finally:
        day.stop()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await db.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
