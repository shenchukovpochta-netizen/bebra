"""«Тарифы» и «Что купить» на живом Postgres - и то же на FakeCrm.

Своя история: выдачи двух сроков, продление, возврат и повторная выдача
того же велосипеда, замена, ранняя сдача, долг, платёж без аренды в
записи и клиент без аренд, велосипед «в аренде» без аренды. Всё сдвинуто
назад, чтобы дни в аренде были днями. Проверяется, что строки
tariff_rentals складываются в общие числа панели, что CrmDB и заглушка на
тех же строках базы говорят одно и то же, и что дни моделей по суткам
сходятся с отчётом «По точкам».

Нужен pgserver, как в tests/test_points_pg.py; без него набор пропускается.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import asyncpg
    import pgserver

    from app.crm import logic, service
    from app.crm.db import CrmDB
    from app.db import Database, _init_connection
    from tests.fake_crm import FakeCrm
    HAVE_PG = True
except ImportError:                                    # pragma: no cover
    HAVE_PG = False

D = Decimal
SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"
POINT = "Павлюхина"


@unittest.skipUnless(HAVE_PG, "pgserver или asyncpg не установлены")
class TestTariffsAndBuyOnPostgres(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Сутки аренд и журналов - по поясу сессии базы, он из TZ.
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
        self.crm = CrmDB(self.pool)
        self.ids = await self.story()
        self.now = datetime.now().astimezone()
        self.since = self.now - timedelta(days=10)

    async def asyncTearDown(self):
        await self.pool.close()

    async def shift_back(self, days):
        """Вся история - на `days` суток назад, во всех журналах и датах разом."""
        gap = timedelta(days=days)
        # Время заведения аренды и замены - тоже: по нему «в аренде» ищет
        # выдачу, открывшую интервал.
        for table, col in (("bike_status_log", "changed_at"),
                           ("bike_location_log", "changed_at"), ("ledger", "created_at"),
                           ("rentals", "created_at"), ("rental_bikes", "created_at")):
            await self.pool.execute(f"update crm.{table} set {col} = {col} - $1::interval",
                                    gap)
        await self.pool.execute(
            "update crm.rentals set started_on = started_on - $1::int, "
            "closed_on = closed_on - $1::int, billed_until = billed_until - $1::int", days)
        await self.pool.execute(
            "update crm.ledger set period_from = period_from - $1::int, "
            "period_to = period_to - $1::int", days)
        await self.pool.execute(
            "update crm.rental_bikes set issued_on = issued_on - $1::int, "
            "returned_on = returned_on - $1::int", days)

    async def story(self):
        """Т-14: три выдачи и «в аренде» без аренды; Т-7: продление, платёж
        без аренды в записи, клиент без аренд, сдача и повторная выдача того
        же велосипеда, замена; Т: сдача в срок, ранняя сдача с долгом, новая
        выдача только что сданного велосипеда."""
        crm = self.crm
        week = await crm.tariff(await crm.create_tariff("Неделя", 7, D(3000), None))
        month = await crm.tariff(await crm.create_tariff("Месяц", 30, D(9000), None))
        await crm.create_bike_model(title="Городской", brand=None, factory_title="M1",
                                    battery_slots=1, note=None)
        bikes = {code: await crm.create_bike(code=code, model=model, location=POINT,
                                             by="staff:op")
                 for code, model in (("B1", "M1"), ("B2", "M2"), ("B3", "M1"),
                                     ("B4", "M2"), ("B5", "M1"))}
        clients = {n: await crm.create_client(full_name=f"Клиент {n}",
                                              phone=f"+79990000{n:03d}")
                   for n in range(1, 7)}

        async def rent(n, code, tariff):
            today = date.today()
            rid = await service.open_rental(
                crm, client=await crm.client(clients[n]), bike=await crm.bike(bikes[code]),
                tariff=tariff, started_on=today, contract_no=None, by="staff:op",
                billing="manual")
            await crm.charge_period(rid, clients[n], period_from=today,
                                    period_to=today + timedelta(days=tariff["period_days"]),
                                    amount=-tariff["price"], note=tariff["name"])
            return rid

        async def pay(n, amount, rental_id=None):
            await crm.add_ledger(client_id=clients[n], kind="payment", amount=D(amount),
                                 rental_id=rental_id, method="sbp", created_by="staff:op")

        async def close(rid, closed_on):
            await service.close_rental(crm, await crm.rental(rid), closed_on=closed_on,
                                       note=None, by="staff:op")

        r1 = await rent(1, "B1", week)
        await pay(1, 3000, r1)
        r2 = await rent(2, "B2", month)
        await pay(2, 9000)
        r3 = await rent(3, "B3", week)
        await pay(3, 3000, r3)
        await crm.update_bike(bikes["B5"], status="rented", by="import")
        await self.shift_back(7)

        await crm.charge_period(r1, clients[1], period_from=date.today(),
                                period_to=date.today() + timedelta(days=7),
                                amount=D(-3000), note="Продление")
        await pay(1, 3000)
        await pay(6, 500)
        await close(r3, date.today())
        r4 = await rent(4, "B3", week)
        await service.swap_bike(crm, await crm.rental(r2), await crm.bike(bikes["B4"]),
                                reason="repair", old_status="available", by="staff:op")
        await self.shift_back(7)

        await close(r1, date.today())
        await close(r4, date.today() - timedelta(days=2))
        r5 = await rent(5, "B1", week)
        await pay(5, 1000, r5)
        return {"rentals": {"r1": r1, "r2": r2, "r3": r3, "r4": r4, "r5": r5},
                "bikes": bikes, "week": week, "month": month}

    async def mirror(self) -> FakeCrm:
        """Заглушка на строках базы: паритет правил, а не двух историй с
        разными миллисекундами. Время записей журнала - в поясе процесса,
        как сутки сессии базы."""
        async def rows(table):
            return [dict(r) for r in await self.pool.fetch(
                f"select * from crm.{table} order by id")]
        fake = FakeCrm()
        fake.bikes_ = {r["id"]: r for r in await rows("bikes")}
        fake.rentals_ = {r["id"]: r for r in await rows("rentals")}
        fake.rental_bikes_ = await rows("rental_bikes")
        fake.ledger_ = [{**r, "created_at": r["created_at"].astimezone()}
                        for r in await rows("ledger")]
        fake.status_log_ = await rows("bike_status_log")
        fake.location_log_ = await rows("bike_location_log")
        return fake

    # ─── тарифы ───

    async def test_tariff_rentals_add_up_to_the_panel(self):
        got = {r["id"]: r for r in await self.crm.tariff_rentals(self.since, self.now)}
        ids = self.ids["rentals"]
        name = {v: k for k, v in ids.items()}
        self.assertEqual({name.get(k) for k in got}, {"r1", "r2", "r3", "r4", "r5", None})
        row = {name.get(k): v for k, v in got.items()}
        self.assertEqual({k: (v["issued"], v["finished"]) for k, v in row.items()},
                         {"r1": (False, True), "r2": (False, False), "r3": (False, True),
                          "r4": (True, True), "r5": (True, False), None: (False, False)})
        # Платёж клиента 1 без аренды в записи - его аренде; клиент без
        # аренд - строке «без аренды»; платежи Т-14 - вне периода.
        self.assertEqual({k: v["paid"] for k, v in row.items()},
                         {"r1": D(3000), "r2": D(0), "r3": D(0), "r4": D(0),
                          "r5": D(1000), None: D(500)})
        self.assertEqual(sum(v["paid"] for v in row.values()),
                         await self.crm.rental_revenue(self.since, self.now))
        self.assertEqual((row["r1"]["renewals"], row["r1"]["charged"]), (1, D(6000)))
        self.assertEqual((row["r4"]["debt"], row["r4"]["charged"]), (D(3000), D(3000)))
        self.assertEqual(row["r2"]["charged"], D(0), "исход - только у закрытых")
        self.assertTrue(logic.early_return(row["r4"]))
        self.assertFalse(logic.early_return(row["r1"]))
        self.assertEqual(row["r2"]["model"], "M2", "модель - велосипеда после замены")
        # Дни: замена оставляет дни той же аренде, повторная выдача того же
        # велосипеда в день сдачи - новой, «в аренде» без аренды - сироте.
        days = {k: float(v["rented_days"]) for k, v in row.items()}
        for key, expected in (("r1", 10), ("r2", 10), ("r3", 3), ("r4", 7), ("r5", 0),
                              (None, 10)):
            self.assertAlmostEqual(days[key], expected, delta=0.01, msg=key)
        overall = await self.crm.bike_days_by_status(self.since, self.now)
        self.assertAlmostEqual(sum(v["rented_days"] for v in row.values()),
                               overall["rented"], delta=D("1e-6"))

        aliases = logic.model_aliases(await self.crm.bike_models())
        report = logic.tariff_rows(got.values(), by_model=True, aliases=aliases)
        titles = {r["title"]: r for r in report["rows"]}
        self.assertEqual(set(titles), {"Неделя · Городской", "Месяц · M2", "без аренды"})
        week = titles["Неделя · Городской"]
        self.assertEqual((week["issued"], week["finished"], week["renewed_share"],
                          week["early_share"], week["debt"], week["debt_share"]),
                         (2, 3, 33.3, 33.3, D(3000), 25.0))
        panel = logic.fleet_metrics(overall, await self.crm.rental_revenue(self.since,
                                                                           self.now))
        self.assertLessEqual(abs(report["total"]["avg_check"] - panel["avg_check"]),
                             D("0.01"), "чек «Итого» - чек сводки")

    async def test_fake_crm_attributes_the_same(self):
        real = await self.crm.tariff_rentals(self.since, self.now)
        fake = await (await self.mirror()).tariff_rentals(self.since, self.now)
        self.assertEqual([r["id"] for r in fake], [r["id"] for r in real])
        for a, b in zip(real, fake, strict=True):
            for key in ("issued", "finished", "paid", "charged", "renewals", "debt",
                        "period_days", "model", "started_on", "closed_on", "tariff_id"):
                self.assertEqual(a[key], b[key], (a["id"], key))
            self.assertAlmostEqual(a["rented_days"], b["rented_days"], delta=D("1e-6"),
                                   msg=a["id"])

    # ─── исход аренды: продления, срок выдачи, чужие дни, потери ───

    async def extra(self, n, code, *, model="M1"):
        """Свой клиент и велосипед сверх истории - для отдельного случая."""
        bike = await self.crm.create_bike(code=code, model=model, location=POINT,
                                          by="staff:op")
        client = await self.crm.create_client(full_name=f"Клиент {n}",
                                              phone=f"+79990001{n:03d}")
        return await self.crm.client(client), await self.crm.bike(bike)

    async def rows_of(self, *ids, days=70):
        """Строки tariff_rentals этих аренд - из базы и из заглушки на её
        строках: правило одно, и ответ обязан совпасть."""
        now = datetime.now().astimezone()
        since = now - timedelta(days=days)
        real = {r["id"]: r for r in await self.crm.tariff_rentals(since, now)}
        fake = {r["id"]: r for r in
                await (await self.mirror()).tariff_rentals(since, now)}
        for rid in ids:
            for key in ("period_days", "base_price", "tariff_changed", "renewals",
                        "term_from", "term_to", "lost", "finished", "issued"):
                self.assertEqual(real[rid][key], fake[rid][key], (rid, key))
            self.assertAlmostEqual(real[rid]["rented_days"], fake[rid]["rented_days"],
                                   delta=D("1e-6"), msg=rid)
        return [real[rid] for rid in ids]

    async def test_charge_on_the_return_day_is_not_a_renewal(self):
        """Ночной проход начисляет следующую неделю утром дня платежа. Сдал
        в тот же день - не продлил и сдал в срок; назавтра - прожил день
        нового срока: продлил и сдал раньше."""
        week = self.ids["week"]
        ids = []
        for n, code in ((21, "B21"), (22, "B22")):
            client, bike = await self.extra(n, code)
            ids.append(await service.open_rental(
                self.crm, client=client, bike=bike, tariff=week, started_on=date.today(),
                contract_no=None, by="staff:op"))
        await self.shift_back(8)
        await service.charge_all(self.crm, today=date.today() - timedelta(days=1))
        on_time, late = ids
        await service.close_rental(self.crm, await self.crm.rental(on_time),
                                   closed_on=date.today() - timedelta(days=1), note=None,
                                   by="staff:op")
        await service.close_rental(self.crm, await self.crm.rental(late),
                                   closed_on=date.today(), note=None, by="staff:op")
        a, b = await self.rows_of(on_time, late)
        self.assertEqual((a["renewals"], logic.early_return(a)), (0, False))
        self.assertEqual((b["renewals"], logic.early_return(b)), (1, True))
        self.assertEqual((a["term_from"], a["term_to"]),
                         (date.today() - timedelta(days=8), date.today() - timedelta(days=1)))
        row = logic.tariff_rows([a, b])["rows"][0]
        self.assertEqual((row["renewed_share"], row["early_share"]), (50.0, 50.0))

    async def test_changed_tariff_stays_under_the_issue_term(self):
        """Неделя, два продления неделями, потом месяц, сдан в последний
        оплаченный день: аренда - недели, продлений три, в срок."""
        week, month = self.ids["week"], self.ids["month"]
        two = await self.crm.tariff(await self.crm.create_tariff("Две недели", 14,
                                                                 D(6000), None))
        client, bike = await self.extra(23, "B23")
        rid = await service.open_rental(self.crm, client=client, bike=bike, tariff=week,
                                        started_on=date.today(), contract_no=None,
                                        by="staff:op", billing="manual")
        today = date.today()
        for n in range(3):
            await self.crm.charge_period(
                rid, client["id"], period_from=today + timedelta(days=7 * n),
                period_to=today + timedelta(days=7 * n + 7), amount=D(-3000), note="w")
        await service.change_tariff(self.crm, await self.crm.rental(rid), month,
                                    billing="manual")
        await self.crm.charge_period(rid, client["id"], period_from=today + timedelta(days=21),
                                     period_to=today + timedelta(days=51), amount=D(-9000),
                                     note="m")
        # Вторая смена выдачу не перетирает.
        await service.change_tariff(self.crm, await self.crm.rental(rid), two,
                                    billing="manual")
        await service.change_tariff(self.crm, await self.crm.rental(rid), month,
                                    billing="manual")
        kept = await self.pool.fetchrow(
            "select period_days, issue_period_days, issue_base_price from crm.rentals "
            "where id = $1", rid)
        self.assertEqual(tuple(kept), (30, 7, D(3000)))
        await self.shift_back(51)
        await service.close_rental(self.crm, await self.crm.rental(rid),
                                   closed_on=date.today(), note=None, by="staff:op")
        (row,) = await self.rows_of(rid)
        self.assertEqual((row["period_days"], row["base_price"], row["tariff_changed"]),
                         (7, D(3000), True))
        self.assertEqual((row["renewals"], logic.early_return(row)), (3, False))
        report = logic.tariff_rows([row])["rows"][0]
        self.assertEqual((report["title"], report["names"], report["price_per_day"]),
                         ("Неделя", [], D("428.57")), "имя и цена - не нового срока")
        # Посреди оплаченного месяца - раньше срока, хотя «30 дней от начала»
        # попадает ровно на его границу.
        self.assertTrue(logic.early_return({**row, "closed_on": row["started_on"]
                                            + timedelta(days=30)}))

    async def test_future_dated_reissue_keeps_its_days(self):
        """Сдан сегодня и тут же выдан с завтрашнего дня: «в аренде» с
        сегодняшнего вечера - дни новой аренды, а не прошлой."""
        week, month = self.ids["week"], self.ids["month"]
        first, bike = await self.extra(24, "B24")
        second, _ = await self.extra(25, "B25")
        was = await service.open_rental(self.crm, client=first, bike=bike, tariff=week,
                                        started_on=date.today(), contract_no=None,
                                        by="staff:op", billing="manual")
        await self.shift_back(10)
        await service.close_rental(self.crm, await self.crm.rental(was),
                                   closed_on=date.today(), note=None, by="staff:op")
        now = await service.open_rental(self.crm, client=second,
                                        bike=await self.crm.bike(bike["id"]), tariff=month,
                                        started_on=date.today() + timedelta(days=1),
                                        contract_no=None, by="staff:op", billing="manual")
        # Ещё до своего дня: общих суток с новой нет, но открыла интервал она.
        a, b = await self.rows_of(was, now, days=30)
        self.assertAlmostEqual(float(a["rented_days"]), 10, delta=0.01)
        self.assertGreater(b["rented_days"], 0)
        await self.shift_back(5)
        a, b = await self.rows_of(was, now, days=30)
        self.assertAlmostEqual(float(a["rented_days"]), 10, delta=0.01)
        self.assertAlmostEqual(float(b["rented_days"]), 5, delta=0.01)

    async def test_rented_without_a_rental_is_not_the_nearest_rental(self):
        """Импорт ставит «в аренде» и без клиента. Эти дни - «без аренды»:
        ни выданной после них, ни сданной задолго до них аренде они не
        принадлежат - их дни без платежей роняли бы её чек."""
        week = self.ids["week"]
        client, bike = await self.extra(27, "B27")
        old = await service.open_rental(self.crm, client=client, bike=bike, tariff=week,
                                        started_on=date.today(), contract_no=None,
                                        by="staff:op", billing="manual")
        await self.shift_back(10)
        await service.close_rental(self.crm, await self.crm.rental(old),
                                   closed_on=date.today(), note=None, by="staff:op")
        await self.shift_back(20)
        await self.crm.update_bike(bike["id"], status="rented", by="import")
        await self.shift_back(20)
        await self.crm.update_bike(bike["id"], status="available", by="staff:op")
        await self.shift_back(1)
        other, _ = await self.extra(28, "B28")
        new = await service.open_rental(self.crm, client=other,
                                        bike=await self.crm.bike(bike["id"]), tariff=week,
                                        started_on=date.today(), contract_no=None,
                                        by="staff:op", billing="manual")
        await self.shift_back(9)
        # Сдана за 50 дней, «в аренде» без клиента - с 30-го по 10-й, новая - с 9-го.
        (row,) = await self.rows_of(new, days=45)
        self.assertAlmostEqual(float(row["rented_days"]), 9, delta=0.01)
        now = datetime.now().astimezone()
        since = now - timedelta(days=45)
        real = await self.crm.tariff_rentals(since, now)
        fake = await (await self.mirror()).tariff_rentals(since, now)
        self.assertNotIn(old, [r["id"] for r in real], "давняя аренда чужих дней не берёт")
        self.assertEqual([r["id"] for r in fake], [r["id"] for r in real])
        overall = await self.crm.bike_days_by_status(since, now)
        self.assertAlmostEqual(sum(r["rented_days"] for r in real), overall["rented"],
                               delta=D("1e-6"), msg="дни без аренды не теряются")

    async def test_theft_is_not_a_return(self):
        """Признанная потерянной закрыта, но не сдана: не «раньше срока» и
        не в среднем сроке, долг - при ней."""
        client, bike = await self.extra(26, "B26")
        week = self.ids["week"]
        rid = await service.open_rental(self.crm, client=client, bike=bike, tariff=week,
                                        started_on=date.today(), contract_no=None,
                                        by="staff:op", billing="manual")
        for n in range(3):
            await self.crm.charge_period(
                rid, client["id"], period_from=date.today() + timedelta(days=7 * n),
                period_to=date.today() + timedelta(days=7 * n + 7), amount=D(-3000),
                note="w")
        await self.shift_back(16)
        await service.declare_theft(self.crm, await self.crm.rental(rid), note=None,
                                    by="staff:op")
        (row,) = await self.rows_of(rid)
        self.assertTrue(row["lost"])
        self.assertFalse(logic.early_return(row))
        report = logic.tariff_rows([row])["rows"][0]
        self.assertEqual((report["finished"], report["lost"], report["returned"]), (1, 1, 0))
        self.assertIsNone(report["avg_days"])
        self.assertIsNone(report["early_share"])
        self.assertEqual(report["debt"], D(9000))
        # Сданный обычным порядком - не потерянный.
        story = await self.rows_of(self.ids["rentals"]["r1"], self.ids["rentals"]["r4"])
        self.assertEqual([r["lost"] for r in story], [False, False])

    # ─── что купить ───

    async def test_model_days_match_points_and_the_fake(self):
        first = date.today() - timedelta(days=20)
        rows = await self.crm.model_point_days(first, date.today())
        self.assertEqual({r["location"] for r in rows}, {POINT})
        # По модели и суткам - те же дни, что «По точкам» за то же окно.
        start = datetime.combine(first, datetime.min.time(), tzinfo=self.now.tzinfo)
        places = await self.crm.bike_days_by_location(start, datetime.now().astimezone())
        for status in logic.OPERATIONAL_STATUSES:
            self.assertAlmostEqual(
                sum(r["days"] for r in rows if r["status"] == status),
                places.get(POINT, {}).get(status, D(0)), delta=D("0.001"), msg=status)
        fake = await (await self.mirror()).model_point_days(first, date.today())

        def cells(items):
            return {(r["model"], r["location"], r["day"], r["status"]): r["days"]
                    for r in items}
        real, mirror = cells(rows), cells(fake)
        self.assertEqual(set(real), set(mirror))
        for key, value in real.items():
            self.assertAlmostEqual(value, mirror[key], delta=D("0.001"), msg=key)

        aliases = logic.model_aliases(await self.crm.bike_models())
        zero = logic.zero_free_days(rows, before=date.today(), aliases=aliases)
        # Городской (M1) ездил весь период, свободного не было ни дня; у M2
        # после замены снятый велосипед стоял свободным.
        self.assertGreaterEqual(zero["Городской"][POINT], 13)
        self.assertNotIn("M2", zero)
        presence = logic.model_presence(rows, now=datetime.now().astimezone(),
                                        aliases=aliases)
        self.assertGreater(presence["Городской"], D(14))
        self.assertLess(presence["Городской"], D(15))
        days = logic.model_days(rows, aliases=aliases)
        self.assertEqual(set(days), {"Городской", "M2"})

    async def test_model_days_from_the_floor(self):
        """«С» 2000 года - те же строки, что с первого дня журнала, и не
        вложенный цикл «сутки окна × интервалы»: на трёх тысячах
        интервалов (два года журнала) он шёл секундами, на журнале
        побольше - за предел запроса и в 500."""
        await self.pool.execute("""
            insert into crm.bike_status_log (bike_id, from_status, to_status, changed_at)
            select b.id, null, (array['available', 'repair'])[1 + n % 2],
                   now() - interval '700 days' + n * interval '1 day'
                     + b.id * interval '1 hour'
              from crm.bikes b, generate_series(0, 599) n""")
        first = await self.pool.fetchval("select min(changed_at)::date "
                                         "from crm.bike_status_log")
        started = time.monotonic()
        floor = await self.crm.model_point_days(logic.REPORT_FLOOR, date.today())
        self.assertLess(time.monotonic() - started, 3)
        since_log = await self.crm.model_point_days(first, date.today())

        def cells(items):
            return {(r["model"], r["location"], r["day"], r["status"]): r["days"]
                    for r in items}
        a, b = cells(floor), cells(since_log)
        self.assertEqual(set(a), set(b))
        self.assertEqual(min(r["day"] for r in floor), first)
        for key, value in a.items():
            # Последние сутки идут до этой минуты, а между запросами она сдвинулась.
            self.assertAlmostEqual(value, b[key], delta=D("0.001"), msg=key)


if __name__ == "__main__":
    unittest.main()
