"""Обучение на засеянном демо: новичок проходит маршрут целиком.

Не выборка шагов, а весь путь через формы панели - те же POST, что жмёт
человек: учебный вход с кнопки на странице входа, клиент, выдача,
пополнение, отметка по сроку, плановое ТО, приём; у мастера - наряд,
работа, запчасть, закрытие, ТО статусом и приход. После каждого шага
его отметка проверяется запросом learn_facts на живом Postgres: так
видно и то, что шаг выполним на данных демо (свободный велосипед на
точке, ТО в прайсе, запчасть на полке), и то, что SQL фактов считает
его сделанным.

Нужен pgserver, как в test_demo_crawl; без него набор пропускается.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import re
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import asyncpg
    import pgserver
    from httpx import ASGITransport, AsyncClient

    from app.crm import learning, logic
    from app.crm.db import CrmDB
    from app.db import Database, _init_connection
    from app.demo import runtime
    from app.demo.world import MSK
    from app.web.app import create_app
    from app.web.config import WebConfig
    HAVE_PG = True
except ImportError:                                    # pragma: no cover
    HAVE_PG = False

SECRET = "demo-learning-secret"
CREDS = re.compile(r"логин (learn-\d+), пароль ([a-z0-9]+)")


@unittest.skipUnless(HAVE_PG, "pgserver или asyncpg не установлены")
class TestLearningOnDemo(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tz_before = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Moscow"
        time.tzset()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.tmp.name)
        files = Path(cls.tmp.name) / "files"
        cls.cfg = dataclasses.replace(
            runtime.demo_config(WebConfig(pg={}, secret=SECRET, admin_login="admin",
                                          admin_password="", bot_token="",
                                          storage_dir=files, port=8080,
                                          remind_before_days=2)),
            storage_dir=files / "kyc", bike_photo_dir=files / "bikes",
            doc_dir=files / "doctemplates")
        cls.now = datetime.now(MSK).replace(microsecond=0)
        asyncio.run(cls._reset())

    @classmethod
    async def _reset(cls):
        pool = await asyncpg.create_pool(cls.pg.get_uri(), min_size=1, max_size=2,
                                         init=_init_connection)
        try:
            await runtime.reset(pool, cls.cfg, now=cls.now)
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
        asyncio.get_running_loop().slow_callback_duration = 5
        self.pool = await asyncpg.create_pool(self.pg.get_uri(), min_size=1, max_size=4,
                                              init=_init_connection)
        self.crm = CrmDB(self.pool)
        self.app = create_app(crm=self.crm, db=Database(self.pool), cfg=self.cfg,
                              bot=None)
        self.app.state.demo_limits = None

    async def asyncTearDown(self):
        await self.pool.close()

    def client(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test",
                           follow_redirects=False)

    async def start(self, web: AsyncClient, track: str) -> tuple[dict, str]:
        """Кнопка «Обучение» на входе: учебный вход, пароль во flash."""
        r = await web.post("/learn/start", data={"track": track})
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/learn"))
        r = await web.get("/learn")
        self.assertEqual(r.status_code, 200)
        found = CREDS.search(r.text)
        self.assertIsNotNone(found, "логин и пароль показаны на первой странице")
        staff = await self.crm.staff_by_login(found.group(1))
        self.assertEqual(learning.track_of(staff), track)
        self.assertIn("Ученик", staff["name"])
        return staff, found.group(2)

    async def facts(self, staff: dict) -> dict:
        return await self.crm.learn_facts(learning.actor(staff), staff["id"])

    async def done(self, staff: dict) -> list[str]:
        track = learning.track_of(staff)
        return learning.done_codes(learning.progress(track, await self.facts(staff))["steps"])

    async def post_ok(self, web: AsyncClient, url: str, data: dict) -> str:
        """POST формы: ответ - редирект, а не страница ошибки. Ошибка
        формы у панели - flash на следующей странице: он и в провал."""
        r = await web.post(url, data=data)
        self.assertEqual(r.status_code, 303, f"{url}: {r.status_code}")
        where = r.headers["location"]
        page = await web.get(where)
        errors = re.findall(r'<div class="flash err">([^<]+)</div>', page.text)
        self.assertEqual(errors, [], f"{url} → {where}")
        self.last_page = page.text
        return where

    async def free_phone(self) -> str:
        for n in range(1000):
            phone = f"+70000999{n:03d}"
            if await self.crm.client_by_phone(phone) is None:
                return phone
        raise AssertionError("нет свободного вымышленного номера")

    async def test_admin_walks_the_whole_track(self):
        web = self.client()
        staff, password = await self.start(web, "admin")
        self.assertEqual(await self.done(staff), [])
        point = staff["location"]
        self.assertTrue(point, "учебному входу дана точка - касса и выдача по ней")
        # Свой учебный велосипед на его точке: выдача не зависит от того,
        # сколько свободных оставили демо и посетители.
        kit = await self.crm.bike((await self.facts(staff))["kit_id"])
        self.assertEqual((kit["code"], kit["status"], kit["location"]),
                         (learning.kit_code(staff["login"]), "available", point))

        # Настоящий номер в демо не принимается: он висел бы у всех на виду.
        before = len(await self.crm.clients(q="Учебный"))
        r = await web.post("/clients", data={"full_name": "Учебный Курьер Иванович",
                                             "phone": "+7 900 123-45-67"})
        self.assertIn("только вымышленные", (await web.get(r.headers["location"])).text)
        self.assertEqual(len(await self.crm.clients(q="Учебный")), before)
        where = await self.post_ok(web, "/clients", {
            "full_name": "Учебный Курьер Иванович", "phone": await self.free_phone(),
            "channel": "avito"})
        client_id = int(where.rsplit("/", 1)[1])
        # Первая страница после шага говорит, что он засчитан, - один раз.
        self.assertIn("✓ Засчитано: Зарегистрируйте клиента", self.last_page)
        self.assertEqual(await self.done(staff), ["client"])
        facts = await self.facts(staff)
        self.assertEqual(facts["client_id"], client_id)
        # Карточка шага ведёт на выдачу уже с этим клиентом.
        page = (await web.get("/")).text
        self.assertIn("Шаг 2. Выдайте ему велосипед", page)
        self.assertIn(f'href="/issue?client={client_id}&amp;bike={kit["id"]}"', page)
        self.assertNotIn("✓ Засчитано", page)

        tariffs = await self.crm.tariffs(active_only=True)
        aliases = logic.model_aliases(await self.crm.bike_models())
        week = next(t for t in tariffs if t["period_days"] == 7
                    and (t.get("kind") or "bike") == "bike")
        bike = kit
        tariff = logic.match_tariff(tariffs, week, bike["model"], aliases=aliases)
        self.assertIsNotNone(tariff, "у модели учебного велосипеда есть недельный тариф")
        where = await self.post_ok(web, "/issue", {
            "client_id": client_id, "tariff_id": tariff["id"], "bike_id": bike["id"],
            "started_on": self.now.date().isoformat(), "pay_amount": str(tariff["price"]),
            "pay_method": "sbp", "mileage": str(bike.get("mileage_km") or 0),
            "location": point})
        self.assertTrue(where.startswith("/issue/docs"), where)
        facts = await self.facts(staff)
        self.assertEqual((facts["issue"], facts["topup"]), (True, False),
                         "оплата при выдаче - не пополнение")
        self.assertEqual(facts["bike_id"], bike["id"])

        await self.post_ok(web, f"/clients/{client_id}/ledger", {
            "kind": "payment", "amount": "1000", "method": "sbp",
            "note": "Продление"})
        self.assertIn("topup", await self.done(staff))

        other = next(r for r in await self.crm.active_rentals()
                     if r["client_id"] != client_id)
        await self.post_ok(web, f"/rentals/{other['id']}/intent",
                           {"intent": "renew", "next": "/"})
        self.assertIn("intent", await self.done(staff))
        # Стенд общий: другой посетитель снял отметку с той же аренды -
        # шаг новичка остаётся засчитанным (журнал отметок, а не поле аренды).
        await self.crm.update_rental(other["id"], intent=None, intent_until=None,
                                     intent_by=None, intent_at=None)
        self.assertIn("intent", await self.done(staff))

        where = await self.post_ok(web, "/orders", {
            "bike_id": bike["id"], "payer": "own", "complaint": "плановое ТО"})
        order_id = int(where.rsplit("/", 1)[1])
        to_type = next(t for t in await self.crm.work_types(active_only=True)
                       if t.get("category") == learning.TO_CATEGORY)
        await self.post_ok(web, f"/orders/{order_id}/items",
                           {"work_type_id": to_type["id"], "qty": "1"})
        self.assertNotIn("to_order", await self.done(staff), "ТО - после закрытия")
        await self.post_ok(web, f"/orders/{order_id}/close", {"bike_status": "available"})
        self.assertIn("to_order", await self.done(staff))
        self.assertEqual((await self.crm.bike(bike["id"]))["status"], "rented",
                         "ТО на велосипеде клиента аренду не снимает")

        rental_id = (await self.facts(staff))["rental_id"]
        await self.post_ok(web, f"/rentals/{rental_id}/close", {
            "closed_on": self.now.date().isoformat(),
            "mileage": str((bike.get("mileage_km") or 0) + 40),
            "return_location": point, "bike_status": "available", "note": "чистый"})
        self.assertEqual(await self.done(staff),
                         [s.code for s in learning.TRACKS["admin"].steps])
        page = (await web.get("/learn")).text
        self.assertIn("Обучение пройдено", page)
        self.assertIn(staff["login"], page)

        # Выход и вход с того же пароля ведёт к шагам, а не на сводку.
        await web.post("/logout")
        again = self.client()
        r = await again.post("/login", data={"login": staff["login"], "password": password})
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/learn"))

    async def test_mechanic_walks_the_whole_track(self):
        web = self.client()
        staff, _ = await self.start(web, "tech")
        bike = await self.crm.bike((await self.facts(staff))["kit_id"])
        self.assertIn(f'href="/orders/new?bike={bike["id"]}"', (await web.get("/service")).text)
        where = await self.post_ok(web, "/orders", {
            "bike_id": bike["id"], "payer": "own", "complaint": "не тянет мотор"})
        order_id = int(where.rsplit("/", 1)[1])
        self.assertEqual((await self.crm.bike(bike["id"]))["status"], "repair")
        self.assertEqual(await self.done(staff), ["order"])
        self.assertIn(f'href="/orders/{order_id}"', (await web.get("/service")).text)
        self.assertIn(f'href="/bikes/{bike["id"]}"', (await web.get("/learn")).text)

        await self.post_ok(web, f"/orders/{order_id}/edit", {
            "status": "in_work", "tech_id": staff["id"], "estimate": "0", "note": ""})
        self.assertIn("take", await self.done(staff))

        work = next(t for t in await self.crm.work_types(active_only=True)
                    if t.get("node") and t.get("category") != learning.TO_CATEGORY)
        await self.post_ok(web, f"/orders/{order_id}/items",
                           {"work_type_id": work["id"], "qty": "1"})
        self.assertIn("work", await self.done(staff))

        stocks = await self.crm.stock_map()
        part = next(p for p in await self.crm.parts(active_only=True)
                    if stocks.get(p["id"], 0) > 0)
        await self.post_ok(web, f"/orders/{order_id}/parts", {"part_id": part["id"],
                                                              "qty": "1"})
        self.assertIn("part", await self.done(staff))

        await self.post_ok(web, f"/orders/{order_id}/close", {"bike_status": "available"})
        self.assertIn("close", await self.done(staff))
        self.assertEqual((await self.crm.bike(bike["id"]))["status"], "available")

        mileage = str((await self.crm.bike(bike["id"])).get("mileage_km") or 0)
        await self.post_ok(web, f"/bikes/{bike['id']}/status",
                           {"status": "maintenance", "mileage": mileage})
        self.assertNotIn("to_status", await self.done(staff), "ТО - и снять с ТО")
        await self.post_ok(web, f"/bikes/{bike['id']}/status", {"status": "available"})
        self.assertIn("to_status", await self.done(staff))

        suppliers = await self.crm.suppliers()
        await self.post_ok(web, "/parts/receipts", {
            "supplier_id": suppliers[0]["id"] if suppliers else "",
            "part_id_0": part["id"], "qty_0": "2", "price_0": "150"})
        self.assertEqual(await self.done(staff),
                         [s.code for s in learning.TRACKS["tech"].steps])
        self.assertIn("Обучение пройдено", (await web.get("/learn")).text)

    async def test_trainee_sees_only_its_track_sections(self):
        """Профиль учебного входа: мастер не видит клиентов и денег,
        администратор - настроек и отчётов."""
        tech = self.client()
        await self.start(tech, "tech")
        self.assertEqual((await tech.get("/clients")).status_code, 403)
        self.assertEqual((await tech.get("/finance")).status_code, 403)
        admin = self.client()
        await self.start(admin, "admin")
        for path in ("/company", "/reports", "/finance"):
            self.assertEqual((await admin.get(path)).status_code, 403, path)
        # В сотрудники демо не пускает никого, учебный вход тем более.
        self.assertEqual((await admin.get("/staff")).status_code, 403)


if __name__ == "__main__":
    unittest.main()
