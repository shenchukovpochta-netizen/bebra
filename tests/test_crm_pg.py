"""Слой CRM на настоящем Postgres: схема, ограничения, транзакции.

Нужен пакет pgserver (pip install pgserver) - он поднимает встроенный
Postgres во временном каталоге. Без него набор пропускается: остальные
тесты CRM работают на заглушке в памяти и на парсере SQL, а этот
проверяет то, что они проверить не могут, - уникальные индексы,
транзакции close_rental/charge_period и реальные типы данных.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import asyncpg
    import pgserver

    from app.crm import logic, service
    from app.crm.db import CrmDB
    from app.db import Database, _init_connection
    from app.services.crypto import generate_key
    from tests.fake_crm import FakeCrm
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
        self.pool = await asyncpg.create_pool(self.pg.get_uri(), min_size=1, max_size=3,
                                              init=_init_connection)
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
        # второй оператор не оставил в журнале ничего: ни платежа, ни отмены
        entries = await self.crm.ledger_of(self.client_id)
        self.assertEqual([x["kind"] for x in entries], ["payment"])
        self.assertEqual((await self.crm.claim(pid))["ledger_id"], entries[0]["id"])
        self.assertEqual((await self.crm.claim(pid))["status"], "confirmed")
        self.assertEqual(await self.crm.pending_claims(), [])

    async def test_status_log_is_written_by_trigger(self):
        await self.seed()
        log = await self.crm.bike_status_log(self.bike_id)
        self.assertEqual([(x["from_status"], x["to_status"]) for x in log],
                         [(None, "available")])
        await self.crm.update_bike(self.bike_id, by="staff:admin", status="repair")
        await self.crm.update_bike(self.bike_id, note="без смены статуса")
        rid = await self.crm.create_rental(client_id=self.client_id, bike_id=self.bike_id,
                                           tariff_id=None, tariff_name="t", period_days=7,
                                           price=D(1000), billing="manual",
                                           started_on=date.today(), contract_no=None,
                                           created_by="bot")
        await self.crm.close_rental(rid, closed_on=date.today(), note=None,
                                    bike_status="maintenance", closed_by="staff:irik")
        log = await self.crm.bike_status_log(self.bike_id)
        created = await self.crm.create_bike(by="staff:admin", code="B-actor", model="T")
        first = await self.crm.bike_status_log(created)
        self.assertEqual((first[0]["to_status"], first[0]["changed_by"]),
                         ("available", "staff:admin"))
        pairs = [(x["from_status"], x["to_status"], x["changed_by"]) for x in log]
        self.assertEqual(pairs[0], ("rented", "maintenance", "staff:irik"))
        self.assertEqual(pairs[-1], (None, "available", None))
        self.assertIn(("available", "repair", "staff:admin"), pairs)
        self.assertIn(("repair", "rented", "bot"), pairs)
        self.assertEqual(len(pairs), 4)
        # повторное применение схемы не дублирует бэкфилл
        await Database(self.pool).apply_schema(SCHEMA)
        self.assertEqual(len(await self.crm.bike_status_log(self.bike_id)), 4)
        # с какого момента велосипед в текущем статусе - последняя запись
        since = await self.crm.bike_status_since()
        self.assertEqual(since[self.bike_id], log[0]["changed_at"])
        self.assertEqual(since[created], first[0]["changed_at"])
        self.assertIsNotNone(since[self.bike_id].tzinfo)

    async def test_bike_days_and_revenue(self):
        await self.seed()
        from datetime import UTC, datetime, timedelta
        now = datetime.now(UTC)
        # переписать журнал руками: 10 дней назад свободен, 7 дней назад в аренде
        await self.pool.execute("delete from crm.bike_status_log where bike_id = $1",
                                self.bike_id)
        await self.pool.execute(
            "insert into crm.bike_status_log (bike_id, from_status, to_status, changed_at) "
            "values ($1, null, 'available', $2), ($1, 'available', 'rented', $3)",
            self.bike_id, now - timedelta(days=10), now - timedelta(days=7))
        await self.pool.execute("update crm.bikes set status = 'rented' where id = $1",
                                self.bike_id)
        days = await self.crm.bike_days_by_status(now - timedelta(days=10), now)
        self.assertAlmostEqual(float(days["available"]), 3.0, places=2)
        self.assertAlmostEqual(float(days["rented"]), 7.0, places=2)
        await self.crm.add_ledger(client_id=self.client_id, kind="payment", amount=D("3500"))
        await self.crm.add_ledger(client_id=self.client_id, kind="charge", amount=D("-3500"))
        revenue = await self.crm.rental_revenue(now - timedelta(days=10), now + timedelta(days=1))
        self.assertEqual(revenue, D("3500.00"))
        m = logic.fleet_metrics(days, revenue)
        self.assertEqual(m["idle_percent"], 30.0)
        self.assertEqual(m["avg_check"], D("500.00"))

    async def test_repair_by_node(self):
        await self.seed()
        from datetime import UTC, datetime, timedelta
        nodes = await self.crm.repair_nodes()
        self.assertEqual(len(nodes), len(logic.REPAIR_NODES))
        self.assertEqual({n["code"] for n in nodes}, set(logic.REPAIR_NODES))
        log_id = await self.crm.create_repair(
            self.bike_id, items=[{"node": "brake_pads", "parts_cost": D(400), "labor_cost": D(300)},
                                 {"node": "controller", "parts_cost": D(2500)}],
            note="тормоза и контроллер", created_by="staff:mech")
        entries = await self.crm.bike_log(self.bike_id)
        self.assertEqual((entries[0]["id"], entries[0]["kind"], entries[0]["cost"]),
                         (log_id, "repair", D("3200.00")))
        now = datetime.now(UTC)
        stats = await self.crm.repair_stats(now - timedelta(days=1), now + timedelta(days=1))
        self.assertEqual([(r["code"], r["n"], r["cost"]) for r in stats["by_node"]],
                         [("controller", 1, D("2500.00")), ("brake_pads", 1, D("700.00"))])
        self.assertEqual([(r["model"], r["bikes"], r["n"], r["cost"]) for r in stats["by_model"]],
                         [("Kugoo V3", 1, 1, D("3200.00"))])
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            await self.crm.create_repair(self.bike_id, items=[{"node": "warp_drive"}],
                                         note=None, created_by=None)

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

    async def test_rental_intent_columns(self):
        """Намерение клиента и отсрочка хранятся на аренде и читаются обратно."""
        from datetime import UTC, datetime
        await self.seed()
        rid = await self.crm.create_rental(client_id=self.client_id, bike_id=self.bike_id,
                                           tariff_id=self.tariff_id, tariff_name="Неделя",
                                           period_days=7, price=D("3000"), billing="auto",
                                           started_on=date.today(), contract_no=None,
                                           created_by="t")
        self.assertIsNone((await self.crm.rental(rid))["intent"])
        when = datetime.now(UTC)
        await self.crm.update_rental(rid, intent="return", intent_until=date.today(),
                                     intent_by="staff:admin", intent_at=when,
                                     snooze_until=date.today() + timedelta(days=1))
        fresh = await self.crm.rental(rid)
        self.assertEqual((fresh["intent"], fresh["intent_until"], fresh["intent_by"]),
                         ("return", date.today(), "staff:admin"))
        self.assertEqual(fresh["snooze_until"], date.today() + timedelta(days=1))
        self.assertIsNotNone(fresh["intent_at"].tzinfo)
        self.assertIn("intent", (await self.crm.active_rentals())[0])

    async def test_mileage_follows_rental(self):
        """Одометр: выдача поднимает пробег парка, возврат пишет накат,
        назад пробег не уезжает."""
        await self.seed()
        await self.crm.update_bike(self.bike_id, mileage_km=4266)
        rid = await self.crm.create_rental(client_id=self.client_id, bike_id=self.bike_id,
                                           tariff_id=self.tariff_id, tariff_name="Неделя",
                                           period_days=7, price=D("3000"), billing="auto",
                                           started_on=date.today(), contract_no=None,
                                           created_by="t", mileage_start=4300)
        self.assertEqual((await self.crm.bike(self.bike_id))["mileage_km"], 4300)
        self.assertEqual((await self.crm.rental(rid))["mileage_start"], 4300)
        self.assertTrue(await self.crm.close_rental(rid, closed_on=date.today(), note=None,
                                                    mileage_end=4586))
        fresh = await self.crm.rental(rid)
        self.assertEqual((fresh["mileage_start"], fresh["mileage_end"]), (4300, 4586))
        self.assertEqual(logic.ridden(fresh), 286)
        self.assertEqual((await self.crm.bike(self.bike_id))["mileage_km"], 4586)
        # закрытие без пробега ничего не обнуляет
        rid2 = await self.crm.create_rental(client_id=self.client_id, bike_id=self.bike_id,
                                            tariff_id=self.tariff_id, tariff_name="Неделя",
                                            period_days=7, price=D("3000"), billing="auto",
                                            started_on=date.today(), contract_no=None,
                                            created_by="t")
        await self.crm.close_rental(rid2, closed_on=date.today(), note=None)
        self.assertIsNone((await self.crm.rental(rid2))["mileage_end"])
        self.assertEqual((await self.crm.bike(self.bike_id))["mileage_km"], 4586)

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

    async def test_access_profiles_match_the_code(self):
        """Матрицу встроенных профилей держат в двух местах: schema.sql
        кладёт её в базу, logic.BUILT_IN_PROFILES - в заглушку для тестов.
        Разойдутся - панель будет пускать не туда, где её проверяли."""
        rows = {p["code"]: p for p in await self.crm.access_profiles()}
        self.assertEqual(list(rows), ["owner", "manager", "tech"])
        for code, name, perms, built_in in logic.BUILT_IN_PROFILES:
            row = rows[code]
            self.assertEqual(row["name"], name, code)
            self.assertEqual(row["built_in"], built_in, code)
            self.assertEqual(logic.normalize_perms(row["perms"]),
                             logic.normalize_perms(perms), code)
            self.assertEqual(row["staff_count"], 0)

    async def test_profile_crud_and_staff_backfill(self):
        owner = await self.crm.access_profile_by_code("owner")
        # сотрудник, заведённый до профилей, получает профиль повторным
        # применением схемы - оно идемпотентно и в проде выполняется при старте
        sid = await self.crm.create_staff("old", logic.hash_password("password-1"),
                                          "Старый", "admin")
        await self.pool.execute("update crm.staff set profile_id = null where id = $1", sid)
        await Database(self.pool).apply_schema(SCHEMA)
        self.assertEqual((await self.crm.staff_by_id(sid))["profile_code"], "owner")

        pid = await self.crm.create_access_profile(
            "Точка", {"sections": {"bikes": "edit"}, "actions": {}})
        got = await self.crm.access_profile(pid)
        self.assertEqual(logic.normalize_perms(got["perms"])["sections"], {"bikes": "edit"})
        self.assertFalse(got["built_in"])
        await self.crm.update_access_profile(
            pid, name="Точка Павлюхина",
            perms={"sections": {"bikes": "view"}, "actions": {"money_edit": True}})
        got = await self.crm.access_profile(pid)
        self.assertEqual(got["name"], "Точка Павлюхина")
        self.assertTrue(logic.can_act({"perms": got["perms"]}, "money_edit"))
        # встроенный не правится и не удаляется
        await self.crm.update_access_profile(owner["id"], name="Хозяин", perms={})
        self.assertEqual((await self.crm.access_profile(owner["id"]))["name"], "Владелец")
        self.assertFalse(await self.crm.delete_access_profile(owner["id"]))
        # занятый профиль держится сотрудником
        await self.crm.set_staff_profile(sid, pid)
        self.assertFalse(await self.crm.delete_access_profile(pid))
        self.assertEqual({p["code"]: p["staff_count"]
                          for p in await self.crm.access_profiles()}["owner"], 0)
        await self.crm.set_staff_profile(sid, owner["id"])
        self.assertTrue(await self.crm.delete_access_profile(pid))
        self.assertIsNone(await self.crm.access_profile(pid))

    async def test_work_order_lifecycle_on_postgres(self):
        """Наряд от открытия до закрытия на настоящей базе: номер, запрет
        второго открытого наряда и запись ремонта в журнал велосипеда."""
        await self.seed()
        bike = await self.crm.bike(self.bike_id)
        order_id = await service.open_order(
            self.crm, bike=bike, payer="own", client=None, complaint="не едет",
            object_note=None, tech_id=None, estimate=D(0), by="t")
        order = await self.crm.work_order(order_id)
        self.assertEqual(order["no"], "РЕМ-000001")
        self.assertEqual((await self.crm.bike(self.bike_id))["status"], "repair")

        # второй открытый наряд на тот же велосипед не пройдёт
        with self.assertRaises(service.ServiceError):
            await service.open_order(
                self.crm, bike=await self.crm.bike(self.bike_id), payer="own",
                client=None, complaint=None, object_note=None, tech_id=None,
                estimate=D(0), by="t")

        types = await self.crm.work_types(active_only=True)
        self.assertTrue(types, "каталог работ кладётся схемой")
        work = next(t for t in types if t["node"])
        await self.crm.add_order_item(
            order_id, title=work["title"], node=work["node"],
            work_type_id=work["id"], qty=2, price=D("200"),
            parts_cost=D("50"), labor_cost=D("30"))
        totals = await service.close_order(self.crm, order, by="t")
        self.assertEqual(totals["total"], D("400.00"))
        self.assertEqual(totals["cost"], D("160.00"))

        closed = await self.crm.work_order(order_id)
        self.assertEqual(closed["status"], "done")
        self.assertIsNotNone(closed["log_id"])
        self.assertEqual((await self.crm.bike(self.bike_id))["status"], "available")
        log = await self.crm.bike_log(self.bike_id, kind="repair")
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["cost"], D("160.00"))

        # после закрытия номер продолжает расти
        second = await service.open_order(
            self.crm, bike=await self.crm.bike(self.bike_id), payer="own",
            client=None, complaint=None, object_note=None, tech_id=None,
            estimate=D(0), by="t")
        self.assertEqual((await self.crm.work_order(second))["no"], "РЕМ-000002")

    async def test_client_repair_revenue_is_not_rental_revenue(self):
        """Красная линия CLAUDE.md: выручка чужого ремонта не в журнале."""
        await self.seed()
        order_id = await service.open_order(
            self.crm, bike=None, payer="client", client=None, complaint=None,
            object_note="Самокат Kugoo", tech_id=None, estimate=D("1500"), by="t")
        await self.crm.add_order_item(
            order_id, title="Диагностика", node=None, work_type_id=None, qty=1,
            price=D("1500"), parts_cost=D("200"), labor_cost=D("100"))
        await service.close_order(self.crm, await self.crm.work_order(order_id), by="t")
        now = datetime.now(UTC)
        stats = await self.crm.order_stats(now - timedelta(days=1), now + timedelta(days=1))
        self.assertEqual(stats["closed"], 1)
        self.assertEqual(stats["revenue"], D("1500.00"))
        self.assertEqual(await self.crm.rental_revenue(now - timedelta(days=1), now), D(0),
                         "арендная выручка чужим ремонтом не растёт")

    async def test_stock_take_on_postgres(self):
        """Пересчёт на живой базе: нумерация, одна открытая ведомость,
        недостача и возврат найденного потерянного в парк."""
        await self.seed()
        lost_id = await self.crm.create_bike(code="B-9", model="Kugoo V3",
                                             location="Павлюхина")
        await self.crm.update_bike(lost_id, status="lost", by="t")
        take_id = await service.start_stock_take(
            self.crm, scope="all", location=None, note="Плановый", by="staff:t")
        take = await self.crm.stock_take(take_id)
        self.assertEqual(take["no"], "ПРТ-000001")
        self.assertEqual(take["expected"], 1, "потерянный в ведомость не ставится")

        # вторую ведомость не открыть: держит частичный уникальный индекс
        with self.assertRaises(service.ServiceError):
            await service.start_stock_take(self.crm, scope="all", location=None,
                                           note=None, by="staff:t")

        found = await service.take_add_found(self.crm, take, "B-9")
        self.assertEqual(found["state"], "extra")
        # тот же номер второй раз: строка не задваивается и лишний
        # не превращается в «нашли» - иначе найдено было бы больше, чем ждали
        again = await service.take_add_found(self.crm, take, "B-9")
        self.assertEqual(again["state"], "extra")
        self.assertIn("уже записан лишним", again["message"])
        self.assertEqual(len(await self.crm.take_items(take_id)), 2)

        result = await service.finish_stock_take(self.crm, take, by="staff:t",
                                                 lose_missing=True, return_found=True)
        self.assertEqual((result["found"], result["missing"], result["extra"]),
                         (0, 1, 1))
        self.assertEqual(result["lost"], 1)
        self.assertEqual(result["returned"], 1)
        self.assertEqual((await self.crm.bike(self.bike_id))["status"], "lost")
        self.assertEqual((await self.crm.bike(lost_id))["status"], "available")
        closed = await self.crm.stock_take(take_id)
        self.assertEqual(closed["status"], "done")
        self.assertEqual(closed["missing"], 1)
        self.assertIsNone(await self.crm.open_stock_take())

        # после закрытия номер продолжает расти
        second = await service.start_stock_take(
            self.crm, scope="location", location="Павлюхина", note=None, by="staff:t")
        self.assertEqual((await self.crm.stock_take(second))["no"], "ПРТ-000002")

    async def test_model_money_on_postgres(self):
        """Окупаемость на живой базе: платёж без аренды всё равно находит
        свою модель, а дни аренды берутся из журнала статусов."""
        await self.seed()
        rid = await service.open_rental(
            self.crm, client=await self.crm.client(self.client_id),
            bike=await self.crm.bike(self.bike_id),
            tariff=await self.crm.tariff(self.tariff_id),
            started_on=date.today() - timedelta(days=3), contract_no="АВ-1", by="t")
        # платёж с арендой и платёж без неё - как зачисление по заявке из бота
        await self.crm.add_ledger(client_id=self.client_id, rental_id=rid,
                                  kind="payment", amount=D("2000"), method="sbp",
                                  note=None, created_by="t")
        await self.crm.add_ledger(client_id=self.client_id, rental_id=None,
                                  kind="payment", amount=D("1000"), method="sbp",
                                  note=None, created_by="t")
        await self.crm.create_repair(
            self.bike_id, items=[{"node": "brake_pads", "parts_cost": D("800"),
                                  "labor_cost": D("400"), "note": None}],
            note=None, created_by="t")
        now = datetime.now(UTC)
        money = await self.crm.model_money(now - timedelta(days=30), now + timedelta(days=1))
        cell = money["Kugoo V3"]
        self.assertEqual(cell["paid"], D("3000.00"), "платёж без аренды тоже наш")
        self.assertEqual(cell["charged"], D("3000.00"))
        self.assertEqual(cell["repair_cost"], D("1200.00"))
        self.assertGreater(cell["rented_days"], 0)

        # предоплата за день до выдачи: аренды в тот день ещё не было
        await self.crm.add_ledger(
            client_id=self.client_id, rental_id=None, kind="payment", amount=D("500"),
            method="cash", note=None, created_by="t",
            created_at=now - timedelta(days=10))
        money = await self.crm.model_money(now - timedelta(days=30), now + timedelta(days=1))
        self.assertEqual(money["Kugoo V3"]["paid"], D("3500.00"),
                         "предоплата тоже находит свою модель")

        rows = logic.payback_rows(await self.crm.bikes(limit=100), money, days=30)
        row = next(r for r in rows if r["model"] == "Kugoo V3")
        self.assertEqual(row["paid"], D("3500.00"))
        self.assertEqual(row["repair_cost"], D("1200.00"))
        self.assertEqual(logic.payback_total(rows)["paid"], D("3500.00"))

    async def test_referrals_on_postgres(self):
        """Приглашения на живой базе: один переход на человека, бонус
        один раз и записью вида adjust, а не платежом."""
        await self.seed()
        agent = await self.crm.client(self.client_id)
        self.assertTrue(await self.crm.set_ref_code(agent["id"], "AB3D9K"))
        # тот же код второму клиенту не достанется
        other_id = await self.crm.create_client(full_name="Пётр", phone="+79995554433")
        self.assertFalse(await self.crm.set_ref_code(other_id, "AB3D9K"))

        ref_id = await self.crm.add_referral(agent_id=agent["id"], tg_id=9001)
        self.assertIsNotNone(ref_id)
        self.assertIsNone(await self.crm.add_referral(agent_id=other_id, tg_id=9001),
                          "друг остаётся за первым агентом")

        friend_id = await self.crm.create_client(full_name="Друг", phone="+79993334455",
                                                 tg_id=9001)
        await self.crm.update_referral(ref_id, client_id=friend_id, status="rented")
        friend = await self.crm.client(friend_id)
        paid = await service.ref_paid(self.crm, friend, D("3000"), by="test")
        self.assertIsNotNone(paid)
        self.assertEqual(paid["bonus"], logic.REF_BONUS_DEFAULT)
        # повторный платёж бонуса не удваивает
        self.assertIsNone(await service.ref_paid(self.crm, friend, D("3000"), by="test"))

        rows = await self.crm.ledger_of(agent["id"], limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "bonus")
        now = datetime.now(UTC)
        self.assertEqual(await self.crm.rental_revenue(now - timedelta(days=1), now),
                         D(0), "бонус не арендная выручка и средний чек не поднимает")
        funnel = logic.ref_funnel(await self.crm.referrals(agent_id=agent["id"]))
        self.assertEqual((funnel["click"], funnel["paid"]), (1, 1))
        self.assertEqual(funnel["bonus"], logic.REF_BONUS_DEFAULT)

    async def test_settings_round_trip_on_postgres(self):
        await self.crm.set_setting("ref_bonus", "700", by="staff:t")
        await self.crm.set_setting("ref_bonus", "800", by="staff:t")
        self.assertEqual(logic.ref_settings(await self.crm.settings())["bonus"],
                         D("800.00"))

    async def test_client_channel_on_postgres(self):
        """Канал привлечения на живой базе: пишется, читается, считается."""
        await self.seed()
        await self.crm.update_client(self.client_id, channel="avito")
        self.assertEqual((await self.crm.client(self.client_id))["channel"], "avito")
        rows = await self.crm.clients_since(datetime.now(UTC) - timedelta(days=1))
        self.assertEqual([r["channel"] for r in rows], ["avito"])
        data = logic.channel_rows(rows, months=3)
        self.assertEqual(data["totals"]["avito"], 1)
        # приглашённому каналу «сарафан» проставляется само
        friend_id = await self.crm.create_client(full_name="Друг", phone="+79993334455",
                                                 tg_id=9002)
        await self.crm.add_referral(agent_id=self.client_id, tg_id=9002)
        await service.ref_signed(self.crm, await self.crm.client(friend_id))
        self.assertEqual((await self.crm.client(friend_id))["channel"], "referral")

    async def test_warehouse_on_postgres(self):
        """Склад на живой базе: средневзвешенная себестоимость, нумерация
        документов, расход в наряд и остаток как сумма движений."""
        await self.seed()
        part_id = await self.crm.create_part(
            title="Колодки дисковые", node="brake_pads", unit="шт", cost=D(0),
            price=D("600"), min_stock=10, model=None, note=None)
        supplier_id = await self.crm.create_supplier(name="ВелоЗапчасть", phone=None,
                                                     note=None)
        first = await service.receive_parts(
            self.crm, supplier_id=supplier_id,
            lines=[{"part_id": part_id, "qty": 4, "price": D("300")}],
            note=None, by="staff:t")
        self.assertEqual((await self.crm.part_doc(first))["no"], "ПРХ-000001")
        self.assertEqual((await self.crm.part(part_id))["cost"], D("300.00"))

        await service.receive_parts(
            self.crm, supplier_id=supplier_id,
            lines=[{"part_id": part_id, "qty": 6, "price": D("400")}],
            note=None, by="staff:t")
        part = await self.crm.part(part_id)
        self.assertEqual(part["cost"], D("360.00"), "средневзвешенная, а не последняя")
        self.assertEqual(await self.crm.part_stock(part_id), 10)

        order_id = await service.open_order(
            self.crm, bike=await self.crm.bike(self.bike_id), payer="own", client=None,
            complaint="не тормозит", object_note=None, tech_id=None, estimate=D(0),
            by="staff:t")
        order = await self.crm.work_order(order_id)
        await service.issue_part_to_order(self.crm, order, part, 2, by="staff:t")
        self.assertEqual(await self.crm.part_stock(part_id), 8)
        with self.assertRaises(service.ServiceError):
            await service.issue_part_to_order(self.crm, order, part, 99, by="staff:t")

        await service.close_order(self.crm, order, by="staff:t")
        log = await self.crm.bike_log(self.bike_id, kind="repair")
        self.assertEqual(log[0]["cost"], D("720.00"), "две колодки по 360")

        # списание и пересчёт - тоже движения
        await service.write_off_parts(
            self.crm, lines=[{"part_id": part_id, "qty": 1}], note="брак", by="staff:t")
        self.assertEqual(await self.crm.part_stock(part_id), 7)
        result = await service.count_part(self.crm, await self.crm.part(part_id), 6,
                                          by="staff:t")
        self.assertEqual((result["delta"], result["stock"]), (-1, 6))
        stocks = await self.crm.stock_map()
        self.assertEqual(stocks[part_id], 6)

    async def test_part_order_cycle_on_postgres(self):
        await self.seed()
        part_id = await self.crm.create_part(
            title="Контроллер", node="controller", unit="шт", cost=D("2500"),
            price=D("4000"), min_stock=2, model=None, note=None)
        collected = await service.collect_part_needs(self.crm, by="staff:t")
        self.assertEqual(collected["added"], 1)
        order = collected["order"]
        self.assertEqual(order["no"], "ЗАП-000001")
        # повторный сбор не задваивает строки
        again = await service.collect_part_needs(self.crm, by="staff:t")
        self.assertEqual(again["added"], 0)
        items = await self.crm.part_order_items(order["id"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["qty"], 2)

        doc_id = await service.receive_part_order(self.crm, order, by="staff:t")
        self.assertEqual(await self.crm.part_stock(part_id), 2)
        self.assertEqual((await self.crm.part_doc(doc_id))["kind"], "receipt")
        with self.assertRaises(service.ServiceError):
            await service.receive_part_order(
                self.crm, await self.crm.part_order(order["id"]), by="staff:t")

    async def test_bike_swap_on_postgres(self):
        """Замена на живой базе: одна транзакция, деньги и сроки на месте,
        журнал перемещений держит обе единицы."""
        await self.seed()
        spare_id = await self.crm.create_bike(code="B-SPARE", model="Truck+")
        await self.crm.update_bike(spare_id, spare=True, by="t")
        rental_id = await service.open_rental(
            self.crm, client=await self.crm.client(self.client_id),
            bike=await self.crm.bike(self.bike_id),
            tariff=await self.crm.tariff(self.tariff_id),
            started_on=date.today() - timedelta(days=3), contract_no="АВ-1",
            by="staff:t", mileage=1000)
        before = await self.crm.rental(rental_id)
        self.assertEqual(len(await self.crm.rental_bikes(rental_id)), 1)

        await service.swap_bike(
            self.crm, before, await self.crm.bike(spare_id), reason="repair",
            mileage_old=1200, mileage_new=300, by="staff:t")
        after = await self.crm.rental(rental_id)
        self.assertEqual(after["bike_id"], spare_id)
        self.assertEqual(after["billed_until"], before["billed_until"])
        self.assertEqual(after["balance"], before["balance"])
        self.assertEqual((await self.crm.bike(self.bike_id))["status"], "repair")
        self.assertEqual((await self.crm.bike(self.bike_id))["mileage_km"], 1200)
        self.assertEqual((await self.crm.bike(spare_id))["status"], "rented")

        rows = await self.crm.rental_bikes(rental_id)
        self.assertEqual(len(rows), 2)
        self.assertIsNotNone(rows[0]["returned_on"])
        self.assertIsNone(rows[1]["returned_on"])
        self.assertEqual(logic.rental_mileage(rows, current=350), 250)

        # открытая строка одна: частичный уникальный индекс
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.crm.add_rental_bike(rental_id, bike_id=self.bike_id,
                                           issued_on=date.today(), mileage_start=0,
                                           reason="дубль", created_by="t")

        # замена по устаревшей карточке аренды не проходит
        with self.assertRaises(service.ServiceError):
            await service.swap_bike(self.crm, before, await self.crm.bike(self.bike_id),
                                    reason="repair", by="staff:t")

    async def test_swap_log_is_backfilled_for_old_rentals(self):
        """Аренда старше журнала перемещений: строка выдачи создаётся задним
        числом, иначе первая замена потеряла бы, что было до неё."""
        await self.seed()
        rental_id = await self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D("3000"), billing="auto",
            started_on=date.today() - timedelta(days=5), contract_no=None,
            created_by="import", mileage_start=500)
        self.assertEqual(await self.crm.rental_bikes(rental_id), [])
        spare_id = await self.crm.create_bike(code="B-SPARE", model="Truck+")
        await service.swap_bike(
            self.crm, await self.crm.rental(rental_id),
            await self.crm.bike(spare_id), reason="client", by="staff:t")
        rows = await self.crm.rental_bikes(rental_id)
        self.assertEqual([r["bike_id"] for r in rows], [self.bike_id, spare_id])
        self.assertEqual(rows[0]["reason"], "Выдача")
        self.assertEqual(rows[0]["mileage_start"], 500)

    async def test_purchase_on_postgres(self):
        """Закупка на живой базе: партия целиком, нумерация, износ."""
        await self.seed()
        supplier_id = await self.crm.create_supplier(name="ВелоОпт", phone=None,
                                                     note=None)
        result = await service.buy_bikes(
            self.crm, supplier_id=supplier_id,
            purchased_on=date.today() - timedelta(days=365),
            codes=["B-101", "B-102"], model="Truck+", price=D("47000"),
            battery_count=2, service_months=24, residual=D("5000"),
            battery_price=D("9000"), battery_months=15, location="Павлюхина",
            note="Весенняя партия", by="staff:t")
        self.assertEqual(result["bikes"], 2)
        purchase = await self.crm.purchase(result["purchase_id"])
        self.assertEqual(purchase["no"], "ЗАК-000001")
        self.assertEqual(purchase["total"], D("94000.00"))
        bikes = await self.crm.purchase_bikes(purchase["id"])
        self.assertEqual([b["code"] for b in bikes], ["B-101", "B-102"])

        rows = logic.asset_rows(await self.crm.bikes(limit=100))
        got = next(r for r in rows if r["code"] == "B-101")
        self.assertEqual(got["wear"], 50.0, "год из двух лет срока")
        self.assertEqual(got["book"], D("26000.00"))
        summary = logic.asset_summary(rows)
        self.assertEqual(summary["spent"], D("94000.00"))

        # занятый номер отменяет всю партию
        with self.assertRaises(service.ServiceError):
            await service.buy_bikes(
                self.crm, supplier_id=None, purchased_on=date.today(),
                codes=["B-103", "B-101"], model="Truck+", price=D("1"),
                battery_count=2, service_months=24, residual=D(0),
                battery_price=None, battery_months=15, location=None, note=None,
                by="staff:t")
        self.assertIsNone(await self.crm.bike_by_code("B-103"))
        self.assertEqual(len(await self.crm.purchases()), 1)

    async def test_batteries_on_postgres(self):
        """Батареи на живой базе: триггер журнала, выдача и возврат, каталог."""
        await self.seed()
        seeded = await self.crm.location_names()
        self.assertEqual(seeded, ["Павлюхина", "Адоратского"],
                         "точки приезжают со схемой - парк заведён с ними")

        model_id = await self.crm.create_battery_model(
            title="48V 20Ah", brand="Sanyo", voltage=48, capacity=D("20"),
            price=D("9000"), service_months=15, note=None)
        bike_model_id = await self.crm.create_bike_model(
            title="Kugoo V3", brand=None, factory_title=None, battery_slots=2,
            note=None)
        await self.crm.set_compat(bike_model_id, model_id, fits=True, primary_fit=True)
        fit = await self.crm.compat_for_bike_model("Kugoo V3")
        self.assertEqual([m["title"] for m in fit], ["48V 20Ah"])

        first = await self.crm.create_battery(code="A-1", model_id=model_id,
                                              location="Павлюхина",
                                              purchase_price=D("9000"),
                                              by="staff:kolya")
        second = await self.crm.create_battery(code="A-2", model_id=model_id,
                                               by="staff:kolya")
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.crm.create_battery(code="A-1", by="staff:kolya")

        # Журнал пишет триггер, автор - из set_config в той же транзакции.
        log = await self.crm.battery_status_log(first)
        self.assertEqual([(x["from_status"], x["to_status"]) for x in log],
                         [(None, "available")])
        self.assertEqual(log[0]["changed_by"], "staff:kolya")

        client = await self.crm.client(self.client_id)
        bike = await self.crm.bike(self.bike_id)
        tariff = await self.crm.tariff(self.tariff_id)
        rental_id = await service.open_rental(
            self.crm, client=client, bike=bike, tariff=tariff,
            started_on=date.today(), contract_no=None, by="staff:kolya")
        await service.issue_with_batteries(self.crm, rental_id, bike=bike,
                                           battery_ids=[first], by="staff:kolya")
        row = await self.crm.battery(first)
        self.assertEqual(row["status"], "rented")
        self.assertEqual(row["bike_code"], "B-1")
        self.assertEqual(row["client_name"], "Иванов Иван")
        self.assertEqual(row["model_price"], D("9000.00"))

        rental = await self.crm.rental(rental_id)
        await service.swap_battery(self.crm, rental, row,
                                   await self.crm.battery(second), by="staff:kolya")
        old = await self.crm.battery(first)
        self.assertEqual(old["status"], "repair")
        self.assertIsNone(old["rental_id"])
        self.assertEqual(old["cycles"], 1)
        self.assertEqual((await self.crm.battery(second))["status"], "rented")

        await service.close_rental(self.crm, await self.crm.rental(rental_id),
                                   closed_on=date.today(), note=None, by="staff:kolya")
        back = await self.crm.battery(second)
        self.assertEqual(back["status"], "available")
        self.assertIsNone(back["rental_id"])
        self.assertEqual(back["cycles"], 1, "возврат - один цикл")
        self.assertEqual([x["to_status"] for x in
                          await self.crm.battery_status_log(first)],
                         ["repair", "rented", "available"])
        self.assertEqual(await self.crm.battery_counts(),
                         {"repair": 1, "available": 1})

        # Батарея заведена поштучно - у велосипеда остаётся только рама.
        await self.crm.update_bike(self.bike_id, purchase_price=D("47000"),
                                   residual_price=D("5000"), service_months=24,
                                   battery_price=D("9000"), battery_count=2,
                                   battery_service_months=15)
        await self.crm.update_battery(first, bike_id=self.bike_id, by="staff:kolya")
        bikes = await self.crm.bikes(limit=100)
        batteries = await self.crm.batteries()
        self.assertEqual(logic.amortization_total(bikes), D("2950.00"))
        self.assertEqual(logic.amortization_total(bikes, batteries), D("2950.00"),
                         "две карточки по 600 вместо счётчика на 1200")

    async def test_zero_points_from_before_are_cleaned_once(self):
        # Точки «0, 0» от опросов до исправления разбора: схема вычищает их
        # один раз и помечает это в настройках.
        tid = await self.crm.create_tracker(device_id="ZERO-1", alias="Z")
        await self.pool.execute(
            "update crm.trackers set lat = 0, lon = 0 where id = $1", tid)
        await self.pool.execute(
            "insert into crm.tracker_positions (tracker_id, lat, lon, recorded_at) "
            "values ($1, 0, 0, now()), ($1, 55.79, 49.12, now() - interval '5 minutes')",
            tid)
        await self.pool.execute(
            "delete from crm.settings where key = 'tracker_zero_fix_cleaned'")
        await Database(self.pool).apply_schema(SCHEMA)
        row = await self.pool.fetchrow("select lat, lon from crm.trackers where id = $1", tid)
        self.assertIsNone(row["lat"])
        self.assertIsNone(row["lon"])
        left = await self.pool.fetch(
            "select lat from crm.tracker_positions where tracker_id = $1", tid)
        self.assertEqual([r["lat"] for r in left], [55.79], "настоящая точка осталась")
        self.assertEqual((await self.crm.settings()).get("tracker_zero_fix_cleaned"), "1")

    async def test_trackers_on_postgres(self):
        """Трекеры на живой базе: апсерт по устройству, журнал позиций
        без дублей, одна открытая тревога на вид."""
        await self.seed()
        moment = datetime.now(UTC).replace(microsecond=0)
        device = {"device_id": "1001", "alias": "Truck+ 101", "lat": 55.7692,
                  "lon": 49.1440, "speed": 0.0, "course": 90,
                  "recorded_at": moment, "voltage": 12.6, "gsm_level": 20,
                  "alarm": False}
        saved = await self.crm.save_tracker_state(device)
        self.assertTrue(saved["created"], "неизвестное устройство заводится само")
        again = await self.crm.save_tracker_state({**device, "speed": 24.0})
        self.assertFalse(again["created"])
        self.assertEqual(again["id"], saved["id"])

        tracker = await self.crm.tracker(saved["id"])
        self.assertEqual(tracker["device_id"], "1001")
        self.assertEqual(tracker["speed"], D("24.00"))
        self.assertEqual(tracker["voltage"], D("12.60"))
        # Метка времени та же - вторая точка в журнал не легла.
        self.assertEqual(len(await self.crm.tracker_positions(saved["id"])), 1)
        await self.crm.save_tracker_state({**device,
                                           "recorded_at": moment + timedelta(minutes=5)})
        self.assertEqual(len(await self.crm.tracker_positions(saved["id"])), 2)

        await self.crm.update_tracker(saved["id"], bike_id=self.bike_id)
        self.assertEqual((await self.crm.tracker(saved["id"]))["bike_code"], "B-1")
        other = await self.crm.create_tracker(device_id="1002")
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.crm.update_tracker(other, bike_id=self.bike_id)
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.crm.create_tracker(device_id="1001")

        alert_id = await self.crm.raise_alert(
            tracker_id=saved["id"], kind="moving", note="24 км/ч",
            bike_id=self.bike_id, lat=55.7692, lon=49.1440)
        self.assertIsNotNone(alert_id)
        self.assertIsNone(await self.crm.raise_alert(
            tracker_id=saved["id"], kind="moving", note="ещё раз",
            bike_id=self.bike_id, lat=None, lon=None),
            "вторая такая же тревога не поднимается")
        rows = await self.crm.tracker_alerts()
        self.assertEqual([r["bike_code"] for r in rows], ["B-1"])
        self.assertEqual(await self.crm.close_alerts(saved["id"], ["moving"],
                                                     by="tracking"), 1)
        self.assertEqual(await self.crm.tracker_alerts(), [])
        # Закрытая не мешает поднять тревогу заново.
        self.assertIsNotNone(await self.crm.raise_alert(
            tracker_id=saved["id"], kind="moving", note="опять поехал",
            bike_id=self.bike_id, lat=None, lon=None))

        self.assertEqual(await self.crm.purge_tracker_positions(30), 0)
        await self.pool.execute(
            "update crm.tracker_positions "
            "set recorded_at = recorded_at - interval '60 days'")
        self.assertEqual(await self.crm.purge_tracker_positions(30), 2)

    async def test_cash_and_bank_on_postgres(self):
        """Касса на живой базе: одна открытая смена на точку, наличные
        подтягиваются из журнала, выписка не двоится."""
        await self.seed()
        shift_id = await service.open_cash_shift(
            self.crm, location="Павлюхина", opening=D("2000"), note=None,
            by="staff:t")
        shift = await self.crm.cash_shift(shift_id)
        self.assertEqual(shift["no"], "КСМ-000001")
        with self.assertRaises(service.ServiceError):
            await service.open_cash_shift(self.crm, location="Павлюхина",
                                          opening=D(0), note=None, by="staff:t")
        # на другой точке своя смена открывается спокойно
        other = await service.open_cash_shift(self.crm, location="Адоратского",
                                              opening=D(0), note=None, by="staff:t")
        self.assertIsNotNone(other)

        client = await self.crm.client(self.client_id)
        await service.add_entry(self.crm, client, kind="payment", amount=D("3000"),
                                method="cash", note="аренда", by="staff:t")
        await service.add_entry(self.crm, client, kind="payment", amount=D("5000"),
                                method="sbp", note="перевод", by="staff:t")
        payments = await self.crm.shift_payments(shift_id)
        self.assertEqual([p["amount"] for p in payments], [D("3000.00")],
                         "в кассу идут только наличные")

        await service.cash_move(self.crm, shift, kind="out", amount=D("1000"),
                                reason="инкассация", by="staff:t")
        state = await service.close_cash_shift(
            self.crm, shift, counted=D("3900"), note="сотни не хватает", by="staff:t")
        self.assertEqual(state["expected"], D("4000.00"))
        closed = await self.crm.cash_shift(shift_id)
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["diff"], D("-100.00"))
        self.assertFalse(logic.shift_rows([closed])[0]["big_diff"])
        # Платёж после закрытия в смену уже не попадает.
        await service.add_entry(self.crm, client, kind="payment", amount=D("700"),
                                method="cash", note="после смены", by="staff:t")
        self.assertEqual(len(await self.crm.shift_payments(shift_id)), 1)

        moment = datetime.now(UTC)
        row = {"txn_id": "T-1", "account": "ACC", "booked_at": moment,
               "amount": D("3000"), "direction": "credit",
               "payer_name": "ИВАНОВ ИВАН", "payer_inn": None,
               "purpose": "Оплата по договору АВ-2026-000042"}
        txn_id = await self.crm.save_bank_txn(row)
        self.assertIsNotNone(txn_id)
        self.assertIsNone(await self.crm.save_bank_txn(row),
                          "та же операция второй раз не ложится")
        await self.crm.update_client(self.client_id, contract_no="АВ-2026-000042")
        guessed = logic.bank_rows(await self.crm.bank_txns(status="new"),
                                  await self.crm.clients(limit=100))
        self.assertTrue(guessed[0]["sure"])
        ledger_id = await service.credit_bank_txn(
            self.crm, await self.crm.bank_txn(txn_id),
            await self.crm.client(self.client_id), by="staff:t")
        self.assertIsNotNone(ledger_id)
        marked = await self.crm.bank_txn(txn_id)
        self.assertEqual(marked["status"], "matched")
        self.assertEqual(marked["ledger_id"], ledger_id)
        self.assertEqual(await self.crm.last_bank_txn_at(), moment)

    async def test_mailing_on_postgres(self):
        """Рассылка на живой базе: встроенные шаблоны, очередь без дублей,
        связь MAX-аккаунта с карточкой."""
        await self.seed()
        seeded = {t["code"] for t in await self.crm.templates()}
        self.assertEqual(seeded, {"debt", "comeback", "expiring"},
                         "шаблоны приезжают со схемой")
        debt = next(t for t in await self.crm.templates() if t["code"] == "debt")
        self.assertIn("\n", debt["body"], "перенос строки, а не два символа")

        # должник с Telegram и должник с MAX
        await self.crm.add_ledger(client_id=self.client_id, kind="charge",
                                  amount=D("-3000"))
        other = await self.crm.create_client(full_name="Петров Пётр",
                                             phone="+79990000002")
        self.assertEqual(await self.crm.link_client_max("+79990000002", 777), other)
        self.assertIsNone(await self.crm.link_client_max("+79990000000", 777),
                          "чужой MAX-аккаунт не перевешивается")
        await self.crm.add_ledger(client_id=other, kind="charge", amount=D("-1500"))

        people = logic.pick_audience("debtors", await self.crm.clients_for_mailing(),
                                     await self.crm.active_rentals())
        self.assertEqual({p["channel"] for p in people}, {"tg", "max"})

        created = await service.create_campaign(
            self.crm, title="Долги", template=debt, audience="debtors",
            note=None, by="staff:t")
        self.assertEqual(created["queued"], 2)
        campaign = await self.crm.campaign(created["id"])
        self.assertEqual(campaign["no"], "РСЛ-000001")
        self.assertEqual(campaign["status"], "draft")
        # Повторная постановка в очередь ничего не добавляет.
        self.assertEqual(
            await self.crm.queue_sends(created["id"],
                                       [(int(p["id"]), p["channel"]) for p in people]),
            0)

        sends = await self.crm.campaign_sends(created["id"])
        await self.crm.mark_send(sends[0]["id"], status="sent")
        await self.crm.mark_send(sends[1]["id"], status="failed", error="заблокировал")
        progress = logic.campaign_progress(await self.crm.campaign_sends(created["id"]))
        self.assertEqual((progress["sent"], progress["failed"]), (1, 1))
        rows = await self.crm.campaigns()
        self.assertEqual((rows[0]["sent"], rows[0]["failed"], rows[0]["total"]),
                         (1, 1, 2))
        await self.crm.set_campaign_status(created["id"], "done")
        self.assertEqual(await self.crm.sending_campaigns(), [])

    async def test_signing_on_postgres(self):
        """ПЭП на живой базе: пакет в jsonb, код только хэшем, подпись одна."""
        await self.seed()
        client = await self.crm.client(self.client_id)
        company = {"company_name": "ИП Гарипов И. Р.", "company_inn": "166012345678"}
        created = await service.start_signing(
            self.crm, client=client, rental=None, company=company,
            bot_user={"contract_sha256": "c" * 64, "contract_path": "/tmp/c.pdf",
                      "contract_no": "АВ-2026-000042"},
            by="staff:t")
        self.assertEqual(created["no"], "ПЭП-000001")
        row = await self.crm.sign_request(created["id"])
        self.assertEqual([d["kind"] for d in row["docs"]], ["esign", "contract"],
                         "jsonb вернулся списком, а не строкой")
        self.assertIn("ПЭП-000001", row["agreement"])
        self.assertEqual(row["status"], "new")

        code = await service.issue_sign_code(self.crm, row, ip="10.0.0.1")
        stored = await self.crm.sign_request(created["id"])
        self.assertEqual(stored["status"], "code")
        self.assertNotIn(code, stored["code_hash"])
        self.assertEqual(stored["code_hash"],
                         logic.hash_sign_code(row["token"], code))

        with self.assertRaises(service.ServiceError):
            await service.verify_sign(self.crm, stored, "000000", ip="10.0.0.1")
        after = await self.crm.sign_request(created["id"])
        self.assertEqual(after["attempts"], 1)

        signed = await service.verify_sign(self.crm, after, code, ip="10.0.0.1",
                                           agent="Mozilla/5.0")
        self.assertEqual(signed["digest"], logic.sign_docs_digest(after["docs"]))
        done = await self.crm.sign_request(created["id"])
        self.assertEqual(done["status"], "signed")
        self.assertEqual(done["signed_ip"], "10.0.0.1")
        self.assertIsNone(done["code_hash"])
        # Вторая подпись невозможна: обновление не находит строку.
        self.assertFalse(await self.crm.mark_signed(created["id"], ip=None,
                                                    agent=None))
        kinds = [e["kind"] for e in await self.crm.sign_events(created["id"])]
        self.assertEqual(kinds, ["created", "code_sent", "code_wrong", "signed"])
        by_token = await self.crm.sign_request_by_token(row["token"])
        self.assertEqual(by_token["id"], created["id"])
        self.assertEqual(by_token["full_name"], "Иванов Иван")

    async def test_catalog_and_prices_from_the_owner_table(self):
        """Каталог, цены и пункты приезжают со схемой и не двоятся."""
        models = {m["title"]: m for m in await self.crm.bike_models()}
        self.assertIn("Monster Truck + (Два АКБ)", models)
        self.assertEqual(models["Kugoo V3 Pro (Два АКБ)"]["speed_kmh"], 60)
        self.assertEqual(models["Kugoo V3 Pro (Два АКБ)"]["motor_watt"], 1200)
        self.assertEqual(models["Monster Truck + (Два АКБ)"]["size_note"],
                         "120х43х110")

        tariffs = await self.crm.tariffs(active_only=True)
        truck = logic.tariffs_for_model(tariffs, "Monster Truck + (Два АКБ)")
        self.assertEqual({int(t["period_days"]): t["price"] for t in truck},
                         {7: D("3000.00"), 14: D("5400.00"), 30: D("11000.00")})
        kugoo = logic.tariffs_for_model(tariffs, "Kugoo V3 Pro (Два АКБ)")
        self.assertEqual({int(t["period_days"]): t["price"] for t in kugoo},
                         {7: D("3500.00"), 14: D("6000.00"), 30: D("12500.00")})

        # Второй такой же срок у той же модели база не примет.
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.crm.create_tariff("Неделя", 7, D("4000"), None,
                                         model="Kugoo V3 Pro (Два АКБ)")
        # А выключенный тариф места не занимает: цену можно переиграть.
        old_id = int(kugoo[0]["id"])
        await self.crm.update_tariff(old_id, active=False)
        new_id = await self.crm.create_tariff("Неделя", 7, D("4000"), None,
                                              model="Kugoo V3 Pro (Два АКБ)")
        self.assertNotEqual(new_id, old_id)

        points = {p["name"]: p for p in await self.crm.locations()}
        self.assertAlmostEqual(points["Павлюхина"]["lat"], 55.7669, places=4)
        self.assertEqual(points["Адоратского"]["hours"], "пн-вс: 10:00-19:00")
        self.assertIn("Адоратского, 11А", points["Адоратского"]["address"])

        # Повторное применение схемы ничего не дублирует.
        db = Database(self.pool)
        await db.apply_schema(SCHEMA)
        self.assertEqual(len(await self.crm.bike_models()), len(models))

    async def test_payments_on_postgres(self):
        """Счета на живой базе: деньги в журнал один раз, карта одна."""
        await self.seed()
        client = await self.crm.client(self.client_id)
        order_id = await self.crm.create_pay_order(
            client_id=self.client_id, rental_id=None, amount=D("3500"),
            purpose=logic.pay_purpose(client, None), created_by="staff:t")
        order = await self.crm.pay_order(order_id)
        self.assertEqual(order["no"], "СЧТ-000001")
        self.assertEqual(order["status"], "new")
        self.assertEqual(await self.crm.client_balance(self.client_id), D(0),
                         "счёт баланса не трогает")

        await self.crm.set_pay_link(order_id, link="https://pay/1",
                                    operation_id="op-1")
        sent = await self.crm.pay_order(order_id)
        self.assertEqual(sent["status"], "sent")
        self.assertEqual([o["id"] for o in await self.crm.open_pay_orders()],
                         [order_id])

        # Одна операция банка - один счёт: частичный уникальный индекс.
        other = await self.crm.create_pay_order(
            client_id=self.client_id, rental_id=None, amount=D("100"),
            purpose="дубль", created_by="staff:t")
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.crm.set_pay_link(other, link="https://pay/2",
                                        operation_id="op-1")

        ledger_id = await self.crm.mark_pay_paid(order_id, method="card",
                                                 by="эквайринг")
        self.assertIsNotNone(ledger_id)
        self.assertEqual(await self.crm.client_balance(self.client_id), D("3500.00"))
        # Второй ответ банка про тот же счёт денег не добавляет.
        self.assertIsNone(await self.crm.mark_pay_paid(order_id))
        self.assertEqual(await self.crm.client_balance(self.client_id), D("3500.00"))
        paid = await self.crm.pay_order(order_id)
        self.assertEqual(paid["ledger_id"], ledger_id)
        self.assertEqual(await self.crm.open_pay_orders(), [],
                         "оплаченный счёт больше не опрашивается")

        # Карта одна: привязка новой снимает старую.
        await self.crm.save_card_token(client_id=self.client_id, token="tk1",
                                       mask="4477")
        await self.crm.save_card_token(client_id=self.client_id, token="tk2",
                                       mask="1111")
        card = await self.crm.card_of(self.client_id)
        self.assertEqual(card["token"], "tk2")
        self.assertEqual(len(await self.crm.cards()), 1)
        await self.crm.drop_card(self.client_id)
        self.assertIsNone(await self.crm.card_of(self.client_id))

    async def test_notices_on_postgres(self):
        """Уведомления на живой базе: правка поверх каталога и история."""
        await self.seed()
        # Пустая таблица - всё включено по умолчанию из каталога.
        state = logic.notice_settings(await self.crm.notices())
        self.assertEqual(set(state), set(logic.NOTICES))
        self.assertTrue(state["daily_digest"]["enabled"])

        await self.crm.set_notice("daily_digest", enabled=False, at_hour=21,
                                  at_minute=15, chat_id="-100500",
                                  extra={}, by="staff:t")
        # Повторная правка того же кода - обновление, а не вторая строка.
        await self.crm.set_notice("daily_digest", enabled=False, at_hour=22,
                                  at_minute=0, chat_id=None, extra={},
                                  by="staff:t")
        rows = await self.crm.notices()
        self.assertEqual(len(rows), 1)
        state = logic.notice_settings(rows)
        self.assertFalse(state["daily_digest"]["enabled"])
        self.assertEqual(logic.notice_time(state["daily_digest"]), "22:00")
        self.assertIsNone(state["daily_digest"]["chat_id"])

        # jsonb возвращается словарём, а не строкой.
        await self.crm.set_notice("review_ask", enabled=True, at_hour=10,
                                  extra={"after_days": 45}, by="staff:t")
        state = logic.notice_settings(await self.crm.notices())
        self.assertEqual(logic.notice_param(state["review_ask"], "after_days"), 45)

        await self.crm.log_notice("rent_due", target="client", status="sent",
                                  client_id=self.client_id)
        await self.crm.log_notice("rent_due", target="client", status="failed",
                                  client_id=self.client_id, detail="бот заблокирован")
        self.assertEqual(await self.crm.notice_counts(30), {"rent_due": 1},
                         "в счётчик идут только отправленные")
        log = await self.crm.notice_log(code="rent_due")
        self.assertEqual([r["status"] for r in log], ["failed", "sent"])
        self.assertIsNotNone(log[0]["full_name"])

        await self.pool.execute(
            "update crm.notice_log set created_at = created_at - interval '40 days'")
        self.assertEqual(await self.crm.purge_notice_log(30), 2)
        self.assertEqual(await self.crm.notice_log(), [])

    async def test_repair_invoice_stays_out_of_the_ledger(self):
        """Красная линия на живой базе: ремонт не попадает в журнал аренды."""
        await self.seed()
        order_id = await self.crm.create_work_order(
            bike_id=self.bike_id, payer="client", client_id=self.client_id,
            complaint="стук", object_note=None, tech_id=None,
            estimate=D(0), created_by="staff:t")
        await self.crm.add_order_item(
            order_id, title="Замена колодок", node="brake_pads",
            work_type_id=None, qty=1, price=D("1500"), parts_cost=D("300"),
            labor_cost=D("200"), note=None)
        await self.crm.update_work_order(order_id, status="done",
                                         total=D("1500"), cost=D("500"))
        order = await self.crm.work_order(order_id)

        invoice = await service.invoice_order(self.crm, order, by="staff:t",
                                              acquiring=None)
        self.assertEqual(invoice["kind"], "repair")
        self.assertEqual(invoice["work_order_id"], order_id)
        self.assertEqual(await self.crm.client_balance(self.client_id), D(0))
        self.assertEqual([i["id"] for i in
                          await self.crm.work_order_invoices(order_id)],
                         [invoice["id"]])

        # Оплата ремонта: наряд помечен, журнал пуст.
        self.assertEqual(await self.crm.mark_pay_paid(invoice["id"], method="card"), 0,
                         "закрыт этим вызовом, но записи в журнале нет")
        self.assertIsNone(await self.crm.mark_pay_paid(invoice["id"], method="card"),
                          "второй раз - уже оплачен")
        self.assertEqual(await self.crm.client_balance(self.client_id), D(0))
        self.assertEqual(await self.crm.ledger_of(self.client_id), [])
        self.assertIsNotNone((await self.crm.work_order(order_id))["paid_at"])
        self.assertEqual((await self.crm.pay_order(invoice["id"]))["status"],
                         "paid")

        # А счёт за аренду по-прежнему ложится в журнал.
        rent = await self.crm.create_pay_order(
            client_id=self.client_id, rental_id=None, amount=D("3000"),
            purpose="Аренда велосипеда", created_by="staff:t")
        self.assertIsNotNone(await self.crm.mark_pay_paid(rent, method="card"))
        self.assertEqual(await self.crm.client_balance(self.client_id),
                         D("3000.00"))

    async def test_estimate_columns_survive_reapply(self):
        """Согласование сметы на живой базе: статус и отметки."""
        await self.seed()
        order_id = await self.crm.create_work_order(
            bike_id=self.bike_id, payer="client", client_id=self.client_id,
            complaint="стук", object_note=None, tech_id=None,
            estimate=D(0), created_by="staff:t")
        await self.crm.add_order_item(
            order_id, title="Диагностика", node="wiring", work_type_id=None,
            qty=1, price=D("600"), parts_cost=D(0), labor_cost=D(0), note=None)
        got = await service.send_estimate(
            self.crm, await self.crm.work_order(order_id), by="staff:t")
        self.assertEqual(got["total"], D("600.00"))
        order = await self.crm.work_order(order_id)
        self.assertEqual(order["status"], "approve")
        self.assertIsNotNone(order["estimate_sent_at"])

        await service.answer_estimate(self.crm, order, agree=True, by="клиент")
        order = await self.crm.work_order(order_id)
        self.assertEqual((order["status"], order["approved_by"]),
                         ("in_work", "клиент"))
        # Повторное применение схемы колонок не теряет.
        db = Database(self.pool)
        await db.apply_schema(SCHEMA)
        self.assertEqual((await self.crm.work_order(order_id))["approved_by"],
                         "клиент")

    async def test_bonuses_on_postgres(self):
        """Баллы на живой базе: вид journal, повод рядом, один раз."""
        await self.seed()
        bonus_id = await self.crm.grant_bonus(
            client_id=self.client_id, kind="review", amount=D("300"),
            note="отзыв на Яндекс.Картах", by="staff:t")
        self.assertIsNotNone(bonus_id)
        self.assertEqual(await self.crm.client_balance(self.client_id),
                         D("300.00"))
        entry = (await self.crm.ledger_of(self.client_id))[0]
        self.assertEqual(entry["kind"], "bonus")

        # Платежей не прибавилось - средний чек цел.
        self.assertEqual(await self.crm.payments_total(
            since=date(2000, 1, 1), until=date(2100, 1, 1)), D(0))

        # Бонус за отзыв - один раз: частичный уникальный индекс.
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.crm.grant_bonus(client_id=self.client_id, kind="review",
                                       amount=D("300"), by="staff:t")
        # И баланс от неудачной попытки не поехал: обе вставки одной
        # транзакцией, откатились обе.
        self.assertEqual(await self.crm.client_balance(self.client_id),
                         D("300.00"))

        # А начислять руками можно сколько угодно раз.
        await self.crm.grant_bonus(client_id=self.client_id, kind="manual",
                                   amount=D("100"), by="staff:t")
        await self.crm.grant_bonus(client_id=self.client_id, kind="manual",
                                   amount=D("100"), by="staff:t")
        self.assertEqual(await self.crm.client_balance(self.client_id),
                         D("500.00"))
        rows = await self.crm.bonuses(client_id=self.client_id)
        self.assertEqual(len(rows), 3)
        totals = logic.bonus_totals(rows, D("50000"))
        self.assertEqual(totals["total"], D("500.00"))
        self.assertEqual(totals["by_kind"]["manual"], D("200.00"))
        self.assertIsNotNone(await self.crm.bonus_of(self.client_id, "review"))

    async def test_intake_on_postgres(self):
        """Сверка на живой базе: jsonb сливается, статус пишет триггер."""
        await self.seed()
        bike_id = await self.crm.create_bike(
            code="B-900", model="Kugoo", frame_no="DEMO-900", status="new",
            by="staff:t")
        bike = await self.crm.bike(bike_id)
        self.assertEqual(bike["status"], "new")
        self.assertEqual(bike["checked"], {},
                         "jsonb вернулся словарём, а не строкой")

        await self.crm.mark_bike_checked(bike_id, "model", by="staff:t")
        await self.crm.mark_bike_checked(bike_id, "frame_no", by="staff:t",
                                         photo="900-frame.jpg")
        bike = await self.crm.bike(bike_id)
        # Слияние, а не перезапись: вторая отметка не съела первую.
        self.assertEqual(set(bike["checked"]), {"model", "frame_no"})
        self.assertEqual(bike["checked"]["frame_no"]["photo"], "900-frame.jpg")

        await self.crm.clear_bike_check(bike_id, "model")
        self.assertEqual(set((await self.crm.bike(bike_id))["checked"]),
                         {"frame_no"})

        # Ввод в эксплуатацию: статус меняется, автора пишет триггер.
        self.assertTrue(await self.crm.commission_bike(bike_id, by="staff:t"))
        bike = await self.crm.bike(bike_id)
        self.assertEqual(bike["status"], "available")
        self.assertIsNotNone(bike["commissioned_at"])
        log = await self.crm.bike_status_log(bike_id)
        self.assertEqual((log[0]["from_status"], log[0]["to_status"]),
                         ("new", "available"))
        self.assertEqual(log[0]["changed_by"], "staff:t")
        # Второй раз выпустить нельзя.
        self.assertFalse(await self.crm.commission_bike(bike_id, by="staff:t"))

        # На сборке остались только те, кого не выпустили.
        await self.crm.create_bike(code="B-901", model="Kugoo", status="new",
                                   by="staff:t")
        self.assertEqual([b["code"] for b in await self.crm.bikes_on_assembly()],
                         ["B-901"])

    async def test_doc_templates_on_postgres(self):
        """Свои шаблоны на живой базе: включённый ровно один."""
        await self.seed()
        self.assertEqual(await self.crm.doc_templates(), [])
        first = await self.crm.add_doc_template(
            kind="contract", filename="contract-0001.docx",
            original="договор.docx", size_bytes=1234, sha256="a" * 64,
            by="staff:t")
        second = await self.crm.add_doc_template(
            kind="contract", filename="contract-0002.docx",
            original="договор-2.docx", size_bytes=2345, sha256="b" * 64,
            by="staff:t")
        # Загруженный шаблон сам по себе не включается.
        self.assertIsNone(await self.crm.active_doc_template("contract"))

        self.assertTrue(await self.crm.enable_doc_template(first))
        self.assertEqual((await self.crm.active_doc_template("contract"))["id"],
                         first)
        # Второй включается вместо первого: частичный уникальный индекс
        # не даст двум быть включёнными сразу.
        self.assertTrue(await self.crm.enable_doc_template(second))
        active = await self.crm.active_doc_template("contract")
        self.assertEqual(active["id"], second)
        self.assertEqual(sum(1 for r in await self.crm.doc_templates("contract")
                             if r["active"]), 1)

        # Включённый из архива не удаляется.
        self.assertIsNone(await self.crm.drop_doc_template(second))
        self.assertIsNotNone(await self.crm.drop_doc_template(first))

        await self.crm.disable_doc_templates("contract")
        self.assertIsNone(await self.crm.active_doc_template("contract"))
        self.assertEqual(len(await self.crm.doc_templates("contract")), 1,
                         "прошлая редакция остаётся в архиве")

        # Подпись и печать: одна строка на вид, замена перезаписывает.
        await self.crm.set_company_mark("stamp", filename="stamp.png",
                                        size_bytes=10, by="staff:t")
        await self.crm.set_company_mark("stamp", filename="stamp.png",
                                        size_bytes=20, by="staff:t")
        marks = await self.crm.company_marks()
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["size_bytes"], 20)
        await self.crm.drop_company_mark("stamp")
        self.assertEqual(await self.crm.company_marks(), [])

    async def test_rental_extras_on_postgres(self):
        """Доп. аккумулятор на живой базе: цена периода сходится всегда."""
        await self.seed()
        model_id = await self.crm.create_battery_model(
            title="Аккумулятор 70 Ач", brand=None, voltage=60, capacity=D("70"),
            price=D("12000"), service_months=15, note=None)
        battery_id = await self.crm.create_battery(
            code="9510001", model_id=model_id, status="available")
        # Вид тарифа разводит одинаковые сроки: раньше на «7 дн.» стоял
        # один уникальный индекс, и цена батареи в него не помещалась.
        await self.crm.create_tariff("АКБ · неделя", 7, D("1170"), None,
                                     model="Аккумулятор 70 Ач", kind="battery")
        rental_id = await service.open_rental(
            self.crm, client=await self.crm.client(self.client_id),
            bike=await self.crm.bike(self.bike_id),
            tariff=await self.crm.tariff(self.tariff_id),
            started_on=date.today(), contract_no="АВ-1", by="test")
        rental = await self.crm.rental(rental_id)
        self.assertEqual(rental["price"], D("3000.00"))
        self.assertEqual(rental["base_price"], D("3000.00"))

        price = await service.add_battery_extra(
            self.crm, rental, await self.crm.battery(battery_id),
            tariffs=await self.crm.tariffs(active_only=True), by="test")
        self.assertEqual(price, D("1170.00"))
        rental = await self.crm.rental(rental_id)
        self.assertEqual(rental["price"], D("4170.00"))
        self.assertEqual(rental["base_price"], D("3000.00"),
                         "база не двигается: она цена велосипеда на выдаче")
        self.assertEqual((await self.crm.battery(battery_id))["status"], "rented")

        # Та же батарея второй строкой - это двойная цена за одну вещь.
        with self.assertRaises(asyncpg.exceptions.UniqueViolationError):
            await self.crm.add_rental_extra(
                rental_id, kind="battery", title="Доп. аккумулятор",
                price=D("1170"), battery_id=battery_id, by="test")

        extra = (await self.crm.rental_extras(rental_id, live_only=True))[0]
        await service.drop_battery_extra(self.crm, rental, extra, by="test")
        self.assertEqual((await self.crm.rental(rental_id))["price"], D("3000.00"))
        self.assertEqual((await self.crm.battery(battery_id))["status"], "available")

        # Закрытие аренды закрывает и позиции.
        await service.add_battery_extra(
            self.crm, await self.crm.rental(rental_id),
            await self.crm.battery(battery_id),
            tariffs=await self.crm.tariffs(active_only=True), by="test")
        await self.crm.close_rental(rental_id, closed_on=date.today(), note=None,
                                    closed_by="test")
        self.assertEqual(await self.crm.rental_extras(rental_id, live_only=True), [])

    async def test_tariff_kind_survives_reapply(self):
        """Схема идемпотентна: вид тарифа и позиции переживают повтор."""
        await self.seed()
        await Database(self.pool).apply_schema(SCHEMA)
        rows = await self.crm.tariffs()
        self.assertTrue(all((r.get("kind") or "") == "bike" for r in rows),
                        "старый тариф без вида читается как велосипед")
        await self.crm.create_tariff("АКБ · неделя", 7, D("1170"), None,
                                     kind="battery")
        self.assertEqual(len(await self.crm.tariffs(kind="battery")), 1)

    async def test_battery_passport_on_postgres(self):
        """Сверка батареи на живой базе: отметки сливаются, не затирая."""
        await self.seed()
        model_id = await self.crm.create_battery_model(
            title="Аккумулятор 70 Ач", brand=None, voltage=60, capacity=D("70"),
            price=D("12000"), service_months=15, note=None)
        battery_id = await self.crm.create_battery(
            code="9510001", model_id=model_id, serial_no="SN-1", volts=60,
            amp_hours=D("70"), status="new", by="staff:t")
        battery = await self.crm.battery(battery_id)
        state = logic.battery_check_state(battery, {})
        self.assertTrue(state["new"])
        self.assertEqual(len(state["left"]), len(logic.BATTERY_PASSPORT))

        # jsonb правится слиянием: соседние поля не затирают друг друга.
        await self.crm.mark_battery_checked(battery_id, "code", by="staff:a")
        await self.crm.mark_battery_checked(battery_id, "serial_no", by="staff:b",
                                            photo="akb-1-serial_no.jpg")
        marks = (await self.crm.battery(battery_id))["checked"]
        self.assertEqual(set(marks), {"code", "serial_no"})
        self.assertEqual(marks["serial_no"]["photo"], "akb-1-serial_no.jpg")

        # Пока не сверено всё - в оборот не выходит.
        with self.assertRaises(service.ServiceError):
            await service.commission_battery(
                self.crm, await self.crm.battery(battery_id), by="staff:t")
        for field in ("model", "volts", "amp_hours"):
            await self.crm.mark_battery_checked(battery_id, field, by="staff:t")
        await service.commission_battery(
            self.crm, await self.crm.battery(battery_id), by="staff:t")
        row = await self.crm.battery(battery_id)
        self.assertEqual(row["status"], "available")
        self.assertIsNotNone(row["commissioned_at"])
        # Смену статуса пишет триггер, автор - из set_config.
        log = await self.crm.battery_status_log(battery_id)
        self.assertEqual(log[0]["to_status"], "available")
        self.assertEqual(log[0]["changed_by"], "staff:t")

        await self.crm.clear_battery_check(battery_id, "code")
        self.assertNotIn("code", (await self.crm.battery(battery_id))["checked"])

    async def test_battery_columns_survive_reapply(self):
        await self.seed()
        battery_id = await self.crm.create_battery(code="9510002", status="new")
        await Database(self.pool).apply_schema(SCHEMA)
        row = await self.crm.battery(battery_id)
        self.assertEqual(row["status"], "new")
        self.assertEqual(row["checked"], {})

    async def test_stock_take_with_batteries_on_postgres(self):
        """Ведомость на живой базе считает и батареи."""
        await self.seed()
        battery_id = await self.crm.create_battery(
            code="9510001", status="available", location="Павлюхина")
        gone_id = await self.crm.create_battery(
            code="9510002", status="available", location="Павлюхина")
        take_id = await service.start_stock_take(
            self.crm, scope="all", location=None, note=None, what="all",
            by="staff:t")
        items = await self.crm.take_items(take_id)
        self.assertEqual(len(items), 3, "велосипед и две батареи")

        # Номер может оказаться и велосипедным, и батарейным.
        got = await service.take_add_found(
            self.crm, await self.crm.stock_take(take_id), "9510001")
        self.assertEqual(got["state"], "found")
        self.assertEqual(got["kind"], "battery")
        got = await service.take_add_found(
            self.crm, await self.crm.stock_take(take_id), "B-1")
        self.assertEqual(got["kind"], "bike")

        # Та же батарея второй раз - уникальный индекс не даст задвоить.
        with self.assertRaises(asyncpg.exceptions.UniqueViolationError):
            await self.crm.add_take_item(take_id, bike_id=None,
                                         battery_id=battery_id, code="9510001")

        result = await service.finish_stock_take(
            self.crm, await self.crm.stock_take(take_id), by="staff:t",
            lose_missing=True)
        self.assertEqual(result["missing"], 1)
        self.assertEqual((await self.crm.battery(gone_id))["status"], "lost")
        self.assertEqual((await self.crm.battery(battery_id))["status"],
                         "available")
        # Смену статуса батареи пишет триггер, автор - из set_config.
        log = await self.crm.battery_status_log(gone_id)
        self.assertEqual(log[0]["to_status"], "lost")
        self.assertEqual(log[0]["changed_by"], "staff:t")

    async def test_alert_workflow_on_postgres(self):
        """Тревога как задача: «это норма» не даёт ей подняться заново."""
        await self.seed()
        tracker_id = await self.crm.create_tracker(
            device_id="1001", alias="Метка", bike_id=self.bike_id)
        alert_id = await self.crm.raise_alert(
            tracker_id=tracker_id, kind="moving", note="30 км/ч",
            bike_id=self.bike_id, lat=55.8, lon=49.1, level="urgent")
        self.assertIsNotNone(alert_id)

        # Частичный уникальный индекс: одна открытая тревога вида на трекер.
        self.assertIsNone(await self.crm.raise_alert(
            tracker_id=tracker_id, kind="moving", note="ещё", bike_id=None,
            lat=None, lon=None, level="urgent"))

        self.assertTrue(await self.crm.set_alert_state(
            alert_id, state="working", by="staff:t"))
        row = await self.crm.tracker_alert(alert_id)
        self.assertEqual(row["state"], "working")
        self.assertEqual(row["taken_by"], "staff:t")

        # «Это норма» оставляет её открытой - потому вторая и не встаёт.
        await self.crm.set_alert_state(alert_id, state="normal", by="staff:t")
        row = await self.crm.tracker_alert(alert_id)
        self.assertEqual(row["state"], "normal")
        self.assertIsNone(row["handled_at"])
        self.assertIsNone(await self.crm.raise_alert(
            tracker_id=tracker_id, kind="moving", note="и ещё", bike_id=None,
            lat=None, lon=None, level="urgent"))

        # Причина исчезла - опрос закрывает её сам, и место освобождается.
        await self.crm.close_alerts(tracker_id, ["moving"], by="tracking")
        self.assertIsNotNone((await self.crm.tracker_alert(alert_id))["handled_at"])
        self.assertIsNotNone(await self.crm.raise_alert(
            tracker_id=tracker_id, kind="moving", note="снова", bike_id=None,
            lat=None, lon=None, level="urgent"))

        # Закрытую в работу не возвращают.
        self.assertFalse(await self.crm.set_alert_state(
            alert_id, state="working", by="staff:t"))

        # Фильтры по уровню и виду.
        await self.crm.raise_alert(tracker_id=tracker_id, kind="offline",
                                   note="молчит", bike_id=None, lat=None,
                                   lon=None, level="yellow")
        urgent = await self.crm.tracker_alerts(open_only=True, level="urgent")
        self.assertEqual({a["kind"] for a in urgent}, {"moving"})
        # Срочные сверху.
        everything = await self.crm.tracker_alerts(open_only=True)
        self.assertEqual(everything[0]["level"], "urgent")

    async def test_moved_at_tracks_the_last_ride(self):
        """`moved_at` не сбрасывается остановкой: по нему считают простой."""
        await self.seed()
        rode = datetime.now(UTC) - timedelta(days=4)
        await self.crm.save_tracker_state({
            "device_id": "1002", "alias": "Метка", "lat": 55.8, "lon": 49.1,
            "speed": D("18"), "voltage": D("12.6"), "recorded_at": rode,
            "course": None, "gsm_level": None, "alarm": False})
        stopped = datetime.now(UTC)
        await self.crm.save_tracker_state({
            "device_id": "1002", "alias": "Метка", "lat": 55.8, "lon": 49.1,
            "speed": D("0"), "voltage": D("12.6"), "recorded_at": stopped,
            "course": None, "gsm_level": None, "alarm": False})
        row = await self.crm.tracker_by_device("1002")
        self.assertEqual(row["moved_at"], rode)
        self.assertEqual(row["last_seen"], stopped)

    async def test_service_reports_on_postgres(self):
        """Отчёты сервиса на живой базе: сумма сходится с нарядами."""
        await self.seed()
        tech_id = await self.crm.create_staff(
            "homyakov", logic.hash_password("homyakov-pass"), name="Хомяков И.",
            role="tech", profile_id=None)
        part_id = await self.crm.create_part(
            title="Камера", node="tube_tire", unit="шт", cost=D("300"),
            price=D("600"), min_stock=2, model=None, note=None)
        first = await self.crm.create_work_order(
            bike_id=self.bike_id, payer="own", client_id=None, complaint="стук",
            object_note=None, tech_id=tech_id, estimate=D("0"), created_by="t")
        second = await self.crm.create_work_order(
            bike_id=None, payer="client", client_id=self.client_id,
            complaint="чужой самокат", object_note="самокат", tech_id=None,
            estimate=D("0"), created_by="t")
        now = datetime.now(UTC)
        await self.crm.update_work_order(first, status="done", total=D("2000"),
                                         cost=D("900"), closed_at=now)
        await self.crm.update_work_order(second, status="done", total=D("1000"),
                                         cost=D("300"), closed_at=now)
        await self.crm.add_part_move(part_id=part_id, kind="order", qty=-3,
                                     cost=D("300"), order_id=first,
                                     created_by="t")
        await self.crm.add_part_move(part_id=part_id, kind="order", qty=-1,
                                     cost=D("300"), order_id=second,
                                     created_by="t")
        since, until = now - timedelta(days=1), now + timedelta(days=1)

        techs = logic.tech_rows(await self.crm.tech_work(since, until))
        by_name = {r["tech"]: r for r in techs}
        self.assertEqual(by_name["Хомяков И."]["orders"], 1)
        self.assertIn("не назначен", by_name,
                      "наряд без техника не теряется - иначе сумма не сойдётся")
        total = logic.tech_total(techs)
        self.assertEqual(total["total"], D("3000.00"))
        self.assertEqual(total["works"], D("1800.00"))

        models = logic.model_parts_rows(
            await self.crm.model_parts(since, until),
            await self.crm.bikes(limit=100), days=2)
        by_model = {r["model"]: r for r in models}
        self.assertEqual(by_model["Kugoo V3"]["cost"], D("900.00"))
        self.assertEqual(by_model["чужая техника"]["cost"], D("300.00"),
                         "чужая техника отдельной строкой: не наш парк")

        spend = logic.spend_rows(await self.crm.part_spend(since, until))
        self.assertEqual(len(spend), 1)
        self.assertEqual(spend[0]["qty"], 4)
        self.assertEqual(spend[0]["cost"], D("1200.00"))
        self.assertEqual(spend[0]["orders"], 2)

    async def test_bikes_in_status_by_day_on_postgres(self):
        """Суточная разбивка: полдня в ремонте - это 0,5, а не 0 и не 1."""
        await self.seed()
        today = date.today()
        tz = datetime.now().astimezone().tzinfo
        now = datetime.now(tz)
        # Ремонт начался три часа назад, но не раньше сегодняшней полуночи:
        # «с полудня» зависело от времени суток и до полудня давало
        # отрицательную долю - тест падал по утрам.
        midnight = datetime.combine(today, datetime.min.time(), tzinfo=tz)
        start = max(midnight + timedelta(minutes=1), now - timedelta(hours=3))
        # Журнал пишет триггер; подменяем время записей, чтобы получить
        # известную долю суток ремонта - на живой базе это и проверяем.
        await self.crm.update_bike(self.bike_id, status="repair", by="staff:t")
        await self.pool.execute(
            "update crm.bike_status_log set changed_at = $2 "
            "where bike_id = $1 and to_status = 'available'",
            self.bike_id, start - timedelta(days=10))
        await self.pool.execute(
            "update crm.bike_status_log set changed_at = $2 "
            "where bike_id = $1 and to_status = 'repair'", self.bike_id, start)
        by_day = await self.crm.bikes_in_status_by_day("repair", today, today)
        self.assertEqual(set(by_day), {today})
        # Ремонт идёт с start по «сейчас»: доля суток, а не 0 и не 1.
        # Ждём ровно столько, сколько прошло, с запасом на время запроса.
        want = D(str((datetime.now(tz) - start).total_seconds() / 86400))
        self.assertAlmostEqual(by_day[today], want, delta=D("0.001"))
        self.assertGreater(by_day[today], D("0"))
        self.assertLess(by_day[today], D("1"))

        # Дни без ремонта в ряду остаются нулями, а не пропадают: иначе
        # на графике будет дыра. И не единицами: greatest/least в
        # Postgres игнорируют NULL, и на этом легко получить сутки
        # ремонта там, где ремонта не было вовсе.
        week = await self.crm.bikes_in_status_by_day(
            "repair", today - timedelta(days=3), today)
        self.assertEqual(len(week), 4)
        self.assertEqual(week[today - timedelta(days=3)], D("0"))
        self.assertEqual(week[today - timedelta(days=1)], D("0"))

    async def test_money_by_day_on_postgres(self):
        """Деньги по дням: пустой день - ноль, а не пропуск."""
        await self.seed()
        today = date.today()
        await self.crm.add_ledger(client_id=self.client_id, rental_id=None,
                                  kind="payment", amount=D("4200"))
        await self.crm.add_ledger(client_id=self.client_id, rental_id=None,
                                  kind="charge", amount=D("-3000"))
        rows = await self.crm.money_by_day(today - timedelta(days=2), today)
        self.assertEqual(len(rows), 3, "дни без движения остаются в ряду")
        by_day = {r["day"]: r for r in rows}
        self.assertEqual(by_day[today]["paid"], D("4200.00"))
        self.assertEqual(by_day[today]["charged"], D("3000.00"),
                         "начисление показываем положительным числом")
        self.assertEqual(by_day[today - timedelta(days=2)]["paid"], D("0"))

        chart = logic.money_chart(rows, plan_per_day=D("2000"), today=today)
        self.assertEqual(chart["paid"], D("4200.00"))
        self.assertEqual(chart["debt"], D("0"),
                         "заплатили больше, чем начислили - это не долг")
        self.assertEqual(chart["over_days"], 1)

    async def test_saved_views_on_postgres(self):
        """Свои фильтры: одно имя на список у сотрудника, чужой не трогаем."""
        await self.seed()
        boss = await self.crm.create_staff(
            "boss", logic.hash_password("boss-pass-1"), name="Владелец",
            role="admin", profile_id=None)
        other = await self.crm.create_staff(
            "sosed", logic.hash_password("sosed-pass-1"), name="Сосед",
            role="admin", profile_id=None)
        first = await self.crm.save_view(staff_id=boss, section="/rentals",
                                         name="Мои должники",
                                         query="status=active&view=debt")
        # Второе сохранение под тем же именем обновляет набор, а не
        # заводит второй: спорить, какой из них настоящий, не о чем.
        again = await self.crm.save_view(staff_id=boss, section="/rentals",
                                         name="мои должники",
                                         query="status=closed")
        self.assertEqual(first, again)
        views = await self.crm.saved_views(boss, "/rentals")
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0]["query"], "status=closed")

        # Тот же список у другого сотрудника - свой.
        await self.crm.save_view(staff_id=other, section="/rentals",
                                 name="Мои должники", query="status=active")
        self.assertEqual(len(await self.crm.saved_views(other, "/rentals")), 1)
        self.assertEqual(len(await self.crm.saved_views(boss, "/rentals")), 1)

        # Чужой фильтр не удаляется по одному лишь id.
        self.assertFalse(await self.crm.drop_saved_view(first, staff_id=other))
        self.assertTrue(await self.crm.drop_saved_view(first, staff_id=boss))
        self.assertEqual(await self.crm.saved_views(boss, "/rentals"), [])

    async def test_stock_value_by_month_on_postgres(self):
        """Деньги на полке: qty * себестоимость единицы, накопительно."""
        await self.seed()
        part_id = await self.crm.create_part(
            title="Камера", node="tube_tire", unit="шт", cost=D("300"),
            price=D("600"), min_stock=2, model=None, note=None)
        await self.crm.add_part_move(part_id=part_id, kind="receipt", qty=10,
                                     cost=D("300"), created_by="t")
        await self.crm.add_part_move(part_id=part_id, kind="order", qty=-3,
                                     cost=D("300"), created_by="t")
        rows = await self.crm.stock_value_by_month()
        self.assertEqual(len(rows), 1, "оба движения - в текущем месяце")
        self.assertEqual(rows[0]["value"], D("2100.00"),
                         "10 × 300 − 3 × 300: цена в движении за единицу")
        self.assertEqual(rows[0]["month"], date.today().replace(day=1))
        chart = logic.stock_value_chart(rows)
        self.assertEqual(chart["now"], D("2100.00"))

    async def test_status_log_keeps_the_mileage_on_postgres(self):
        """Пробег снимается тем же триггером, что и статус."""
        await self.seed()
        await self.crm.update_bike(self.bike_id, status="repair",
                                   mileage_km=4266, by="staff:оператор")
        rows = await self.crm.bike_status_log(self.bike_id)
        self.assertEqual(rows[0]["to_status"], "repair")
        self.assertEqual(rows[0]["mileage_km"], 4266)
        self.assertEqual(rows[0]["changed_by"], "staff:оператор")
        # Смена статуса без нового одометра пишет прежний: пробег
        # у велосипеда один, и обнулять его в журнале нечестно.
        await self.crm.update_bike(self.bike_id, status="available",
                                   by="staff:оператор")
        rows = await self.crm.bike_status_log(self.bike_id)
        self.assertEqual(rows[0]["to_status"], "available")
        self.assertEqual(rows[0]["mileage_km"], 4266)

    async def test_order_on_approval_is_still_open_on_postgres(self):
        """«На согласовании» - открытый статус: SQL брал список из головы
        и терял его, и такой наряд на рабочем столе выглядел как «без наряда»."""
        await self.seed()
        order_id = await self.crm.create_work_order(
            bike_id=self.bike_id, payer="client", client_id=self.client_id,
            complaint="тормоза", object_note=None, tech_id=None,
            estimate=D("0"), created_by="t")
        await self.crm.update_work_order(order_id, status="approve")
        self.assertIn(self.bike_id, await self.crm.open_orders_by_bike())
        self.assertEqual((await self.crm.open_order_of(self.bike_id))["id"], order_id)
        self.assertEqual([o["id"] for o in await self.crm.work_orders(open_only=True)],
                         [order_id])
        await self.crm.update_work_order(order_id, status="cancelled")
        self.assertNotIn(self.bike_id, await self.crm.open_orders_by_bike())

    async def test_client_search_by_spare_phone_on_postgres(self):
        """Запасной номер ищется цифрами так же, как основной."""
        await self.seed()
        await self.crm.update_client(self.client_id, phone2="+79171112233",
                                     employer="samokat", experience="over_3")
        found = await self.crm.clients(q="917 111")
        self.assertEqual([c["id"] for c in found], [self.client_id])
        self.assertEqual(found[0]["employer"], "samokat")
        self.assertEqual(await self.crm.clients(q="905 555"), [])
        # Повторное применение схемы колонки не теряет.
        await Database(self.pool).apply_schema(SCHEMA)
        self.assertEqual((await self.crm.client(self.client_id))["phone2"], "+79171112233")

    async def test_battery_status_since_on_postgres(self):
        """Дни в статусе у батареи - по журналу, который пишет триггер."""
        await self.seed()
        battery_id = await self.crm.create_battery(code="9510009", status="available")
        since = await self.crm.battery_status_since()
        self.assertIn(battery_id, since)
        await self.crm.update_battery(battery_id, status="sold", by="staff:t")
        rows = await self.crm.battery_status_log(battery_id)
        self.assertEqual(rows[0]["to_status"], "sold")
        self.assertGreaterEqual((await self.crm.battery_status_since())[battery_id],
                                since[battery_id])
        # Аренда у батареи - в выборке вместе с датой выдачи.
        rental_id = await self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D("3000"), billing="weekly",
            started_on=date.today() - timedelta(days=3), contract_no="АВ-9",
            created_by="t")
        await self.crm.update_battery(battery_id, status="rented", rental_id=rental_id,
                                      by="staff:t")
        row = (await self.crm.batteries(rental_id=rental_id))[0]
        self.assertEqual(row["rental_started"], date.today() - timedelta(days=3))
        self.assertEqual(logic.battery_rows([row])[0]["rental_days"], 3)

    async def test_mileage_column_survives_reapply(self):
        """schema.sql идемпотентен: повторный старт не теряет колонку."""
        await Database(self.pool).apply_schema(SCHEMA)
        rows = await self.crm.pool.fetch(
            "select column_name from information_schema.columns "
            "where table_schema = 'crm' and table_name = 'bike_status_log'")
        self.assertIn("mileage_km", {r["column_name"] for r in rows})

    async def test_work_price_seed_on_postgres(self):
        """Прайс владельца ложится один раз на версию и правок в панели
        не трогает; заглушки первого сида, которых не касались, гаснут."""
        types = {t["title"]: t for t in await self.crm.work_types()}
        self.assertGreaterEqual(len(types), 100)
        row = types["Замена контроллера"]
        self.assertEqual((row["price"], row["parts_price"]), (D("1000"), D("3500")))
        self.assertEqual((row["price_ext"], row["parts_price_ext"]), (D("2500"), D("4500")))
        self.assertEqual(logic.sheet_price(row, "own"), D("4500"))
        self.assertEqual(logic.sheet_price(row, "ext"), D("7000"))
        self.assertIsNone(types["Заварить раму"]["price"], "арендатору не предлагается")
        self.assertIsNone(types["Потеря аккумулятора"]["price_ext"])
        self.assertEqual(types["Замена зеркал, шт"]["price"], D(0), "работа прочерком - ноль")
        self.assertEqual(types["Замена зеркал, шт"]["parts_price"], D(500))
        self.assertEqual((await self.crm.settings()).get("work_price_version"), "2026-08-17")
        # Правка в панели переживает повторный старт.
        await self.crm.update_work_type(row["id"], price=D("1100"))
        await Database(self.pool).apply_schema(SCHEMA)
        self.assertEqual((await self.crm.work_type(row["id"]))["price"], D("1100"))
        # Новая редакция (отметки нет): нетронутая заглушка выключается,
        # цена регламента возвращается, строки не плодятся.
        old = await self.crm.create_work_type(title="Замена камеры", category="Ходовая",
                                              minutes=15, price=D(200), node="tube_tire")
        await self.pool.execute("delete from crm.settings where key = 'work_price_version'")
        await Database(self.pool).apply_schema(SCHEMA)
        self.assertFalse((await self.crm.work_type(old))["active"])
        self.assertEqual((await self.crm.work_type(row["id"]))["price"], D("1000"))
        self.assertEqual(len(await self.crm.work_types()), len(types) + 1)

    async def test_one_pending_claim_per_client(self):
        """Двойное нажатие «Я оплатил(а)» давало две карточки оператору
        и риск зачислить один платёж дважды: теперь вторую открытую
        заявку не даёт частичный уникальный индекс."""
        await self.seed()
        first = await self.crm.create_claim(self.client_id, D("3000"))
        self.assertIsNotNone(first)
        self.assertIsNone(await self.crm.create_claim(self.client_id, D("3000")),
                          "вторая открытая заявка не заводится")
        await self.crm.resolve_claim(first, status="rejected", resolved_by="t")
        self.assertIsNotNone(await self.crm.create_claim(self.client_id, D("3000")),
                             "после разбора первой можно завести новую")

    async def test_schema_collapses_duplicate_claims_before_the_index(self):
        """Индекс создаётся на живой базе, где дубли уже могли накопиться:
        схема применяется при каждом старте и упасть на них не должна."""
        await self.seed()
        await self.pool.execute("drop index if exists crm.claims_one_pending")
        for _ in range(3):
            await self.pool.execute(
                "insert into crm.payment_claims (client_id) values ($1)",
                self.client_id)
        await Database(self.pool).apply_schema(SCHEMA)
        left = await self.pool.fetchval(
            "select count(*) from crm.payment_claims where status = 'pending'")
        self.assertEqual(left, 1, "лишние открытые заявки схлопнуты")
        await Database(self.pool).apply_schema(SCHEMA)   # идемпотентность

    async def test_rental_and_its_first_charge_are_one_transaction(self):
        """Аренда из бота заводится вместе с начислением: порознь сбой между
        ними оставлял клиента с лишним периодом на балансе навсегда."""
        await self.seed()
        today = date.today()
        rental_id = await self.crm.start_rental_charged(
            client_id=self.client_id, bike_id=self.bike_id, tariff_name="Неделя",
            period_days=7, price=D("3000"), billing="manual", started_on=today,
            period_to=today + timedelta(days=7), contract_no="АВ-7",
            note="Аренда по договору № АВ-7", created_by="bot")
        rental = await self.crm.rental(rental_id)
        self.assertEqual(rental["billed_until"], today + timedelta(days=7))
        self.assertEqual(await self.crm.client_balance(self.client_id), D("-3000.00"))
        self.assertEqual((await self.crm.bike(self.bike_id))["status"], "rented")

        # Продление: платёж и начисление тоже одной парой.
        await self.crm.extend_rental_paid(
            rental_id, self.client_id, amount=D("3000"),
            period_from=today + timedelta(days=7), period_to=today + timedelta(days=14),
            pay_note="Продление", charge_note="Продление: неделя",
            method="sbp", created_by="bot")
        self.assertEqual(await self.crm.client_balance(self.client_id), D("-3000.00"))
        self.assertEqual((await self.crm.rental(rental_id))["billed_until"],
                         today + timedelta(days=14))

    async def test_card_is_dropped_after_three_refusals(self):
        """«Три отказа подряд снимают карту» было написано в коде, но
        считать их было нечем: просроченная карта получала отказ каждые
        сутки и каждые сутки писала об этом клиенту."""
        await self.seed()
        card_id = await self.crm.save_card_token(
            client_id=self.client_id, token="tok", mask="4477", expires="12/28")
        self.assertEqual(await self.crm.card_failed(card_id, limit=3), 1)
        self.assertEqual(await self.crm.card_failed(card_id, limit=3), 2)
        self.assertTrue(await self.crm.cards(), "на втором отказе карта ещё жива")
        self.assertEqual(await self.crm.card_failed(card_id, limit=3), 3)
        self.assertEqual(await self.crm.cards(), [], "на третьем снята")
        # Удачное списание обнуляет счётчик: отказы считаются подряд идущими.
        await self.pool.execute(
            "update crm.card_tokens set active = true where id = $1", card_id)
        await self.crm.touch_card(card_id)
        self.assertEqual(await self.crm.card_failed(card_id, limit=3), 1)

    async def test_tracker_commands_on_postgres(self):
        """Очередь команд: одна в ожидании на трекер, ответ ложится на команду."""
        await self.seed()
        tracker_id = await self.crm.create_tracker(device_id="1001", alias="T",
                                                   bike_id=self.bike_id)
        cid = await self.crm.queue_tracker_command(tracker_id=tracker_id,
                                                   command="block", by="admin")
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.crm.queue_tracker_command(tracker_id=tracker_id,
                                                 command="unblock", by="admin")
        pending = await self.crm.pending_tracker_commands()
        self.assertEqual([(p["id"], p["device_id"], p["bike_id"]) for p in pending],
                         [(cid, "1001", self.bike_id)])
        await self.crm.finish_tracker_command(cid, ok=True, result="ok")
        self.assertIsNone(await self.crm.pending_command_of(tracker_id))
        await self.crm.update_tracker(tracker_id, blocked=True,
                                      blocked_at=datetime.now(UTC), blocked_by="admin")
        self.assertTrue((await self.crm.tracker(tracker_id))["blocked"])
        await self.crm.queue_tracker_command(tracker_id=tracker_id, command="unblock",
                                             by="admin")
        rows = await self.crm.tracker_commands(tracker_id)
        self.assertEqual([r["command"] for r in rows], ["unblock", "block"])
        await self.crm.raise_alert(tracker_id=tracker_id, kind="moving", note=None,
                                   bike_id=self.bike_id, lat=None, lon=None,
                                   level="urgent")
        self.assertTrue((await self.crm.tracker_alerts())[0]["tracker_blocked"])

    # ───────────── сверка кода: то, что нашли агенты ─────────────

    async def test_one_open_order_index_knows_approve(self):
        """«На согласовании» - открытый статус, и индекс обязан его знать:
        без этого на велосипеде, ждущем ответа по смете, открывался второй
        наряд, и два техника не знали друг о друге."""
        await self.seed()
        first = await self.crm.create_work_order(
            bike_id=self.bike_id, payer="client", client_id=self.client_id,
            complaint="тормоза", object_note=None, tech_id=None,
            estimate=D("0"), created_by="t")
        await self.crm.update_work_order(first, status="approve")
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.crm.create_work_order(
                bike_id=self.bike_id, payer="own", client_id=None,
                complaint="ещё и руль", object_note=None, tech_id=None,
                estimate=D("0"), created_by="t")

    async def test_disabled_tariff_is_not_resurrected_by_the_seed(self):
        """Сид цен опирался на частичный индекс `where active`, из которого
        выключенный тариф выпадает: следующий старт контейнера вставлял его
        заново, уже активным."""
        rows = await self.pool.fetch(
            "select id from crm.tariffs where kind = 'bike' and model = "
            "'Monster Truck + (Два АКБ)' and period_days = 7")
        self.assertEqual(len(rows), 1, "сид кладёт ровно одну строку")
        await self.pool.execute("update crm.tariffs set active = false where id = $1",
                                rows[0]["id"])
        await Database(self.pool).apply_schema(SCHEMA)
        again = await self.pool.fetch(
            "select id, active from crm.tariffs where kind = 'bike' and model = "
            "'Monster Truck + (Два АКБ)' and period_days = 7")
        self.assertEqual(len(again), 1, "перезапуск не вставил вторую строку")
        self.assertFalse(again[0]["active"], "выключил владелец - выключенным и остаётся")

    async def test_document_numbers_come_from_the_last_number(self):
        """Номер считался как count(*) + 1: удалённая смена сдвигала
        нумерацию, а две открытые в одну секунду получали один номер и
        падали на уникальном `no`."""
        first = await self.crm.create_shift(location="Павлюхина", opening=D(0),
                                            note=None, by="staff:admin")
        await self.crm.close_shift(first, counted=D(0), expected=D(0), note=None,
                                   by="staff:admin")
        second = await self.crm.create_shift(location="Павлюхина", opening=D(0),
                                             note=None, by="staff:admin")
        await self.pool.execute("delete from crm.cash_shifts where id = $1", first)
        third = await self.crm.create_shift(location="Адоратского", opening=D(0),
                                            note=None, by="staff:other")
        self.assertEqual((await self.crm.cash_shift(second))["no"], "КСМ-000002")
        self.assertEqual((await self.crm.cash_shift(third))["no"], "КСМ-000003",
                         "номер идёт от последнего, а не от количества строк")

    async def test_cash_belongs_to_one_shift_of_two(self):
        """Точек две, смены открыты одновременно: наличный платёж попадал
        в обе, и на второй точке закрытие писало недостачу как факт."""
        await self.seed()
        mine = await self.crm.create_shift(location="Павлюхина", opening=D(0),
                                           note=None, by="staff:admin")
        other = await self.crm.create_shift(location="Адоратского", opening=D(0),
                                            note=None, by="staff:other")
        shift = await self.crm.cash_shift_for("staff:admin")
        self.assertEqual(shift["id"], mine)
        await self.crm.add_ledger(client_id=self.client_id, kind="payment",
                                  amount=D("3000"), method="cash",
                                  created_by="staff:admin", shift_id=shift["id"])
        self.assertEqual([p["amount"] for p in await self.crm.shift_payments(mine)],
                         [D("3000.00")])
        self.assertEqual(await self.crm.shift_payments(other), [])

    async def test_bank_row_is_credited_once(self):
        """Автозачисление и оператор видели статус строки по словарю,
        прочитанному раньше, - в журнале оказывались две записи `payment`
        на одно поступление."""
        await self.seed()
        txn_id = await self.crm.save_bank_txn({
            "txn_id": "T-1", "booked_at": datetime.now(UTC), "amount": D("3000"),
            "direction": "credit", "purpose": "оплата АВ-1",
            "payer_name": "Иванов", "payer_inn": None, "payer_account": None})
        first = await self.crm.credit_bank_txn(
            txn_id, client_id=self.client_id, amount=D("3000"), method="transfer",
            note="Выписка банка: оплата", created_by="staff:admin")
        self.assertIsNotNone(first)
        second = await self.crm.credit_bank_txn(
            txn_id, client_id=self.client_id, amount=D("3000"), method="transfer",
            note="Выписка банка: оплата", created_by="staff:other")
        self.assertIsNone(second, "строку уже разобрали")
        rows = await self.crm.ledger_of(self.client_id, limit=10)
        self.assertEqual([r["kind"] for r in rows], ["payment"])
        self.assertEqual((await self.crm.bank_txn(txn_id))["ledger_id"], first)

    async def test_removing_a_line_returns_the_part_to_the_shelf(self):
        """Строка наряда помнит своё движение склада: убрали строку -
        запчасть вернулась, иначе на полке остаётся минус."""
        await self.seed()
        part_id = await self.crm.create_part(
            title="Камера", node="tube_tire", unit="шт", cost=D("300"),
            price=D("500"), min_stock=0, model=None, note=None)
        await self.crm.add_part_move(part_id=part_id, kind="receipt", qty=10,
                                     cost=D("300"), created_by="t")
        order_id = await self.crm.create_work_order(
            bike_id=self.bike_id, payer="own", client_id=None, complaint="прокол",
            object_note=None, tech_id=None, estimate=D("0"), created_by="t")
        order = await self.crm.work_order(order_id)
        part = await self.crm.part(part_id)
        result = await service.issue_part_to_order(self.crm, order, part, 5, by="t")
        self.assertEqual(await self.crm.part_stock(part_id), 5)
        self.assertTrue(await self.crm.delete_order_item(order_id, result["item_id"],
                                                         by="t"))
        self.assertEqual(await self.crm.part_stock(part_id), 10)
        self.assertEqual(await self.crm.order_items(order_id), [])

    async def test_order_closes_once_and_writes_one_repair(self):
        await self.seed()
        order_id = await self.crm.create_work_order(
            bike_id=self.bike_id, payer="own", client_id=None, complaint="стук",
            object_note=None, tech_id=None, estimate=D("0"), created_by="t")
        await self.crm.add_order_item(
            order_id, title="Перебрать каретку", node=None, work_type_id=None,
            qty=1, price=D("0"), parts_cost=D("1500"), labor_cost=D("2500"))
        order = await self.crm.work_order(order_id)
        await service.close_order(self.crm, order, by="t")
        with self.assertRaises(service.ServiceError):
            await service.close_order(self.crm, order, by="t")
        logs = [r for r in await self.crm.bike_log(self.bike_id)
                if r["kind"] == "repair"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["cost"], D("4000.00"),
                         "узел не выбран, но себестоимость ремонта в журнале есть")

    async def test_purchase_puts_the_batch_on_the_bench(self):
        """Партия по накладной заводится «на сборке», как и одиночный
        велосипед: недособранный в операционный парк не входит."""
        await service.buy_bikes(
            self.crm, supplier_id=None, purchased_on=date(2026, 9, 1),
            codes=["P-1", "P-2"], model="Kugoo V3", price=D("50000"),
            battery_count=2, service_months=36, residual=D("5000"),
            battery_price=D("9000"), battery_months=24, location=None,
            note=None, by="t")
        for code in ("P-1", "P-2"):
            self.assertEqual((await self.crm.bike_by_code(code))["status"], "new")
        await self.crm.set_setting("bike_check_required", "0", by="t")
        await service.buy_bikes(
            self.crm, supplier_id=None, purchased_on=date(2026, 9, 1),
            codes=["P-3"], model="Kugoo V3", price=D("50000"),
            battery_count=2, service_months=36, residual=D("5000"),
            battery_price=D("9000"), battery_months=24, location=None,
            note=None, by="t")
        self.assertEqual((await self.crm.bike_by_code("P-3"))["status"], "available")

    # ─── акции: индексы живут в базе, проверяем их там ───

    async def make(self, **over):
        fields = {"kind": "promocode", "title": "Весна", "percent": 10, "amount": None,
                  "code": "ВЕСНА", "params": {}, "starts_on": None, "ends_on": None,
                  "max_uses": None, "once_per_client": True, "text": None,
                  "note": None, "by": "t"}
        fields.update(over)
        return await self.crm.create_promo(**fields)

    async def test_one_live_code_per_word(self):
        first = await self.make()
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.make(title="Дубль", code="весна")
        await self.crm.update_promo(first, active=False)
        second = await self.make(title="Снова")
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.crm.update_promo(first, active=True)
        got = await self.crm.promo_by_code("весна")
        self.assertEqual(got["id"], second)
        self.assertEqual(got["uses"], 0)
        self.assertEqual(got["total"], D(0))

    async def test_discount_once_per_period_and_never_a_payment(self):
        await self.seed()
        await self.make(kind="season", code=None, once_per_client=False)
        applied: list[dict] = []
        rid = await service.open_rental(
            self.crm, client=await self.crm.client(self.client_id),
            bike=await self.crm.bike(self.bike_id),
            tariff=await self.crm.tariff(self.tariff_id),
            started_on=date.today(), contract_no="АВ-1", by="t", applied=applied)
        self.assertEqual(len(applied), 1)
        self.assertEqual((await self.crm.rental(rid))["balance"], D("-2700.00"))
        # повтор того же периода упирается в индекс начисления: вся
        # транзакция откатывается, ни баллов, ни строки журнала
        promo = (await self.crm.promos())[0]
        again = await self.crm.charge_period(
            rid, self.client_id, period_from=date.today(),
            period_to=date.today() + timedelta(days=7), amount=D(-3000), note="повтор",
            bonus={"promo_id": promo["id"], "amount": D(300), "note": "повтор", "by": "t"})
        self.assertFalse(again)
        self.assertEqual((await self.crm.rental(rid))["balance"], D("-2700.00"))
        rows = await self.crm.ledger_of(self.client_id)
        self.assertEqual(sorted(r["kind"] for r in rows), ["bonus", "charge"])
        bonus = next(r for r in rows if r["kind"] == "bonus")
        self.assertEqual(bonus["period_from"], date.today())
        self.assertEqual(bonus["rental_id"], rid)
        # jsonb хранит объект, а не строку: кодек пула кодирует сам
        self.assertEqual(await self.pool.fetchval(
            "select jsonb_typeof(params) from crm.promos where id = $1", promo["id"]),
            "object")
        self.assertEqual(promo["params"], {})
        self.assertEqual(await self.crm.rental_revenue(
            datetime.now(UTC) - timedelta(days=1), datetime.now(UTC)), D(0),
            "скидка в выручку среднего чека не попала")
        grants = await self.crm.bonuses(kind="promo")
        self.assertEqual(grants[0]["promo_title"], "Весна")
        self.assertEqual(await self.crm.promo_client_uses(self.client_id),
                         {grants[0]["promo_id"]: 1})
        self.assertEqual(await self.crm.rental_charge_count(rid), 1)

    async def test_limit_and_duplicate_are_decided_under_lock(self):
        """Предел применений считается в транзакции начисления под замком
        строки акции, а повтор скидки на период откатывает только её."""
        await self.seed()
        pid = await self.make(kind="season", code=None, once_per_client=False, max_uses=1)
        rid = await service.open_rental(
            self.crm, client=await self.crm.client(self.client_id),
            bike=await self.crm.bike(self.bike_id),
            tariff=await self.crm.tariff(self.tariff_id),
            started_on=date.today() - timedelta(days=8), contract_no=None, by="t")
        # два периода начислены, скидка одна: предел выбран на втором
        self.assertEqual((await self.crm.promo(pid))["uses"], 1)
        self.assertEqual((await self.crm.rental(rid))["balance"], D("-5700.00"))
        other = await self.crm.create_client(full_name="Петров Пётр",
                                             phone="+79990000002", tg_id=5002)
        bike2 = await self.crm.create_bike(code="B-2", model="Kugoo V3")
        rid2 = await self.crm.create_rental(
            client_id=other, bike_id=bike2, tariff_id=self.tariff_id, tariff_name="Неделя",
            period_days=7, price=D(3000), billing="auto", started_on=date.today(),
            contract_no=None, created_by="t")
        bonus = {"promo_id": pid, "amount": D(300), "note": "снимок", "by": "t"}
        self.assertTrue(await self.crm.charge_period(
            rid2, other, period_from=date.today(),
            period_to=date.today() + timedelta(days=7), amount=D(-3000), note="п",
            bonus=bonus))
        self.assertFalse(bonus["granted"])
        self.assertEqual((await self.crm.rental(rid2))["balance"], D("-3000.00"))
        # повтор скидки на период при новом начислении: начисление есть,
        # второй скидки нет, транзакция не развалилась
        await self.crm.update_promo(pid, max_uses=None)
        await self.pool.execute(
            "insert into crm.bonuses (client_id, kind, amount, promo_id, rental_id, "
            "period_from) values ($1, 'promo', 1, $2, $3, $4)",
            other, pid, rid2, date.today() + timedelta(days=7))
        bonus = {"promo_id": pid, "amount": D(300), "note": "повтор", "by": "t"}
        self.assertTrue(await self.crm.charge_period(
            rid2, other, period_from=date.today() + timedelta(days=7),
            period_to=date.today() + timedelta(days=14), amount=D(-3000), note="п",
            bonus=bonus))
        self.assertFalse(bonus["granted"])
        self.assertEqual((await self.crm.rental(rid2))["balance"], D("-6000.00"))

    async def test_promo_code_lives_on_the_rental(self):
        await self.seed()
        await self.make()
        rid = await service.open_rental(
            self.crm, client=await self.crm.client(self.client_id),
            bike=await self.crm.bike(self.bike_id),
            tariff=await self.crm.tariff(self.tariff_id),
            started_on=date.today() + timedelta(days=3), contract_no=None, by="t",
            promo_code="весна")
        self.assertEqual((await self.crm.rental(rid))["promo_code"], "ВЕСНА")
        self.assertEqual(await self.crm.bonuses(kind="promo"), [])
        applied: list[dict] = []
        await service.charge_all(self.crm, today=date.today() + timedelta(days=3),
                                 applied=applied)
        self.assertEqual([a["rental_id"] for a in applied], [rid])

    # ─── заявки на аренду ───

    async def test_one_open_booking_per_client(self):
        await self.seed()
        # Точки приезжают сидом схемы: берём первую, а не заводим свою.
        loc = (await self.crm.locations())[0]["id"]
        first = await self.crm.create_booking(
            client_id=self.client_id, model="Kugoo V3", tariff_id=self.tariff_id,
            location_id=loc, wanted_on=date.today())
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.crm.create_booking(
                client_id=self.client_id, model="Kugoo V3", tariff_id=None,
                location_id=None, wanted_on=date.today())
        row = await self.crm.open_booking_of(self.client_id)
        self.assertEqual(row["id"], first)
        self.assertEqual(row["tariff_name"], "Неделя")
        self.assertTrue(row["location_title"])
        await self.crm.update_booking(first, status="cancelled", handled_by="t",
                                      handled_at=datetime.now(UTC))
        self.assertIsNone(await self.crm.open_booking_of(self.client_id))
        second = await self.crm.create_booking(
            client_id=self.client_id, model=None, tariff_id=None, location_id=None,
            wanted_on=date.today() + timedelta(days=1))
        rows = await self.crm.bookings()
        self.assertEqual([r["id"] for r in rows], [second, first], "открытые первыми")
        with self.assertRaises(ValueError):
            await self.crm.update_booking(second, client_id=1)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_PG, "pgserver или asyncpg не установлены")
class TestOpsAndFixesOnPostgres(unittest.IsolatedAsyncioTestCase):
    """Запросы, которые FakeCrm только имитирует: поиск по номеру с
    кириллицей, журнал группы, стоп-лист, перепроверка счетов.

    Обвязка - та же, что у TestCrmOnPostgres, но без наследования: иначе
    все его тесты прогонялись бы второй раз."""

    setUpClass = classmethod(TestCrmOnPostgres.setUpClass.__func__)
    tearDownClass = classmethod(TestCrmOnPostgres.tearDownClass.__func__)
    asyncSetUp = TestCrmOnPostgres.asyncSetUp
    asyncTearDown = TestCrmOnPostgres.asyncTearDown
    seed = TestCrmOnPostgres.seed

    async def test_bike_by_vin_normalises_like_logic(self):
        await self.seed()
        await self.crm.update_bike(self.bike_id, motor_no="60V240W 2305-001")
        other = await self.crm.create_bike(code="B-2", model="Kugoo V3",
                                           frame_no="LXR99", motor_no="60v240w2305999")
        # Кириллические «В» и «С» с телефона, пробелы и регистр.
        self.assertEqual((await self.crm.bike_by_vin("60В240W2305001"))["id"], self.bike_id)
        self.assertEqual((await self.crm.bike_by_vin("60v240w 2305 999"))["id"], other)
        self.assertEqual((await self.crm.bike_by_vin("lxr99"))["id"], other)
        self.assertEqual((await self.crm.bike_by_vin("b-2"))["id"], other, "по коду")
        self.assertIsNone(await self.crm.bike_by_vin("6"), "один знак - не номер")
        self.assertIsNone(await self.crm.bike_by_vin("60V"), "только точное совпадение")
        self.assertIsNone(await self.crm.bike_by_vin("NOPE1234"))

    async def test_ops_report_is_one_per_message(self):
        await self.seed()
        base = dict(kind="fix", chat_id=-100, message_id=7, thread_id=7, author_tg=1,
                    author="@p", bike_id=self.bike_id, rental_id=None,
                    client_id=self.client_id, data={"fio": "Иванов"}, ok=False,
                    note="нет аренды")
        first = await self.crm.save_ops_report(**base)
        again = await self.crm.save_ops_report(**{**base, "ok": True, "note": None})
        self.assertEqual(first, again)
        [row] = await self.crm.ops_reports()
        self.assertTrue(row["ok"])
        self.assertEqual(row["data"], {"fio": "Иванов"})
        self.assertEqual(row["bike_code"], "B-1")
        self.assertEqual(await self.crm.ops_reports(ok=False), [])

    async def test_flagged_clients_and_rental_by_bike(self):
        await self.seed()
        await self.crm.update_client(self.client_id, status="blacklist")
        [row] = await self.crm.flagged_clients()
        self.assertEqual(row["id"], self.client_id)
        self.assertIsNone(await self.crm.active_rental_of_bike(self.bike_id))
        self.assertIsNone(await self.crm.last_rental_of_bike(self.bike_id))

    async def test_closed_links_are_rechecked_for_a_week(self):
        await self.seed()
        order_id = await self.crm.create_pay_order(
            client_id=self.client_id, rental_id=None, amount=D("100"),
            purpose="Аренда", created_by="t")
        await self.crm.set_pay_link(order_id, link="https://pay/1", operation_id="op-7")
        await self.crm.cancel_pay_order(order_id, by="оператор")
        self.assertEqual(await self.crm.open_pay_orders(), [],
                         "только что снят и проверен - полчаса не спрашиваем")
        await self.pool.execute("update crm.pay_orders set checked_at = now() - "
                                "interval '1 hour' where id = $1", order_id)
        self.assertEqual([o["id"] for o in await self.crm.open_pay_orders()], [order_id])
        await self.pool.execute("update crm.pay_orders set created_at = now() - "
                                "interval '9 days' where id = $1", order_id)
        self.assertEqual(await self.crm.open_pay_orders(), [], "неделя прошла")

    async def test_ignore_does_not_touch_a_credited_row(self):
        await self.seed()
        txn_id = await self.crm.save_bank_txn({
            "txn_id": "T-9", "booked_at": datetime.now(UTC), "amount": D("300"),
            "direction": "credit", "purpose": "оплата", "payer_name": "И",
            "payer_inn": None, "payer_account": None})
        await self.crm.credit_bank_txn(txn_id, client_id=self.client_id, amount=D("300"),
                                       method="transfer", note="x", created_by="t")
        self.assertFalse(await self.crm.mark_bank_txn(txn_id, status="ignored", by="t"))
        self.assertEqual((await self.crm.bank_txn(txn_id))["status"], "matched")

    async def test_closed_order_line_stays(self):
        await self.seed()
        order_id = await self.crm.create_work_order(
            bike_id=self.bike_id, payer="own", client_id=None, complaint="x",
            object_note=None, tech_id=None, estimate=D("0"), created_by="t")
        item_id = await self.crm.add_order_item(
            order_id, title="Работа", node=None, work_type_id=None, qty=1,
            price=D("0"), parts_cost=D("0"), labor_cost=D("100"))
        await self.crm.update_work_order(order_id, status="cancelled")
        self.assertFalse(await self.crm.delete_order_item(order_id, item_id))
        await self.crm.update_work_order(order_id, status="in_work")
        self.assertTrue(await self.crm.delete_order_item(order_id, item_id))

    async def test_tracker_phone_is_kept_when_starline_has_none(self):
        await self.seed()
        await self.crm.save_tracker_state({"device_id": "D1", "phone": "+79005554433"})
        await self.crm.save_tracker_state({"device_id": "D1", "phone": None})
        self.assertEqual((await self.crm.tracker_by_device("D1"))["phone"], "+79005554433")


# ─────────────────────────────── «Входящие» ───────────────────────────────

async def _inbox_story(crm) -> tuple[list, dict]:
    """Одна и та же история обращений - для базы и для FakeCrm.

    Возвращает только наблюдаемое: флаги, состояния, шифротексты и метки
    времени сценария. id и «сейчас» у двух реализаций свои, поэтому время
    вне сценария читается как «сейчас», а обращения - по (канал, адрес).
    """
    t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=5)
    t1, t2 = t0 + timedelta(hours=1), t0 + timedelta(hours=2)
    stamps = {t0: "t0", t1: "t1", t2: "t2"}

    def got(result):
        return result["created"], result["message_id"] is not None

    def stamp(value):
        return None if value is None else stamps.get(value, "сейчас")

    steps: list = []
    renter = await crm.create_client(full_name="Сидоров Сидор", phone="+79991112233",
                                     tg_id=7001)
    await crm.create_rental(client_id=renter, bike_id=None, tariff_id=None,
                            tariff_name="Неделя", period_days=7, price=D("3000"),
                            billing="manual", started_on=date.today(), contract_no=None,
                            created_by="t")
    booker = await crm.create_client(full_name="Петров Пётр", phone="+79994445566")
    await crm.create_booking(client_id=booker, model="Kugoo V3", tariff_id=None,
                             location_id=None, wanted_on=date.today())
    await crm.update_client(booker, max_id=8001)
    steps.append((await crm.client_by_max(8001))["id"] == booker)
    steps.append(await crm.client_by_max(8002))

    # Авито: вопрос, повтор опроса, второе сообщение и ответ из приложения
    avito = {"channel": "avito", "origin": "avito_api", "ext_id": "chat-1"}
    steps.append(got(await crm.inbox_record(
        **avito, direction="in", msg_id="a1", body_enc="e1", name="Пётр",
        subject="Kugoo V3", subject_url="https://avito.ru/kazan/1", at=t0)))
    steps.append(got(await crm.inbox_record(**avito, direction="in", msg_id="a1",
                                            body_enc="e1", at=t2)))
    steps.append(got(await crm.inbox_record(**avito, direction="in", msg_id="a2",
                                            kind="image", subject="Другое", at=t1)))
    steps.append(got(await crm.inbox_record(**avito, direction="out", msg_id="a3",
                                            body_enc="e3", author="avito-app", at=t2,
                                            announce=False)))
    # Telegram: клиент с арендой, вопрос и событие анкеты
    tg = await crm.inbox_record(channel="tg", origin="bot", ext_id="7001", direction="in",
                                body_enc="e4", client_id=renter, username="sid", at=t1)
    steps.append(got(tg))
    steps.append(got(await crm.inbox_record(channel="tg", origin="bot", ext_id="7001",
                                            direction="event", body_enc="ev", at=t2)))
    # MAX: вопрос в поддержку - без сигнала, у него своя карточка в чате
    mx = await crm.inbox_record(channel="max", origin="max_bot", ext_id="8001",
                                direction="in", client_id=booker, announce=False, at=t0)
    steps.append(got(mx))
    # WhatsApp: разобранное оживает от нового вопроса, спам остаётся спамом
    wa = {"channel": "wa", "origin": "hook", "ext_id": "+79990000009"}
    first = await crm.inbox_record(**wa, direction="in", phone="+79990000009", at=t0)
    await crm.update_inbox_thread(first["thread_id"], status="done",
                                  announced_at=datetime.now(UTC))
    steps.append(got(await crm.inbox_record(**wa, direction="in", msg_id="w2", at=t2)))
    spam = {"channel": "wa", "origin": "hook", "ext_id": "+79990000010"}
    first = await crm.inbox_record(**spam, direction="in", at=t1)
    await crm.update_inbox_thread(first["thread_id"], status="spam")
    steps.append(got(await crm.inbox_record(**spam, direction="in", at=t2)))

    # Очередь ответов: двойной клик, выдача, итог, перезапуск, повтор
    q1 = await crm.queue_inbox_reply(tg["thread_id"], body_enc="r1", author="staff:admin")
    steps.append(await crm.queue_inbox_reply(tg["thread_id"], body_enc="r1b",
                                             author="staff:admin"))
    q2 = await crm.queue_inbox_reply(mx["thread_id"], body_enc="r2", author="staff:admin")
    claimed = await crm.claim_inbox_out()
    steps.append((claimed["id"] == q1, claimed["status"], claimed["direction"],
                  claimed["channel"], claimed["origin"], claimed["thread_ext_id"],
                  claimed["client_id"] == renter, claimed["thread_status"],
                  claimed["body_enc"]))
    steps.append(await crm.finish_inbox_out(q1, ok=True, ext_id="tg-1"))
    steps.append(await crm.finish_inbox_out(q1, ok=True))
    steps.append((await crm.claim_inbox_out())["id"] == q2)
    steps.append(await crm.claim_inbox_out())
    steps.append(await crm.fail_stuck_inbox_out())
    again = await crm.inbox_retry(q2, author="staff:boss")
    steps.append(again is not None)
    steps.append(await crm.inbox_retry(q2, author="staff:boss"))
    steps.append(await crm.inbox_retry(q1, author="staff:boss"))
    steps.append((await crm.claim_inbox_out())["id"] == again)
    steps.append(await crm.finish_inbox_out(again, ok=False, error="нет связи; " * 80))
    try:
        await crm.update_inbox_thread(tg["thread_id"], channel="wa")
    except ValueError:
        steps.append("ValueError")

    threads = {}
    for t in await crm.inbox_threads(limit=50):
        threads[(t["channel"], t["ext_id"])] = {
            "origin": t["origin"], "status": t["status"], "name": t["name"],
            "username": t["username"], "phone": t["phone"], "subject": t["subject"],
            "subject_url": t["subject_url"], "client": t["client_name"],
            "client_phone": t["client_phone"], "renting": bool(t["renting"]),
            "booking_open": bool(t["booking_open"]), "waiting": stamp(t["waiting_since"]),
            "last_in": stamp(t["last_in_at"]), "answered": t["last_out_at"] is not None,
            "announced": t["announced_at"] is not None,
            "last": (t["last_direction"], t["last_kind"], t["last_body_enc"]),
            "messages": [(m["direction"], m["kind"], m["ext_id"], m["body_enc"],
                          m["author"], m["status"], m["error"], stamp(m["created_at"]),
                          m["sent_at"] is not None)
                         for m in await crm.inbox_messages(t["id"])],
        }
    picture = {"threads": threads, "open": await crm.inbox_open_count(),
               "announce": [(t["channel"], t["ext_id"])
                            for t in await crm.inbox_to_announce()]}
    return steps, picture


async def _inbox_purge_story(crm, age) -> tuple[int, dict]:
    """Чистка по сроку - для базы и FakeCrm. `age(messages, threads)`
    состаривает строки: у двух реализаций время хранится по-разному."""
    ids = {}
    for ext_id, status in (("done", "done"), ("spam", "spam"), ("new", "new"),
                           ("queue", "done"), ("send", "work"), ("touched", "done")):
        got = await crm.inbox_record(channel="wa", origin="hook", ext_id=ext_id,
                                     direction="in")
        await crm.update_inbox_thread(got["thread_id"], status=status)
        ids[ext_id] = got["thread_id"]
    sending = await crm.queue_inbox_reply(ids["send"], body_enc="s", author="staff:a")
    await crm.claim_inbox_out()
    queued = await crm.queue_inbox_reply(ids["queue"], body_enc="q", author="staff:a")
    fresh = await crm.inbox_record(channel="wa", origin="hook", ext_id="done",
                                   direction="in", msg_id="fresh")
    everything = [m["id"] for tid in ids.values() for m in await crm.inbox_messages(tid)]
    await age([m for m in everything if m != fresh["message_id"]] + [sending, queued],
              [tid for key, tid in ids.items() if key != "touched"])
    gone = await crm.purge_inbox(logic.INBOX_KEEP_DAYS)
    left = {}
    for t in await crm.inbox_threads(limit=50):
        left[t["ext_id"]] = sorted((m["direction"], m["status"] or "", m["ext_id"] or "")
                                   for m in await crm.inbox_messages(t["id"]))
    return gone, left


async def _inbox_rules_story(crm, age) -> tuple[list, dict]:
    """Правила обращения - для базы и для FakeCrm: чей чат, ручная
    карточка, ожидание после «разобрано», сигнал в чат, когда человек
    начинает ждать, застрявшая отправка и повтор той же строкой.

    `age(claimed, legacy, at)` состаривает отметку «взят в работу»: у
    claimed она становится at, у legacy её нет вовсе (строка из версии до
    колонки), а заведена строка в at. У двух реализаций время хранится
    по-разному."""
    t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=6)
    t1, t2, t3, t4 = (t0 + timedelta(hours=n) for n in range(1, 5))
    stamps = {t0: "t0", t1: "t1", t2: "t2", t3: "t3", t4: "t4"}

    def got(result):
        return None if result is None else (result["created"],
                                            result["message_id"] is not None)

    def stamp(value):
        return None if value is None else stamps.get(value, "сейчас")

    async def pending():
        return sorted((t["channel"], t["ext_id"]) for t in await crm.inbox_to_announce())

    async def seen():
        # Как круг сигналов: ушло в чат - отметка.
        for t in await crm.inbox_to_announce():
            await crm.update_inbox_thread(t["id"], announced_at=datetime.now(UTC))

    async def state(thread_id):
        t = await crm.inbox_thread(thread_id)
        return (t["status"], stamp(t["waiting_since"]), t["announced_at"] is None,
                t["client_name"], t["client_manual"])

    steps: list = []
    ivan = await crm.create_client(full_name="Иванов Иван", phone="+79990000000",
                                   tg_id=5001)
    maria = await crm.create_client(full_name="Иванова Мария", phone="+79991230000")

    # Чей чат: опрос Авито завёл - хук с тем же адресом не пишет ни вопрос,
    # ни «ответ», и наоборот; свой источник пишет как писал.
    avito = {"channel": "avito", "ext_id": "chat-1"}
    first = await crm.inbox_record(**avito, origin="avito_api", direction="in",
                                   msg_id="a1", body_enc="e1", name="Пётр", at=t0)
    steps.append(got(first))
    steps.append(got(await crm.inbox_record(
        **avito, origin="hook", direction="in", msg_id="h1", body_enc="подделка",
        name="Мошенник", phone="+79990000666", subject="Чужое", client_id=ivan, at=t1)))
    steps.append(got(await crm.inbox_record(**avito, origin="hook", direction="out",
                                            author="avito-app", announce=False)))
    gateway = {"channel": "avito", "ext_id": "gw-1"}
    steps.append(got(await crm.inbox_record(**gateway, origin="hook", direction="in",
                                            at=t0)))
    steps.append(got(await crm.inbox_record(**gateway, origin="avito_api", direction="in",
                                            msg_id="a9", name="Чужой", at=t1)))
    steps.append(got(await crm.inbox_record(**avito, origin="avito_api", direction="in",
                                            msg_id="a2", at=t1)))
    steps.append(await pending())
    await seen()

    # Чат начался нашим ответом из приложения Авито: сигнала нет, пока не
    # ответит клиент; его второе сообщение подряд - снова без сигнала.
    mine = {"channel": "avito", "origin": "avito_api", "ext_id": "chat-2"}
    own = await crm.inbox_record(**mine, direction="out", msg_id="o1", author="avito-app",
                                 announce=False)
    steps.append((await state(own["thread_id"]), await pending()))
    await crm.inbox_record(**mine, direction="in", msg_id="o2", at=t2)
    steps.append((await state(own["thread_id"]), await pending()))
    await seen()
    await crm.inbox_record(**mine, direction="in", msg_id="o3", at=t3)
    steps.append((await state(own["thread_id"]), await pending()))

    # WhatsApp с общего телефона: человек перепривязал к жене - следующее
    # сообщение Иванова не вернёт; отвязал - не привяжет снова.
    wa = {"channel": "wa", "origin": "hook", "ext_id": "+79990000000"}
    wid = (await crm.inbox_record(**wa, direction="in", phone="+79990000000",
                                  client_id=ivan, at=t0))["thread_id"]
    steps.append(await state(wid))
    await seen()
    await service.inbox_link_client(crm, await crm.inbox_thread(wid), str(maria),
                                    by="staff:admin")
    await crm.inbox_record(**wa, direction="in", msg_id="w2", client_id=ivan, at=t1)
    steps.append((await state(wid), await pending()))
    await service.inbox_link_client(crm, await crm.inbox_thread(wid), "", by="staff:admin")
    await crm.inbox_record(**wa, direction="in", msg_id="w3", client_id=ivan, at=t2)
    steps.append(await state(wid))

    # «Разобрано» снимает ожидание; новый вопрос ждёт с себя, а не с t0.
    await crm.update_inbox_thread(wid, status="done")
    steps.append((await state(wid), await crm.inbox_open_count()))
    await crm.inbox_record(**wa, direction="in", msg_id="w4", at=t3)
    steps.append((await state(wid), await pending()))
    await seen()
    # Ответ из панели снял ожидание: первое входящее после него - сигнал.
    reply = await crm.queue_inbox_reply(wid, body_enc="r1", author="staff:admin")
    await crm.claim_inbox_out()
    await crm.finish_inbox_out(reply, ok=True)
    steps.append(await state(wid))
    await crm.inbox_record(**wa, direction="in", msg_id="w5", at=t4)
    steps.append((await state(wid), await pending()))
    await seen()
    await crm.inbox_record(**wa, direction="in", msg_id="w6")
    steps.append(await pending())
    # Спам снимает ожидание и сигнала не просит.
    await crm.update_inbox_thread(wid, status="spam")
    steps.append(await state(wid))
    await crm.inbox_record(**wa, direction="in", msg_id="w7")
    steps.append(await pending())

    # Очередь: отметка взятия, застрявшее по сроку, повтор той же строкой.
    tg = (await crm.inbox_record(channel="tg", origin="bot", ext_id="5001", direction="in",
                                 client_id=ivan, at=t0))["thread_id"]
    mx = (await crm.inbox_record(channel="max", origin="max_bot", ext_id="8001",
                                 direction="in", announce=False, at=t0))["thread_id"]
    old = (await crm.inbox_record(channel="wa", origin="hook", ext_id="+79990000002",
                                  direction="in", at=t0))["thread_id"]
    q_tg = await crm.queue_inbox_reply(tg, body_enc="r-tg", author="staff:admin")
    q_mx = await crm.queue_inbox_reply(mx, body_enc="r-mx", author="staff:admin")
    q_old = await crm.queue_inbox_reply(old, body_enc="r-old", author="staff:admin")
    steps.append((await crm.inbox_messages(tg))[-1]["claimed_at"])
    claims = [await crm.claim_inbox_out() for _ in range(3)]
    steps.append([(c["id"] == q, c["status"], c["claimed_at"] is not None)
                  for c, q in zip(claims, (q_tg, q_mx, q_old), strict=True)])
    await age([q_tg], [q_old], t0)
    steps.append(await crm.fail_stuck_inbox_out(older_minutes=10))
    steps.append(await crm.fail_stuck_inbox_out(older_minutes=10))
    steps.append(await crm.queue_inbox_reply(mx, body_enc="r-mx2", author="staff:admin"))
    again = await crm.inbox_retry(q_tg, author="staff:boss")
    steps.append(again == q_tg)
    steps.append(await crm.queue_inbox_reply(tg, body_enc="r-tg2", author="staff:admin"))
    steps.append((await crm.claim_inbox_out())["id"] == q_tg)
    steps.append(await crm.finish_inbox_out(q_tg, ok=False, error="bot was blocked"))
    blocker = await crm.queue_inbox_reply(tg, body_enc="r-tg3", author="staff:admin")
    steps.append(blocker is not None)
    steps.append(await crm.inbox_retry(q_tg, author="staff:boss"))
    steps.append(await crm.inbox_retry(q_old, author="staff:boss") == q_old)
    steps.append(await crm.fail_stuck_inbox_out())

    threads = {}
    for t in await crm.inbox_threads(limit=50):
        threads[(t["channel"], t["ext_id"])] = {
            "origin": t["origin"], "status": t["status"], "name": t["name"],
            "phone": t["phone"], "subject": t["subject"], "client": t["client_name"],
            "manual": t["client_manual"], "waiting": stamp(t["waiting_since"]),
            "last_in": stamp(t["last_in_at"]), "announced": t["announced_at"] is not None,
            "messages": [(m["direction"], m["ext_id"], m["body_enc"], m["author"],
                          m["status"], m["error"], stamp(m["created_at"]),
                          m["sent_at"] is not None, m["claimed_at"] is not None)
                         for m in await crm.inbox_messages(t["id"])],
        }
    return steps, {"threads": threads, "open": await crm.inbox_open_count(),
                   "announce": await pending()}


@unittest.skipUnless(HAVE_PG, "pgserver или asyncpg не установлены")
class TestInboxOnPostgres(unittest.IsolatedAsyncioTestCase):
    """«Входящие» на настоящем Postgres: upsert обращения, уникальные
    индексы ленты и очереди, проверки схемы, выборка панели с джойнами и
    чистка по сроку - то, что FakeCrm только имитирует.

    Обвязка - та же, что у TestCrmOnPostgres, но без наследования: иначе
    все его тесты прогонялись бы второй раз."""

    maxDiff = None
    setUpClass = classmethod(TestCrmOnPostgres.setUpClass.__func__)
    tearDownClass = classmethod(TestCrmOnPostgres.tearDownClass.__func__)
    asyncSetUp = TestCrmOnPostgres.asyncSetUp
    asyncTearDown = TestCrmOnPostgres.asyncTearDown
    seed = TestCrmOnPostgres.seed

    async def say(self, ext_id="+79990000001", direction="in", **fields):
        """Сообщение из WhatsApp-хука - канал без ботов и опроса."""
        fields.setdefault("channel", "wa")
        fields.setdefault("origin", "hook")
        return await self.crm.inbox_record(ext_id=ext_id, direction=direction, **fields)

    async def test_event_signal_is_limited_to_one_per_12_hours(self):
        """«Нужен человек» (событие с сигналом) ожидания не ставит: без предела
        каждая новая тема кнопки давала бы новый сигнал в чат. Не чаще раза
        в 12 часов, пока человек не ждёт; и так же на заглушке."""
        from tests.fake_crm import FakeCrm

        async def story(crm, age_hours):
            got = await crm.inbox_record(channel="tg", origin="bot", ext_id="7001",
                                         direction="event", msg_id="anketa:1",
                                         announce=False)
            tid = got["thread_id"]
            await crm.inbox_record(channel="tg", origin="bot", ext_id="7001",
                                   direction="event", msg_id="faq:a", announce=True)
            first = [t["id"] for t in await crm.inbox_to_announce()]
            when = datetime.now(UTC) - timedelta(hours=age_hours)
            await crm.update_inbox_thread(tid, announced_at=when)
            await crm.inbox_record(channel="tg", origin="bot", ext_id="7001",
                                   direction="event", msg_id="faq:b", announce=True)
            second = [t["id"] for t in await crm.inbox_to_announce()]
            return first, second, tid

        for age, expect in ((1, False), (13, True)):
            with self.subTest(age=age):
                await self.pool.execute("delete from crm.inbox_threads")
                pg = await story(self.crm, age)
                fake = await story(FakeCrm(), age)
                for first, second, tid in (pg, fake):
                    self.assertEqual(first, [],
                                     "обращение уже отмечено анкетой - тема в пределах суток")
                    self.assertEqual(second == [tid], expect, (age, second))

    async def row(self, thread_id):
        """Строка обращения как есть - без джойнов выборки панели."""
        return dict(await self.pool.fetchrow(
            "select * from crm.inbox_threads where id = $1", thread_id))

    async def message(self, message_id):
        return dict(await self.pool.fetchrow(
            "select * from crm.inbox_messages where id = $1", message_id))

    async def count(self, table, where="true", *args):
        return await self.pool.fetchval(
            f"select count(*) from crm.{table} where {where}", *args)

    async def test_thread_is_one_per_contact_and_repeats_are_ignored(self):
        await self.seed()
        other = await self.crm.create_client(full_name="Второй", phone="+79995555555")
        t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=3)
        t1, t2 = t0 + timedelta(hours=1), t0 + timedelta(hours=2)
        first = await self.say(msg_id="m1", name="Иван", phone="+79990000001",
                               body_enc="x1", at=t0)
        self.assertTrue(first["created"])
        self.assertIsNotNone(first["message_id"])
        tid = first["thread_id"]
        row = await self.row(tid)
        self.assertEqual((row["status"], row["waiting_since"], row["last_in_at"]),
                         ("new", t0, t0))
        self.assertIsNone(row["announced_at"], "новое обращение ждёт сигнала в чат")
        self.assertIsNone(row["client_id"])
        self.assertEqual((await self.message(first["message_id"]))["created_at"], t0)

        # Второе «алло?»: то же обращение, пустое дополняется, ожидание не сдвигается.
        second = await self.say(msg_id="m2", username="ivan", client_id=self.client_id,
                                at=t1)
        self.assertEqual((second["thread_id"], second["created"]), (tid, False))
        self.assertIsNotNone(second["message_id"])
        row = await self.row(tid)
        self.assertEqual((row["name"], row["phone"]), ("Иван", "+79990000001"),
                         "пустое не затирает известное")
        self.assertEqual(row["username"], "ivan")
        self.assertEqual(row["client_id"], self.client_id)
        self.assertEqual((row["waiting_since"], row["last_in_at"]), (t0, t1))

        # Повтор доставки хука: второй строки нет, время обращения не двигается,
        # привязанную карточку повтор не подменяет.
        again = await self.say(msg_id="m1", client_id=other, at=t2)
        self.assertEqual(again, {"thread_id": tid, "created": False, "message_id": None})
        row = await self.row(tid)
        self.assertEqual((row["waiting_since"], row["last_in_at"]), (t0, t1))
        self.assertEqual(row["client_id"], self.client_id)

        # Опоздавшее сообщение не откатывает «последнее входящее».
        late = await self.say(msg_id="m0", at=t0 - timedelta(hours=1))
        self.assertIsNotNone(late["message_id"])
        row = await self.row(tid)
        self.assertEqual((row["waiting_since"], row["last_in_at"]), (t0, t1))

        # Ключ - (канал, адрес): тот же адрес в другом канале - другое
        # обращение, и номер сообщения уникален лишь внутри своего обращения.
        tg = await self.crm.inbox_record(channel="tg", origin="bot", ext_id="+79990000001",
                                         direction="in", msg_id="m1")
        self.assertTrue(tg["created"])
        self.assertNotEqual(tg["thread_id"], tid)
        self.assertIsNotNone(tg["message_id"])
        # Без номера сообщения повтор не распознать - пишутся оба.
        for _ in range(2):
            self.assertIsNotNone((await self.say(body_enc="без номера"))["message_id"])
        self.assertEqual(await self.count("inbox_threads"), 2)
        self.assertEqual(await self.count("inbox_messages", "thread_id = $1", tid), 5)

    async def test_reply_and_new_question_move_the_thread(self):
        t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
        tid = (await self.say(at=t0))["thread_id"]
        out = await self.say(direction="out", author="staff:admin", announce=False)
        msg = await self.message(out["message_id"])
        self.assertEqual((msg["direction"], msg["status"], msg["author"]),
                         ("out", "sent", "staff:admin"))
        self.assertIsNotNone(msg["sent_at"])
        row = await self.row(tid)
        self.assertEqual(row["status"], "work", "ответ берёт новое в работу")
        self.assertIsNone(row["waiting_since"])
        self.assertIsNotNone(row["last_out_at"])
        self.assertEqual(row["last_in_at"], t0, "ответ - не входящее")

        # Событие («анкета на проверке») ожидания не ставит и последним
        # сообщением в списке не считается.
        await self.say(direction="event", body_enc="анкета")
        self.assertIsNone((await self.row(tid))["waiting_since"])
        self.assertEqual((await self.crm.inbox_thread(tid))["last_direction"], "out")

        t1 = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
        await self.say(at=t1)
        row = await self.row(tid)
        self.assertEqual((row["status"], row["waiting_since"]), ("work", t1))

        # Ответ в разобранное его не открывает, новый вопрос - открывает
        # и снова просит сигнала в чат.
        await self.crm.update_inbox_thread(tid, status="done", announced_at=datetime.now(UTC))
        await self.say(direction="out", author="staff:admin", announce=False)
        self.assertEqual((await self.row(tid))["status"], "done")
        await self.say()
        row = await self.row(tid)
        self.assertEqual(row["status"], "new")
        self.assertIsNone(row["announced_at"])
        self.assertIsNotNone(row["waiting_since"])

        # Вопрос в поддержку (announce=False) открывает, но сигнал не повторяет.
        await self.crm.update_inbox_thread(tid, status="done", announced_at=datetime.now(UTC))
        await self.say(announce=False)
        row = await self.row(tid)
        self.assertEqual(row["status"], "new")
        self.assertIsNotNone(row["announced_at"])

        await self.crm.update_inbox_thread(tid, status="spam")
        await self.say()
        self.assertEqual((await self.row(tid))["status"], "spam", "спам остаётся спамом")

        # Чат начался с нашего ответа из приложения Авито: сигнала нет, в работе.
        mine = await self.crm.inbox_record(channel="avito", origin="avito_api",
                                           ext_id="chat-9", direction="out", msg_id="a1",
                                           author="avito-app", announce=False)
        self.assertTrue(mine["created"])
        row = await self.row(mine["thread_id"])
        self.assertEqual(row["status"], "work")
        self.assertIsNotNone(row["announced_at"])
        self.assertIsNone(row["waiting_since"])
        self.assertIsNone(row["last_in_at"])

    async def test_schema_rejects_what_the_code_never_writes(self):
        await self.seed()
        tid = (await self.say())["thread_id"]
        # Статус есть только у ответа, и только из четырёх.
        for direction, status in (("out", None), ("in", "sent"), ("event", "queued"),
                                  ("out", "delivered")):
            with self.subTest(direction=direction, status=status), \
                    self.assertRaises(asyncpg.CheckViolationError):
                await self.pool.execute(
                    "insert into crm.inbox_messages (thread_id, direction, status) "
                    "values ($1, $2, $3)", tid, direction, status)
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.say(kind="sticker")
        for bad in ({"channel": "sms"}, {"origin": "web"}, {"direction": "sideways"}):
            with self.subTest(**bad), self.assertRaises(asyncpg.CheckViolationError):
                await self.say(ext_id="+79990000077", **bad)
        self.assertEqual(await self.count("inbox_threads", "ext_id = $1", "+79990000077"), 0,
                         "обращение и сообщение - одна транзакция")
        self.assertEqual(await self.count("inbox_messages"), 1)
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.crm.update_inbox_thread(tid, status="closed")
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            await self.crm.queue_inbox_reply(tid + 1000, body_enc="x", author="staff:admin")
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.pool.execute("insert into crm.inbox_threads (channel, origin, ext_id) "
                                    "values ('wa', 'hook', '+79990000001')")

        # Удалённая карточка отвязывается, а не уносит переписку; удалённое
        # обращение уносит свою ленту.
        gone = await self.crm.create_client(full_name="Удалим", phone="+79995555555")
        await self.crm.update_inbox_thread(tid, client_id=gone)
        await self.pool.execute("delete from crm.clients where id = $1", gone)
        thread = await self.crm.inbox_thread(tid)
        self.assertIsNone(thread["client_id"])
        self.assertIsNone(thread["client_name"])
        await self.pool.execute("delete from crm.inbox_threads where id = $1", tid)
        self.assertEqual(await self.count("inbox_messages"), 0)

    async def test_one_reply_in_queue_and_the_claim_carries_the_address(self):
        await self.seed()
        tg = (await self.crm.inbox_record(channel="tg", origin="bot", ext_id="5001",
                                          direction="in", client_id=self.client_id)
              )["thread_id"]
        avito = (await self.crm.inbox_record(channel="avito", origin="avito_api",
                                             ext_id="chat-1", direction="in", msg_id="a1")
                 )["thread_id"]
        self.assertIsNone(await self.crm.claim_inbox_out(), "очередь пуста")
        q1 = await self.crm.queue_inbox_reply(tg, body_enc="enc-1", author="staff:admin")
        self.assertIsNotNone(q1)
        self.assertIsNone(await self.crm.queue_inbox_reply(tg, body_enc="enc-2",
                                                           author="staff:admin"),
                          "двойной клик не ставит второй ответ")
        q2 = await self.crm.queue_inbox_reply(avito, body_enc="enc-3", author="staff:admin")
        self.assertIsNotNone(q2, "очередь - одна на обращение, а не на всех")

        got = await self.crm.claim_inbox_out()
        self.assertEqual((got["id"], got["thread_id"], got["status"], got["direction"],
                          got["kind"], got["body_enc"], got["author"], got["sent_at"]),
                         (q1, tg, "sending", "out", "text", "enc-1", "staff:admin", None))
        self.assertEqual((got["channel"], got["origin"], got["thread_ext_id"],
                          got["client_id"], got["thread_status"]),
                         ("tg", "bot", "5001", self.client_id, "new"))
        self.assertEqual((await self.message(q1))["status"], "sending")
        self.assertIsNone(await self.crm.queue_inbox_reply(tg, body_enc="enc-2",
                                                           author="staff:admin"),
                          "пока отправляется - место тоже занято")
        got = await self.crm.claim_inbox_out()
        self.assertEqual((got["id"], got["channel"], got["origin"], got["thread_ext_id"],
                          got["client_id"]), (q2, "avito", "avito_api", "chat-1", None))
        self.assertIsNone(await self.crm.claim_inbox_out())

        # Ушло: ответ отправлен, обращение в работе, ожидание снято.
        self.assertTrue(await self.crm.finish_inbox_out(q1, ok=True))
        self.assertFalse(await self.crm.finish_inbox_out(q1, ok=False, error="поздно"),
                         "итог пишется один раз")
        msg = await self.message(q1)
        self.assertEqual((msg["status"], msg["error"], msg["ext_id"]), ("sent", None, None))
        self.assertIsNotNone(msg["sent_at"])
        row = await self.row(tg)
        self.assertEqual(row["status"], "work")
        self.assertIsNone(row["waiting_since"])
        self.assertIsNotNone(row["last_out_at"])

        # Не ушло: ошибка обрезана, обращение ждёт, как ждало.
        waiting = (await self.row(avito))["waiting_since"]
        self.assertIsNotNone(waiting)
        self.assertTrue(await self.crm.finish_inbox_out(q2, ok=False,
                                                        error="Авито: " + "x" * 600))
        msg = await self.message(q2)
        self.assertEqual(msg["status"], "failed")
        self.assertEqual(len(msg["error"]), 500)
        row = await self.row(avito)
        self.assertEqual((row["status"], row["waiting_since"], row["last_out_at"]),
                         ("new", waiting, None))

        # Авито вернул id ответа, опрос приносит его же - второй строки нет.
        q3 = await self.crm.queue_inbox_reply(avito, body_enc="enc-4", author="staff:admin")
        self.assertEqual((await self.crm.claim_inbox_out())["id"], q3)
        self.assertTrue(await self.crm.finish_inbox_out(q3, ok=True, ext_id="av-77"))
        echo = await self.crm.inbox_record(channel="avito", origin="avito_api",
                                           ext_id="chat-1", direction="out", msg_id="av-77",
                                           author="avito-app", announce=False)
        self.assertIsNone(echo["message_id"])
        self.assertEqual(await self.count("inbox_messages", "ext_id = 'av-77'"), 1)
        self.assertEqual((await self.row(avito))["status"], "work")
        self.assertIsNotNone(await self.crm.queue_inbox_reply(tg, body_enc="enc-5",
                                                              author="staff:admin"),
                             "ушедший ответ очередь не держит")

    async def test_two_workers_never_take_one_reply(self):
        queued = []
        for n in range(4):
            tid = (await self.say(ext_id=f"+7999000000{n}"))["thread_id"]
            queued.append(await self.crm.queue_inbox_reply(tid, body_enc=f"e{n}",
                                                           author="staff:admin"))
        got = await asyncio.gather(*(self.crm.claim_inbox_out() for _ in range(6)))
        self.assertEqual(sorted(g["id"] for g in got if g is not None), sorted(queued))
        self.assertEqual(sum(g is None for g in got), 2)
        self.assertEqual(await self.count("inbox_messages", "status = 'sending'"), 4)

    async def test_stuck_reply_is_failed_and_retry_requeues_the_same_row(self):
        a = await self.say(ext_id="+79990000001")
        b = (await self.say(ext_id="+79990000002"))["thread_id"]
        stuck = await self.crm.queue_inbox_reply(a["thread_id"], body_enc="enc-a",
                                                 author="staff:admin")
        self.assertEqual((await self.crm.claim_inbox_out())["id"], stuck)
        queued = await self.crm.queue_inbox_reply(b, body_enc="enc-b", author="staff:admin")
        self.assertEqual(await self.crm.fail_stuck_inbox_out(), 1)
        self.assertEqual(await self.crm.fail_stuck_inbox_out(), 0)
        msg = await self.message(stuck)
        self.assertEqual(msg["status"], "failed")
        self.assertIn("неизвестно, ушло ли", msg["error"])
        self.assertIsNotNone(msg["sent_at"])
        self.assertEqual((await self.message(queued))["status"], "queued",
                         "очередь при перезапуске не трогается")
        row = await self.row(a["thread_id"])
        self.assertIsNotNone(row["waiting_since"], "неизвестно, ушло ли, - ожидание не снято")
        self.assertIsNone(row["last_out_at"])

        # Повтор - та же строка обратно в очередь, а не копия: у копии
        # исходное «не ушло» оставалось бы с кнопкой, и второе нажатие после
        # удачного повтора слало бы человеку дубль.
        again = await self.crm.inbox_retry(stuck, author="staff:boss")
        self.assertEqual(again, stuck)
        new = await self.message(again)
        self.assertEqual((new["thread_id"], new["direction"], new["kind"], new["body_enc"],
                          new["author"], new["status"], new["error"], new["sent_at"],
                          new["claimed_at"]),
                         (a["thread_id"], "out", "text", "enc-a", "staff:boss", "queued",
                          None, None, None))
        self.assertEqual(await self.count("inbox_messages", "direction = 'out'"), 2,
                         "копии не появилось")
        self.assertIsNone(await self.crm.inbox_retry(stuck, author="staff:boss"),
                          "повтор уже в очереди")
        self.assertIsNone(await self.crm.inbox_retry(queued, author="staff:boss"),
                          "повторяется только не ушедший")
        self.assertIsNone(await self.crm.inbox_retry(a["message_id"], author="staff:boss"),
                          "входящее не повторяется")
        self.assertIsNone(await self.crm.inbox_retry(10 ** 9, author="staff:boss"))
        self.assertEqual(await self.count("inbox_messages", "status = 'queued'"), 2)
        # Очередь - по номеру строки, повтор старого ответа идёт первым;
        # пустая ошибка - NULL, а не пустая строка.
        self.assertEqual((await self.crm.claim_inbox_out())["id"], again)
        self.assertEqual((await self.crm.claim_inbox_out())["id"], queued)
        self.assertTrue(await self.crm.finish_inbox_out(again, ok=False, error=""))
        self.assertIsNone((await self.message(again))["error"])

    async def test_panel_list_with_and_without_a_card(self):
        await self.seed()
        await self.crm.create_rental(client_id=self.client_id, bike_id=self.bike_id,
                                     tariff_id=self.tariff_id, tariff_name="Неделя",
                                     period_days=7, price=D("3000"), billing="manual",
                                     started_on=date.today(), contract_no=None,
                                     created_by="t")
        await self.crm.create_booking(client_id=self.client_id, model="Kugoo V3",
                                      tariff_id=self.tariff_id, location_id=None,
                                      wanted_on=date.today())
        quiet = await self.crm.create_client(full_name="Без аренды", phone="+79995555555")
        t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=3)
        stranger = (await self.say(name="Незнакомец", kind="image", body_enc="s1",
                                   at=t0))["thread_id"]
        await self.say(direction="event", body_enc="ev")
        renter = (await self.crm.inbox_record(
            channel="tg", origin="bot", ext_id="5001", direction="in", body_enc="t1",
            client_id=self.client_id, at=t0 + timedelta(hours=1)))["thread_id"]
        await self.crm.inbox_record(channel="tg", origin="bot", ext_id="5001",
                                    direction="out", body_enc="r1", author="staff:admin",
                                    announce=False)
        other = (await self.crm.inbox_record(
            channel="max", origin="max_bot", ext_id="9001", direction="in", body_enc="q1",
            client_id=quiet, at=t0 + timedelta(hours=2)))["thread_id"]

        rows = await self.crm.inbox_threads()
        self.assertEqual([r["id"] for r in rows], [stranger, other, renter],
                         "дольше ждёт - выше, отвеченные в конце")
        by_id = {r["id"]: r for r in rows}
        row = by_id[stranger]
        self.assertEqual((row["name"], row["client_name"], row["client_phone"],
                          row["renting"], row["booking_open"]),
                         ("Незнакомец", None, None, False, False))
        self.assertEqual((row["last_direction"], row["last_kind"], row["last_body_enc"]),
                         ("in", "image", "s1"), "событие последним сообщением не считается")
        row = by_id[renter]
        self.assertEqual((row["client_name"], row["client_phone"], row["renting"],
                          row["booking_open"]),
                         ("Иванов Иван", "+79990000000", True, True))
        self.assertEqual((row["last_direction"], row["last_kind"], row["last_body_enc"]),
                         ("out", "text", "r1"))
        row = by_id[other]
        self.assertEqual((row["client_name"], row["renting"], row["booking_open"]),
                         ("Без аренды", False, False))
        for tid in (stranger, renter, other):
            self.assertEqual(await self.crm.inbox_thread(tid), by_id[tid])
        self.assertIsNone(await self.crm.inbox_thread(10 ** 9))

        async def ids(**kw):
            return [r["id"] for r in await self.crm.inbox_threads(**kw)]

        self.assertEqual(await ids(statuses=("new",)), [stranger, other])
        self.assertEqual(await ids(statuses=["work"]), [renter])
        self.assertEqual(await ids(statuses=("done", "spam")), [])
        self.assertEqual(await ids(channel="tg"), [renter])
        self.assertEqual(await ids(channel="avito"), [])
        self.assertEqual(await ids(statuses=("new", "work"), channel="max"), [other])
        self.assertEqual(await ids(limit=1), [stranger])

        messages = await self.crm.inbox_messages(stranger)
        self.assertEqual([(m["direction"], m["body_enc"]) for m in messages],
                         [("in", "s1"), ("event", "ev")])
        self.assertEqual([m["body_enc"] for m in await self.crm.inbox_messages(stranger, 1)],
                         ["ev"], "предел берёт последние, а не первые")

        # Лента вычищена сроком - строка списка без последнего сообщения.
        await self.pool.execute("delete from crm.inbox_messages where thread_id = $1", other)
        row = await self.crm.inbox_thread(other)
        self.assertEqual((row["last_direction"], row["last_kind"], row["last_body_enc"]),
                         (None, None, None))

    async def test_open_count_and_announce(self):
        waiting_new = (await self.say(ext_id="1"))["thread_id"]
        waiting_work = (await self.say(ext_id="2"))["thread_id"]
        await self.crm.update_inbox_thread(waiting_work, status="work")
        answered = (await self.say(ext_id="3"))["thread_id"]
        await self.say(ext_id="3", direction="out", author="staff:admin", announce=False)
        done = (await self.say(ext_id="4"))["thread_id"]
        await self.crm.update_inbox_thread(done, status="done")
        spam = (await self.say(ext_id="5"))["thread_id"]
        await self.crm.update_inbox_thread(spam, status="spam")
        support = (await self.say(ext_id="6", announce=False))["thread_id"]

        self.assertEqual(await self.crm.inbox_open_count(), 3,
                         "ждут ответа - только новые и в работе")
        announce = [t["id"] for t in await self.crm.inbox_to_announce()]
        self.assertEqual(announce, [waiting_new, waiting_work, answered, done, spam])
        self.assertNotIn(support, announce)
        self.assertEqual([t["id"] for t in await self.crm.inbox_to_announce(2)],
                         [waiting_new, waiting_work])
        await self.crm.update_inbox_thread(waiting_new, announced_at=datetime.now(UTC))
        self.assertEqual([t["id"] for t in await self.crm.inbox_to_announce(1)],
                         [waiting_work])

    async def test_thread_update_is_whitelisted(self):
        await self.seed()
        tid = (await self.say())["thread_id"]
        before = await self.row(tid)
        for bad in ({"channel": "tg"}, {"ext_id": "x"}, {"origin": "bot"},
                    {"waiting_since": None}, {"status": "done", "phone": "+7"},
                    {"note = null; drop table crm.ledger; --": 1}):
            with self.subTest(fields=sorted(bad)), self.assertRaises(ValueError):
                await self.crm.update_inbox_thread(tid, **bad)
        self.assertEqual(await self.row(tid), before, "отказ ничего не записал")
        self.assertEqual(await self.count("ledger"), 0)

        at = datetime.now(UTC).replace(microsecond=0)
        await self.crm.update_inbox_thread(
            tid, status="done", note="позвонил сам", client_id=self.client_id,
            handled_by="staff:admin", handled_at=at, ext_cursor="c-9", announced_at=at)
        row = await self.row(tid)
        self.assertEqual((row["status"], row["note"], row["client_id"], row["handled_by"],
                          row["handled_at"], row["ext_cursor"], row["announced_at"]),
                         ("done", "позвонил сам", self.client_id, "staff:admin", at, "c-9",
                          at))
        self.assertGreater(row["updated_at"], before["updated_at"])
        await self.crm.update_inbox_thread(10 ** 9, note="нет такого")

    async def test_purge_keeps_the_queue_and_drops_empty_closed_threads(self):
        old = datetime.now(UTC) - timedelta(days=logic.INBOX_KEEP_DAYS + 1)
        recent = datetime.now(UTC) - timedelta(days=logic.INBOX_KEEP_DAYS - 1)

        async def thread(ext_id, status, at=old):
            tid = (await self.say(ext_id=ext_id, at=at))["thread_id"]
            await self.crm.update_inbox_thread(tid, status=status)
            return tid

        done_old = await thread("done-old", "done")
        spam_old = await thread("spam-old", "spam")
        new_old = await thread("new-old", "new")
        done_touched = await thread("done-touched", "done")
        done_fresh = await thread("done-fresh", "done", recent)
        holds_queued = await thread("queued", "done")
        holds_sending = await thread("sending", "work")
        sent = (await self.say(ext_id="done-old", direction="out", author="staff:admin",
                               announce=False))["message_id"]
        failed = await self.crm.queue_inbox_reply(spam_old, body_enc="f", author="staff:a")
        await self.crm.claim_inbox_out()
        await self.crm.finish_inbox_out(failed, ok=False, error="нет")
        sending = await self.crm.queue_inbox_reply(holds_sending, body_enc="s",
                                                   author="staff:a")
        self.assertEqual((await self.crm.claim_inbox_out())["id"], sending)
        queued = await self.crm.queue_inbox_reply(holds_queued, body_enc="q",
                                                  author="staff:a")
        await self.pool.execute("update crm.inbox_messages set created_at = $1 "
                                "where id = any($2::bigint[])",
                                old, [sent, failed, sending, queued])
        # «Разобрали недавно» - только у одного; остальные трогали давно.
        await self.pool.execute("update crm.inbox_threads set updated_at = $1 "
                                "where id <> $2", old, done_touched)

        # Старше срока: шесть входящих (свежее остаётся), ушедший и не ушедший ответ.
        self.assertEqual(await self.crm.purge_inbox(logic.INBOX_KEEP_DAYS), 8)
        left = {r["thread_id"]: r["status"] for r in await self.pool.fetch(
            "select thread_id, status from crm.inbox_messages")}
        self.assertEqual(left, {done_fresh: None, holds_sending: "sending",
                                holds_queued: "queued"})
        threads = {r["id"] for r in await self.pool.fetch("select id from crm.inbox_threads")}
        self.assertEqual(threads, {new_old, done_touched, done_fresh, holds_queued,
                                   holds_sending},
                         "пустые разобранные и спам уходят, открытые и с очередью - нет")
        self.assertNotIn(done_old, threads)
        self.assertEqual(await self.crm.purge_inbox(logic.INBOX_KEEP_DAYS), 0)
        self.assertEqual(await self.count("inbox_threads"), 5)

    async def test_card_is_found_by_tg_max_and_phone(self):
        await self.seed()
        self.assertIsNone(await self.crm.client_by_max(777))
        await self.crm.update_client(self.client_id, max_id=777)
        self.assertEqual((await self.crm.client_by_max(777))["id"], self.client_id)
        self.assertIsNone(await self.crm.client_by_max(778))
        other = await self.crm.create_client(full_name="Второй", phone="+79995555555")
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.crm.update_client(other, max_id=777)

        for fields in ({"channel": "tg", "origin": "bot", "ext_id": 5001},
                       {"channel": "max", "origin": "max_bot", "ext_id": "777"},
                       {"channel": "wa", "origin": "hook", "ext_id": "+79990000000",
                        "phone": "8 (999) 000-00-00"}):
            with self.subTest(channel=fields["channel"]):
                got = await service.inbox_in(self.crm, None, text="Здравствуйте", **fields)
                thread = await self.crm.inbox_thread(got["thread_id"])
                self.assertEqual((thread["client_id"], thread["client_name"]),
                                 (self.client_id, "Иванов Иван"))
                self.assertIsNone(thread["last_body_enc"], "без ключа текста нет")
        got = await service.inbox_in(self.crm, None, channel="tg", origin="bot",
                                     ext_id="6001", text="Кто вы?")
        self.assertIsNone((await self.crm.inbox_thread(got["thread_id"]))["client_id"])

    async def test_inbox_never_touches_money_or_rentals(self):
        """Обращение - не клиент и не деньги: весь путь от вопроса до чистки
        не пишет в журнал, не создаёт аренд и не двигает технику."""
        await self.seed()
        await service.open_rental(
            self.crm, client=await self.crm.client(self.client_id),
            bike=await self.crm.bike(self.bike_id), tariff=await self.crm.tariff(self.tariff_id),
            started_on=date.today(), contract_no="АВ-1", by="test")
        snapshot = """
            select (select count(*) from crm.ledger) as ledger_rows,
                   (select coalesce(sum(amount), 0) from crm.ledger) as ledger_sum,
                   (select string_agg(r.status || ':' || r.billed_until, ',' order by r.id)
                      from crm.rentals r) as rentals,
                   (select count(*) from crm.bike_status_log) as status_log,
                   (select string_agg(b.status, ',' order by b.id) from crm.bikes b) as bikes,
                   (select count(*) from crm.bookings) as bookings,
                   (select count(*) from crm.clients) as clients
        """
        before = dict(await self.pool.fetchrow(snapshot))
        self.assertGreater(before["ledger_rows"], 0)

        vault = service.inbox_vault(generate_key())
        got = await service.inbox_in(self.crm, vault, channel="tg", origin="bot",
                                     ext_id="5001", text="Когда продлевать?", name="Иван")
        thread = await self.crm.inbox_thread(got["thread_id"])
        self.assertEqual(thread["client_id"], self.client_id)
        self.assertTrue(thread["renting"])
        self.assertEqual(service.inbox_open(vault, thread["last_body_enc"]),
                         "Когда продлевать?")
        mid = await service.inbox_reply(self.crm, vault, thread, "До пятницы",
                                        by="staff:admin", avito_ok=False)
        claimed = await self.crm.claim_inbox_out()
        self.assertEqual(claimed["id"], mid)
        self.assertEqual(service.inbox_open(vault, claimed["body_enc"]), "До пятницы")
        await self.crm.finish_inbox_out(mid, ok=False, error="бот заблокирован")
        await service.inbox_retry(self.crm, mid, by="staff:admin")
        await self.crm.claim_inbox_out()
        self.assertEqual(await self.crm.fail_stuck_inbox_out(), 1)
        thread = await self.crm.inbox_thread(got["thread_id"])
        await service.inbox_answered_elsewhere(self.crm, thread, by="staff:admin")
        await service.inbox_set_status(self.crm, thread, "done", note="продлит",
                                       by="staff:admin")
        await service.inbox_link_client(self.crm, thread, "", by="staff:admin")
        await service.inbox_link_client(self.crm, thread, "+7 999 000-00-00",
                                        by="staff:admin")
        await service.inbox_in(self.crm, vault, channel="wa", origin="hook",
                               ext_id="+79990000000", phone="+79990000000", text="Это Иван")
        await self.crm.update_inbox_thread(got["thread_id"], announced_at=datetime.now(UTC))
        await self.crm.purge_inbox(0)

        self.assertEqual(dict(await self.pool.fetchrow(snapshot)), before)
        self.assertEqual(await self.count("inbox_messages"), 0, "чистка с нулевым сроком")

    async def test_fake_crm_tells_the_same_story(self):
        """tests/fake_crm.py - опора тестов панели и бота; здесь его
        сверяют с настоящими запросами на одной истории."""
        fake_steps, fake_picture = await _inbox_story(FakeCrm())
        real_steps, real_picture = await _inbox_story(self.crm)
        self.assertEqual(fake_steps, real_steps)
        self.assertEqual(fake_picture, real_picture)

    async def test_fake_crm_purges_the_same_way(self):
        old = datetime.now(UTC) - timedelta(days=logic.INBOX_KEEP_DAYS + 1)
        fake = FakeCrm()

        async def age_fake(messages, threads):
            for mid in messages:
                fake.inbox_messages_[mid]["created_at"] = old
            for tid in threads:
                fake.inbox_threads_[tid]["updated_at"] = old

        async def age_real(messages, threads):
            await self.pool.execute("update crm.inbox_messages set created_at = $1 "
                                    "where id = any($2::bigint[])", old, messages)
            await self.pool.execute("update crm.inbox_threads set updated_at = $1 "
                                    "where id = any($2::bigint[])", old, threads)

        real = await _inbox_purge_story(self.crm, age_real)
        self.assertEqual(await _inbox_purge_story(fake, age_fake), real)
        gone, left = real
        self.assertEqual(gone, 6)
        self.assertEqual(left, {"done": [("in", "", "fresh")], "new": [],
                                "queue": [("out", "queued", "")],
                                "send": [("out", "sending", "")], "touched": []})

    # ─────────── правила после правки: чей чат, карточка, ожидание, очередь ───────────

    async def test_thread_belongs_to_its_origin(self):
        """Хук с каналом «avito» не пишет в чат, заведённый опросом Авито:
        утёкший токен хука подкладывал бы «слова клиента» в настоящий чат.
        Отказ - None, и ничего не тронуто: ни лента, ни имя, ни телефон."""
        await self.seed()
        t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=3)
        for channel, owner, stranger in (("avito", "avito_api", "hook"),
                                         ("avito", "hook", "avito_api"),
                                         ("tg", "bot", "hook"),
                                         ("max", "max_bot", "hook")):
            with self.subTest(channel=channel, owner=owner, stranger=stranger):
                ext = f"{channel}-{owner}"
                mine = await self.crm.inbox_record(
                    channel=channel, origin=owner, ext_id=ext, direction="in", msg_id="m1",
                    body_enc="e1", name="Пётр", subject="Kugoo V3",
                    subject_url="https://www.avito.ru/kazan/1", at=t0)
                tid = mine["thread_id"]
                # Разобрано и отмечено в чате: подделка вернула бы его в новые.
                await self.crm.update_inbox_thread(tid, status="done",
                                                   announced_at=datetime.now(UTC))
                before = await self.row(tid)
                messages = await self.count("inbox_messages", "thread_id = $1", tid)
                for direction in ("in", "out", "event"):
                    self.assertIsNone(await self.crm.inbox_record(
                        channel=channel, origin=stranger, ext_id=ext, direction=direction,
                        msg_id=f"x-{direction}", body_enc="подделка", author="avito-app",
                        name="Мошенник", username="fake", phone="+79990000666",
                        subject="Чужое", subject_url="https://www.avito.ru/kazan/666",
                        client_id=self.client_id, at=t0 + timedelta(hours=1)))
                self.assertEqual(await self.row(tid), before, "обращение не тронуто")
                self.assertEqual(await self.count("inbox_messages", "thread_id = $1", tid),
                                 messages, "в ленту ничего не легло")
                with self.assertRaises(service.ServiceError):
                    await service.inbox_in(self.crm, None, channel=channel, origin=stranger,
                                           ext_id=ext, text="Подделка",
                                           phone="+79990000000")
                self.assertEqual(await self.row(tid), before)
                # Свой источник пишет как писал.
                self.assertIsNotNone((await self.crm.inbox_record(
                    channel=channel, origin=owner, ext_id=ext, direction="in",
                    msg_id="m2"))["message_id"])
                self.assertEqual((await self.row(tid))["status"], "new")
        self.assertEqual(await self.count("inbox_threads"), 4, "второго обращения нет")

    async def test_manual_card_link_is_not_overwritten(self):
        """Карточку привязал или отвязал человек: автопривязка по общему
        телефону следующим сообщением её не переписывает."""
        await self.seed()
        wife = await self.crm.create_client(full_name="Иванова Мария", phone="+79991230000")
        wa = {"channel": "wa", "origin": "hook", "ext_id": "+79990000000",
              "phone": "+79990000000"}
        tid = (await service.inbox_in(self.crm, None, text="Здравствуйте", **wa))["thread_id"]
        row = await self.row(tid)
        self.assertEqual((row["client_id"], row["client_manual"]), (self.client_id, False))

        # Автопривязка и без флага не подменяет найденную раньше карточку.
        await self.crm.inbox_record(channel="wa", origin="hook", ext_id="+79990000000",
                                    direction="in", client_id=wife)
        self.assertEqual((await self.row(tid))["client_id"], self.client_id)

        # Пишет жена с его телефона - администратор перепривязал по номеру карточки.
        linked = await service.inbox_link_client(self.crm, await self.crm.inbox_thread(tid),
                                                 str(wife), by="staff:admin")
        self.assertEqual(linked["id"], wife)
        row = await self.row(tid)
        self.assertEqual((row["client_id"], row["client_manual"], row["handled_by"]),
                         (wife, True, "staff:admin"))
        await service.inbox_in(self.crm, None, text="Это снова я", **wa)
        self.assertEqual((await self.row(tid))["client_id"], wife)

        # Отвязал - следующее сообщение с того же телефона не привязывает снова.
        self.assertIsNone(await service.inbox_link_client(
            self.crm, await self.crm.inbox_thread(tid), "", by="staff:admin"))
        row = await self.row(tid)
        self.assertEqual((row["client_id"], row["client_manual"]), (None, True))
        await service.inbox_in(self.crm, None, text="Алло?", **wa)
        await self.crm.inbox_record(channel="wa", origin="hook", ext_id="+79990000000",
                                    direction="out", author="staff:admin", announce=False,
                                    client_id=self.client_id)
        thread = await self.crm.inbox_thread(tid)
        self.assertEqual((thread["client_id"], thread["client_name"], thread["renting"]),
                         (None, None, False))
        self.assertEqual(await self.count("inbox_messages", "thread_id = $1", tid), 5)

        # Вернуть карточку может только человек - телефоном.
        linked = await service.inbox_link_client(self.crm, await self.crm.inbox_thread(tid),
                                                 "+7 999 000-00-00", by="staff:admin")
        self.assertEqual(linked["id"], self.client_id)
        self.assertEqual((await self.row(tid))["client_id"], self.client_id)

        # Telegram: карточка по tg_id тоже не возвращается после отвязки.
        tg = (await service.inbox_in(self.crm, None, channel="tg", origin="bot",
                                     ext_id="5001", text="Вопрос"))["thread_id"]
        self.assertEqual((await self.row(tg))["client_id"], self.client_id)
        await service.inbox_link_client(self.crm, await self.crm.inbox_thread(tg), "",
                                        by="staff:admin")
        await service.inbox_in(self.crm, None, channel="tg", origin="bot", ext_id="5001",
                               text="Ещё вопрос")
        self.assertIsNone((await self.row(tg))["client_id"])

    async def test_done_and_spam_drop_waiting_and_reopen_waits_from_the_new_question(self):
        t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=6)
        t1, t2, t3 = t0 + timedelta(hours=1), t0 + timedelta(hours=2), t0 + timedelta(hours=3)
        tid = (await self.say(at=t0))["thread_id"]
        await self.crm.update_inbox_thread(tid, status="work", note="перезвоню")
        self.assertEqual((await self.row(tid))["waiting_since"], t0,
                         "«в работе» и заметка ожидание не снимают")

        # «Разобрано» из панели - с заметкой и автором - снимает ожидание.
        await service.inbox_set_status(self.crm, await self.crm.inbox_thread(tid), "done",
                                       note="позвонили", by="staff:admin")
        row = await self.row(tid)
        self.assertEqual((row["status"], row["waiting_since"], row["note"]),
                         ("done", None, "позвонили"))
        self.assertEqual(await self.crm.inbox_open_count(), 0)

        # Новый вопрос открывает обращение и ждёт с себя, а не с t0.
        await self.say(at=t1)
        row = await self.row(tid)
        self.assertEqual((row["status"], row["waiting_since"], row["last_in_at"]),
                         ("new", t1, t1))
        self.assertEqual([r["id"] for r in await self.crm.inbox_threads()], [tid])

        # Спам - тоже.
        await self.crm.update_inbox_thread(tid, status="spam")
        self.assertIsNone((await self.row(tid))["waiting_since"])

        # Разобранное из версии до правки хранило старое ожидание - новый
        # вопрос всё равно ждёт с себя.
        await self.pool.execute("update crm.inbox_threads set status = 'done', "
                                "waiting_since = $2 where id = $1", tid, t0)
        await self.say(at=t2)
        row = await self.row(tid)
        self.assertEqual((row["status"], row["waiting_since"]), ("new", t2))
        # Второе «алло?» ожидание не сдвигает.
        await self.say(at=t3)
        self.assertEqual((await self.row(tid))["waiting_since"], t2)

        # Ответ в разобранное ожидания не ставит и обращение не открывает.
        await self.crm.update_inbox_thread(tid, status="done")
        await self.say(direction="out", author="staff:admin", announce=False)
        row = await self.row(tid)
        self.assertEqual((row["status"], row["waiting_since"]), ("done", None))

    async def test_team_signal_when_someone_starts_waiting(self):
        """Сигнал в чат - когда человек начинает ждать: новое обращение,
        первое входящее после ответа, возврат из разобранных. Второе
        «алло?» подряд и спам сигнала не дают."""
        tid = (await self.say())["thread_id"]

        async def signal():
            return tid in [t["id"] for t in await self.crm.inbox_to_announce()]

        async def seen():
            await self.crm.update_inbox_thread(tid, announced_at=datetime.now(UTC))

        self.assertTrue(await signal(), "новое обращение")
        await seen()
        await self.say()
        self.assertFalse(await signal(), "второе «алло?» подряд")
        await self.crm.update_inbox_thread(tid, status="work")
        await self.say()
        self.assertFalse(await signal(), "взяли в работу, но не ответили - всё ещё ждёт")

        # Ответ из панели снял ожидание - следующий вопрос снова сигналит.
        reply = await self.crm.queue_inbox_reply(tid, body_enc="r1", author="staff:admin")
        await self.crm.claim_inbox_out()
        await self.crm.finish_inbox_out(reply, ok=True)
        await self.say()
        self.assertTrue(await signal(), "первое входящее после ответа")
        await seen()

        # Ответ не ушёл - человек ждёт, как ждал: сигнала нет.
        failed = await self.crm.queue_inbox_reply(tid, body_enc="r2", author="staff:admin")
        await self.crm.claim_inbox_out()
        await self.crm.finish_inbox_out(failed, ok=False, error="bot was blocked")
        await self.say()
        self.assertFalse(await signal())

        # «Ответил вне панели» - тоже ответ; вопрос в поддержку (announce=False)
        # сигнала не просит, у него своя карточка в чате.
        await service.inbox_answered_elsewhere(self.crm, await self.crm.inbox_thread(tid),
                                               by="staff:admin")
        await self.say(announce=False)
        self.assertFalse(await signal())
        await self.say()
        self.assertFalse(await signal(), "вопрос в поддержку уже поставил ожидание")

        # Событие без сигнала ожидания не ставит и сигнал не просит.
        await self.say(direction="out", author="staff:admin", announce=False)
        await self.say(direction="event", body_enc="анкета", announce=False)
        self.assertFalse(await signal())
        self.assertIsNone((await self.row(tid))["waiting_since"])

        # Возврат из разобранных - сигнал.
        await self.crm.update_inbox_thread(tid, status="done")
        await self.say()
        self.assertTrue(await signal(), "возврат из разобранных")
        await seen()

        # Спам: ни после ответа, ни после «разобрано» - никогда.
        await self.say(direction="out", author="staff:admin", announce=False)
        await self.crm.update_inbox_thread(tid, status="spam")
        await self.say()
        self.assertFalse(await signal())
        self.assertEqual((await self.row(tid))["status"], "spam")

    async def test_avito_chat_started_by_us_signals_when_the_client_answers(self):
        t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
        chat = {"channel": "avito", "origin": "avito_api", "ext_id": "chat-9"}
        mine = await self.crm.inbox_record(**chat, direction="out", msg_id="a1",
                                           author="avito-app", announce=False)
        tid = mine["thread_id"]
        self.assertEqual(await self.crm.inbox_to_announce(), [])
        await self.crm.inbox_record(**chat, direction="in", msg_id="a2", body_enc="e2",
                                    at=t0)
        row = await self.row(tid)
        self.assertEqual((row["status"], row["waiting_since"], row["announced_at"]),
                         ("work", t0, None))
        self.assertEqual([t["id"] for t in await self.crm.inbox_to_announce()], [tid])
        self.assertEqual(await self.crm.inbox_open_count(), 1)

    async def test_claim_stamps_the_time_and_stuck_fails_only_after_the_limit(self):
        tids = [(await self.say(ext_id=f"+7999000000{n}"))["thread_id"] for n in range(5)]
        queued = [await self.crm.queue_inbox_reply(t, body_enc=f"e{n}", author="staff:admin")
                  for n, t in enumerate(tids)]
        self.assertIsNone((await self.message(queued[0]))["claimed_at"], "в очереди не взят")
        got = await self.crm.claim_inbox_out()
        now = await self.pool.fetchval("select now()")
        self.assertEqual(got["id"], queued[0])
        self.assertIsNotNone(got["claimed_at"])
        self.assertLessEqual(got["claimed_at"], now)
        self.assertGreater(got["claimed_at"], now - timedelta(minutes=1))
        self.assertEqual((await self.message(queued[0]))["claimed_at"], got["claimed_at"])
        for _ in range(3):
            await self.crm.claim_inbox_out()
        long_stuck, fresh, legacy_old, legacy_new, waiting = queued

        # Взят полчаса назад; взят только что, хотя в очереди простоял час
        # (бот лежал); «отправляется» из версии без отметки - по времени
        # заведения; в очереди давно, но не взят - не трогается вовсе.
        hour_ago = now - timedelta(hours=1)
        await self.pool.execute("update crm.inbox_messages set claimed_at = $2 "
                                "where id = $1", long_stuck, now - timedelta(minutes=30))
        await self.pool.execute("update crm.inbox_messages set created_at = $2 "
                                "where id = any($1::bigint[])", [fresh, waiting], hour_ago)
        await self.pool.execute("update crm.inbox_messages set claimed_at = null, "
                                "created_at = $2 where id = $1", legacy_old, hour_ago)
        await self.pool.execute("update crm.inbox_messages set claimed_at = null "
                                "where id = $1", legacy_new)
        self.assertEqual(await self.crm.fail_stuck_inbox_out(older_minutes=10), 2)
        self.assertEqual(await self.crm.fail_stuck_inbox_out(older_minutes=10), 0)
        status = {r["id"]: r["status"] for r in await self.pool.fetch(
            "select id, status from crm.inbox_messages where direction = 'out'")}
        self.assertEqual(status, {long_stuck: "failed", fresh: "sending",
                                  legacy_old: "failed", legacy_new: "sending",
                                  waiting: "queued"})
        msg = await self.message(long_stuck)
        self.assertEqual(msg["error"], "неизвестно, ушло ли: отправка прервалась")
        self.assertIsNotNone(msg["sent_at"])
        row = await self.row(tids[0])
        self.assertIsNotNone(row["waiting_since"], "неизвестно, ушло ли, - ожидание не снято")
        self.assertIsNone(row["last_out_at"])

        # Очередь обращения больше не стоит: новый ответ ставится, а у
        # всё ещё отправляемого - нет.
        self.assertIsNotNone(await self.crm.queue_inbox_reply(tids[0], body_enc="e0b",
                                                              author="staff:admin"))
        self.assertIsNone(await self.crm.queue_inbox_reply(tids[1], body_enc="e1b",
                                                           author="staff:admin"))
        # Поздний итог застрявшего не переписывает «неизвестно, ушло ли».
        self.assertFalse(await self.crm.finish_inbox_out(long_stuck, ok=True))
        # Без предела (перезапуск процесса) - все «отправляется».
        self.assertEqual(await self.crm.fail_stuck_inbox_out(), 2)
        self.assertEqual(await self.count("inbox_messages", "status = 'sending'"), 0)
        self.assertEqual(await self.count("inbox_messages", "status = 'queued'"), 2)

    async def test_retry_waits_for_the_reply_in_flight(self):
        tid = (await self.say())["thread_id"]
        first = await self.crm.queue_inbox_reply(tid, body_enc="enc-1", author="staff:admin")
        await self.crm.claim_inbox_out()
        await self.crm.finish_inbox_out(first, ok=False, error="нет связи")
        before = await self.message(first)
        second = await self.crm.queue_inbox_reply(tid, body_enc="enc-2", author="staff:admin")
        self.assertIsNotNone(second, "не ушедший очередь не держит")

        # Пока в обращении есть ответ в очереди или в отправке - повтора нет,
        # и строка не тронута.
        self.assertIsNone(await self.crm.inbox_retry(first, author="staff:boss"))
        self.assertEqual((await self.crm.claim_inbox_out())["id"], second)
        self.assertIsNone(await self.crm.inbox_retry(first, author="staff:boss"))
        with self.assertRaises(service.ServiceError):
            await service.inbox_retry(self.crm, first, by="staff:boss")
        self.assertEqual(await self.message(first), before)

        # Ушло - теперь повтор ставит ту же строку, с новым автором и без итога.
        self.assertTrue(await self.crm.finish_inbox_out(second, ok=True))
        self.assertEqual(await service.inbox_retry(self.crm, first, by="staff:boss"), first)
        msg = await self.message(first)
        self.assertEqual((msg["status"], msg["author"], msg["error"], msg["sent_at"],
                          msg["claimed_at"], msg["body_enc"], msg["created_at"]),
                         ("queued", "staff:boss", None, None, None, "enc-1",
                          before["created_at"]))
        claimed = await self.crm.claim_inbox_out()
        self.assertEqual(claimed["id"], first)
        self.assertIsNotNone(claimed["claimed_at"], "взятие ставит отметку заново")
        self.assertEqual(await self.count("inbox_messages", "direction = 'out'"), 2)

        # Не ушедший без текста повторять нечем.
        await self.crm.finish_inbox_out(first, ok=True)
        blank = await self.pool.fetchval(
            "insert into crm.inbox_messages (thread_id, direction, status, error) "
            "values ($1, 'out', 'failed', 'нет ключа') returning id", tid)
        self.assertIsNone(await self.crm.inbox_retry(blank, author="staff:boss"))
        self.assertEqual((await self.message(blank))["status"], "failed")

    async def test_two_clicks_on_retry_queue_one_message(self):
        # Одна строка, два нажатия - одно в очереди.
        one = (await self.say(ext_id="+79990000001"))["thread_id"]
        mid = await self.crm.queue_inbox_reply(one, body_enc="e", author="staff:admin")
        await self.crm.claim_inbox_out()
        await self.crm.finish_inbox_out(mid, ok=False, error="нет")
        got = await asyncio.gather(*(self.crm.inbox_retry(mid, author=f"staff:{n}")
                                     for n in range(4)))
        self.assertEqual(sorted(got, key=lambda v: v is None), [mid, None, None, None])

        self.assertEqual((await self.crm.claim_inbox_out())["id"], mid)
        await self.crm.finish_inbox_out(mid, ok=True)

        # Два разных не ушедших одного обращения - в очередь встаёт один.
        two = (await self.say(ext_id="+79990000002"))["thread_id"]
        failed = []
        for n in range(2):
            m = await self.crm.queue_inbox_reply(two, body_enc=f"e{n}", author="staff:admin")
            await self.crm.claim_inbox_out()
            await self.crm.finish_inbox_out(m, ok=False, error="нет")
            failed.append(m)
        got = await asyncio.gather(*(self.crm.inbox_retry(m, author="staff:boss")
                                     for m in failed))
        self.assertEqual(sum(g is not None for g in got), 1)
        self.assertEqual(await self.count("inbox_messages",
                                          "thread_id = $1 and status = 'queued'", two), 1)
        self.assertEqual(await self.count("inbox_messages",
                                          "thread_id = $1 and status = 'failed'", two), 1)

    async def test_rows_from_before_the_fix_get_the_new_columns(self):
        """Прод до правки: колонок client_manual и claimed_at нет. Схема
        добавляет их поверх живых строк, и правила работают на старых."""
        await self.seed()
        await self.pool.execute("alter table crm.inbox_threads drop column client_manual; "
                                "alter table crm.inbox_messages drop column claimed_at")
        long_ago = datetime.now(UTC) - timedelta(hours=1)
        tid = await self.pool.fetchval(
            "insert into crm.inbox_threads (channel, origin, ext_id, client_id, status, "
            "waiting_since) values ('wa', 'hook', '+79990000000', $1, 'done', $2) "
            "returning id", self.client_id, long_ago)
        stuck = await self.pool.fetchval(
            "insert into crm.inbox_messages (thread_id, direction, body_enc, status, "
            "created_at) values ($1, 'out', 'e', 'sending', $2) returning id", tid, long_ago)
        await Database(self.pool).apply_schema(SCHEMA)

        row = await self.row(tid)
        self.assertIs(row["client_manual"], False)
        self.assertIsNone((await self.message(stuck))["claimed_at"])
        self.assertEqual(await self.crm.fail_stuck_inbox_out(older_minutes=10), 1)
        at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
        await self.say(ext_id="+79990000000", at=at)
        row = await self.row(tid)
        self.assertEqual((row["status"], row["waiting_since"], row["client_id"]),
                         ("new", at, self.client_id))
        await service.inbox_link_client(self.crm, await self.crm.inbox_thread(tid), "",
                                        by="staff:admin")
        await self.say(ext_id="+79990000000", client_id=self.client_id)
        self.assertIsNone((await self.row(tid))["client_id"])

    async def test_fake_crm_follows_the_same_rules(self):
        """Новые правила FakeCrm - те же, что у настоящих запросов."""
        fake = FakeCrm()

        async def age_fake(claimed, legacy, at):
            for mid in claimed:
                fake.inbox_messages_[mid]["claimed_at"] = at
            for mid in legacy:
                fake.inbox_messages_[mid].update(claimed_at=None, created_at=at)

        async def age_real(claimed, legacy, at):
            await self.pool.execute("update crm.inbox_messages set claimed_at = $2 "
                                    "where id = any($1::bigint[])", claimed, at)
            await self.pool.execute("update crm.inbox_messages set claimed_at = null, "
                                    "created_at = $2 where id = any($1::bigint[])",
                                    legacy, at)

        real_steps, real_picture = await _inbox_rules_story(self.crm, age_real)
        fake_steps, fake_picture = await _inbox_rules_story(fake, age_fake)
        self.assertEqual(fake_steps, real_steps)
        self.assertEqual(fake_picture, real_picture)

        # И сама история - та, что задумана, а не просто одинаковая.
        (created, forged_in, forged_out, gateway, gateway_poll, own_poll, first_pending,
         *rest) = real_steps
        self.assertEqual((created, forged_in, forged_out, gateway, gateway_poll, own_poll),
                         ((True, True), None, None, (True, True), None, (False, True)))
        self.assertEqual(first_pending, [("avito", "chat-1"), ("avito", "gw-1")])
        (started_by_us, client_answered, answered_again, wa_new, relinked, unlinked,
         done, reopened, replied, first_after_reply, second_after_reply, spam,
         after_spam, never_claimed, claims, stuck, stuck_again, busy, retried,
         busy_after_retry, reclaimed, refinished, blocker, refused, other_retried,
         restart) = rest
        self.assertEqual(started_by_us, (("work", None, False, None, False), []))
        self.assertEqual(client_answered, (("work", "t2", True, None, False),
                                           [("avito", "chat-2")]))
        self.assertEqual(answered_again, (("work", "t2", False, None, False), []))
        self.assertEqual(wa_new, ("new", "t0", True, "Иванов Иван", False))
        self.assertEqual(relinked, (("new", "t0", False, "Иванова Мария", True), []))
        self.assertEqual(unlinked, ("new", "t0", False, None, True))
        self.assertEqual(done, (("done", None, False, None, True), 3),
                         "ждут только два чата Авито и чат, начатый нами")
        self.assertEqual(reopened, (("new", "t3", True, None, True),
                                    [("wa", "+79990000000")]))
        self.assertEqual(replied, ("work", None, False, None, True))
        self.assertEqual(first_after_reply, (("work", "t4", True, None, True),
                                             [("wa", "+79990000000")]))
        self.assertEqual(second_after_reply, [])
        self.assertEqual(spam, ("spam", None, False, None, True))
        self.assertEqual(after_spam, [])
        self.assertIsNone(never_claimed)
        self.assertEqual(claims, [(True, "sending", True)] * 3)
        self.assertEqual((stuck, stuck_again, busy, retried, busy_after_retry),
                         (2, 0, None, True, None))
        self.assertEqual((reclaimed, refinished, blocker, refused, other_retried, restart),
                         (True, True, True, None, True, 1))
        threads = real_picture["threads"]
        self.assertEqual(threads[("avito", "chat-1")]["name"], "Пётр")
        self.assertIsNone(threads[("avito", "chat-1")]["phone"])
        self.assertEqual([m[1] for m in threads[("avito", "chat-1")]["messages"]],
                         ["a1", "a2"])
        self.assertEqual([m[1] for m in threads[("avito", "gw-1")]["messages"]], [None])
