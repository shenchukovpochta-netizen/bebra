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
        self.assertIsNone(await self.crm.mark_pay_paid(invoice["id"],
                                                       method="card"),
                          "записи в журнале быть не должно")
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


if __name__ == "__main__":
    unittest.main()
