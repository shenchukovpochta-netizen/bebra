"""Сервис демо-стенда на настоящем Postgres: наряды, склад, смета, счёт
за ремонт и пересчёт - после полного reset() демо.

Сервис сида пишет SQL напрямую, в обход кнопок панели, поэтому проверки -
те же, что держит панель: расхождения, остаток склада как сумма движений,
себестоимость единицы в движении, журнал ремонта у закрытого наряда,
красная линия «оплата ремонта не в журнале». Страницы сервиса - тем же
ASGI-клиентом, что test_web_pg, под демо-логином. Нужен pgserver.
"""

from __future__ import annotations

import asyncio
import os
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
    from httpx import ASGITransport, AsyncClient

    from app.crm import logic
    from app.crm.db import CrmDB
    from app.db import _init_connection
    from app.demo import seed, seed_service
    from app.demo.world import MSK
    from app.web.app import create_app
    from app.web.config import WebConfig
    from tests.test_web import FakeBot, FakeBotDB
    HAVE_PG = True
except ImportError:                                    # pragma: no cover
    HAVE_PG = False

D = Decimal
OPEN = ("new", "in_work", "approve", "waiting")


@unittest.skipUnless(HAVE_PG, "pgserver, asyncpg или httpx не установлены")
class TestDemoService(unittest.IsolatedAsyncioTestCase):
    """Один сброс на набор: проверки только читают (кроме детерминизма)."""

    @classmethod
    def setUpClass(cls):
        cls.tz_before = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Moscow"
        time.tzset()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.tmp.name)
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

    async def rows(self, sql: str, *args) -> list[dict]:
        return [dict(r) for r in await self.pool.fetch(sql, *args)]

    # ─────────────────────────── наряды ───────────────────────────

    async def test_integrity_has_only_the_deliberate_debts(self):
        """Наряд у каждого велосипеда в ремонте, ни одного - на арендованном."""
        self.assertLess(self.seconds, 60)
        issues = logic.integrity_issues(
            await self.crm.bikes(limit=10000), await self.crm.active_rentals(),
            await self.crm.open_orders_by_bike(), await self.crm.debtors(200),
            batteries=await self.crm.batteries(limit=10000))
        kinds: dict[str, int] = defaultdict(int)
        for issue in issues:
            kinds[issue["kind"]] += 1
        self.assertIn(kinds.pop("debt_without_rental", 0), (1, 2))
        self.assertEqual(dict(kinds), {})

    async def test_open_orders_follow_the_bikes(self):
        bikes = {b["id"]: b for b in await self.rows("select * from crm.bikes")}
        since = {r["bike_id"]: r["at"] for r in await self.rows(
            "select bike_id, max(changed_at) as at from crm.bike_status_log "
            "group by bike_id")}
        orders = await self.rows(
            "select * from crm.work_orders where bike_id is not null "
            "and status = any($1::text[])", list(OPEN))
        per_bike: dict[int, int] = defaultdict(int)
        for o in orders:
            bike = bikes[o["bike_id"]]
            per_bike[o["bike_id"]] += 1
            self.assertIn(bike["status"], ("repair", "maintenance"), o["no"])
            self.assertEqual(o["location"], bike["location"], o["no"])
            # Наряд открыт тем же действием, что увёл велосипед в ремонт.
            self.assertEqual(o["opened_at"], since[o["bike_id"]], o["no"])
            self.assertEqual(o["payer"], "own", o["no"])
        repair = {i for i, b in bikes.items() if b["status"] == "repair"}
        self.assertTrue(repair <= set(per_bike), repair - set(per_bike))
        self.assertEqual(set(per_bike.values()), {1})
        stages = defaultdict(int)
        for o in orders:
            stages[o["status"]] += 1
        self.assertEqual(stages["waiting"], 1, stages)
        self.assertGreaterEqual(stages["in_work"], 1, stages)

    async def test_closed_orders_wrote_the_repair_journal(self):
        """Как кнопка «Закрыть наряд»: итоги по строкам, одна запись bike_log
        вида repair с себестоимостью и позиции по узлам."""
        orders = await self.rows("select * from crm.work_orders order by id")
        items: dict[int, list[dict]] = defaultdict(list)
        for i in await self.rows("select * from crm.work_order_items order by id"):
            items[i["order_id"]].append(i)
        logs = {r["id"]: r for r in await self.rows(
            "select * from crm.bike_log where kind = 'repair'")}
        repair_items: dict[int, list[dict]] = defaultdict(list)
        for r in await self.rows("select * from crm.repair_items"):
            repair_items[r["log_id"]].append(r)
        done_own = 0
        for o in orders:
            lines = items[o["id"]]
            for line in lines:
                self.assertGreaterEqual(line["created_at"], o["opened_at"], o["no"])
                self.assertLessEqual(line["created_at"],
                                     o["closed_at"] or self.now, o["no"])
            if o["status"] != "done":
                self.assertIsNone(o["log_id"], o["no"])
                continue
            sums = logic.order_totals(lines)
            self.assertEqual((o["total"], o["cost"]), (sums["total"], sums["cost"]),
                             o["no"])
            if o["bike_id"] is None:
                self.assertIsNone(o["log_id"], o["no"])
                continue
            done_own += 1
            log = logs[o["log_id"]]
            self.assertEqual((log["bike_id"], log["cost"], log["note"], log["created_at"]),
                             (o["bike_id"], o["cost"], f"Наряд {o['no']}", o["closed_at"]))
            got = repair_items[o["log_id"]]
            self.assertEqual(len(got), sum(1 for i in lines if i["node"]), o["no"])
            self.assertEqual(sum((r["parts_cost"] + r["labor_cost"] for r in got), D(0)),
                             o["cost"], o["no"])
        self.assertEqual(done_own, len(logs), "ремонт в журнале - только от нарядов")
        self.assertGreater(done_own, 300)
        # Номера сквозные и по времени открытия - как счётчик панели.
        by_time = sorted(orders, key=lambda o: (o["opened_at"], o["id"]))
        self.assertEqual([o["no"] for o in by_time],
                         [logic.order_no(n) for n in range(1, len(orders) + 1)])
        places = {r["name"] for r in await self.rows("select name from crm.locations")}
        self.assertTrue(all(o["location"] in places for o in orders))

    async def test_order_point_and_tech(self):
        """Точка наряда на свой велосипед - его точка в момент открытия (как
        пишет панель), у чужой техники - точка приёма. Работу ведёт техник:
        без него только что принятый, ещё не разобранный наряд."""
        rows = await self.rows(
            """
            select o.no, o.location, o.status, o.tech_id, o.bike_id,
                   (select l.to_location from crm.bike_location_log l
                     where l.bike_id = o.bike_id and l.changed_at <= o.opened_at
                     order by l.changed_at desc, l.id desc limit 1) as bike_point
              from crm.work_orders o
            """)
        for o in rows:
            if o["bike_id"] is not None:
                self.assertEqual(o["location"], o["bike_point"], o["no"])
            else:
                self.assertIsNotNone(o["location"], o["no"])
            if o["status"] in ("in_work", "waiting", "done", "approve"):
                self.assertIsNotNone(o["tech_id"], o["no"])
        self.assertEqual(sum(o["tech_id"] is None for o in rows), 1, "один - только принят")
        techs = {r["id"] for r in await self.rows(
            "select s.id from crm.staff s join crm.access_profiles p on p.id = s.profile_id "
            "where p.code = 'tech'")}
        self.assertTrue({o["tech_id"] for o in rows if o["tech_id"]} <= techs)
        # «По точкам» за 30 дней: сервис с выручкой у каждой точки.
        service = await self.crm.service_by_location(self.now - timedelta(days=30),
                                                     self.now)
        places = [r["name"] for r in await self.rows("select name from crm.locations")]
        for place in places:
            self.assertGreater(service.get(place, {}).get("orders", 0), 0, place)
            self.assertGreater(service.get(place, {}).get("revenue", 0), 0, place)
        self.assertNotIn(None, service, "наряды без точки")

    async def test_external_orders_price_from_the_external_sheet(self):
        orders = await self.rows("select * from crm.work_orders where bike_id is null")
        self.assertTrue(10 <= len(orders) <= 20, len(orders))
        self.assertTrue(all(o["payer"] == "client" for o in orders))
        types = {t["id"]: t for t in await self.crm.work_types()}
        for o in orders:
            lines = await self.crm.order_items(o["id"])
            self.assertTrue(lines, o["no"])
            self.assertEqual(o["estimate"], logic.order_totals_client(lines), o["no"])
            for line in lines:
                if line["work_type_id"] is None:
                    self.assertEqual(line["note"], "Со склада", o["no"])
                    continue
                wt = types[line["work_type_id"]]
                self.assertIn(line["price"], (logic.sheet_price(wt, "ext"),
                                              logic.to_money(wt["price_ext"])), o["no"])
        approve = [o for o in orders if o["status"] == "approve"]
        self.assertIn(len(approve), (1, 2))
        for o in approve:
            self.assertIsNotNone(o["estimate_sent_at"])
            self.assertIsNone(o["approved_at"])
            self.assertIsNone(o["declined_at"])
            self.assertIsNotNone(o["client_id"], "смету отправляют клиенту")
        silent = [logic.estimate_state(o, now=self.now)["too_silent"] for o in approve]
        self.assertIn(True, silent, "одна смета ждёт ответа дольше суток")
        unpaid = logic.orders_unpaid(await self.crm.work_orders(payer="client",
                                                                limit=1000))
        self.assertGreaterEqual(unpaid["count"], 1)

    async def test_repair_invoice_stays_out_of_the_ledger(self):
        """Красная линия: оплаченный счёт за ремонт ставит paid_at наряду и
        ничего не пишет в журнал аренды."""
        invoices = await self.rows(
            "select p.*, o.paid_at as order_paid, o.status as order_status, "
            "o.payer from crm.pay_orders p join crm.work_orders o "
            "on o.id = p.work_order_id")
        self.assertGreaterEqual(len(invoices), 1)
        for p in invoices:
            self.assertEqual((p["kind"], p["status"]), ("repair", "paid"))
            self.assertIsNone(p["ledger_id"])
            self.assertEqual(p["order_paid"], p["paid_at"])
            self.assertEqual((p["order_status"], p["payer"]), ("done", "client"))
            self.assertEqual(await self.pool.fetchval(
                "select count(*) from crm.ledger where note like '%' || $1 || '%'",
                p["no"]), 0)
            self.assertEqual(logic.invoice_state(
                await self.crm.work_order(p["work_order_id"]),
                await self.crm.work_order_invoices(p["work_order_id"]))["stage"], "paid")

    # ─────────────────────────── склад ───────────────────────────

    async def test_stock_never_goes_negative(self):
        dips = await self.rows(
            """
            select part_id, created_at, running from (
              select part_id, created_at,
                     sum(qty) over (partition by part_id order by created_at, id)
                       as running
                from crm.part_moves) m
             where running < 0
            """)
        self.assertEqual(dips, [])
        self.assertTrue(all(v >= 0 for v in (await self.crm.stock_map()).values()))

    async def test_move_cost_is_the_unit_cost(self):
        """part_moves.cost - себестоимость ЕДИНИЦЫ: строка наряда со склада
        несёт её же, документ - qty * cost, а средняя - average_cost."""
        moves = await self.rows("select * from crm.part_moves order by created_at, id")
        items = {i["move_id"]: i for i in await self.rows(
            "select * from crm.work_order_items where move_id is not null")}
        orders = {o["id"]: o for o in await self.rows("select * from crm.work_orders")}
        for m in moves:
            self.assertGreater(m["cost"], 0, m["id"])
            self.assertLessEqual(m["created_at"], self.now)
            if m["kind"] == "order":
                item = items[m["id"]]
                self.assertEqual((item["qty"], item["parts_cost"], item["order_id"]),
                                 (-m["qty"], m["cost"], m["order_id"]))
                order = orders[m["order_id"]]
                self.assertGreaterEqual(m["created_at"], order["opened_at"])
                self.assertLessEqual(m["created_at"], order["closed_at"] or self.now)
        self.assertEqual(len(items), sum(1 for m in moves if m["kind"] == "order"))
        docs = await self.rows("select * from crm.part_docs")
        for doc in docs:
            lines = [m for m in moves if m["doc_id"] == doc["id"]]
            self.assertTrue(lines, doc["no"])
            self.assertEqual(doc["total"], sum((abs(m["qty"]) * m["cost"] for m in lines),
                                               D(0)), doc["no"])
            prefix = logic.DOC_PREFIX[doc["kind"]]
            self.assertTrue(doc["no"].startswith(prefix + "-"), doc["no"])
        self.assertEqual({d["kind"] for d in docs}, {"receipt", "write_off"})
        parts = {p["id"]: p for p in await self.crm.parts()}
        stock: dict[int, int] = defaultdict(int)
        cost: dict[int, D] = {}
        for m in moves:
            if m["kind"] == "receipt":
                cost[m["part_id"]] = logic.average_cost(
                    stock[m["part_id"]], cost.get(m["part_id"], D(0)), m["qty"], m["cost"])
            stock[m["part_id"]] += m["qty"]
        for part_id, value in cost.items():
            self.assertEqual(parts[part_id]["cost"], value, parts[part_id]["title"])
        self.assertTrue(all(p["node"] for p in parts.values()), "запчасть - к узлу")

    async def test_supplier_order_in_transit_brings_the_waiting_part(self):
        orders = await self.rows("select * from crm.part_orders order by id")
        transit = [o for o in orders if o["status"] == "ordered"]
        self.assertEqual(len(transit), 1)
        items = await self.crm.part_order_items(transit[0]["id"])
        self.assertEqual(transit[0]["total"], logic.order_total(items))
        waiting = await self.rows("select * from crm.work_orders where status = 'waiting'")
        self.assertEqual(len(waiting), 1)
        linked = [i for i in items if i["source"] == "order"]
        self.assertEqual([i["work_order_id"] for i in linked], [waiting[0]["id"]])
        stock = await self.crm.stock_map()
        self.assertEqual(stock.get(linked[0]["part_id"], 0), 0, "ждут того, чего нет")
        needs = await self.crm.waiting_orders_parts()
        self.assertEqual({n["part_id"] for n in needs}, {linked[0]["part_id"]})
        below = [i for i in items if i["source"] == "min_stock"]
        self.assertTrue(below, "в пути и то, что ниже неснижаемого")
        received = [o for o in orders if o["status"] == "received"]
        self.assertTrue(received)
        for o in received:
            doc = await self.pool.fetchrow("select * from crm.part_docs where id = $1",
                                           o["doc_id"])
            self.assertEqual(doc["note"], f"Заказ {o['no']}")
            self.assertEqual(doc["total"], o["total"])
            self.assertEqual(doc["created_at"], o["closed_at"])
        self.assertEqual(sum(o["status"] == "new" for o in orders), 1)

    # ─────────────────────────── пересчёт ───────────────────────────

    async def test_stock_take_last_month(self):
        """Ведомость сервиса - по всему парку в прошлом месяце, с найденными,
        недостачей и лишними. Ведомости других модулей (seed_extras) обязаны
        сходиться так же: счётчики шапки = строки, номера - по времени."""
        takes = await self.rows("select * from crm.stock_takes order by id")
        first = self.today.replace(day=1)
        whole = [t for t in takes if t["scope"] == "all"]
        self.assertEqual(len(whole), 1, "ведомость всего парка - одна, от сервиса")
        self.assertTrue(all(t["status"] == "done" for t in takes), "открытых нет")
        for take in takes:
            started = take["started_at"].astimezone(MSK).date()
            self.assertTrue(first - timedelta(days=31) <= started < first, started)
            items = await self.crm.take_items(take["id"])
            counts = logic.take_counts(items)
            self.assertEqual(
                (take["expected"], take["found"], take["missing"], take["extra"]),
                (counts["total"], counts["found"], counts["missing"], counts["extra"]),
                take["no"])
            self.assertEqual(counts["expected"], 0, "закрытая - без неотмеченных")
            if take["scope"] == "all":
                self.assertTrue(counts["found"] and counts["missing"] and counts["extra"],
                                counts)
            for item in items:
                if item["state"] in ("found", "missing"):
                    status = await self.pool.fetchval(
                        "select to_status from crm.bike_status_log where bike_id = $1 "
                        "and changed_at <= $2 order by changed_at desc, id desc limit 1",
                        item["bike_id"], take["started_at"])
                    self.assertIn(status, logic.TAKE_EXPECTED_STATUSES, take["no"])
        by_time = sorted(takes, key=lambda t: (t["started_at"], t["id"]))
        self.assertEqual([t["no"] for t in by_time],
                         [logic.take_no(n) for n in range(1, len(takes) + 1)])

    # ─────────────────────────── время и повтор ───────────────────────────

    async def test_nothing_in_the_future(self):
        checks = {
            "work_orders": ("opened_at", "closed_at", "paid_at", "estimate_sent_at",
                            "approved_at", "declined_at"),
            "work_order_items": ("created_at",), "part_moves": ("created_at",),
            "part_docs": ("created_at",), "bike_log": ("created_at",),
            "repair_items": ("created_at",),
            "part_orders": ("created_at", "ordered_at", "closed_at"),
            "pay_orders": ("created_at", "sent_at", "paid_at"),
            "stock_takes": ("started_at", "closed_at"),
        }
        for table, columns in checks.items():
            for column in columns:
                latest = await self.pool.fetchval(f"select max({column}) from crm.{table}")
                if latest is not None:
                    self.assertLessEqual(latest, self.now, f"{table}.{column}")

    async def test_same_seed_same_service(self):
        async def digest() -> list:
            return list(await self.pool.fetchrow(
                """
                select (select md5(string_agg(concat_ws('|', no, bike_id, status, total,
                                                        cost, opened_at, closed_at),
                                              ',' order by id)) from crm.work_orders),
                       (select md5(string_agg(concat_ws('|', part_id, kind, qty, cost,
                                                        created_at),
                                              ',' order by id)) from crm.part_moves)
                """))

        before = await digest()
        again = await seed.reset(self.pool, today=self.today, now=self.now)
        self.assertEqual(again, self.summary)
        self.assertEqual(await digest(), before)

    def test_price_list_rows_exist(self):
        """Рецепты ссылаются на прайс владельца по названию: переименование
        строки в schema.sql должно ронять этот тест, а не ночной сброс."""
        titles = {r.work for r in seed_service.RECIPES}
        titles |= {w[0] for e in seed_service.EXTERNAL for w in e[2]}
        titles |= {seed_service.TO_WORK, seed_service.TO_PADS, seed_service.TO_FLUID}
        parts = {p[0] for p in seed_service.PARTS}
        self.assertTrue({r.part for r in seed_service.RECIPES} <= parts)
        self.assertTrue({w[1] for e in seed_service.EXTERNAL for w in e[2]
                         if w[1]} <= parts)
        schema = seed.SCHEMA.read_text(encoding="utf-8")
        for title in titles:
            self.assertIn(f"('{title}',", schema, title)

    # ─────────────────────────── страницы ───────────────────────────

    async def test_service_pages_open_for_the_demo_owner(self):
        cfg = WebConfig(pg={}, secret="test-secret", admin_login="admin",
                        admin_password="admin-pass-123", bot_token="",
                        storage_dir=Path("/tmp/kyc"), port=8080, remind_before_days=2)
        app = create_app(crm=self.crm, db=FakeBotDB(), cfg=cfg, bot=FakeBot())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               follow_redirects=False) as client:
            r = await client.post("/login", data={"login": "demo", "password": "demo"})
            self.assertEqual(r.status_code, 303)
            one = self.pool.fetchval
            ids = {
                "waiting": await one("select id from crm.work_orders "
                                     "where status = 'waiting'"),
                "approve": await one("select id from crm.work_orders "
                                     "where status = 'approve' limit 1"),
                "invoice": await one("select work_order_id from crm.pay_orders limit 1"),
                "own": await one("select id from crm.work_orders where status = 'done' "
                                 "and bike_id is not null order by id desc limit 1"),
                "part": await one("select id from crm.parts order by id limit 1"),
                "take": await one("select id from crm.stock_takes limit 1"),
                "bike": await one("select id from crm.bikes where status = 'repair' "
                                  "limit 1"),
                "client": await one("select client_id from crm.work_orders "
                                    "where bike_id is null and client_id is not null "
                                    "limit 1"),
            }
            pages = ["/", "/service", "/service.csv", "/orders", "/orders.xlsx",
                     "/orders?status=approve", "/orders?status=waiting",
                     "/orders?payer=client", "/orders?location=none", "/work-types",
                     "/parts", "/parts.csv", "/parts/receipts", "/parts/write-offs",
                     "/parts/moves", "/suppliers", "/part-orders", "/stock-takes",
                     "/stock-takes.csv", "/reports/techs", "/reports/model-parts",
                     "/reports/spend", "/reports/points", "/reports/integrity",
                     f"/orders/{ids['waiting']}", f"/orders/{ids['approve']}",
                     f"/orders/{ids['invoice']}", f"/orders/{ids['own']}",
                     f"/parts/{ids['part']}", f"/stock-takes/{ids['take']}",
                     f"/bikes/{ids['bike']}", f"/orders?bike={ids['bike']}",
                     f"/clients/{ids['client']}"]
            # По наряду каждого вида (статус × плательщик × свой/чужой),
            # каждая запчасть и каждая точка - у всех свои ветки шаблона.
            pages += [f"/orders/{r['id']}" for r in await self.rows(
                "select min(id) as id from crm.work_orders "
                "group by status, payer, bike_id is null")]
            pages += [f"/parts/{r['id']}" for r in await self.rows(
                "select id from crm.parts")]
            pages += [f"/reports/points/{r['id']}" for r in await self.rows(
                "select id from crm.locations")] + ["/reports/points/none"]
            for path in pages:
                r = await client.get(path)
                self.assertEqual(r.status_code, 200, f"{path}: {r.status_code}")


if __name__ == "__main__":
    unittest.main()
