"""Демо-сид на настоящем Postgres: целостность, три числа, журналы.

Сид пишет SQL напрямую, в обход сервисов панели, поэтому проверять его
можно только настоящими запросами панели на настоящей базе: сводка,
«По точкам» и «Расхождения» должны увидеть ровно то, что обещает демо.
Нужен pgserver, как в test_crm_pg; без него набор пропускается.

Пояс - Europe/Moscow на время набора: сутки отчётов режутся по поясу
сессии, а на машине тестов пояс любой (test_audit - тот же приём).
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import time
import unittest
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import asyncpg
    import pgserver

    from app.crm import logic
    from app.crm.db import CrmDB
    from app.db import _init_connection
    from app.demo import core, runtime, seed
    from app.demo.world import MSK
    HAVE_PG = True
except ImportError:                                    # pragma: no cover
    HAVE_PG = False

D = Decimal


@unittest.skipUnless(HAVE_PG, "pgserver или asyncpg не установлены")
class TestDemoSeed(unittest.IsolatedAsyncioTestCase):
    """Один сброс на набор: сид - дорогая часть, проверки только читают."""

    @classmethod
    def setUpClass(cls):
        cls.tz_before = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Moscow"
        time.tzset()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.tmp.name)
        # «Сейчас» - настоящее: метрики панели считают открытые интервалы
        # до now() базы, и сид из будущего дал бы им отрицательные дни.
        cls.now = datetime.now(MSK).replace(microsecond=0)
        cls.today = cls.now.date()
        started = time.monotonic()
        cls.summary = asyncio.run(cls._reset())
        cls.seconds = time.monotonic() - started

    @classmethod
    async def _reset(cls):
        pool = await asyncpg.create_pool(cls.pg.get_uri(), min_size=1, max_size=2,
                                         init=_init_connection)
        try:
            return await seed.reset(pool, today=cls.today, now=cls.now)
        finally:
            await pool.close()

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
        self.crm = CrmDB(self.pool)

    async def asyncTearDown(self):
        await self.pool.close()

    # ─────────────────────────── состав ───────────────────────────

    def test_reset_is_fast_and_fills_the_demo(self):
        s = self.summary
        self.assertLess(self.seconds, 60, "сид обязан укладываться в минуту")
        bikes = s["bikes"]
        self.assertTrue(185 <= sum(bikes.values()) <= 200, bikes)
        self.assertTrue(160 <= s["operational"] <= 172, s["operational"])
        self.assertEqual(bikes.get("new"), 3, "вчерашняя партия на сборке")
        self.assertTrue(18 <= bikes.get("lost", 0) <= 22, bikes)
        self.assertIn(bikes.get("sold"), (2, 3))
        self.assertIn(bikes.get("written_off"), (1, 2))
        self.assertTrue(5 <= bikes.get("repair", 0) <= 9, bikes)
        self.assertTrue(1 <= bikes.get("reserved", 0) <= 2, bikes)
        counts = s["counts"]
        self.assertTrue(450 <= counts["clients"] <= 560, counts)
        self.assertIn(counts["in_search"], (1, 2))
        self.assertTrue(3 <= counts["swaps"] <= 5, counts)
        # Смена открыта на каждой точке - или откроется в свой час сегодня
        # (остаток дня, runtime.live_day): до 10 утра касса закрыта.
        later = [op for op in s["later"] if op["kind"] == "shift"]
        self.assertEqual(counts["shifts_open"] + len(later), 3,
                         "сегодня смена на каждой точке")
        self.assertTrue(360 <= counts["batteries"] <= 480, counts)
        self.assertTrue(0.6 <= s["renewal_share"] <= 0.72, s["renewal_share"])
        self.assertTrue(18 <= s["avg_rental_days"] <= 26, s["avg_rental_days"])

    async def test_demo_logins_work(self):
        for login, profile in (("demo", "owner"), ("operator", "manager"),
                               ("mechanic", "tech")):
            staff = await self.crm.staff_by_login(login)
            self.assertIsNotNone(staff, login)
            self.assertTrue(logic.verify_password("demo", staff["password_hash"]), login)
            self.assertEqual(staff["profile_code"], profile)
        self.assertEqual(await self.crm.staff_count(), len(seed.STAFF))

    # ─────────────────────────── три числа ───────────────────────────

    async def test_three_numbers_like_the_dashboard(self):
        """Ровно как period_metrics сводки: 30 дней до настоящего «сейчас»."""
        for until in (self.now, datetime.now().astimezone()):
            since = until - timedelta(days=30)
            m = logic.fleet_metrics(await self.crm.bike_days_by_status(since, until),
                                    await self.crm.rental_revenue(since, until))
            self.assertTrue(8 <= m["idle_percent"] <= 11, m["idle_percent"])
            self.assertTrue(D(480) <= m["avg_check"] <= D(540), m["avg_check"])
        fleet = await self.crm.bikes(limit=10000)
        operational = sum(b["status"] in logic.OPERATIONAL_STATUSES for b in fleet)
        self.assertTrue(160 <= operational <= 172, operational)

    async def test_points_add_up_to_the_totals(self):
        since, until = self.now - timedelta(days=30), self.now
        total_days = await self.crm.bike_days_by_status(since, until)
        by_point = await self.crm.bike_days_by_location(since, until)
        summed: dict[str, D] = defaultdict(D)
        for cell in by_point.values():
            for status, days in cell.items():
                summed[status] += days
        for status in set(total_days) | set(summed):
            self.assertAlmostEqual(float(total_days.get(status, 0)),
                                   float(summed.get(status, 0)), places=6, msg=status)
        money = await self.crm.money_by_location(since, until)
        self.assertEqual(sum((m["paid"] for m in money.values()), D(0)),
                         await self.crm.rental_revenue(since, until))
        self.assertNotIn(None, money, "каждый платёж демо - к аренде с точкой")

        debt = await self.crm.debt_by_location()
        debtors = await self.crm.debtors(10000)
        self.assertEqual(sum((d["debt"] for d in debt.values()), D(0)),
                         -sum((D(d["balance"]) for d in debtors), D(0)))
        rentals = await self.crm.rentals_by_location(since, until)
        self.assertEqual(sum(r["active"] for r in rentals.values()),
                         len(await self.crm.active_rentals()))
        cash = await self.crm.cash_by_location(since, until)
        self.assertNotIn(None, cash, "наличные - только в сменах точек")
        self.assertEqual(sum(cash.values(), D(0)), await self.pool.fetchval(
            "select coalesce(sum(amount), 0) from crm.ledger where method = 'cash' "
            "and kind in ('payment', 'refund') and created_at >= $1 and created_at < $2",
            since, until))

        report = logic.points_rows(await self.crm.locations(),
                                   bikes=await self.crm.bikes(limit=10000),
                                   days=by_point, money=money)
        rows = {r["title"]: r for r in report["rows"]}
        self.assertEqual(set(rows), {core.P1, core.P2, core.P3})
        total = report["total"]["metrics"]
        overall = logic.fleet_metrics(total_days,
                                      await self.crm.rental_revenue(since, until))
        self.assertEqual(total["idle_percent"], overall["idle_percent"])
        self.assertEqual(total["avg_check"], overall["avg_check"])
        # Точки разные: новая на Победы хуже старых по простою и по чеку.
        idle = {k: r["metrics"]["idle_percent"] for k, r in rows.items()}
        check = {k: r["metrics"]["avg_check"] for k, r in rows.items()}
        self.assertGreater(idle[core.P3], max(idle[core.P1], idle[core.P2]))
        self.assertLess(check[core.P3], check[core.P1])

    # ─────────────────────────── целостность ───────────────────────────

    async def test_integrity_only_the_deliberate_debts(self):
        """Как integrity_data панели. Ремонт без наряда допустим, только пока
        seed_service - заготовка: наряды к ремонтам заводит он."""
        issues = logic.integrity_issues(
            await self.crm.bikes(limit=10000), await self.crm.active_rentals(),
            await self.crm.open_orders_by_bike(), await self.crm.debtors(200),
            batteries=await self.crm.batteries(limit=10000))
        kinds: dict[str, int] = defaultdict(int)
        for issue in issues:
            kinds[issue["kind"]] += 1
        self.assertIn(kinds.pop("debt_without_rental", 0), (1, 2))
        orders = await self.pool.fetchval("select count(*) from crm.work_orders")
        repair = await self.pool.fetchval(
            "select count(*) from crm.bikes where status = 'repair'")
        if orders:
            self.assertNotIn("repair_no_order", kinds)
        else:
            self.assertEqual(kinds.pop("repair_no_order", 0), repair)
        self.assertEqual(dict(kinds), {})

    async def test_status_journal_ends_in_the_current_status(self):
        rows = await self.pool.fetch(
            "select * from crm.bike_status_log order by bike_id, changed_at, id")
        chains: dict[int, list] = defaultdict(list)
        for row in rows:
            chains[row["bike_id"]].append(row)
        bikes = {b["id"]: b for b in await self.pool.fetch("select * from crm.bikes")}
        self.assertEqual(set(chains), set(bikes), "журнал есть у каждого велосипеда")
        for bike_id, chain in chains.items():
            self.assertIsNone(chain[0]["from_status"])
            self.assertEqual(chain[-1]["to_status"], bikes[bike_id]["status"], bike_id)
            self.assertLessEqual(chain[-1]["changed_at"], self.now)
            for prev, row in zip(chain, chain[1:], strict=False):
                self.assertEqual(row["from_status"], prev["to_status"], bike_id)
                self.assertGreater(row["changed_at"], prev["changed_at"], bike_id)
                self.assertGreaterEqual(row["mileage_km"], prev["mileage_km"], bike_id)
        places = await self.pool.fetch(
            "select distinct on (bike_id) bike_id, to_location, changed_at "
            "from crm.bike_location_log order by bike_id, changed_at desc, id desc")
        self.assertEqual(len(places), len(bikes))
        for row in places:
            self.assertEqual(row["to_location"], bikes[row["bike_id"]]["location"])
            self.assertLessEqual(row["changed_at"], self.now)
        batteries = await self.pool.fetch(
            """
            select b.id, b.status, l.to_status
              from crm.batteries b
              join lateral (select to_status from crm.battery_status_log x
                             where x.battery_id = b.id
                             order by changed_at desc, id desc limit 1) l on true
            """)
        self.assertEqual(len(batteries),
                         await self.pool.fetchval("select count(*) from crm.batteries"))
        for row in batteries:
            self.assertEqual(row["status"], row["to_status"], row["id"])

    async def test_rented_bikes_match_active_rentals(self):
        active = await self.crm.active_rentals()
        rented = {r["id"] for r in await self.pool.fetch(
            "select id from crm.bikes where status = 'rented'")}
        self.assertEqual(rented, {r["bike_id"] for r in active})
        places = {r["id"]: r["location"] for r in await self.pool.fetch(
            "select id, location from crm.bikes")}
        for rental in active:
            # Велосипед в аренде стоит на точке аренды - на этом держится
            # чек точки (CLAUDE.md, «Точки»).
            self.assertEqual(places[rental["bike_id"]], rental["location"], rental["id"])
            self.assertGreater(rental["billed_until"], self.today, rental["id"])
            open_rows = [r for r in await self.crm.rental_bikes(rental["id"])
                         if r["returned_on"] is None]
            self.assertEqual([r["bike_id"] for r in open_rows], [rental["bike_id"]])
        self.assertEqual(await self.pool.fetchval(
            "select count(*) from crm.rental_bikes rb join crm.rentals r "
            "on r.id = rb.rental_id where r.status = 'closed' and rb.returned_on is null"),
            0)
        bad = await self.pool.fetchval(
            """
            select count(*) from crm.batteries b
              left join crm.rentals r on r.id = b.rental_id and r.status = 'active'
             where (b.status = 'rented') <> (r.id is not null)
            """)
        self.assertEqual(bad, 0, "батарея у клиента ⇔ её аренда идёт")

    async def test_search_page_has_candidates_and_searches(self):
        """«Розыск» как у панели: один кандидат (просрочка больше недели,
        в розыск не объявлен) и одна-две аренды в розыске."""
        rows = logic.search_rows(await self.crm.active_rentals(),
                                 settings=logic.search_settings(await self.crm.settings()),
                                 today=self.today)
        self.assertEqual(len(rows["candidates"]), 1, rows["candidates"])
        self.assertIn(len(rows["searching"]), (1, 2))

    async def test_cash_shifts_add_up(self):
        shifts = await self.pool.fetch("select * from crm.cash_shifts order by id")
        points = {s["location"] for s in shifts if s["status"] == "open"}
        later = {op["point"] for op in self.summary["later"] if op["kind"] == "shift"}
        self.assertEqual(points | later, {core.P1, core.P2, core.P3})
        self.assertEqual(points & later, set(), "открытая смена не открывается второй раз")
        for shift in shifts:
            if shift["status"] == "open":
                # Касса открывается к открытию точки, а не в час сброса.
                self.assertGreaterEqual(shift["opened_at"].astimezone(MSK).hour, 9, shift["no"])
        for shift in shifts:
            if shift["status"] != "closed":
                continue
            payments = await self.crm.shift_payments(shift["id"])
            moves = await self.crm.cash_moves(shift["id"])
            self.assertEqual(logic.shift_expected(dict(shift), payments, moves),
                             shift["expected"], shift["no"])
            self.assertEqual(shift["diff"], shift["counted"] - shift["expected"])
        loose = await self.pool.fetchval(
            """
            select count(*) from crm.ledger l
              left join crm.cash_shifts s on s.id = l.shift_id
             where l.method = 'cash'
               and (s.id is null or l.created_at < s.opened_at
                    or l.created_at >= coalesce(s.closed_at, 'infinity'))
            """)
        self.assertEqual(loose, 0, "наличные - внутри окна своей смены")

    async def test_phones_cannot_be_dialled(self):
        """Ни одного живого номера: код +7 000 не выдан никому. Прежний
        +7 900 0XX принадлежит мобильным операторам, и «должник» в демо
        был бы чужим человеком с настоящим телефоном."""
        found = []
        for table, column in (("clients", "phone"), ("locations", "phone"),
                              ("suppliers", "phone"), ("inbox_threads", "phone"),
                              ("trackers", "phone")):
            found += [(table, r[0]) for r in await self.pool.fetch(
                f"select {column} from crm.{table} where {column} is not null")]
        found += [(r[0], r[1]) for r in await self.pool.fetch(
            "select key, value from crm.settings where key like '%phone%'")]
        self.assertGreater(len(found), 400)
        for where, phone in found:
            digits = "".join(ch for ch in phone if ch.isdigit())
            self.assertRegex(digits, r"^7000\d{7}$", where)
        # Номера внутри текста: назначения платежей и формы группы точек.
        texts = [r[0] for r in await self.pool.fetch(
            "select purpose from crm.bank_txns where purpose is not null "
            "union all select data::text from crm.ops_reports "
            "union all select note from crm.ops_reports where note is not null")]
        self.assertGreater(len(texts), 10)
        live = re.compile(r"(?:\+7|\b8)[\s()-]*9\d\d[\s)-]*\d{3}[\s-]*\d\d[\s-]*\d\d")
        for text in texts:
            self.assertNotRegex(text, live, text)

    # ─────────────────────────── детерминизм ───────────────────────────

    async def test_same_seed_same_demo(self):
        async def digest() -> list:
            return list(await self.pool.fetchrow(
                """
                select (select md5(string_agg(concat_ws('|', id, client_id, kind, amount,
                                                        created_at), ',' order by id))
                          from crm.ledger),
                       (select md5(string_agg(concat_ws('|', id, bike_id, to_status,
                                                        changed_at), ',' order by id))
                          from crm.bike_status_log)
                """))

        before = await digest()
        # Одно соединение на оба сброса: подготовленные до сброса запросы
        # не должны ронять второй (ночной сброс идёт на пуле панели).
        pool = await asyncpg.create_pool(self.pg.get_uri(), min_size=1, max_size=1,
                                         init=_init_connection)
        try:
            await pool.fetchval("select count(*) from crm.bikes")
            first = await seed.reset(pool, today=self.today, now=self.now)
            second = await seed.reset(pool, today=self.today, now=self.now)
            self.assertEqual(await pool.fetchval("select count(*) from crm.bikes"),
                             sum(first["bikes"].values()))
        finally:
            await pool.close()
        self.assertEqual(first, second)
        self.assertEqual(first, self.summary)
        self.assertEqual(await digest(), before)



@unittest.skipUnless(HAVE_PG, "pgserver или asyncpg не установлены")
class TestDemoLiveDay(unittest.IsolatedAsyncioTestCase):
    """Сброс в 04:00 - как ночью: сегодняшних смен и платежей ещё нет, они
    в остатке дня, и live_day делает их обычным путём панели."""

    @classmethod
    def setUpClass(cls):
        cls.tz_before = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Moscow"
        time.tzset()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.tmp.name)
        cls.today = datetime.now(MSK).date()
        cls.now = datetime.combine(cls.today, datetime.min.time(), MSK) + timedelta(hours=4)
        if cls.now > datetime.now(MSK):
            # До 04:00 сид «на 04:00» был бы сидом из будущего: берём вчера.
            cls.today -= timedelta(days=1)
            cls.now -= timedelta(days=1)

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
        self.crm = CrmDB(self.pool)
        self.summary = await seed.reset(self.pool, today=self.today, now=self.now)

    async def asyncTearDown(self):
        await self.pool.close()

    async def play(self, plan):
        clock = [self.now]

        async def sleep(seconds):
            clock[0] += timedelta(seconds=seconds)

        await runtime.live_day(self.crm, plan, clock=lambda: clock[0], sleep=sleep)
        return clock[0]

    async def test_day_opens_shifts_and_takes_payments(self):
        plan = self.summary["later"]
        self.assertEqual(self.summary["counts"]["shifts_open"], 0,
                         "в 04:00 касса ещё закрыта, а не «открыта в 03:50»")
        shifts = [op for op in plan if op["kind"] == "shift"]
        payments = [op for op in plan if op["kind"] == "payment"]
        self.assertEqual(sorted(op["point"] for op in shifts),
                         sorted((core.P1, core.P2, core.P3)))
        for op in shifts:
            self.assertTrue(9.5 <= op["at"].hour + op["at"].minute / 60 <= 10, op)
        self.assertGreaterEqual(len(payments), 5, "за день продлевают")
        self.assertTrue(all(self.now < op["at"] <= datetime.combine(
            self.today, datetime.min.time(), MSK) + timedelta(hours=core.DAY_END)
            for op in plan))
        before = await self.pool.fetchval("select count(*) from crm.ledger "
                                          "where kind = 'payment'")
        await self.play(plan)
        self.assertEqual(await self.pool.fetchval(
            "select count(*) from crm.cash_shifts where status = 'open'"), 3)
        self.assertEqual(await self.pool.fetchval(
            "select count(*) from crm.ledger where kind = 'payment'"),
            before + len(payments))
        # Наличные дня - в смене своей точки, как у кнопки панели.
        self.assertEqual(await self.pool.fetchval(
            "select count(*) from crm.ledger where method = 'cash' and shift_id is null"), 0)
        issues = logic.integrity_issues(
            await self.crm.bikes(limit=10000), await self.crm.active_rentals(),
            await self.crm.open_orders_by_bike(), await self.crm.debtors(200),
            batteries=await self.crm.batteries(limit=10000))
        self.assertEqual({i["kind"] for i in issues}, {"debt_without_rental"})

    async def test_visitor_changes_win(self):
        """Посетитель уже открыл кассу и принял деньги - день их не дублирует."""
        plan = self.summary["later"]
        shift = next(op for op in plan if op["kind"] == "shift")
        pay = next(op for op in plan if op["kind"] == "payment")
        await self.crm.create_shift(location=shift["point"], opening=D(0), note=None,
                                    by="staff:demo")
        clock = [self.now]

        async def sleep(seconds):
            clock[0] += timedelta(seconds=seconds)
            if clock[0] >= pay["at"] - timedelta(seconds=1) and not paid:
                paid.append(await self.crm.add_ledger(
                    client_id=pay["client_id"], kind="payment", amount=pay["amount"],
                    method="sbp", created_by="staff:demo"))

        paid: list[int] = []
        before = await self.pool.fetchval("select count(*) from crm.ledger "
                                          "where kind = 'payment'")
        await runtime.live_day(self.crm, plan, clock=lambda: clock[0], sleep=sleep)
        self.assertEqual(await self.pool.fetchval(
            "select count(*) from crm.cash_shifts where status = 'open' and location = $1",
            shift["point"]), 1)
        payments = [op for op in plan if op["kind"] == "payment"]
        self.assertEqual(await self.pool.fetchval(
            "select count(*) from crm.ledger where kind = 'payment'"),
            before + len(payments), "платёж посетителя вместо платежа дня, не вдобавок")


if __name__ == "__main__":
    unittest.main()
