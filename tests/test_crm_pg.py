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

        rows = logic.payback_rows(await self.crm.bikes(limit=100), money, days=30)
        row = next(r for r in rows if r["model"] == "Kugoo V3")
        self.assertEqual(row["paid"], D("3000.00"))
        self.assertEqual(row["repair_cost"], D("1200.00"))
        self.assertEqual(logic.payback_total(rows)["paid"], D("3000.00"))


if __name__ == "__main__":
    unittest.main()
