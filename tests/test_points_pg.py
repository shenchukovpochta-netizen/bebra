"""Точки на живом Postgres: журнал мест, точка аренды и наряда, каскад
переименования, касса по своей точке - и то же на FakeCrm.

Нужен pgserver, как в tests/test_crm_pg.py; без него набор пропускается.
Своя обвязка, а не наследование TestCrmOnPostgres: иначе все его тесты
прогонялись бы второй раз.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import asyncpg
    import pgserver

    from app import faq
    from app.crm import logic, points, service
    from app.crm.db import CrmDB
    from app.db import Database, _init_connection
    from tests.fake_crm import FakeCrm
    HAVE_PG = True
except ImportError:                                    # pragma: no cover
    HAVE_PG = False

D = Decimal
SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"
FLAGS = ("points_history_since", "rentals_location_filled", "work_orders_location_filled")


def moves(log):
    """Журнал мест от старых строк к новым: (откуда, куда, кто)."""
    return [(x["from_location"], x["to_location"], x["changed_by"])
            for x in reversed(log)]


@unittest.skipUnless(HAVE_PG, "pgserver или asyncpg не установлены")
class TestPointsOnPostgres(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Даты аренд - date.today(), а пояс сессии базы - из TZ: без
        # Москвы «сегодня» теста и базы расходились бы ночью по UTC.
        cls.tz_before = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Moscow"
        time.tzset()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()
        cls.tmp.cleanup()
        if cls.tz_before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = cls.tz_before
        time.tzset()

    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(self.pg.get_uri(), min_size=1, max_size=3,
                                              init=_init_connection)
        await self.pool.execute("drop schema if exists crm cascade; "
                                "drop schema if exists bot cascade")
        await Database(self.pool).apply_schema(SCHEMA)
        await Database(self.pool).apply_schema(SCHEMA)
        self.crm = CrmDB(self.pool)

    async def asyncTearDown(self):
        await self.pool.close()

    async def count(self, table, where="true", *args):
        return await self.pool.fetchval(
            f"select count(*) from crm.{table} where {where}", *args)

    async def client(self, n):
        return await self.crm.create_client(full_name=f"Клиент {n}",
                                            phone=f"+7999000000{n}")

    async def rent(self, client_id, bike_id, *, location=None, by="staff:op"):
        tariff = await self.crm.tariff(self.tariff_id)
        return await service.open_rental(
            self.crm, client=await self.crm.client(client_id),
            bike=await self.crm.bike(bike_id) if bike_id else None, tariff=tariff,
            started_on=date.today(), contract_no=None, by=by, billing="manual",
            location=location)

    async def seed(self):
        self.tariff_id = await self.crm.create_tariff("Неделя", 7, D("3000"), None)

    # ─── журнал мест ───

    async def test_trigger_logs_moves_not_edits(self):
        bike = await self.crm.create_bike(code="B-1", model="M", location="Павлюхина",
                                          by="staff:a")
        log = await self.crm.bike_location_log(bike)
        self.assertEqual(moves(log), [(None, "Павлюхина", "staff:a")])
        status = await self.crm.bike_status_log(bike)
        self.assertEqual(log[0]["changed_at"], status[0]["changed_at"],
                         "заведение: одно now() у обоих журналов")

        await self.crm.update_bike(bike, note="x", by="staff:b")
        await self.crm.update_bike(bike, location="Павлюхина", by="staff:b")
        await self.crm.update_bike(bike, status="repair", by="staff:b")
        self.assertEqual(len(await self.crm.bike_location_log(bike)), 1,
                         "правка без переезда и смена статуса журнал мест не пишут")

        await self.crm.update_bike(bike, location="Адоратского", status="available",
                                   by="staff:c")
        log = await self.crm.bike_location_log(bike)
        status = await self.crm.bike_status_log(bike)
        self.assertEqual(moves(log)[-1], ("Павлюхина", "Адоратского", "staff:c"))
        self.assertEqual(log[0]["changed_at"], status[0]["changed_at"],
                         "переезд и статус одним UPDATE - одно время")
        await self.crm.update_bike(bike, location=None, by="staff:d")
        self.assertEqual(moves(await self.crm.bike_location_log(bike))[-1],
                         ("Адоратского", None, "staff:d"))

        bare = await self.crm.create_bike(code="B-2", model="M")
        self.assertEqual(moves(await self.crm.bike_location_log(bare)),
                         [(None, None, None)], "история начинается и без точки")

    async def test_backfill_runs_once_and_is_idempotent(self):
        """База до точек: журнала мест, колонок и отметок нет. Первый старт
        восстанавливает историю и точки аренд и нарядов, второй - ничего."""
        await self.seed()
        b1 = await self.crm.create_bike(code="B-1", model="M", location="Павлюхина")
        b2 = await self.crm.create_bike(code="B-2", model="M", location="Адоратского")
        b3 = await self.crm.create_bike(code="B-3", model="M")
        b4 = await self.crm.create_bike(code="B-4", model="M", location="Павлюхина")
        c1, c2, c3 = await self.client(1), await self.client(2), await self.client(3)
        r1 = await self.rent(c1, b1)
        r2 = await self.rent(c2, b2)
        await service.close_rental(self.crm, await self.crm.rental(r2),
                                   closed_on=date.today(), note=None, by="staff:op")
        r3 = await self.rent(c3, b4)
        adoratskogo = next(x["id"] for x in await self.crm.locations()
                           if x["name"] == "Адоратского")
        booking = await self.crm.create_booking(client_id=c1, model="M", tariff_id=None,
                                                location_id=adoratskogo,
                                                wanted_on=date.today())
        await self.crm.update_booking(booking, status="done", rental_id=r1)
        o1 = await self.crm.create_work_order(bike_id=b3, payer="own", client_id=None,
                                              complaint="x", object_note=None,
                                              tech_id=None, estimate=D(0),
                                              created_by="t")
        o2 = await self.crm.create_work_order(bike_id=b2, payer="own", client_id=None,
                                              complaint="x", object_note=None,
                                              tech_id=None, estimate=D(0),
                                              created_by="t")
        o3 = await self.crm.create_work_order(bike_id=None, payer="client", client_id=c2,
                                              complaint="x", object_note="самокат",
                                              tech_id=None, estimate=D(0),
                                              created_by="t")
        # Как было до внедрения: ни журнала, ни колонок, ни отметок.
        await self.pool.execute(
            "drop trigger bikes_location_log on crm.bikes; "
            "drop function crm.log_bike_location(); "
            "drop table crm.bike_location_log; "
            "alter table crm.rentals drop column location; "
            "alter table crm.work_orders drop column location; "
            "alter table crm.staff drop column location")
        await self.pool.execute("delete from crm.settings where key = any($1::text[])",
                                list(FLAGS))
        await self.pool.execute(
            "update crm.bike_status_log set changed_at = changed_at - interval '40 days'")
        began = {r["bike_id"]: r["at"] for r in await self.pool.fetch(
            "select bike_id, min(changed_at) as at from crm.bike_status_log "
            "group by bike_id")}

        await Database(self.pool).apply_schema(SCHEMA)
        settings = await self.crm.settings()
        self.assertTrue(all(k in settings for k in FLAGS))
        since = datetime.fromisoformat(settings["points_history_since"])
        self.assertLess(abs((datetime.now(since.tzinfo) - since).total_seconds()), 120)

        # b1 - в идущей аренде из заявки на Адоратского: его история
        # начинается уже с точки аренды, а не с карточки.
        for bike, place in ((b1, "Адоратского"), (b2, "Адоратского"), (b3, None),
                            (b4, "Павлюхина")):
            first = (await self.crm.bike_location_log(bike))[-1]
            self.assertEqual((first["from_location"], first["to_location"]),
                             (None, place), bike)
            self.assertEqual(first["changed_at"], began[bike],
                             "история места начинается с истории статуса")
        # Точка аренды: заявки, иначе велосипеда. Велосипед идущей аренды
        # из заявки встаёт на её точку - и так с начала истории мест, а не
        # переездом в минуту внедрения.
        self.assertEqual((await self.crm.rental(r1))["location"], "Адоратского")
        self.assertEqual((await self.crm.rental(r2))["location"], "Адоратского")
        self.assertEqual((await self.crm.rental(r3))["location"], "Павлюхина")
        self.assertEqual((await self.crm.bike(b1))["location"], "Адоратского")
        self.assertEqual(moves(await self.crm.bike_location_log(b1)),
                         [(None, "Адоратского", None)])
        # Дни аренды до внедрения - на той же точке, что её деньги: иначе
        # в месяц внедрения у Адоратского были бы платежи без дней, а у
        # Павлюхина - дни без платежей.
        now = datetime.now().astimezone()
        days = await self.crm.bike_days_by_location(now - timedelta(days=39),
                                                    now - timedelta(days=1))
        self.assertAlmostEqual(float(days["Адоратского"]["rented"]), 38, delta=0.01)
        self.assertAlmostEqual(float(days["Павлюхина"]["rented"]), 38, delta=0.01,
                               msg="только b4 - своя аренда Павлюхина")
        self.assertEqual([(await self.crm.work_order(o))["location"] for o in (o1, o2, o3)],
                         [None, "Адоратского", None], "чужая техника без точки")
        self.assertEqual(await self.pool.fetchval(
            "select count(*) from information_schema.columns where table_schema = 'crm' "
            "and table_name = 'staff' and column_name = 'location'"), 1)

        # Второй старт ничего не меняет: ни строк журнала, ни ручных правок.
        logged = await self.count("bike_location_log")
        self.assertEqual(logged, 4)
        await self.pool.execute("update crm.rentals set location = null where id = $1", r2)
        await self.pool.execute("update crm.work_orders set location = null where id = $1",
                                o2)
        await Database(self.pool).apply_schema(SCHEMA)
        self.assertEqual(await self.count("bike_location_log"), logged)
        self.assertIsNone((await self.crm.rental(r2))["location"])
        self.assertIsNone((await self.crm.work_order(o2))["location"])
        self.assertEqual((await self.crm.settings())["points_history_since"],
                         settings["points_history_since"])
        # База, где точки уже внедрены, но отметку аренд сняли руками: у
        # велосипеда есть история мест, и её начало не переписывается -
        # переезд на точку аренды пишется честной строкой от «schema».
        await self.pool.execute("update crm.bikes set location = 'Павлюхина' where id = $1",
                                b1)
        await self.pool.execute("delete from crm.settings where key = 'rentals_location_filled'")
        await Database(self.pool).apply_schema(SCHEMA)
        self.assertEqual(moves(await self.crm.bike_location_log(b1)),
                         [(None, "Адоратского", None), ("Адоратского", "Павлюхина", None),
                          ("Павлюхина", "Адоратского", "schema")],
                         "строка есть - отметка «переименование» сброшена сразу")

    # ─── точка аренды: выдача, возврат, замена ───

    async def test_rental_point_on_issue(self):
        await self.seed()
        a = await self.crm.create_bike(code="A", model="M", location="Павлюхина")
        b = await self.crm.create_bike(code="B", model="M", location="Павлюхина")
        c = await self.crm.create_bike(code="C", model="M", location="Адоратского")
        c1, c2, c3, c4 = [await self.client(n) for n in (1, 2, 3, 4)]

        r1 = await self.rent(c1, a)
        self.assertEqual((await self.crm.rental(r1))["location"], "Павлюхина",
                         "не выбрана - точка велосипеда")
        self.assertEqual(len(await self.crm.bike_location_log(a)), 1, "переезда нет")

        r2 = await self.rent(c2, b, location="Адоратского")
        self.assertEqual((await self.crm.rental(r2))["location"], "Адоратского")
        self.assertEqual((await self.crm.bike(b))["location"], "Адоратского")
        log, status = (await self.crm.bike_location_log(b),
                       await self.crm.bike_status_log(b))
        self.assertEqual(moves(log)[-1], ("Павлюхина", "Адоратского", "staff:op"))
        self.assertEqual(status[0]["to_status"], "rented")
        self.assertEqual(log[0]["changed_at"], status[0]["changed_at"],
                         "точка и «в аренде» - одним UPDATE")

        r3 = await self.crm.start_rental_charged(
            client_id=c3, bike_id=c, tariff_name="t", period_days=7, price=D(3000),
            billing="manual", started_on=date.today(),
            period_to=date.today() + timedelta(days=7), contract_no=None, note="x",
            created_by="bot")
        self.assertEqual((await self.crm.rental(r3))["location"], "Адоратского")
        r4 = await self.crm.create_rental(
            client_id=c4, bike_id=None, tariff_id=None, tariff_name="t", period_days=7,
            price=D(1), billing="manual", started_on=date.today(), contract_no=None,
            created_by="t")
        self.assertIsNone((await self.crm.rental(r4))["location"])

        self.assertEqual({r["id"]: r["location"] for r in await self.crm.active_rentals()},
                         {r1: "Павлюхина", r2: "Адоратского", r3: "Адоратского",
                          r4: None})
        self.assertEqual({r["id"] for r in await self.crm.rentals(location="Адоратского")},
                         {r2, r3})
        self.assertEqual([r["id"] for r in await self.crm.rentals(location="none")], [r4])
        self.assertEqual((await self.crm.active_rental_of(c1))["location"], "Павлюхина")

    async def test_booking_point_is_the_issue_default(self):
        """Выдача по заявке без выбора точки идёт с точки заявки, а не с
        точки велосипеда: клиент сам назвал, куда придёт. Ключ - имя точки,
        вывеска (public_title) для этого не годится."""
        await self.seed()
        bike = await self.crm.create_bike(code="A", model="M", location="Павлюхина")
        c1 = await self.client(1)
        place = next(p for p in await self.crm.locations() if p["name"] == "Адоратского")
        booking_id = await self.crm.create_booking(
            client_id=c1, model="M", tariff_id=self.tariff_id, location_id=place["id"],
            wanted_on=date.today())
        booking = await self.crm.booking(booking_id)
        self.assertEqual(booking["location_name"], "Адоратского")
        self.assertEqual([b["location_name"] for b in await self.crm.bookings()],
                         ["Адоратского"])
        tariff = await self.crm.tariff(self.tariff_id)
        rental_id = await service.open_rental(
            self.crm, client=await self.crm.client(c1), bike=await self.crm.bike(bike),
            tariff=tariff, started_on=date.today(), contract_no=None, by="staff:op",
            billing="manual", booking=booking)
        self.assertEqual((await self.crm.rental(rental_id))["location"], "Адоратского")
        self.assertEqual((await self.crm.bike(bike))["location"], "Адоратского")

    async def test_bike_point_on_return(self):
        await self.seed()
        a = await self.crm.create_bike(code="A", model="M", location="Павлюхина")
        b = await self.crm.create_bike(code="B", model="M", location="Павлюхина")
        c1, c2 = await self.client(1), await self.client(2)
        r1 = await self.rent(c1, a)
        r2 = await self.rent(c2, b, location="Адоратского")

        await service.close_rental(self.crm, await self.crm.rental(r1),
                                   closed_on=date.today(), note=None, by="staff:ret",
                                   return_location="Адоратского")
        self.assertEqual((await self.crm.bike(a))["location"], "Адоратского")
        log, status = (await self.crm.bike_location_log(a),
                       await self.crm.bike_status_log(a))
        self.assertEqual(moves(log)[-1], ("Павлюхина", "Адоратского", "staff:ret"))
        self.assertEqual(log[0]["changed_at"], status[0]["changed_at"])
        self.assertEqual((await self.crm.rental(r1))["location"], "Павлюхина",
                         "точка выдачи - снимок, возврат её не трогает")

        # Точка возврата не указана - точка аренды, даже если карточку
        # велосипеда успели поправить в обход.
        await self.pool.execute("update crm.bikes set location = 'Горького' where id = $1", b)
        await service.close_rental(self.crm, await self.crm.rental(r2),
                                   closed_on=date.today(), note=None, by="staff:ret")
        self.assertEqual((await self.crm.bike(b))["location"], "Адоратского")

    async def test_bike_point_on_swap(self):
        await self.seed()
        old = await self.crm.create_bike(code="OLD", model="M", location="Адоратского")
        new = await self.crm.create_bike(code="NEW", model="M")
        third = await self.crm.create_bike(code="THIRD", model="M", location="Павлюхина")
        c1 = await self.client(1)
        rid = await self.rent(c1, old)

        await service.swap_bike(self.crm, await self.crm.rental(rid),
                                await self.crm.bike(new), reason="repair", by="staff:sw",
                                swap_location="Павлюхина")
        self.assertEqual((await self.crm.bike(old))["location"], "Павлюхина",
                         "снятый - там, где меняли")
        self.assertEqual((await self.crm.bike(new))["location"], "Адоратского",
                         "новый - на точке аренды")
        self.assertEqual((await self.crm.rental(rid))["location"], "Адоратского")
        log, status = (await self.crm.bike_location_log(new),
                       await self.crm.bike_status_log(new))
        self.assertEqual(log[0]["changed_at"], status[0]["changed_at"])

        await service.swap_bike(self.crm, await self.crm.rental(rid),
                                await self.crm.bike(third), reason="client", by="staff:sw")
        self.assertEqual((await self.crm.bike(new))["location"], "Адоратского",
                         "не сказано где - на точке аренды")
        self.assertEqual((await self.crm.bike(third))["location"], "Адоратского")

        # Выдача без велосипеда: точка появится с первым велосипедом.
        c2 = await self.client(2)
        spare = await self.crm.create_bike(code="SPARE", model="M", location="Павлюхина")
        bare = await self.crm.create_rental(
            client_id=c2, bike_id=None, tariff_id=None, tariff_name="t", period_days=7,
            price=D(1), billing="manual", started_on=date.today(), contract_no=None,
            created_by="t")
        await service.swap_bike(self.crm, await self.crm.rental(bare),
                                await self.crm.bike(spare), reason="other", by="staff:sw")
        self.assertEqual((await self.crm.rental(bare))["location"], "Павлюхина")
        self.assertEqual((await self.crm.bike(spare))["location"], "Павлюхина")

    async def test_swap_does_not_move_past_money_of_a_pointless_rental(self):
        """Аренда на велосипеде «не на точке» (бот, импорт, старая база)
        меняет велосипед на стоящий на Павлюхина. Точку аренде задним
        числом не дописываем: её прошлые платежи и выдача переехали бы на
        Павлюхина в закрытом месяце. Новый встаёт «не на точку» - туда же,
        где деньги аренды."""
        await self.seed()
        old = await self.crm.create_bike(code="OLD", model="M")
        new = await self.crm.create_bike(code="NEW", model="M", location="Павлюхина")
        cid = await self.client(1)
        rid = await self.rent(cid, old)
        self.assertIsNone((await self.crm.rental(rid))["location"])
        await self.crm.add_ledger(client_id=cid, kind="payment", amount=D(3000),
                                  rental_id=rid, method="sbp", created_by="staff:op")
        await self.shift_back(20)
        now = datetime.now().astimezone()
        since, until = now - timedelta(days=25), now - timedelta(days=10)
        money = await self.crm.money_by_location(since, until)
        issued = await self.crm.rentals_by_location(since, until)
        self.assertEqual(money[None]["paid"], D(3000))

        await service.swap_bike(self.crm, await self.crm.rental(rid),
                                await self.crm.bike(new), reason="repair", by="staff:sw")
        self.assertIsNone((await self.crm.rental(rid))["location"],
                          "точка аренды - снимок выдачи")
        self.assertEqual(await self.crm.money_by_location(since, until), money,
                         "прошлый месяц не переписан")
        self.assertEqual(await self.crm.rentals_by_location(since, until), issued)
        self.assertIsNone((await self.crm.bike(new))["location"],
                          "в аренде велосипед стоит на точке аренды")
        self.assertEqual(moves(await self.crm.bike_location_log(new))[-1],
                         ("Павлюхина", None, "staff:sw"))

        fake = FakeCrm()
        tariff = await fake.tariff(await fake.create_tariff("Неделя", 7, D("3000"), None))
        f_old = await fake.create_bike(code="OLD", model="M")
        f_new = await fake.create_bike(code="NEW", model="M", location="Павлюхина")
        f_cid = await fake.create_client(full_name="Клиент", phone="+79990000001")
        f_rid = await service.open_rental(
            fake, client=await fake.client(f_cid), bike=await fake.bike(f_old),
            tariff=tariff, started_on=date.today(), contract_no=None, by="staff:op",
            billing="manual")
        await service.swap_bike(fake, await fake.rental(f_rid), await fake.bike(f_new),
                                reason="repair", by="staff:sw")
        self.assertEqual(((await fake.rental(f_rid))["location"],
                          (await fake.bike(f_new))["location"]), (None, None),
                         "заглушка рассказывает то же")

    async def test_card_edit_cannot_move_a_bike_issued_meanwhile(self):
        """Карточку сохраняют, пока выдача держит строку велосипеда. UPDATE
        карточки ждёт замка и перечитывает строку: велосипед уже в аренде,
        его точка остаётся точкой аренды, остальные поля пишутся."""
        bike = await self.crm.create_bike(code="B-1", model="M", location="Павлюхина",
                                          status="available")
        issue = await self.pool.acquire()
        try:
            tx = issue.transaction()
            await tx.start()
            await issue.execute("update crm.bikes set status = 'rented', "
                                "location = 'Адоратского' where id = $1", bike)
            edit = asyncio.ensure_future(self.crm.update_bike(
                bike, note="смотрел", location="Павлюхина", keep_rented_location=True,
                by="staff:card"))
            await asyncio.sleep(0.3)
            self.assertFalse(edit.done(), "карточка ждёт замка строки")
            await tx.commit()
        finally:
            await self.pool.release(issue)
        self.assertEqual(await edit, {"status": "rented", "location": "Адоратского"})
        saved = await self.crm.bike(bike)
        self.assertEqual((saved["location"], saved["note"]), ("Адоратского", "смотрел"))
        self.assertNotIn("staff:card",
                         [x["changed_by"] for x in await self.crm.bike_location_log(bike)])

        free = await self.crm.create_bike(code="B-2", model="M", location="Павлюхина",
                                          status="available")
        self.assertEqual(await self.crm.update_bike(free, location="Адоратского",
                                                    keep_rented_location=True, by="x"),
                         {"status": "available", "location": "Адоратского"},
                         "свободный велосипед карточка двигает")

    async def test_found_lost_bike_comes_back_where_the_take_found_it(self):
        """Потерян на Павлюхина, найден пересчётом Адоратского - возвращается
        на Адоратского: строка журнала мест в тот же миг, что и статус.
        Пересчёт всего парка точки не знает и точку не стирает."""
        lost = await self.crm.create_bike(code="L-1", model="M", location="Павлюхина",
                                          status="available")
        other = await self.crm.create_bike(code="L-2", model="M", location="Павлюхина",
                                           status="available")
        await self.crm.create_bike(code="C-1", model="M", location="Адоратского",
                                   status="available")
        cell = await self.crm.create_battery(code="9510001", status="available",
                                             location="Павлюхина")
        for bike in (lost, other):
            await self.crm.update_bike(bike, status="lost", by="staff:t")
        await self.crm.update_battery(cell, status="lost", by="staff:t")

        take_id = await service.start_stock_take(
            self.crm, scope="location", location="Адоратского", note=None, what="all",
            by="staff:t")
        for code in ("L-1", "9510001"):
            got = await service.take_add_found(
                self.crm, await self.crm.stock_take(take_id), code)
            self.assertEqual(got["state"], "extra", code)
        result = await service.finish_stock_take(
            self.crm, await self.crm.stock_take(take_id), by="staff:take")
        self.assertEqual(result["returned"], 2)
        bike = await self.crm.bike(lost)
        self.assertEqual((bike["status"], bike["location"]), ("available", "Адоратского"))
        log, status = (await self.crm.bike_location_log(lost),
                       await self.crm.bike_status_log(lost))
        self.assertEqual(moves(log)[-1], ("Павлюхина", "Адоратского", "staff:take"))
        self.assertEqual(log[0]["changed_at"], status[0]["changed_at"],
                         "точка и статус - одним обновлением")
        battery = await self.crm.battery(cell)
        self.assertEqual((battery["status"], battery["location"]),
                         ("available", "Адоратского"))

        take_id = await service.start_stock_take(
            self.crm, scope="all", location=None, note=None, what="bikes", by="staff:t")
        await service.take_add_found(self.crm, await self.crm.stock_take(take_id), "L-2")
        await service.finish_stock_take(self.crm, await self.crm.stock_take(take_id),
                                        by="staff:take")
        bike = await self.crm.bike(other)
        self.assertEqual((bike["status"], bike["location"]), ("available", "Павлюхина"),
                         "пересчёт всего парка место не стирает")

    async def test_bot_keeps_the_gsk_directions_after_deploy(self):
        """До справочника бот говорил «Павлюхина, 97А — ГСК «Сокол», 9-й
        бокс». После внедрения ответ собирается из справочника, и без поля
        «как найти» курьер приехал бы к воротам кооператива, а не к боксу.
        Сид - один раз: стёртое владельцем поле не возвращается."""
        points.reset()
        try:
            await points.refresh(self.crm, force=True)
            for code in ("ADDR", "LEAD", "RETURN", "BRK_EL"):
                text = faq.answer(faq.BY_CODE[code], points=points.snapshot())
                self.assertIn("ГСК «Сокол», ищите 9-й бокс", text, code)
                self.assertIn("напишите, встретим", text, code)
        finally:
            points.reset()
        pav = next(x for x in await self.crm.locations() if x["name"] == "Павлюхина")
        await self.crm.update_location(pav["id"], directions=None)
        await Database(self.pool).apply_schema(SCHEMA)
        self.assertIsNone(next(x for x in await self.crm.locations()
                               if x["name"] == "Павлюхина")["directions"],
                          "владелец стёр - сид не возвращает")
        # База, где владелец уже вписал ГСК в адрес: второй раз не пишем.
        await self.pool.execute(
            "delete from crm.settings where key = 'locations_directions_seeded'")
        await self.crm.update_location(pav["id"], address="ул. Павлюхина, 97А, гск сокол")
        await Database(self.pool).apply_schema(SCHEMA)
        self.assertIsNone(next(x for x in await self.crm.locations()
                               if x["name"] == "Павлюхина")["directions"])
        # Точку переименовали раньше, чем приехало поле: находим её по
        # адресу, иначе флаг закрыл бы сид навсегда без подсказки.
        await self.pool.execute(
            "delete from crm.settings where key = 'locations_directions_seeded'")
        await self.crm.update_location(pav["id"], address="ул. Павлюхина, 97А")
        self.assertEqual(await self.crm.rename_location(pav["id"], "Главная"), "ok")
        await Database(self.pool).apply_schema(SCHEMA)
        self.assertIn("ГСК «Сокол»", next(x for x in await self.crm.locations()
                                         if x["id"] == pav["id"])["directions"])

    # ─── наряд и сотрудник ───

    async def test_work_order_and_staff_point(self):
        own = await self.crm.create_bike(code="B-1", model="M", location="Павлюхина")
        o1 = await service.open_order(self.crm, bike=await self.crm.bike(own), payer="own",
                                      client=None, complaint="x", object_note=None,
                                      tech_id=None, estimate=D(0), by="t")
        o2 = await service.open_order(self.crm, bike=None, payer="own", client=None,
                                      complaint="x", object_note="самокат", tech_id=None,
                                      estimate=D(0), by="t", location="Адоратского")
        o3 = await service.open_order(self.crm, bike=None, payer="own", client=None,
                                      complaint="x", object_note="трицикл", tech_id=None,
                                      estimate=D(0), by="t")
        self.assertEqual([(await self.crm.work_order(o))["location"] for o in (o1, o2, o3)],
                         ["Павлюхина", "Адоратского", None])
        await self.crm.update_work_order(o3, location="Павлюхина")
        self.assertEqual({o["id"] for o in await self.crm.work_orders(location="Павлюхина")},
                         {o1, o3})
        self.assertEqual([o["id"] for o in await self.crm.work_orders(location="none")], [])

        sid = await self.crm.create_staff("tech", "h", "Техник", "manager",
                                          location="Павлюхина")
        self.assertEqual((await self.crm.staff_by_id(sid))["location"], "Павлюхина")
        await self.crm.set_staff_location(sid, None)
        self.assertIsNone((await self.crm.staff_by_login("tech"))["location"])

    # ─── переименование ───

    async def test_rename_cascades_in_one_transaction(self):
        await self.seed()
        point = next(x for x in await self.crm.locations() if x["name"] == "Павлюхина")
        bike = await self.crm.create_bike(code="B-1", model="M", location="Павлюхина")
        moved = await self.crm.create_bike(code="B-2", model="M", location="Павлюхина")
        await self.crm.update_bike(moved, location="Адоратского", by="staff:a")
        await self.crm.create_battery(code="A-1", location="Павлюхина")
        await self.crm.create_shift(location="Павлюхина", opening=D(0), note=None,
                                    by="staff:a")
        await self.crm.create_stock_take(scope="location", location="Павлюхина",
                                         note=None, bike_ids=[bike], created_by="staff:a")
        rid = await self.rent(await self.client(1), bike)
        await self.crm.create_work_order(bike_id=None, payer="own", client_id=None,
                                         complaint="x", object_note="самокат",
                                         tech_id=None, estimate=D(0), created_by="t",
                                         location="Павлюхина")
        await self.crm.create_staff("p", "h", "П", "manager", location="Павлюхина")
        refs = [("bikes", "location"), ("batteries", "location"),
                ("cash_shifts", "location"), ("stock_takes", "location"),
                ("rentals", "location"), ("work_orders", "location"),
                ("staff", "location"), ("bike_location_log", "from_location"),
                ("bike_location_log", "to_location")]

        async def where(name):
            return {f"{t}.{c}": await self.count(t, f"{c} = $1", name) for t, c in refs}

        before = await where("Павлюхина")
        self.assertTrue(all(before.values()), before)
        logged = await self.count("bike_location_log")

        self.assertIsNone(await self.crm.rename_location(10 ** 9, "Х"))
        self.assertEqual(await self.crm.rename_location(point["id"], "Адоратского"),
                         "taken", "имя занято другой точкой")
        self.assertEqual(await self.crm.rename_location(point["id"], "Павлюхина"), "ok")
        self.assertEqual(await where("Павлюхина"), before, "отказ ничего не тронул")
        with self.assertRaises(ValueError):
            await self.crm.update_location(point["id"], name="Х")

        self.assertEqual(await self.crm.rename_location(point["id"], "Павлюхина 97А"),
                         "ok")
        self.assertEqual(await where("Павлюхина 97А"), before)
        self.assertFalse(any((await where("Павлюхина")).values()))
        self.assertEqual(await self.count("bike_location_log"), logged,
                         "переименование - не переезд")
        self.assertEqual((await self.crm.rental(rid))["location"], "Павлюхина 97А")
        self.assertIn("Павлюхина 97А", await self.crm.location_names())
        # Отметка жила только в транзакции каскада: переезд снова пишется.
        await self.crm.update_bike(moved, location="Павлюхина 97А", by="staff:a")
        self.assertEqual(await self.count("bike_location_log"), logged + 1)

        # Открытая смена под новым именем (точки такой нет, смена есть) -
        # это записи вне справочника, склеивать их с точкой нельзя.
        await self.crm.create_shift(location="Марс", opening=D(0), note=None, by="x")
        self.assertEqual(await self.crm.rename_location(point["id"], "Марс"), "orphan")
        self.assertEqual((await self.crm.bike(bike))["location"], "Павлюхина 97А")
        self.assertIn("Павлюхина 97А", await self.crm.location_names())

    async def test_rename_refuses_a_name_that_orphan_rows_use(self):
        """Велосипед на «Склад» (имени нет в справочнике - строка «нет в
        справочнике» отчёта). Переименование Павлюхина в «Склад» склеило бы
        их навсегда: обратное переименование увело бы оба велосипеда.
        Отказ, и ничего не тронуто - ни у точки, ни у сирот."""
        point = next(x for x in await self.crm.locations() if x["name"] == "Павлюхина")
        orphan = await self.crm.create_bike(code="O-1", model="M", location="Склад")
        own = await self.crm.create_bike(code="P-1", model="M", location="Павлюхина")
        self.assertEqual(await self.crm.rename_location(point["id"], "Склад"), "orphan")
        self.assertEqual([(await self.crm.bike(b))["location"] for b in (orphan, own)],
                         ["Склад", "Павлюхина"])
        self.assertIn("Павлюхина", await self.crm.location_names())
        # Имя только в журнале мест (велосипед со «Склада» давно уехал) - то же.
        await self.crm.update_bike(orphan, location="Адоратского", by="x")
        self.assertEqual(await self.crm.rename_location(point["id"], "Склад"), "orphan")
        # Открытая смена под чужим именем, у самой точки смены нет.
        ado = next(x for x in await self.crm.locations() if x["name"] == "Адоратского")
        await self.crm.create_shift(location="Марс", opening=D(0), note=None, by="x")
        self.assertEqual(await self.crm.rename_location(ado["id"], "Марс"), "orphan")
        self.assertEqual((await self.crm.cash_shift_for("x"))["location"], "Марс")
        self.assertEqual(await self.crm.rename_location(point["id"], "Сокол"), "ok")

        fake = FakeCrm()
        f_point = await fake.create_location(name="Павлюхина", city="Казань",
                                             address=None, note=None)
        await fake.create_bike(code="O-1", model="M", location="Склад")
        self.assertEqual(await fake.rename_location(f_point, "Склад"), "orphan",
                         "заглушка отказывает так же")

    async def test_rename_rewrites_saved_filters(self):
        """Сохранённый фильтр «Аренды Павлюхиной» (?location=Павлюхина)
        после переименования показывает те же аренды, а не пустой список.
        Чужая точка и остальные параметры фильтра не тронуты."""
        await self.seed()
        point = next(x for x in await self.crm.locations() if x["name"] == "Павлюхина")
        staff = await self.crm.create_staff("op", "h", "Оператор", "manager")
        pav = quote("Павлюхина", safe="")
        mine = await self.crm.save_view(staff_id=staff, section="/rentals",
                                        name="Аренды Павлюхиной",
                                        query=f"status=active&location={pav}&sort=days")
        plus = await self.crm.save_view(staff_id=staff, section="/bikes", name="Плюсом",
                                        query="location=%D0%9F%D0%B0%D0%B2%D0%BB%D1%8E"
                                              "%D1%85%D0%B8%D0%BD%D0%B0&q=a+b")
        other = await self.crm.save_view(staff_id=staff, section="/orders",
                                         name="Адоратского",
                                         query=f"location={quote('Адоратского')}")
        near = await self.crm.save_view(staff_id=staff, section="/bikes", name="Похожее",
                                        query=f"q={pav}&location=none")
        self.assertEqual(await self.crm.rename_location(point["id"], "Павлюхина 97А"), "ok")
        new = quote("Павлюхина 97А", safe="")
        queries = {v: (await self.crm.saved_view(v))["query"]
                   for v in (mine, plus, other, near)}
        self.assertEqual(queries[mine], f"status=active&location={new}&sort=days")
        self.assertEqual(queries[plus], f"location={new}&q=a+b")
        self.assertEqual(queries[other], f"location={quote('Адоратского')}")
        self.assertEqual(queries[near], f"q={pav}&location=none",
                         "совпадение не в параметре точки - не фильтр точки")
        fake = FakeCrm()
        f_point = await fake.create_location(name="Павлюхина", city="Казань",
                                             address=None, note=None)
        f_view = await fake.save_view(staff_id=1, section="/rentals", name="x",
                                      query=f"status=active&location={pav}&sort=days")
        await fake.rename_location(f_point, "Павлюхина 97А")
        self.assertEqual((await fake.saved_view(f_view))["query"],
                         f"status=active&location={new}&sort=days")

    # ─── касса ───

    async def test_cash_goes_to_own_point_shift(self):
        await self.seed()
        s1 = await self.crm.create_shift(location="Павлюхина", opening=D(0), note=None,
                                         by="staff:a")
        s2 = await self.crm.create_shift(location="Адоратского", opening=D(0), note=None,
                                         by="staff:z")
        await self.crm.create_staff("a", "h", "А", "manager")
        await self.crm.create_staff("b", "h", "Б", "manager", location="Адоратского")
        await self.crm.create_staff("d", "h", "Д", "manager", location="Горького")
        e = await self.crm.create_staff("e", "h", "Е", "manager", location="Адоратского")
        await self.crm.link_staff_tg(e, 777, None)

        async def shift(by):
            return (await self.crm.cash_shift_for(by))["id"]

        self.assertEqual(await shift("staff:a"), s1, "своя смена")
        self.assertEqual(await shift("staff:b"), s2, "смена своей точки, а не ранняя")
        self.assertEqual(await shift("staff:d"), s1, "на своей точке смены нет - ранняя")
        self.assertEqual(await shift("staff:никто"), s1)
        self.assertEqual(await shift(None), s1)
        self.assertEqual(await shift("tg:777"), s2, "сотрудник из бота - по tg_id")

        cid = await self.client(1)
        await service.add_entry(self.crm, await self.crm.client(cid), kind="payment",
                                amount=D(500), method="cash", note=None, by="staff:b")
        entry = (await self.crm.ledger_of(cid))[0]
        self.assertEqual(entry["shift_id"], s2)

    # ─── паритет с заглушкой ───

    async def test_fake_crm_tells_the_same_story(self):
        """Одна и та же история на базе и на FakeCrm: точки аренд, места
        велосипедов, журнал мест, каскад и выбор смены совпадают."""
        real = await _points_story(self.crm)
        fake = await _points_story(FakeCrm())
        self.assertEqual(fake, real)
        # И сама история - та, что задумана, а не просто одинаковая.
        self.assertEqual(real["rentals"], {"К1": "Горький", "К2": "Горький",
                                           "К3": "Адоратского"})
        self.assertEqual(real["bikes"]["A"], "Горький")
        self.assertEqual(real["log"]["B"], [(None, "Павлюхина", "staff:op"),
                                            ("Павлюхина", "Горький", "staff:op"),
                                            ("Горький", "Павлюхина", "staff:op")])
        self.assertEqual(real["renamed"], (None, "taken", "ok"))
        self.assertEqual(real["shifts"], ["Горький", "Адоратского", "Горький"])

    # ─── аналитика по точкам ───

    async def shift_back(self, days):
        """Вся история - на `days` суток назад, во всех журналах разом:
        дни парка за прошлый интервал тогда не зависят от «сейчас», и
        база с чистым зеркалом считают одно и то же до знака."""
        gap = timedelta(days=days)
        for table, cols in (("bike_status_log", ("changed_at",)),
                            ("bike_location_log", ("changed_at",)),
                            ("bike_log", ("created_at",)),
                            ("ledger", ("created_at",)),
                            ("work_orders", ("opened_at", "closed_at", "paid_at")),
                            ("cash_shifts", ("opened_at", "closed_at"))):
            sets = ", ".join(f"{c} = {c} - $1::interval" for c in cols)
            await self.pool.execute(f"update crm.{table} set {sets}", gap)
        await self.pool.execute(
            "update crm.rentals set started_on = started_on - $1::int, "
            "closed_on = closed_on - $1::int, billed_until = billed_until - $1::int", days)
        await self.pool.execute(
            "update crm.ledger set period_from = period_from - $1::int, "
            "period_to = period_to - $1::int", days)

    async def test_points_add_up_to_the_panel_numbers(self):
        """Три точки, клиент без аренд, аренда без точки, замена, сдача на
        другой точке, переезд свободного велосипеда: сумма по точкам с
        «без точки» - ровно общие числа панели, а дни на базе - те же, что
        у чистого зеркала на тех же журналах."""
        ids = await _analytics_story(self.crm)
        await self.shift_back(5)
        now = datetime.now().astimezone()
        b5 = ids["bikes"]["B5"]
        # Переезд B5 и два ремонта журналом - в известные моменты: первый
        # ещё на Павлюхина, второй уже на Чистопольской.
        await self.pool.execute(
            "update crm.bike_location_log set changed_at = $2 "
            "where bike_id = $1 and to_location = 'Чистопольская'", b5, now - timedelta(days=3))
        await self.pool.execute(
            "update crm.bike_log set created_at = $2 where id = $1",
            ids["repairs"][0], now - timedelta(days=4))
        await self.pool.execute(
            "update crm.bike_log set created_at = $2 where id = $1",
            ids["repairs"][1], now - timedelta(days=2))
        since, until = now - timedelta(days=6), now - timedelta(days=1)

        days = await self.crm.bike_days_by_location(since, until)
        overall = await self.crm.bike_days_by_status(since, until)
        for status, total in overall.items():
            self.assertAlmostEqual(sum(d.get(status, D(0)) for d in days.values()), total,
                                   delta=D("1e-9"), msg=status)
        mirror = logic.days_by_status_location(
            [dict(r) for r in await self.pool.fetch("select * from crm.bike_status_log")],
            [dict(r) for r in await self.pool.fetch("select * from crm.bike_location_log")],
            since, until)
        self.assertEqual(set(mirror), set(days))
        for key in days:
            self.assertEqual(set(mirror[key]), set(days[key]), key)
            for status in days[key]:
                self.assertAlmostEqual(mirror[key][status], days[key][status],
                                       delta=D("1e-9"), msg=(key, status))
        # Переезд без смены статуса делит простой B5: двое суток на
        # Павлюхина, двое на Чистопольской. На Павлюхина ещё четверо суток
        # B3 - его сдали туда, хотя выдали с Чистопольской.
        self.assertAlmostEqual(float(days["Павлюхина"]["available"]), 2 + 4, delta=0.01)
        self.assertAlmostEqual(float(days["Чистопольская"]["available"]), 2, delta=0.01)
        self.assertAlmostEqual(float(days["Чистопольская"]["repair"]), 4, delta=0.01,
                               msg="снятый при замене остался там, где меняли")
        # В аренде велосипед стоит на точке аренды - и после замены тоже.
        self.assertAlmostEqual(float(days["Павлюхина"]["rented"]), 4, delta=0.01)
        self.assertAlmostEqual(float(days["Адоратского"]["rented"]), 4, delta=0.01)
        self.assertAlmostEqual(float(days[None]["rented"]), 4, delta=0.01)
        self.assertLess(days["Чистопольская"].get("rented", D(0)), D("0.01"))

        money = await self.crm.money_by_location(since, until)
        self.assertEqual({k: v["paid"] for k, v in money.items()},
                         {"Павлюхина": D(4000), "Адоратского": D(2000),
                          "Чистопольская": D(1500), None: D(1200)})
        self.assertEqual(sum(v["paid"] for v in money.values()),
                         await self.crm.rental_revenue(since, until))
        self.assertEqual(money["Павлюхина"]["charged"], D(6000))
        self.assertEqual(money["Павлюхина"]["charged_fines"], D(6300))
        self.assertEqual(money["Павлюхина"]["bonus"], D(200))
        self.assertEqual(money["Адоратского"]["refunded"], D(100))

        numbers = await _analytics_numbers(self.crm, since, until, date.today())
        self.assertEqual(numbers, _EXPECTED_NUMBERS)

        report = logic.points_rows(
            await self.crm.locations(), bikes=await self.crm.bikes(limit=1000), days=days,
            money=money, rentals=await self.crm.rentals_by_location(since, until),
            debt=await self.crm.debt_by_location(),
            cash=await self.crm.cash_by_location(since, until),
            service=await self.crm.service_by_location(since, until))
        self.assertEqual([r["title"] for r in report["rows"]],
                         ["Павлюхина", "Адоратского", "Чистопольская", "без точки"])
        total, panel = report["total"], logic.fleet_metrics(
            overall, await self.crm.rental_revenue(since, until))
        for key in ("idle_percent", "avg_check", "revenue"):
            self.assertEqual(total["metrics"][key], panel[key], key)
        counts = await self.crm.bike_counts()
        self.assertEqual(total["fleet"],
                         sum(counts.get(s, 0) for s in logic.OPERATIONAL_STATUSES))
        self.assertEqual(total["active"], (await self.crm.counts())["rentals"])
        self.assertEqual(total["debt"], -sum(d["balance"] for d in await self.crm.debtors()))

        # Деньги по дням точки - тот же ряд, что у всего парка, и в сумме
        # те же платежи точки.
        first, last = date.today() - timedelta(days=6), date.today()
        pav = await self.crm.location_money_by_day("Павлюхина", first, last)
        self.assertEqual([r["day"] for r in pav],
                         [r["day"] for r in await self.crm.money_by_day(first, last)])
        self.assertEqual(sum(r["paid"] for r in pav), D(4000))
        self.assertEqual(logic.money_chart(pav, today=last)["paid"], D(4000))
        bare = await self.crm.location_money_by_day(None, first, last)
        self.assertEqual(sum(r["paid"] for r in bare), D(1200))

        # Окупаемость держится того же правила денег: предоплата клиента
        # без аренды в окупаемость не попадает, но в точках не теряется.
        by_model = await self.crm.model_money(since, until)
        self.assertEqual(sum(v["paid"] for v in by_model.values()), D(8000))

        # Три числа по месяцам на точку - из тех же ответов.
        month = logic.point_months([{"month": first, "days": days, "money": money}],
                                   "Павлюхина")[0]
        self.assertEqual(month["revenue"], D(4000))
        self.assertEqual(month["rented_days"], days["Павлюхина"]["rented"])

    async def test_fake_crm_counts_points_the_same(self):
        """Деньги, аренды, долг, сервис и касса по точкам на FakeCrm - те
        же, что на базе: панель на заглушке показывает правду."""
        now = datetime.now().astimezone()
        since, until = now - timedelta(days=1), now + timedelta(days=1)
        await _analytics_story(self.crm)
        real = await _analytics_numbers(self.crm, since, until, date.today())
        fake_crm = FakeCrm()
        await _analytics_story(fake_crm)
        fake = await _analytics_numbers(fake_crm, since, until, date.today())
        self.assertEqual(fake, real)
        self.assertEqual(real, _EXPECTED_NUMBERS)


async def _points_story(crm):
    """Выдача с выбранной точкой, возврат на другую, замена, наряд,
    переименование точки и касса - только методами CrmDB, общими для
    базы и заглушки. Ключи - номера и имена: id у них разные."""
    tariff = await crm.tariff(await crm.create_tariff("Неделя", 7, D("3000"), None))
    # Справочник один на обе стороны: в базе две точки из сида, у заглушки -
    # ничего. Без этого «имя занято точкой» и «имя у записей вне
    # справочника» различались бы данными, а не поведением.
    known = {x["name"] for x in await crm.locations()}
    for name in ("Павлюхина", "Адоратского"):
        if name not in known:
            await crm.create_location(name=name, city="Казань", address=None, note=None)
    gorky = await crm.create_location(name="Горького", city="Казань", address=None,
                                      note=None)
    bikes = {}
    for code, place in (("A", "Горького"), ("B", "Павлюхина"), ("C", "Адоратского"),
                        ("D", None)):
        bikes[code] = await crm.create_bike(code=code, model="M", location=place,
                                            by="staff:op")
    clients = {}
    for n in (1, 2, 3):
        clients[f"К{n}"] = await crm.create_client(full_name=f"К{n}",
                                                  phone=f"+7999000001{n}")

    async def rent(who, code, location=None):
        return await service.open_rental(
            crm, client=await crm.client(clients[who]), bike=await crm.bike(bikes[code]),
            tariff=tariff, started_on=date.today(), contract_no=None, by="staff:op",
            billing="manual", location=location)

    rentals = {"К1": await rent("К1", "A"), "К2": await rent("К2", "B", "Горького"),
               "К3": await rent("К3", "C")}
    await service.close_rental(crm, await crm.rental(rentals["К2"]),
                               closed_on=date.today(), note=None, by="staff:op",
                               return_location="Павлюхина")
    await service.swap_bike(crm, await crm.rental(rentals["К3"]),
                            await crm.bike(bikes["D"]), reason="repair", by="staff:op",
                            swap_location="Павлюхина")
    await crm.create_work_order(bike_id=bikes["A"], payer="own", client_id=None,
                                complaint="x", object_note=None, tech_id=None,
                                estimate=D(0), created_by="staff:op")
    await crm.create_shift(location="Горького", opening=D(0), note=None, by="staff:a")
    await crm.create_shift(location="Адоратского", opening=D(0), note=None, by="staff:b")
    await crm.create_staff("c", "h", "В", "manager", location="Адоратского")
    await crm.create_staff("g", "h", "Г", "manager", location="Горького")
    renamed = (await crm.rename_location(10 ** 9, "Х"),
               await crm.rename_location(gorky, "Адоратского"),
               await crm.rename_location(gorky, "Горький"))
    shifts = [(await crm.cash_shift_for(by))["location"]
              for by in ("staff:a", "staff:c", "staff:g")]
    rows = {r["client_id"]: r for r in await crm.rentals(status=None)}
    orders = await crm.work_orders()
    return {
        "rentals": {who: rows[cid]["location"] for who, cid in clients.items()},
        "bikes": {code: (await crm.bike(bid))["location"] for code, bid in bikes.items()},
        "log": {code: moves(await crm.bike_location_log(bid))
                for code, bid in bikes.items()},
        "orders": [o["location"] for o in orders],
        "staff": sorted((s["login"], s["location"]) for s in await crm.staff_all()),
        "renamed": renamed, "shifts": shifts,
        "gorky_rentals": sorted(r["client_id"] == clients["К1"]
                                for r in await crm.rentals(location="Горький")),
    }


POINTS = ("Павлюхина", "Адоратского", "Чистопольская", None)


async def _analytics_story(crm):
    """Три точки и всё, что по ним считается: выдачи и продление, замена,
    сдача на другой точке, платежи с арендой и без, клиент без аренд,
    аренда без точки, штраф, баллы, возврат, две кассы, наряды и ремонт
    журналом до и после переезда велосипеда. Только методы CrmDB, общие
    для базы и заглушки."""
    pav, ado, chi = POINTS[:3]
    await crm.create_location(name=chi, city="Казань", address=None, note=None)
    tariff = await crm.tariff(await crm.create_tariff("Неделя", 7, D("3000"), None))
    bikes = {}
    for code, place in (("B1", pav), ("B2", ado), ("B3", chi), ("B4", None),
                        ("B5", pav), ("B6", chi), ("B7", ado)):
        bikes[code] = await crm.create_bike(code=code, model="M", location=place,
                                            by="staff:op")
    clients = {}
    for n in range(1, 6):
        clients[n] = await crm.create_client(full_name=f"Клиент {n}",
                                             phone=f"+7999000002{n}")
    today = date.today()

    async def rent(n, code):
        rid = await service.open_rental(
            crm, client=await crm.client(clients[n]), bike=await crm.bike(bikes[code]),
            tariff=tariff, started_on=today, contract_no=None, by="staff:op",
            billing="manual")
        await crm.charge_period(rid, clients[n], period_from=today,
                                period_to=today + timedelta(days=7), amount=D(-3000),
                                note="Неделя")
        return rid

    async def pay(n, kind, amount, **extra):
        await crm.add_ledger(client_id=clients[n], kind=kind, amount=D(amount),
                             created_by="staff:op", **extra)

    rentals = {1: await rent(1, "B1"), 2: await rent(2, "B2"), 3: await rent(3, "B3"),
               5: await rent(5, "B4")}
    await crm.charge_period(rentals[1], clients[1], period_from=today + timedelta(days=7),
                            period_to=today + timedelta(days=14), amount=D(-3000),
                            note="Продление")
    ado_shift = await crm.create_shift(location=ado, opening=D(0), note=None,
                                       by="staff:a")
    await pay(1, "payment", 3000, rental_id=rentals[1], method="sbp")
    await pay(1, "payment", 1000, method="sbp")          # по заявке: аренды в записи нет
    await pay(1, "fine", -300, rental_id=rentals[1])
    await pay(1, "bonus", 200, rental_id=rentals[1])
    await pay(2, "payment", 2000, method="sbp")
    await pay(3, "payment", 1500, rental_id=rentals[3], method="cash", shift_id=ado_shift)
    await pay(4, "payment", 700, method="cash")          # без аренд; касса открыта одна
    await pay(5, "payment", 500, rental_id=rentals[5], method="sbp")
    await crm.create_shift(location=pav, opening=D(0), note=None, by="staff:b")
    await pay(2, "refund", -100, method="cash")          # открыты две кассы - ничья

    await service.swap_bike(crm, await crm.rental(rentals[2]), await crm.bike(bikes["B6"]),
                            reason="repair", by="staff:op", swap_location=chi)
    await service.close_rental(crm, await crm.rental(rentals[3]), closed_on=today,
                               note=None, by="staff:op", return_location=pav)
    first = await crm.create_repair(
        bikes["B5"], items=[{"node": "brake_pads", "parts_cost": D(300),
                             "labor_cost": D(200), "note": None}],
        note=None, created_by="staff:t")
    await crm.update_bike(bikes["B5"], location=chi, by="staff:op")
    second = await crm.create_repair(
        bikes["B5"], items=[{"node": "brake_pads", "parts_cost": D(800),
                             "labor_cost": D(400), "note": None}],
        note=None, created_by="staff:t")

    now = datetime.now().astimezone()
    o1 = await crm.create_work_order(bike_id=None, payer="client", client_id=clients[4],
                                     complaint="не едет", object_note="самокат",
                                     tech_id=None, estimate=D(0), created_by="staff:t",
                                     location=pav)
    await crm.add_order_item(o1, title="Камера", node="tube_tire", work_type_id=None,
                             qty=2, price=D(500), parts_cost=D(150), labor_cost=D(100))
    await crm.close_work_order(o1, total=D(1000), cost=D(500), closed_at=now, repair=None)
    await crm.update_work_order(o1, paid_at=now)
    o2 = await crm.create_work_order(bike_id=bikes["B7"], payer="own", client_id=None,
                                     complaint="тормоза", object_note=None, tech_id=None,
                                     estimate=D(0), created_by="staff:t")
    await crm.update_bike(bikes["B7"], status="repair", by="staff:t")
    await crm.close_work_order(
        o2, total=D(0), cost=D(800), closed_at=now,
        repair={"bike_id": bikes["B7"], "cost": D(800), "note": None,
                "created_by": "staff:t",
                "items": [{"node": "brake_pads", "parts_cost": D(500),
                           "labor_cost": D(300)}]})
    o3 = await crm.create_work_order(bike_id=None, payer="client", client_id=clients[3],
                                     complaint="АКБ", object_note="трицикл", tech_id=None,
                                     estimate=D(0), created_by="staff:t", location=chi)
    await crm.close_work_order(o3, total=D(900), cost=D(400), closed_at=now, repair=None)
    # Открытый наряд в закрытые за период не идёт.
    await crm.create_work_order(bike_id=None, payer="client", client_id=clients[4],
                                complaint="x", object_note="самокат", tech_id=None,
                                estimate=D(0), created_by="staff:t", location=ado)
    return {"bikes": bikes, "clients": clients, "rentals": rentals,
            "repairs": (first, second)}


async def _analytics_numbers(crm, since, until, today):
    """Всё, что отчёт «По точкам» берёт из базы, кроме дней парка: те
    зависят от «сейчас» и сверяются с чистым зеркалом отдельно."""
    first, last = today - timedelta(days=7), today + timedelta(days=1)
    by_day, debtors = {}, {}
    for key in POINTS:
        by_day[key] = sum(r["paid"] for r in
                          await crm.location_money_by_day(key, first, last))
        debtors[key] = [d["full_name"] for d in await crm.debtors(location=key or "none")]
    return {"money": await crm.money_by_location(since, until),
            "rentals": await crm.rentals_by_location(since, until),
            "debt": await crm.debt_by_location(),
            "service": await crm.service_by_location(since, until),
            "cash": await crm.cash_by_location(since, until),
            "by_day": by_day, "debtors": debtors}


def _money(paid, charged, fines=0, bonus=0, refunded=0):
    return {"paid": D(paid), "charged": D(charged), "charged_fines": D(charged + fines),
            "bonus": D(bonus), "refunded": D(refunded)}


def _service(orders, client_orders, revenue, cost, parts, repairs, repair_cost):
    return {"orders": orders, "client_orders": client_orders, "revenue": D(revenue),
            "cost": D(cost), "parts_cost": D(parts), "repairs": repairs,
            "repair_cost": D(repair_cost)}


# Что задумано в _analytics_story - руками, а не «как посчитала база».
_EXPECTED_NUMBERS = {
    "money": {"Павлюхина": _money(4000, 6000, fines=300, bonus=200),
              "Адоратского": _money(2000, 3000, refunded=100),
              "Чистопольская": _money(1500, 3000),
              None: _money(1200, 3000)},
    "rentals": {
        "Павлюхина": {"issued": 1, "first_periods": 1, "renewals": 1, "active": 1},
        "Адоратского": {"issued": 1, "first_periods": 1, "renewals": 0, "active": 1},
        "Чистопольская": {"issued": 1, "first_periods": 1, "renewals": 0, "active": 0},
        None: {"issued": 1, "first_periods": 1, "renewals": 0, "active": 1}},
    "debt": {"Павлюхина": {"clients": 1, "debt": D(2100)},
             "Адоратского": {"clients": 1, "debt": D(1100)},
             "Чистопольская": {"clients": 1, "debt": D(1500)},
             None: {"clients": 1, "debt": D(2500)}},
    "service": {"Павлюхина": _service(1, 1, 1000, 500, 300, 1, 500),
                "Адоратского": _service(1, 0, 0, 800, 0, 0, 0),
                "Чистопольская": _service(1, 1, 0, 400, 0, 1, 1200)},
    "cash": {"Адоратского": D(2200), None: D(-100)},
    "by_day": {"Павлюхина": D(4000), "Адоратского": D(2000), "Чистопольская": D(1500),
               None: D(1200)},
    "debtors": {"Павлюхина": ["Клиент 1"], "Адоратского": ["Клиент 2"],
                "Чистопольская": ["Клиент 3"], None: ["Клиент 5"]},
}


if __name__ == "__main__":
    unittest.main()
