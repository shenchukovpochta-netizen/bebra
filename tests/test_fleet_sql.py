"""SQL слоя парка: нумерация плейсхолдеров, белый список, схема.

Как test_sql.py: живого Postgres нет, проверяется то, что от него
не зависит, - именно здесь ошибаются на единицу и узнают об этом в проде.
"""

from __future__ import annotations

import asyncio
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from app.fleet.db import BIKE_PATCHABLE, FleetDB
    HAVE_ASYNCPG = True
except ImportError:                                    # pragma: no cover
    HAVE_ASYNCPG = False


class RecordingPool:
    """Ловит запрос и аргументы вместо обращения к базе."""

    def __init__(self) -> None:
        self.query: str = ""
        self.args: tuple = ()

    async def fetchrow(self, query, *args):
        self.query, self.args = query, args
        return {"id": 1}

    async def fetchval(self, query, *args):
        self.query, self.args = query, args
        return 1

    async def fetch(self, query, *args):
        self.query, self.args = query, args
        return []

    async def execute(self, query, *args):
        self.query, self.args = query, args
        return "UPDATE 1"


def run(coro):
    return asyncio.run(coro)


def placeholders(query: str) -> list[int]:
    return sorted({int(n) for n in re.findall(r"\$(\d+)", query)})


@unittest.skipUnless(HAVE_ASYNCPG, "asyncpg не установлен")
class TestFleetSql(unittest.TestCase):
    def setUp(self):
        self.pool = RecordingPool()
        self.fleet = FleetDB(self.pool)

    def check_numbering(self):
        nums = placeholders(self.pool.query)
        self.assertEqual(nums, list(range(1, len(nums) + 1)), self.pool.query)
        self.assertEqual(len(self.pool.args), max(nums), self.pool.query)

    def test_upsert_bike_numbering(self):
        run(self.fleet.upsert_bike("AB123", vin_motor="M9", model_id=2,
                                   point_id=1, battery_count=2,
                                   status="free", notes="x"))
        self.check_numbering()

    def test_upsert_bike_normalizes_vin(self):
        run(self.fleet.upsert_bike(" ab 12 3 "))
        self.assertIn("AB123", self.pool.args)

    def test_patch_bike_numbering(self):
        run(self.fleet.patch_bike(5, status="service", notes="скрипит"))
        self.check_numbering()
        self.assertIn("updated_at = now()", self.pool.query)

    def test_patch_bike_unknown_column_rejected(self):
        with self.assertRaises(ValueError):
            run(self.fleet.patch_bike(5, evil="x"))

    def test_patch_bike_names_never_come_from_arguments(self):
        run(self.fleet.patch_bike(5, notes="'; drop table fleet.bikes; --"))
        self.assertNotIn("drop table", self.pool.query)
        self.assertIn("'; drop table fleet.bikes; --", self.pool.args)

    def test_get_bike_digits_go_by_id_first(self):
        run(self.fleet.get_bike("42"))
        self.assertIn("id = $1", self.pool.query)

    def test_get_bike_vin_is_normalized(self):
        # RecordingPool возвращает строку и на поиск по id, поэтому ветку
        # вина проверяем нецифровой ссылкой.
        run(self.fleet.get_bike("ab 12x"))
        self.assertIn("vin_frame = $1", self.pool.query)
        self.assertEqual(self.pool.args, ("AB12X",))

    def test_patchable_matches_fleet_schema(self):
        schema = (Path(__file__).resolve().parent.parent
                  / "fleet_schema.sql").read_text("utf-8")
        for col in BIKE_PATCHABLE:
            self.assertRegex(schema, rf"\b{col}\b",
                             f"колонки {col} нет в fleet_schema.sql")

    def test_hold_interval_is_parameterized(self):
        # Минуты уезжают параметром, а не f-строкой в SQL: это ввод оператора.
        pool = self.pool

        class Conn(RecordingPool):
            def transaction(self):
                class T:
                    async def __aenter__(self):
                        return None

                    async def __aexit__(self, *exc):
                        return False
                return T()

        conn = Conn()

        class AcquireCtx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        pool.acquire = lambda: AcquireCtx()
        run(self.fleet.hold(3, 120, "Иван", 77))
        self.assertIn("::interval", conn.query)
        self.assertIn("120", conn.args)


if __name__ == "__main__":
    unittest.main()
