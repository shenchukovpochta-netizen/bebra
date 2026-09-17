"""Панель на настоящем Postgres (pgserver): все страницы и ключевые формы
через ASGI, а не через заглушку базы. Ловит расхождения SQL и заглушки,
которые тесты на FakeCrm не видят. Пропускается без pgserver."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import asyncpg
    import pgserver
    from httpx import ASGITransport, AsyncClient

    from app.crm.db import CrmDB
    from app.db import Database, _init_connection
    from app.web.app import create_app, ensure_admin
    from app.web.config import WebConfig
    from tests.test_import import ROWS, sheet
    from tests.test_web import FakeBot, FakeBotDB
    HAVE_ALL = True
except ImportError:                                    # pragma: no cover
    HAVE_ALL = False

D = Decimal
SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"
PAGES = ("/", "/clients", "/clients.csv", "/bikes", "/bikes?location=none",
         "/bikes?location=Павлюхина&status=repair", "/rentals", "/rentals/new", "/claims",
         "/finance", "/finance.csv", "/tariffs", "/reports", "/staff", "/import", "/bikes/new",
         "/clients/new")


@unittest.skipUnless(HAVE_ALL, "pgserver, asyncpg или httpx не установлены")
class TestPanelOnPostgres(unittest.IsolatedAsyncioTestCase):
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
        await self.pool.execute("drop schema if exists crm cascade; "
                                "drop schema if exists bot cascade")
        await Database(self.pool).apply_schema(SCHEMA)
        self.crm = CrmDB(self.pool)
        self.cfg = WebConfig(pg={}, secret="test-secret", admin_login="admin",
                             admin_password="admin-pass-123", bot_token="",
                             storage_dir=Path("/tmp/kyc"), port=8080, remind_before_days=2)
        await ensure_admin(self.crm, self.cfg)
        self.bot = FakeBot()
        app = create_app(crm=self.crm, db=FakeBotDB(), cfg=self.cfg, bot=self.bot)
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                                  follow_redirects=False)
        r = await self.client.post("/login", data={"login": "admin",
                                                   "password": "admin-pass-123"})
        self.assertEqual(r.status_code, 303)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.pool.close()

    async def get_ok(self, path: str) -> str:
        r = await self.client.get(path)
        self.assertEqual(r.status_code, 200, f"{path}: {r.status_code}")
        return r.text

    async def post(self, path: str, **data) -> str:
        r = await self.client.post(path, data=data)
        self.assertEqual(r.status_code, 303, f"{path}: {r.status_code} {r.text[:200]}")
        return r.headers["location"]

    async def test_every_page_and_main_flows(self):
        for path in PAGES:
            await self.get_ok(path)

        # велосипед со всеми новыми полями, ремонт по узлу, статус, история
        loc = await self.post("/bikes", code="B-1", model="Kugoo V3", frame_no="fr-001",
                              battery_count="2", location="Павлюхина", purchase_price="47000",
                              service_months="24", residual_price="5000",
                              battery_price="9000", battery_service_months="15")
        bike_id = int(loc.rsplit("/", 1)[1])
        page = await self.get_ok(loc)
        self.assertIn("2 950 ₽</b> в месяц", page)
        self.assertIn("История статусов", page)
        await self.post(f"/bikes/{bike_id}/repair", node="brake_pads", parts_cost="400",
                        labor_cost="300", note="передние")
        # Велосипед заведён «на сборке»: в оборот его выпускает сверка,
        # а не смена статуса - её запрос отобьётся.
        await self.crm.commission_bike(bike_id, by="staff:admin")
        await self.post(f"/bikes/{bike_id}/status", status="maintenance", note="ТО")
        page = await self.get_ok(f"/bikes/{bike_id}")
        self.assertIn("Тормоза: колодки: передние", page)
        self.assertIn("На ТО", page)
        log = await self.crm.bike_status_log(bike_id)
        # Заводится велосипед «на сборке», выпускает его ввод в
        # эксплуатацию, и обе смены статуса видны в журнале.
        self.assertEqual([(x["to_status"], x["changed_by"]) for x in log],
                         [("maintenance", "staff:admin"),
                          ("available", "staff:admin"), ("new", "staff:admin")])
        await self.post(f"/bikes/{bike_id}/status", status="available")

        # клиент, тариф, аренда, зачисление заявки, закрытие со списанием
        loc = await self.post("/clients", full_name="Иванов Иван", phone="+7 999 000-00-00")
        client_id = int(loc.rsplit("/", 1)[1])
        await self.post("/tariffs", name="Неделя", period_days="7", price="3000")
        tariff = (await self.crm.tariffs())[0]
        loc = await self.post("/rentals", client_id=str(client_id), bike_id=str(bike_id),
                              tariff_id=str(tariff["id"]), started_on=date.today().isoformat())
        rental_id = int(loc.rsplit("/", 1)[1])
        self.assertEqual(await self.crm.client_balance(client_id), D("-3000.00"))
        await self.get_ok(f"/rentals/{rental_id}")
        await self.get_ok(f"/clients/{client_id}")
        pid = await self.crm.create_claim(client_id, D("3000"))
        await self.post(f"/claims/{pid}/confirm", amount="3000", method="sbp")
        await self.post(f"/claims/{pid}/confirm", amount="3000", method="sbp")   # повтор
        self.assertEqual(await self.crm.client_balance(client_id), D("0.00"))
        self.assertEqual([x["kind"] for x in await self.crm.ledger_of(client_id)],
                         ["payment", "charge"])
        page = await self.get_ok("/")
        self.assertIn("Три числа", page)
        self.assertIn("2 950 ₽", page)                       # отложить на парк
        report = await self.get_ok("/reports")
        self.assertIn("Тормоза: колодки", report)
        self.assertIn("Kugoo V3", report)
        await self.post(f"/rentals/{rental_id}/close", bike_status="written_off",
                        note="рама лопнула")
        self.assertEqual((await self.crm.bike(bike_id))["status"], "written_off")
        self.assertEqual((await self.crm.bike_status_log(bike_id))[0]["changed_by"],
                         "staff:admin")
        page = await self.get_ok("/")
        self.assertIn("Списан: <b>1</b>", page)

    async def test_import_on_postgres_is_idempotent(self):
        data = sheet(ROWS)
        r = await self.client.post("/import", files={"file": ("t.xlsx", data)})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Это сухой прогон", r.text)
        self.assertEqual(await self.crm.bike_counts(), {})
        r = await self.client.post("/import", data={"apply": "1"},
                                   files={"file": ("t.xlsx", data)})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Записано: велосипедов 8, клиентов 4, аренд 3", r.text)
        counts = await self.crm.bike_counts()
        self.assertEqual(counts["rented"], 4)
        self.assertEqual((await self.crm.bike_by_frame("JL20240715478"))["location"],
                         "Павлюхина")
        r = await self.client.post("/import", data={"apply": "1"},
                                   files={"file": ("t.xlsx", data)})
        self.assertIn("Записано: велосипедов 0, клиентов 0, аренд 0", r.text)
        # журнал статусов заведён триггером для каждого импортированного
        for b in await self.crm.bikes():
            log = await self.crm.bike_status_log(b["id"])
            self.assertTrue(log, b["code"])
            self.assertEqual(log[-1]["changed_by"], "staff:admin")
        await self.get_ok("/")
        await self.get_ok("/reports")
        await self.get_ok("/clients")


if __name__ == "__main__":
    unittest.main()
