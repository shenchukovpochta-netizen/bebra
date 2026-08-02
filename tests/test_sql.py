"""Проверка SQL, который собирается в коде.

Живого Postgres в тестах нет, поэтому проверяется то, что от него не зависит:
нумерация плейсхолдеров, соответствие числа аргументов, наличие guard-условий
и белый список колонок. Именно здесь легко ошибиться на единицу и обнаружить
это только в проде.
"""

from __future__ import annotations

import asyncio
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from app.db import PATCHABLE, Database
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
        return {"tg_id": 1}

    async def execute(self, query, *args):
        self.query, self.args = query, args
        return "UPDATE 1"


def run(coro):
    return asyncio.run(coro)


def placeholders(query: str) -> list[int]:
    return sorted({int(n) for n in re.findall(r"\$(\d+)", query)})


@unittest.skipUnless(HAVE_ASYNCPG, "asyncpg не установлен")
class TestPatchSql(unittest.TestCase):
    def setUp(self):
        self.pool = RecordingPool()
        self.db = Database(self.pool)

    def test_placeholders_are_sequential_and_match_args(self):
        run(self.db.patch(1, full_name="Иванов", phone="7999", state="wait_doc"))
        nums = placeholders(self.pool.query)
        self.assertEqual(nums, list(range(1, len(nums) + 1)), self.pool.query)
        self.assertEqual(len(self.pool.args), max(nums))

    def test_state_guard_numbering(self):
        run(self.db.patch(1, expected_state="wait_fio", full_name="И", state="wait_oferta"))
        nums = placeholders(self.pool.query)
        self.assertEqual(nums, list(range(1, len(nums) + 1)), self.pool.query)
        self.assertEqual(len(self.pool.args), max(nums))
        self.assertIn("and state = $", self.pool.query)
        self.assertEqual(self.pool.args[-1], "wait_fio")

    def test_both_guards_numbering(self):
        run(self.db.patch(1, expected_state="confirm", expected_status="pending",
                          status="approved", state="approved"))
        nums = placeholders(self.pool.query)
        self.assertEqual(nums, list(range(1, len(nums) + 1)), self.pool.query)
        self.assertEqual(len(self.pool.args), max(nums))
        self.assertIn("and state = $", self.pool.query)
        self.assertIn("and status = $", self.pool.query)
        self.assertEqual(self.pool.args[-2:], ("confirm", "pending"))

    def test_guard_only_without_fields_is_valid_sql(self):
        # раньше здесь собиралось "set , updated_at = now()"
        run(self.db.patch(1, expected_status="pending"))
        self.assertNotIn("set ,", self.pool.query)
        self.assertIn("updated_at = now()", self.pool.query)
        nums = placeholders(self.pool.query)
        self.assertEqual(nums, list(range(1, len(nums) + 1)), self.pool.query)

    def test_no_guards_no_fields_is_noop(self):
        self.assertTrue(run(self.db.patch(1)))
        self.assertEqual(self.pool.query, "")

    def test_unknown_column_rejected(self):
        with self.assertRaises(ValueError):
            run(self.db.patch(1, evil="x"))

    def test_column_names_never_come_from_arguments(self):
        # имена колонок подставляются в текст запроса, поэтому их источник -
        # только белый список; иначе это прямая инъекция
        run(self.db.patch(1, full_name="'; drop table bot.users; --"))
        self.assertNotIn("drop table", self.pool.query)
        self.assertIn("'; drop table bot.users; --", self.pool.args)

    def test_patchable_matches_schema(self):
        schema = (Path(__file__).resolve().parent.parent / "schema.sql").read_text("utf-8")
        # каждая патчируемая колонка должна существовать в bot.users
        for col in PATCHABLE:
            self.assertRegex(schema, rf"\b{col}\b", f"колонки {col} нет в schema.sql")

    def test_null_is_written_not_skipped(self):
        run(self.db.patch(1, doc_file_id=None))
        self.assertIn(None, self.pool.args)


if __name__ == "__main__":
    unittest.main()
