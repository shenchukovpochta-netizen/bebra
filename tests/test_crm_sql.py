"""SQL слоя CRM без живого Postgres: каждый метод CrmDB прогоняется через
записывающий пул, и у пойманного запроса проверяются нумерация
плейсхолдеров, число аргументов и (если установлен pglast) синтаксис.
Именно здесь легко ошибиться на единицу и обнаружить это только в проде.
"""

from __future__ import annotations

import asyncio
import re
import sys
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from app.crm.db import CrmDB
    HAVE_ASYNCPG = True
except ImportError:                                    # pragma: no cover
    HAVE_ASYNCPG = False

try:
    import pglast
    HAVE_PGLAST = True
except ImportError:                                    # pragma: no cover
    HAVE_PGLAST = False


class Row(dict):
    """Строка ответа: недостающая колонка читается единицей. Методы,
    которые берут из строки своё (created у обращения, thread_id и
    body_enc у повторяемого ответа), не падают на KeyError, а те, что
    перебирают строку целиком (counts), видят только id, bike_id и n."""

    def __missing__(self, key):
        return 1


class RecordingConn:
    def __init__(self, sink: list) -> None:
        self.sink = sink

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchrow(self, query, *args):
        self.sink.append((query, args))
        return Row(id=1, bike_id=1, n=1)

    async def fetchval(self, query, *args):
        self.sink.append((query, args))
        return 1

    async def fetch(self, query, *args):
        self.sink.append((query, args))
        return []

    async def execute(self, query, *args):
        self.sink.append((query, args))
        return "UPDATE 1"


class RecordingPool(RecordingConn):
    def acquire(self):
        return self


def run(coro):
    return asyncio.run(coro)


def placeholders(query: str) -> list[int]:
    return sorted({int(n) for n in re.findall(r"\$(\d+)", query)})


@unittest.skipUnless(HAVE_ASYNCPG, "asyncpg не установлен")
class TestCrmSql(unittest.TestCase):
    def setUp(self):
        self.sink: list = []
        self.db = CrmDB(RecordingPool(self.sink))

    def calls(self):
        """Все запросы, которые выполняет каждый публичный метод."""
        d, today = Decimal("10"), date(2026, 9, 13)
        return [
            self.db.staff_count(), self.db.staff_by_login("a"), self.db.staff_by_id(1),
            self.db.staff_all(), self.db.create_staff("a", "h", "n", "admin"),
            self.db.set_staff_password(1, "h"), self.db.set_staff_active(1, False),
            self.db.tariffs(), self.db.tariffs(active_only=True), self.db.tariff(1),
            self.db.create_tariff("n", 7, d, None), self.db.update_tariff(1, name="x", price=d),
            self.db.bikes(), self.db.bikes(status="available", q="k"), self.db.bike(1),
            self.db.bike_by_frame("f"), self.db.bike_by_code("c"),
            self.db.create_bike(code="c", model="m", frame_no=None),
            self.db.update_bike(1, status="repair", note="x"), self.db.bike_counts(),
            self.db.bike_log(1), self.db.add_bike_log(1, "note", "x", d, "me"),
            self.db.bike_rentals(1),
            self.db.clients(), self.db.clients(q="и", status="active"), self.db.client(1),
            self.db.client_by_tg(1), self.db.client_by_phone("+7"),
            self.db.create_client(full_name="a", phone="+7"),
            self.db.update_client(1, note="x", status="blocked"),
            self.db.link_client_tg(1, 2, "u"), self.db.client_balance(1),
            self.db.client_rentals(1),
            self.db.rentals(), self.db.rentals(status="active"), self.db.rental(1),
            self.db.active_rental_of(1), self.db.active_rentals(),
            self.db.create_rental(client_id=1, bike_id=1, tariff_id=None, tariff_name="t",
                                  period_days=7, price=d, billing="auto", started_on=today,
                                  contract_no=None, created_by="me"),
            self.db.update_rental(1, billed_until=today, notified_on=today, notified_kind="k"),
            self.db.close_rental(1, closed_on=today, note=None),
            self.db.charge_period(1, 1, period_from=today, period_to=today, amount=-d,
                                  note="x"),
            self.db.mark_notified(1, today, "soon"),
            self.db.add_ledger(client_id=1, kind="payment", amount=d),
            self.db.ledger_of(1), self.db.ledger(), self.db.ledger(since=today, until=today,
                                                                     kind="payment"),
            self.db.ledger_totals(), self.db.ledger_totals(since=today, until=today),
            self.db.revenue_by_month(), self.db.debtors(), self.db.counts(),
            self.db.create_claim(1, d), self.db.claim(1), self.db.pending_claims(),
            self.db.pending_claim_of(1), self.db.claim_by_card(1, 2),
            self.db.set_claim_card(1, 2, 3), self.db.set_claim_receipt(1, "f", True),
            self.db.resolve_claim(1, status="confirmed", resolved_by="me", ledger_id=1),
            # входящие обращения
            self.db.client_by_max(1),
            self.db.inbox_record(channel="tg", origin="bot", ext_id="7", direction="in",
                                 msg_id="m1", body_enc="x", name="n", username="u",
                                 phone="+7", subject="s", client_id=1),
            self.db.inbox_record(channel="avito", origin="avito_api", ext_id="c1",
                                 direction="out", author="me", announce=False),
            self.db.inbox_record(channel="tg", origin="bot", ext_id="7", direction="event",
                                 kind="other", at=datetime(2026, 9, 13, tzinfo=UTC)),
            self.db.inbox_threads(), self.db.inbox_threads(statuses=("new", "work")),
            self.db.inbox_threads(channel="avito"),
            self.db.inbox_threads(statuses=["done"], channel="tg", limit=5),
            self.db.inbox_thread(1), self.db.inbox_messages(1), self.db.inbox_open_count(),
            self.db.update_inbox_thread(1, status="done", handled_by="me",
                                        handled_at=None, note="x"),
            self.db.update_inbox_thread(1, client_id=None),
            self.db.queue_inbox_reply(1, body_enc="x", author="me"),
            self.db.claim_inbox_out(),
            self.db.finish_inbox_out(1, ok=True, ext_id="e1"),
            self.db.finish_inbox_out(1, ok=False, error="boom"),
            self.db.fail_stuck_inbox_out(), self.db.inbox_retry(1, author="me"),
            self.db.inbox_to_announce(), self.db.purge_inbox(30),
        ]

    def test_placeholders_match_arguments(self):
        for coro in self.calls():
            self.sink.clear()
            run(coro)
            self.assertTrue(self.sink, "метод не выполнил ни одного запроса")
            for query, args in self.sink:
                nums = placeholders(query)
                self.assertEqual(nums, list(range(1, len(nums) + 1)), query)
                self.assertEqual(len(args), max(nums, default=0), query)

    @unittest.skipUnless(HAVE_PGLAST, "pglast не установлен")
    def test_queries_parse_as_postgres(self):
        for coro in self.calls():
            self.sink.clear()
            run(coro)
            for query, _ in self.sink:
                try:
                    pglast.parse_sql(query)
                except Exception as exc:                # noqa: BLE001
                    self.fail(f"{exc}\n{query}")

    def test_unknown_columns_rejected(self):
        for coro in (self.db.update_bike(1, evil="x"), self.db.update_client(1, evil="x"),
                     self.db.update_tariff(1, evil="x"), self.db.update_rental(1, evil="x"),
                     self.db.create_bike(evil="x"),
                     self.db.update_inbox_thread(1, evil="x")):
            with self.assertRaises(ValueError):
                run(coro)

    def test_inbox_thread_keeps_its_identity_and_text(self):
        """Правкой обращения не подменить собеседника, канал и переписку:
        адрес ответа и текст пишет только приём сообщения."""
        for col in ("channel", "origin", "ext_id", "body_enc", "phone", "id"):
            self.sink.clear()
            with self.assertRaises(ValueError, msg=col):
                run(self.db.update_inbox_thread(1, **{col: "x"}))
            self.assertEqual(self.sink, [], f"{col}: запрос ушёл в базу")

    def test_columns_exist_in_schema(self):
        schema = (Path(__file__).resolve().parent.parent / "schema.sql").read_text("utf-8")
        for coro in self.calls():
            self.sink.clear()
            run(coro)
            for query, _ in self.sink:
                for col in re.findall(r"(\w+)\s*=\s*\$\d+", query):
                    self.assertRegex(schema, rf"\b{col}\b", f"колонки {col} нет в schema.sql")


if __name__ == "__main__":
    unittest.main()
