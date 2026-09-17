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
        self.assertEqual(rows[0]["kind"], "adjust")
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


if __name__ == "__main__":
    unittest.main()
