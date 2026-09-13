"""Слой CRM на настоящем Postgres: схема, ограничения, транзакции.

Нужен пакет pgserver (pip install pgserver) - он поднимает встроенный
Postgres во временном каталоге. Без него набор пропускается: остальные
тесты CRM работают на заглушке в памяти и на парсере SQL, а этот
проверяет то, что они проверить не могут, - уникальные индексы,
транзакции close_rental/charge_period и реальные типы данных.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import asyncpg
    import pgserver

    from app.crm import logic, service
    from app.crm.db import CrmDB
    from app.db import Database
    HAVE_PG = True
except ImportError:                                    # pragma: no cover
    HAVE_PG = False

D = Decimal
SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"


@unittest.skipUnless(HAVE_PG, "pgserver или asyncpg не установлены")
class TestCrmOnPostgres(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(self.pg.get_uri(), min_size=1, max_size=3)
        # Чистая схема на каждый тест: идемпотентный скрипт поверх drop.
        await self.pool.execute("drop schema if exists crm cascade; "
                                "drop schema if exists bot cascade")
        db = Database(self.pool)
        await db.apply_schema(SCHEMA)
        await db.apply_schema(SCHEMA)
        self.crm = CrmDB(self.pool)

    async def asyncTearDown(self):
        await self.pool.close()

    async def seed(self):
        self.tariff_id = await self.crm.create_tariff("Неделя", 7, D("3000"), None)
        self.bike_id = await self.crm.create_bike(code="B-1", model="Kugoo V3",
                                                  frame_no="FR1")
        self.client_id = await self.crm.create_client(
            full_name="Иванов Иван", phone="+79990000000", tg_id=5001, source="bot")

    async def test_rental_charges_and_balance(self):
        await self.seed()
        rid = await service.open_rental(
            self.crm, client=await self.crm.client(self.client_id),
            bike=await self.crm.bike(self.bike_id), tariff=await self.crm.tariff(self.tariff_id),
            started_on=date.today() - timedelta(days=8), contract_no="АВ-1", by="test")
        rental = await self.crm.rental(rid)
        self.assertEqual(rental["billed_until"], date.today() - timedelta(days=8)
                         + timedelta(days=14))
        self.assertEqual(rental["balance"], D("-6000.00"))
        self.assertEqual(rental["bike_code"], "B-1")
        self.assertEqual((await self.crm.bike(self.bike_id))["status"], "rented")
        # повтор периода не проходит уникальный индекс
        self.assertFalse(await self.crm.charge_period(
            rid, self.client_id, period_from=rental["started_on"],
            period_to=rental["started_on"] + timedelta(days=7), amount=D("-3000"),
            note="dup"))
        self.assertEqual(await self.crm.client_balance(self.client_id), D("-6000.00"))

    async def test_one_active_rental_per_client_and_bike(self):
        await self.seed()
        await self.crm.create_rental(client_id=self.client_id, bike_id=self.bike_id,
                                     tariff_id=None, tariff_name="t", period_days=7,
                                     price=D(1), billing="auto", started_on=date.today(),
                                     contract_no=None, created_by="t")
        other = await self.crm.create_client(full_name="Второй", phone="+79995555555")
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.crm.create_rental(client_id=self.client_id, bike_id=None,
                                         tariff_id=None, tariff_name="t", period_days=7,
                                         price=D(1), billing="auto",
                                         started_on=date.today(), contract_no=None,
                                         created_by="t")
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.crm.create_rental(client_id=other, bike_id=self.bike_id,
                                         tariff_id=None, tariff_name="t", period_days=7,
                                         price=D(1), billing="auto",
                                         started_on=date.today(), contract_no=None,
                                         created_by="t")
        # service переводит ошибку в понятное сообщение
        with self.assertRaises(service.ServiceError):
            await service.open_rental(
                self.crm, client=await self.crm.client(other),
                bike=await self.crm.bike(self.bike_id),
                tariff=await self.crm.tariff(self.tariff_id), started_on=date.today(),
                contract_no=None, by="t")

    async def test_claim_is_credited_once(self):
        await self.seed()
        pid = await self.crm.create_claim(self.client_id, D("3000"))
        await self.crm.set_claim_card(pid, -100, 42)
        self.assertEqual((await self.crm.claim_by_card(-100, 42))["id"], pid)
        claim = await self.crm.claim(pid)
        self.assertIsNotNone(await service.credit_claim(self.crm, claim, D("2500"), by="a"))
        self.assertIsNone(await service.credit_claim(self.crm, claim, D("2500"), by="b"))
        self.assertEqual(await self.crm.client_balance(self.client_id), D("2500.00"))
        self.assertEqual((await self.crm.claim(pid))["status"], "confirmed")
        self.assertEqual(await self.crm.pending_claims(), [])

    async def test_close_rental_frees_bike_once(self):
        await self.seed()
        rid = await self.crm.create_rental(client_id=self.client_id, bike_id=self.bike_id,
                                           tariff_id=None, tariff_name="t", period_days=7,
                                           price=D(1), billing="manual",
                                           started_on=date.today(), contract_no=None,
                                           created_by="t")
        self.assertTrue(await self.crm.close_rental(rid, closed_on=date.today(), note="ok",
                                                    bike_status="repair"))
        self.assertEqual((await self.crm.bike(self.bike_id))["status"], "repair")
        self.assertFalse(await self.crm.close_rental(rid, closed_on=date.today(), note="x"))
        self.assertIsNone(await self.crm.active_rental_of(self.client_id))

    async def test_lists_reports_and_links(self):
        await self.seed()
        await self.crm.add_ledger(client_id=self.client_id, kind="charge", amount=D("-500"))
        await self.crm.add_bike_log(self.bike_id, "repair", "камера", D("800"), "t")
        clients = await self.crm.clients(q="иван")
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0]["balance"], D("-500.00"))
        self.assertEqual([d["id"] for d in await self.crm.debtors()], [self.client_id])
        months = await self.crm.revenue_by_month(1)
        self.assertEqual(months[0]["charged"], D("500.00"))
        self.assertEqual(months[0]["repairs"], D("800.00"))
        self.assertEqual(await self.crm.counts(),
                         {"clients": 1, "rentals": 0, "claims": 0, "bikes": 1})
        totals = await self.crm.ledger_totals(since=date.today(), until=date.today())
        self.assertEqual(totals, {"charge": D("-500.00")})
        # телефон уникален, tg_id уникален
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.crm.create_client(full_name="Дубль", phone="+79990000000")
        other = await self.crm.create_client(full_name="Второй", phone="+79995555555")
        self.assertFalse(await self.crm.link_client_tg(other, 5001, "x"))
        self.assertTrue(await self.crm.link_client_tg(other, 5002, "y"))
        # пароль сотрудника хранится и проверяется
        sid = await self.crm.create_staff("admin", logic.hash_password("password-1"),
                                          "A", "admin")
        staff = await self.crm.staff_by_id(sid)
        self.assertTrue(logic.verify_password("password-1", staff["password_hash"]))


if __name__ == "__main__":
    unittest.main()
