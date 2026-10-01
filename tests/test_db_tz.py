"""Часовой пояс сессий Postgres переживает возврат соединения в пул.

Пул asyncpg при возврате соединения делает RESET ALL: пояс, выставленный
командой SET в init, жил до первого запроса, а дальше сутки и месяцы в
SQL (отчёты, «за сегодня», чей это платёж) считались в поясе сервера -
в контейнере Postgres это UTC. Пояс уходит параметром подключения."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import asyncpg  # noqa: F401
    import pgserver

    from app.db import Database, session_timezone
    HAVE_PG = True
except ImportError:                                    # pragma: no cover
    HAVE_PG = False


class TestSessionTimezone(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.get("TZ")

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.saved

    @unittest.skipUnless(HAVE_PG, "asyncpg не установлен")
    def test_value_from_env(self):
        os.environ["TZ"] = "Europe/Moscow"
        self.assertEqual(session_timezone(), "Europe/Moscow")
        os.environ["TZ"] = "Нет/Такого"
        self.assertIsNone(session_timezone(), "негодный пояс не ломает подключение")
        os.environ.pop("TZ")
        self.assertIsNone(session_timezone())


@unittest.skipUnless(HAVE_PG, "pgserver или asyncpg не установлены")
class TestPoolKeepsTimezone(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()
        cls.tmp.cleanup()

    def test_every_query_runs_in_moscow(self):
        saved = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Moscow"

        async def probe():
            admin = await asyncpg.connect(self.pg.get_uri())
            try:
                # Сервер в UTC, как контейнер postgres:16-alpine без TZ.
                await admin.execute("alter database postgres set timezone = 'UTC'")
            finally:
                await admin.close()
            db = await Database.connect({"dsn": self.pg.get_uri()})
            try:
                return [await db.pool.fetchval("show timezone") for _ in range(4)]
            finally:
                await db.close()

        try:
            zones = asyncio.run(probe())
        finally:
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
        self.assertEqual(zones, ["Europe/Moscow"] * 4)


if __name__ == "__main__":
    unittest.main()
