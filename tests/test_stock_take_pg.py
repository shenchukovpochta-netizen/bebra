"""Пересчёт техники на настоящем Postgres (pgserver) через панель: парк в
200 велосипедов и 400 батарей, ввод номеров так, как их вводит человек,
две точки разом, права администратора точки, закрытие и выгрузки.

Ловит то, чего не видит заглушка: замки и условия в SQL, индекс одной
открытой ведомости на точку, разовую выдачу действия в схеме. Без
pgserver набор пропускается.
"""

from __future__ import annotations

import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import asyncpg
    import pgserver
    from httpx import ASGITransport, AsyncClient

    from app.crm import logic, service
    from app.crm.db import CrmDB
    from app.db import Database, _init_connection
    from app.web.app import create_app, ensure_admin
    from app.web.config import WebConfig
    from tests.fake_crm import FakeCrm
    from tests.plain import plain
    from tests.test_web import FakeBot, FakeBotDB
    HAVE_ALL = True
except ImportError:                                    # pragma: no cover
    HAVE_ALL = False

SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"
POINTS = ("Павлюхина", "Адоратского")


@unittest.skipUnless(HAVE_ALL, "pgserver, asyncpg или httpx не установлены")
class TestStockTakeOnPostgres(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(self.pg.get_uri(), min_size=1, max_size=4,
                                              init=_init_connection)
        await self.pool.execute("drop schema if exists crm cascade; "
                                "drop schema if exists bot cascade")
        await Database(self.pool).apply_schema(SCHEMA)
        self.crm = CrmDB(self.pool)
        cfg = WebConfig(pg={}, secret="test-secret", admin_login="admin",
                        admin_password="admin-pass-123", bot_token="",
                        storage_dir=Path("/tmp/kyc"), port=8080, remind_before_days=2)
        await ensure_admin(self.crm, cfg)
        self.app = create_app(crm=self.crm, db=FakeBotDB(), cfg=cfg, bot=FakeBot())
        self.client = await self.login("admin", "admin-pass-123")
        await self.park()

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.pool.close()

    async def login(self, login: str, password: str) -> AsyncClient:
        client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test",
                             follow_redirects=False)
        r = await client.post("/login", data={"login": login, "password": password})
        self.assertEqual(r.status_code, 303)
        return client

    async def park(self) -> None:
        """200 велосипедов и 400 батарей поровну на две точки. Каждый
        десятый велосипед в аренде (его на точке не ждут), МБ-200 потерян."""
        model_id = await self.pool.fetchval(
            "insert into crm.battery_models (title, price) values ('АКБ Тест 60V 21Ah', 9000) "
            "returning id")
        await self.pool.execute(
            """
            insert into crm.bikes (code, model, status, location, motor_no)
            select 'МБ-' || lpad(n::text, 3, '0'), 'Truck +',
                   case when n = 200 then 'lost' when n % 10 = 0 then 'rented'
                        when n % 7 = 0 then 'repair' else 'available' end,
                   case when n % 2 = 1 then $1 else $2 end,
                   '60V240W2305' || lpad(n::text, 6, '0')
              from generate_series(1, 200) n
            """, *POINTS)
        await self.pool.execute(
            """
            insert into crm.batteries (code, model_id, status, location, serial_no)
            select 'АКБ-' || lpad(n::text, 4, '0'), $3, 'available',
                   case when n % 2 = 1 then $1 else $2 end, 'SN-' || (70000 + n)
              from generate_series(1, 400) n
            """, *POINTS, model_id)

    async def get_ok(self, path: str, client: AsyncClient | None = None) -> str:
        r = await (client or self.client).get(path)
        self.assertEqual(r.status_code, 200, f"{path}: {r.status_code}")
        return plain(r.text)

    async def post(self, path: str, client: AsyncClient | None = None, **data) -> str:
        r = await (client or self.client).post(path, data=data)
        self.assertEqual(r.status_code, 303, f"{path}: {r.status_code} {r.text[:300]}")
        return r.headers["location"]

    async def start(self, client: AsyncClient | None = None, **over) -> int:
        data = {"scope": "all", "what": "all", "location": "", "note": ""}
        data.update(over)
        location = await self.post("/stock-takes", client, **data)
        self.assertRegex(location, r"^/stock-takes/\d+$", location)
        return int(location.rsplit("/", 1)[1])

    async def scan(self, take_id: int, code: str) -> str:
        return await self.get_ok(await self.post(f"/stock-takes/{take_id}/scan", code=code))

    async def states(self, take_id: int) -> dict[str, str]:
        return {(i["bike_code"] or i["battery_code"] or i["code"]): i["state"]
                for i in await self.crm.take_items(take_id)}

    async def test_full_park_on_a_phone(self):
        take_id = await self.start(note="Плановый пересчёт")
        items = await self.crm.take_items(take_id)
        # 200 - 19 в аренде - 1 потерян = 180 велосипедов и 400 батарей.
        self.assertEqual(len(items), 580)

        started = time.monotonic()
        page = await self.get_ok(f"/stock-takes/{take_id}")
        took = time.monotonic() - started
        self.assertLess(took, 5, "ведомость на 600 строк открывается быстро")
        self.assertLess(len(page.encode()), 1_500_000, "и весит разумно для телефона")
        self.assertIn("Велосипед · Truck +", page)
        self.assertIn("АКБ Тест 60V 21Ah", page)
        self.assertIn('class="cards take-table"', page, "на телефоне - карточки")
        # Фильтр строк сокращает страницу до того, что ещё ищут.
        left = await self.get_ok(f"/stock-takes/{take_id}?show=extra")
        self.assertIn("Таких строк нет", left)

        # Как вводит человек: регистр, раскладка, без дефиса, без нулей,
        # номер мотора, номер на корпусе и серийный номер АКБ.
        for raw, code in (("мб-1", "МБ-001"), ("MB 003", "МБ-003"), ("mb005", "МБ-005"),
                          ("МБ 0009", "МБ-009"), ("60v240w2305000011", "МБ-011"),
                          ("000013", "МБ-013"), ("акб-1", "АКБ-0001"),
                          ("SN 70002", "АКБ-0002")):
            text = await self.scan(take_id, raw)
            self.assertIn(f"{code} на месте", text, raw)
        states = await self.states(take_id)
        self.assertEqual([states[c] for c in ("МБ-001", "МБ-003", "МБ-005", "МБ-009",
                                              "МБ-011", "МБ-013", "АКБ-0001", "АКБ-0002")],
                         ["found"] * 8)

        # Список с листа: 50 номеров через перевод строки, запятую и пробел.
        batch = "\n".join(f"акб-{n}" for n in range(101, 121))
        batch += ", " + ", ".join(f"MB-{n:03d}" for n in range(101, 111))
        batch += " " + " ".join(f"АКБ {n:04d}" for n in range(201, 221))
        text = await self.scan(take_id, batch)
        # MB-110 в аренде, а не на точке - лишний, и это видно.
        flash = re.search(r'<div class="flash [^"]*">([^<]*)</div>', text).group(1)
        self.assertEqual(flash, "Номеров: 50; на месте: 49; лишние: 1 (MB-110).")

        # Повтор, неизвестный номер дважды, потерянный и в аренде.
        self.assertIn("уже отмечен", await self.scan(take_id, "МБ-001"))
        await self.scan(take_id, "ZZ-404")
        self.assertIn("уже записан лишним", await self.scan(take_id, "zz 404"))
        self.assertIn("статус «Утерян»", await self.scan(take_id, "мб-200"))
        self.assertIn("статус «В аренде»", await self.scan(take_id, "мб-20"))
        extra = [i for i in await self.crm.take_items(take_id) if i["state"] == "extra"]
        self.assertEqual(sorted(i["bike_code"] or i["code"] for i in extra),
                         ["ZZ-404", "МБ-020", "МБ-110", "МБ-200"])

        # Лишняя строка в ожидаемую не превращается - ни отметкой, ни «снять».
        zz = next(i for i in extra if i["code"] == "ZZ-404")
        self.assertFalse(await self.crm.set_take_item(take_id, zz["id"], state="found"))
        await self.post(f"/stock-takes/{take_id}/mark-all", state="expected")
        self.assertEqual(sum(1 for i in await self.crm.take_items(take_id)
                             if i["state"] == "extra"), 4)
        await self.post(f"/stock-takes/{take_id}/mark-all", state="found")

        # Комментарии: строки и всей ведомости.
        await self.post(f"/stock-takes/{take_id}/items/{zz['id']}/note",
                        note="наклейка чужая")
        await self.post(f"/stock-takes/{take_id}/note",
                        note="Считали вдвоём, МБ-200 нашли в подсобке")

        # Выгрузки: ведомость (с шапкой документа) и список.
        r = await self.client.get(f"/stock-takes/{take_id}.csv")
        self.assertEqual(r.status_code, 200)
        head = r.content.decode("utf-8-sig").splitlines()
        self.assertEqual(head[0], "ПРТ-000001 · Всё · Весь парк")
        self.assertIn("провёл staff:admin", head[1])
        self.assertEqual(head[3], "Комментарий: Считали вдвоём, МБ-200 нашли в подсобке")
        self.assertIn("ZZ-404;—;нет в парке;;Лишний;наклейка чужая", r.text)
        # Номер за пределом bigint в выгрузке - 404, а не 500 из базы.
        r = await self.client.get(f"/stock-takes/{'9' * 20}.csv")
        self.assertEqual(r.status_code, 404)
        self.assertEqual((await self.client.get(f"/stock-takes/{take_id}.xlsx")).status_code,
                         200)

        location = await self.post(f"/stock-takes/{take_id}/close", lose_missing="1",
                                   return_found="1")
        text = await self.get_ok(location)
        self.assertIn("Пересчёт закрыт: нашли 580 из 580, вернулось в парк: 1.", text)
        self.assertIn("Считали вдвоём, МБ-200 нашли в подсобке", text)
        done = await self.crm.stock_take(take_id)
        self.assertEqual((done["expected"], done["found"], done["missing"], done["extra"]),
                         (580, 580, 0, 4))
        lost = await self.crm.bike_by_code("МБ-200")
        self.assertEqual((lost["status"], lost["location"]), ("available", "Адоратского"),
                         "весь парк место не трогает")
        self.assertEqual((await self.crm.bike_by_code("МБ-020"))["status"], "rented",
                         "аренду пересчёт не трогает")
        listing = await self.get_ok("/stock-takes?q=подсобке")
        self.assertIn(done["no"], listing)
        r = await self.client.get("/stock-takes.xlsx?q=подсобке")
        self.assertEqual(r.status_code, 200)

        # Закрытую не правят: ни отметка, ни строка, ни комментарий.
        self.assertIsNone(await self.crm.add_take_item(take_id, bike_id=None, code="X",
                                                       state="extra"))
        self.assertFalse(await self.crm.set_stock_take_note(take_id, "поздно"))
        self.assertIsNone(await self.crm.close_stock_take(take_id, closed_at=done["closed_at"]))
        self.assertIn("Пересчёт закрыт", await self.scan(take_id, "мб-2"))

    async def test_shortage_goes_to_lost_only_by_the_checkbox(self):
        take_id = await self.start(scope="location", location="Павлюхина", what="bikes")
        await self.scan(take_id, "мб-1")
        await self.post(f"/stock-takes/{take_id}/close")
        self.assertEqual((await self.crm.bike_by_code("МБ-003"))["status"], "available")
        take_id = await self.start(scope="location", location="Павлюхина", what="bikes")
        await self.scan(take_id, "мб-1")
        await self.post(f"/stock-takes/{take_id}/close", lose_missing="1")
        self.assertEqual((await self.crm.bike_by_code("МБ-003"))["status"], "lost")
        self.assertEqual((await self.crm.bike_by_code("МБ-001"))["status"], "available")
        log = await self.crm.bike_status_log((await self.crm.bike_by_code("МБ-003"))["id"])
        self.assertEqual((log[0]["to_status"], log[0]["changed_by"]),
                         ("lost", "staff:admin"))

    async def test_two_points_at_once_and_the_park_alone(self):
        here = await self.start(scope="location", location="Павлюхина")
        there = await self.start(scope="location", location="Адоратского")
        self.assertNotEqual(here, there)
        self.assertEqual(len(await self.crm.open_stock_takes()), 2)
        for data in ({"scope": "location", "location": "Павлюхина"},
                     {"scope": "all", "location": ""}):
            location = await self.post("/stock-takes", what="all", note="", **data)
            self.assertIn("Пересчёт уже идёт", await self.get_ok(location))
        # Под замком таблицы - даже мимо проверки сервиса.
        self.assertIsNone(await self.crm.create_stock_take(
            scope="all", location=None, note=None, bike_ids=[], created_by="t"))
        with self.assertRaises(asyncpg.exceptions.UniqueViolationError):
            await self.pool.execute(
                "insert into crm.stock_takes (no, scope, location) "
                "values ('ПРТ-X', 'location', 'Павлюхина')")
        index = await self.pool.fetch(
            "select indexname from pg_indexes where tablename = 'stock_takes'")
        names = {r["indexname"] for r in index}
        self.assertIn("stock_takes_one_open_place", names)
        self.assertNotIn("stock_takes_one_open", names)
        # Номер точки чужой ведомости - лишний, с её точкой.
        text = await self.scan(here, "мб-2")
        self.assertIn("МБ-002 не ждали здесь: статус «Свободен», точка Адоратского", text)

    async def test_seen_at_another_point_is_not_lost(self):
        """Две точки считают разом. МБ-002 числится на Адоратского, а стоит
        на Павлюхина: в ведомости Павлюхина он лишний, на Адоратского не
        найден. Галочка «в Утерян» на Адоратского его не теряет - в какой бы
        очерёдности ни закрывали; а пересчёт, закрытый до начала нового,
        уже не спасает."""
        here = await self.start(scope="location", location="Павлюхина", what="bikes")
        there = await self.start(scope="location", location="Адоратского", what="bikes")
        self.assertIn("МБ-002 не ждали здесь", await self.scan(here, "мб-2"))
        await self.post(f"/stock-takes/{here}/close")
        # «Не нашли» руками - тоже недостача.
        item = await self.crm.take_item_of_bike(
            there, (await self.crm.bike_by_code("МБ-006"))["id"])
        await self.post(f"/stock-takes/{there}/items/{item['id']}", state="missing")
        text = await self.get_ok(await self.post(f"/stock-takes/{there}/close",
                                                 lose_missing="1"))
        self.assertIn("не переведено в «Утерян»: 1 — их отметили в ПРТ-000001", text)
        self.assertEqual((await self.crm.bike_by_code("МБ-002"))["status"], "available")
        for code in ("МБ-004", "МБ-006"):
            self.assertEqual((await self.crm.bike_by_code(code))["status"], "lost", code)
        # Новый пересчёт Адоратского: Павлюхина закрыт до его начала.
        await self.pool.execute("update crm.bikes set status = 'available' "
                                "where status = 'lost' and code <> 'МБ-200'")
        again = await self.start(scope="location", location="Адоратского", what="bikes")
        text = await self.get_ok(await self.post(f"/stock-takes/{again}/close",
                                                 lose_missing="1"))
        self.assertNotIn("не переведено", text)
        self.assertEqual((await self.crm.bike_by_code("МБ-002"))["status"], "lost")

    async def test_point_admin_counts_her_point(self):
        profile = await self.crm.access_profile_by_code("manager")
        self.assertTrue(logic.normalize_perms(profile["perms"])["actions"]["stock_take"])
        await self.crm.create_staff("anna", logic.hash_password("password-1"), "Анна",
                                    "manager", profile["id"], location="Павлюхина")
        anna = await self.login("anna", "password-1")
        try:
            page = await self.get_ok("/stock-takes", anna)
            self.assertIn("Новый пересчёт", page)
            location = await self.post("/stock-takes", anna, scope="all", what="all",
                                       location="", note="")
            self.assertIn("только своей точки", await self.get_ok(location, anna))
            take_id = await self.start(anna, scope="location", location="Павлюхина")
            await self.post(f"/stock-takes/{take_id}/scan", anna, code="мб-1")
            self.assertEqual((await self.states(take_id))["МБ-001"], "found")
            # Галочка «в Утерян» - правка парка: присланная ею руками не
            # действует, недостача так и остаётся недостачей.
            lost_before = await self.pool.fetchval(
                "select count(*) from crm.bikes where status = 'lost'")
            await self.post(f"/stock-takes/{take_id}/close", anna, lose_missing="1")
            self.assertEqual(await self.pool.fetchval(
                "select count(*) from crm.bikes where status = 'lost'"), lost_before)
            self.assertEqual((await self.crm.bike_by_code("МБ-003"))["status"],
                             "available")
            self.assertGreater((await self.crm.stock_take(take_id))["missing"], 0)
            # Карточку велосипеда ей по-прежнему не править.
            bike = await self.crm.bike_by_code("МБ-001")
            r = await anna.post(f"/bikes/{bike['id']}/status", data={"status": "repair"})
            self.assertEqual(r.status_code, 403)
        finally:
            await anna.aclose()

    async def test_fake_crm_counts_the_same(self):
        """Заглушка ведёт ведомость так же, как база: веб-тесты на ней
        проверяют то же, что работает в проде."""
        async def story(crm):
            for n, place in ((1, "Север"), (2, "Север"), (3, "Юг")):
                await crm.create_bike(code=f"Т-{n:02d}", model="M", location=place,
                                      status="available")
            await crm.create_battery(code="Б-7", status="available", location="Север")
            take_id = await service.start_stock_take(
                crm, scope="location", location="Север", note="н", what="all", by="t")
            busy = await crm.create_stock_take(scope="all", location=None, note=None,
                                               bike_ids=[], created_by="t")
            south = await service.start_stock_take(
                crm, scope="location", location="Юг", note=None, what="bikes", by="t")
            take = await crm.stock_take(take_id)
            got = [(await service.take_scan(crm, take, raw))["message"]
                   for raw in ("т-1", "T 01", "т-3", "НЕТ-1", "нет 1", "б-7, т 2")]
            # Т-03 юга стоит на севере: юг его не нашёл, но в «Утерян» не отдаёт.
            seen = sorted((r["state"], r["no"], r["location"], r["battery_id"] is None)
                          for r in await crm.take_seen_elsewhere(south))
            south_result = await service.finish_stock_take(
                crm, await crm.stock_take(south), by="t", lose_missing=True)
            south_result = {k: south_result[k] for k in ("missing", "lost", "kept",
                                                         "kept_in")}
            extra = next(i for i in await crm.take_items(take_id) if i["code"] == "НЕТ-1")
            marked = await crm.set_take_item(take_id, extra["id"], state="found")
            noted = await crm.set_stock_take_note(take_id, "итог")
            result = await service.finish_stock_take(crm, take, by="t", lose_missing=True)
            late = await crm.add_take_item(take_id, bike_id=None, code="X")
            items = sorted((i["bike_code"] or i["battery_code"] or i["code"], i["state"],
                            i["note"]) for i in await crm.take_items(take_id))
            done = await crm.stock_take(take_id)
            return (busy, got, marked, noted, late, items,
                    {k: result[k] for k in ("total", "found", "missing", "extra")},
                    (done["status"], done["note"], done["expected"], done["extra"]),
                    seen, south_result)

        # В базе уже лежит парк из asyncSetUp - на других точках и с другими
        # номерами: история его не задевает.
        real = await story(self.crm)
        fake = await story(FakeCrm())
        self.assertEqual(real, fake)
        self.assertIsNone(real[0], "весь парк рядом с точкой не открыть")
        self.assertEqual(real[1][1], "Т-01 уже отмечен.")
        self.assertEqual(real[-1], {"missing": 1, "lost": 0, "kept": 1,
                                    "kept_in": ["ПРТ-000001"]})

    async def test_action_is_granted_once(self):
        """Старой установке действие выдаётся ролям администратора один раз:
        снятое владельцем следующий старт не возвращает."""
        await self.pool.execute(
            "update crm.access_profiles set perms = perms #- '{actions,stock_take}' "
            "where code in ('manager', 'tasks_operator')")
        await self.pool.execute("delete from crm.settings where key = 'stock_take_action_v1'")
        await Database(self.pool).apply_schema(SCHEMA, force=True)
        for code in ("manager", "tasks_operator"):
            row = await self.crm.access_profile_by_code(code)
            self.assertTrue(logic.can_act({"perms": row["perms"]}, "stock_take"), code)
        await self.pool.execute(
            "update crm.access_profiles set perms = perms #- '{actions,stock_take}' "
            "where code = 'manager'")
        await Database(self.pool).apply_schema(SCHEMA, force=True)
        row = await self.crm.access_profile_by_code("manager")
        self.assertFalse(logic.can_act({"perms": row["perms"]}, "stock_take"))


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
