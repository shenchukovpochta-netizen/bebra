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

    from app.crm import logic
    from app.crm.db import CrmDB
    from app.db import Database, _init_connection
    from app.web.app import create_app, ensure_admin
    from app.web.config import WebConfig
    from tests.plain import plain
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
         "/clients/new", "/reports/points", "/reports/points?month=2026-01",
         "/reports/points?since=2026-01-01&until=2026-01-31", "/reports/points/none",
         "/reports/points.csv", "/reports/points.xlsx", "/locations", "/map",
         "/rentals?location=none", "/rentals.csv?location=Павлюхина",
         "/orders?location=Павлюхина", "/orders.csv?location=none")


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
        return plain(r.text)

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

    async def test_overlong_and_unicode_ids_are_404_not_500(self):
        """Двадцать цифр FastAPI читает в int, а asyncpg в bigint не кладёт:
        карточка падала 500 (value out of int64 range). «²» в пути давал
        JSON 422. Оба - адрес без записи. В параметре списка - просто мусор."""
        huge = "9" * 20
        # Pydantic читает в int и «+N», «-N», « N», «N.0», «1_000»: страж
        # только по голым цифрам пропускал их в базу тем же 500.
        for path in (f"/clients/{huge}", f"/trackers/{huge}", f"/rentals/{huge}",
                     "/clients/%C2%B2", "/bikes/abc", f"/clients/+{huge}",
                     f"/clients/-{huge}", f"/clients/%20{huge}", f"/clients/{huge}%20",
                     f"/clients/{huge}.0", f"/trackers/+{huge}",
                     "/rentals/1_000_000_000_000_000_000_000",
                     "/clients/-9223372036854775809", f"/bikes/-{huge}/photo/x"):
            r = await self.client.get(path)
            self.assertEqual(r.status_code, 404, path)
            self.assertIn("не найден", r.text, path)
        for path in (f"/orders?bike={huge}", f"/orders.csv?bike={huge}",
                     f"/issue?client={huge}", f"/orders/new?bike={huge}"):
            await self.get_ok(path)

    async def test_rental_form_lists_clients_past_the_500th(self):
        """Форма новой аренды брала клиентов пределом по умолчанию - 500 по
        имени: курьер дальше пятисотого в выпадающий список не попадал."""
        await self.pool.execute(
            "insert into crm.clients (full_name, phone) "
            "select 'Аа ' || lpad(n::text, 3, '0'), '+7999' || lpad(n::text, 7, '0') "
            "  from generate_series(1, 500) n")
        last = await self.crm.create_client(full_name="Яковлев Яков", phone="+79880000001")
        page = await self.get_ok("/rentals/new")
        # assertTrue, а не assertIn: провал печатал бы всю страницу.
        self.assertTrue(f'<option value="{last}"' in page and "Яковлев Яков" in page,
                        "клиента за пятисотым нет в списке")

    async def test_history_start_is_the_earliest_record(self):
        """Начало истории - самая ранняя запись журнала статусов или денег:
        с него стрелка «прошлый месяц» кончается."""
        self.assertIsNone(await self.crm.history_start())
        bike = await self.crm.create_bike(code="H-1", model="Kugoo V3")
        client = await self.crm.create_client(full_name="Иванов Иван", phone="+79990000000")
        await self.crm.add_ledger(client_id=client, kind="payment", amount=D(100))
        await self.pool.execute("update crm.ledger set created_at = '2025-03-14 10:00+03'")
        self.assertEqual((await self.crm.history_start()).date(), date(2025, 3, 14))
        await self.pool.execute("update crm.bike_status_log set changed_at = "
                                "'2025-01-31 23:30+03' where bike_id = $1", bike)
        start = await self.crm.history_start()
        self.assertEqual(start.astimezone(logic.MOSCOW).date(), date(2025, 1, 31))
        self.assertNotIn('title="прошлый месяц"', await self.get_ok("/?month=2025-01"))
        self.assertIn('/?month=2025-01" title="прошлый месяц"',
                      await self.get_ok("/?month=2025-02"))

    async def test_first_run_wizard_on_a_fresh_schema(self):
        """Свежая схема - как у нового франчайзи: точки и цены поставочные,
        реквизитов и сотрудников нет. Мастер встречает владельца, шаг цен
        правит поставочный тариф на месте (частичный уникальный индекс
        «модель + срок» не спорит), а готовое обязательное его убирает."""
        async def login() -> str:
            await self.client.post("/logout")
            r = await self.client.post("/login", data={"login": "admin",
                                                       "password": "admin-pass-123"})
            return r.headers["location"]

        self.assertEqual(await login(), "/setup")
        self.assertIn("Первый запуск", await self.get_ok("/setup?step=points"))
        tariffs = await self.crm.tariffs(active_only=True, kind="bike")
        [first, second, *_] = await self.crm.bike_models(active_only=True)
        week = next(t for t in tariffs if t["model"] == first["title"]
                    and t["period_days"] == 7)
        self.assertEqual(await self.post("/setup/prices", **{
            f"use_{first['id']}": "1", f"week_{first['id']}": "2900"}), "/setup")
        self.assertEqual((await self.crm.tariff(week["id"]))["price"], D("2900.00"))
        self.assertEqual(len(await self.crm.tariffs(active_only=True, kind="bike")),
                         len(tariffs), "правка на месте, а не второй тариф на срок")
        models = {m["id"]: m for m in await self.crm.bike_models()}
        self.assertFalse(models[second["id"]]["active"], "без галочки - в архив")
        await self.post("/setup/staff", role="mechanic", login="petr")
        self.assertEqual((await self.crm.staff_by_login("petr"))["profile_code"], "tech")
        self.assertIn("пароль <code>", await self.get_ok("/setup?step=staff"))
        self.assertEqual(await login(), "/setup", "реквизитов ещё нет")
        await self.post("/setup/company", **{
            "company_name": "ООО «Тест»", "company_short": "ООО «Тест»",
            "company_inn": "000000000019", "company_ogrn": "1000000000000",
            "company_address": "Самара", "company_phone": "+7 900",
            "company_bank": "Банк", "company_account": "40702810000000000000",
            "company_bik": "044525000", "company_corr": "30101810000000000000"})
        # «Готовность» довольна (казанские точки с адресом и телефоном), но
        # поставочные точки мастер ещё не показал - встречает дальше.
        self.assertEqual(await login(), "/setup", "точки не пройдены")
        self.assertIn('class="now">2 · Точки', await self.get_ok("/setup"))
        await self.post("/setup/pass", step="points")
        self.assertEqual(await login(), "/", "обязательное готово - мастера нет")
        self.assertNotIn("Продолжить настройку", await self.get_ok("/"))

    async def test_points_report_on_postgres(self):
        """Третья точка через панель, выдача с неё, отчёт и страница точки,
        переименование каскадом - на настоящем SQL, а не на заглушке."""
        loc = await self.post("/locations", name="Декабристов", city="Казань",
                              address="ул. Декабристов, 1", lat="55.81", lon="49.11")
        self.assertEqual(loc, "/locations")
        dek = next(p for p in await self.crm.locations() if p["name"] == "Декабристов")
        loc = await self.post("/bikes", code="D-1", model="Kugoo V3", location="Декабристов")
        bike_id = int(loc.rsplit("/", 1)[1])
        await self.crm.commission_bike(bike_id, by="staff:admin")
        loc = await self.post("/clients", full_name="Денисов Дмитрий",
                              phone="+7 999 000-00-03")
        client_id = int(loc.rsplit("/", 1)[1])
        await self.post("/tariffs", name="Неделя", period_days="7", price="3000")
        tariff = (await self.crm.tariffs())[0]
        loc = await self.post("/rentals", client_id=str(client_id), bike_id=str(bike_id),
                              tariff_id=str(tariff["id"]),
                              started_on=date.today().isoformat())
        rental_id = int(loc.rsplit("/", 1)[1])
        self.assertEqual((await self.crm.rental(rental_id))["location"], "Декабристов")
        await self.crm.add_ledger(client_id=client_id, kind="payment", amount=D(12000),
                                  rental_id=rental_id, method="card")

        page = await self.get_ok("/reports/points")
        for name in ("Павлюхина", "Адоратского", "Декабристов", "Итого"):
            self.assertIn(name, page)
        self.assertIn("История мест ведётся", page,
                      "окно начинается раньше внедрения журнала мест")
        r = await self.client.get("/reports/points.csv")
        rows = {line.split(";")[0]: line.split(";")
                for line in r.text.lstrip("\ufeff").splitlines()}
        head = rows["Точка"]
        self.assertEqual(rows["Декабристов"][head.index("Выручка")], "12000,00")
        self.assertEqual(rows["ИТОГО"][head.index("Выручка")], "12000,00")
        self.assertEqual(rows["Декабристов"][head.index("Идёт аренд")], "1")
        point = await self.get_ok(f"/reports/points/{dek['id']}")
        self.assertIn("ул. Декабристов, 1", point)
        self.assertIn("Денисов Дмитрий", await self.get_ok(
            "/rentals?location=%D0%94%D0%B5%D0%BA%D0%B0%D0%B1%D1%80%D0%B8%D1%81%D1%82%D0%BE%D0%B2"))
        self.assertIn("По точкам за 30 дней", await self.get_ok("/"))
        self.assertIn("map-place", await self.get_ok("/map"))

        await self.post(f"/locations/{dek['id']}/rename", name="Декабристов 1")
        self.assertEqual((await self.crm.bike(bike_id))["location"], "Декабристов 1")
        self.assertEqual((await self.crm.rental(rental_id))["location"], "Декабристов 1")
        page = await self.get_ok("/reports/points")
        self.assertIn("Декабристов 1", page)
        await self.post(f"/locations/{dek['id']}/rename", name="Павлюхина")
        self.assertEqual((await self.crm.bike(bike_id))["location"], "Декабристов 1",
                         "занятое имя - отказ, и не изменилось ничего")

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
