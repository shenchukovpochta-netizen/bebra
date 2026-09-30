"""Обучение новичков: шаги и прогресс (app/crm/learning.py), учебный вход
на демо и его пределы, карточка шага, правило телефонов демо, страница
/learn в боевой панели. Панель - на FakeCrm; весь путь по шагам на
засеянном демо и SQL фактов - в test_learning_pg.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import learning, logic

try:
    from fastapi.testclient import TestClient

    from app.web.app import create_app
    from app.web.config import WebConfig, demo_url
    from tests.fake_crm import FakeCrm
    from tests.test_web import FakeBotDB, run
    HAVE_WEB = True
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

CREDS = re.compile(r"логин (learn-\d+), пароль ([a-z0-9]+)")


class TestSteps(unittest.TestCase):
    def test_profiles_survive_normalization(self):
        """Права учебных профилей - только существующие разделы и действия:
        normalize_perms молча выбросил бы опечатку, и шаг стал бы
        невыполнимым («нет доступа» посреди обучения)."""
        for code, (title, perms) in learning.PROFILES.items():
            self.assertEqual(logic.normalize_perms(perms), perms, code)
            self.assertIn("Обучение", title)
        self.assertEqual(set(learning.TRACK_OF_PROFILE), set(learning.PROFILES))

    def test_every_step_is_reachable_with_its_profile(self):
        """Адрес шага открыт профилем маршрута: запись по ссылке шага
        (POST) требует права «менять» раздела."""
        for track in learning.TRACKS.values():
            staff = {"perms": learning.PROFILES[track.profile][1]}
            for step in track.steps:
                for url in (step.url, step.fallback):
                    path = url.split("?")[0]
                    section = logic.section_for(path)
                    if section and path != "/":
                        self.assertTrue(logic.can_edit(staff, section),
                                        f"{track.code}/{step.code}: {path}")
        admin = {"perms": learning.PROFILES["learn_admin"][1]}
        self.assertTrue(logic.can_act(admin, "money_edit"), "пополнение баланса руками")
        self.assertFalse(logic.can_view(admin, "settings"))
        tech = {"perms": learning.PROFILES["learn_tech"][1]}
        self.assertFalse(logic.can_view(tech, "clients"))
        self.assertFalse(logic.can_act(tech, "money_edit"))

    def test_codes_unique_and_texts_filled(self):
        for track in learning.TRACKS.values():
            codes = [s.code for s in track.steps]
            self.assertEqual(len(codes), len(set(codes)), track.code)
            for s in track.steps:
                self.assertTrue(s.title and s.how and s.why, s.code)
                self.assertTrue(s.url.startswith("/") and s.fallback.startswith("/"))

    def test_progress_marks_and_current(self):
        state = learning.progress("admin", {})
        self.assertEqual((state["done"], state["total"], state["finished"]), (0, 6, False))
        self.assertEqual(state["current"]["code"], "client")
        self.assertEqual([s["no"] for s in state["steps"]], [1, 2, 3, 4, 5, 6])
        # Сделанное раньше срока засчитано, текущий - первый несделанный.
        state = learning.progress("admin", {"client": True, "topup": True})
        self.assertEqual(state["done"], 2)
        self.assertEqual(state["current"]["code"], "issue")
        self.assertEqual([s["code"] for s in state["steps"] if s["now"]], ["issue"])
        done = learning.progress("tech", dict.fromkeys(
            (s.code for s in learning.TRACKS["tech"].steps), True))
        self.assertTrue(done["finished"])
        self.assertIsNone(done["current"])

    def test_step_links_use_own_records(self):
        state = learning.progress("admin", {"client_id": 7, "rental_id": 9, "bike_id": 3})
        urls = {s["code"]: s["url"] for s in state["steps"]}
        self.assertEqual(urls["issue"], "/issue?client=7")
        self.assertEqual(urls["topup"], "/clients/7")
        self.assertEqual(urls["to_order"], "/orders/new?bike=3")
        self.assertEqual(urls["return"], "/rentals/9")
        # Записи ещё нет - запасной адрес, а не «/clients/None».
        urls = {s["code"]: s["url"] for s in learning.progress("admin", {})["steps"]}
        self.assertEqual((urls["topup"], urls["return"]), ("/clients", "/rentals"))
        tech = {s["code"]: s["url"] for s in
                learning.progress("tech", {"order_id": 5})["steps"]}
        self.assertEqual(tech["take"], "/orders/5")
        # Учебный велосипед: выдача с клиентом и велосипедом, наряд и ТО - на нём.
        urls = {s["code"]: s["url"] for s in
                learning.progress("admin", {"client_id": 7, "kit_id": 11})["steps"]}
        self.assertEqual(urls["issue"], "/issue?client=7&bike=11")
        urls = {s["code"]: s["url"] for s in
                learning.progress("admin", {"kit_id": 11})["steps"]}
        self.assertEqual(urls["issue"], "/issue?bike=11")
        tech = {s["code"]: s["url"] for s in
                learning.progress("tech", {"kit_id": 11})["steps"]}
        self.assertEqual((tech["order"], tech["to_status"]),
                         ("/orders/new?bike=11", "/bikes/11"))
        tech = {s["code"]: s["url"] for s in learning.progress("tech", {})["steps"]}
        self.assertEqual((tech["order"], tech["to_status"]),
                         ("/orders/new", "/bikes?status=available"))

    def test_training_bike(self):
        self.assertEqual(learning.kit_code("learn-12345"), "УЧ-12345")
        self.assertIsNone(learning.kit_code("operator"))
        self.assertIsNone(learning.kit_code("learn-x1"))
        tariffs = [{"period_days": 1, "model": "A", "active": True},
                   {"period_days": 7, "model": None, "active": True},
                   {"period_days": 7, "model": "B", "active": False},
                   {"period_days": 7, "model": "C", "active": True, "kind": "battery"},
                   {"period_days": 7, "model": "D", "active": True}]
        self.assertEqual(learning.kit_model(tariffs, [{"title": "Z"}]), "D")
        self.assertEqual(learning.kit_model(tariffs[:2], [{"title": "Z"}]), "Z")
        self.assertIsNone(learning.kit_model([], []))

    def test_fresh_and_track_of(self):
        steps = learning.progress("tech", {"order": True, "take": True})["steps"]
        self.assertEqual(learning.fresh(steps, ["order"]), ["Возьмите наряд в работу"])
        self.assertEqual(learning.fresh(steps, ["order", "take"]), [])
        self.assertEqual(learning.track_of({"profile_code": "learn_tech"}), "tech")
        self.assertIsNone(learning.track_of({"profile_code": "manager"}))
        self.assertIsNone(learning.track_of(None))
        self.assertEqual(learning.actor({"login": "learn-10001"}), "staff:learn-10001")

    def test_issue_note_marks_first_payment(self):
        """Пополнение отличается от оплаты на выдаче только заметкой: её
        пишет мастер выдачи, и шаблон у них один."""
        note = logic.ISSUE_PAY_NOTE.format(code="МБ-7")
        self.assertTrue(note.startswith(logic.ISSUE_PAY_NOTE.split("{")[0]))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestDemoUrl(unittest.TestCase):
    def test_domain_only(self):
        self.assertEqual(demo_url(" Demo.MyBike-kzn.ru/ "), "https://demo.mybike-kzn.ru")
        self.assertEqual(demo_url(""), "")
        self.assertEqual(demo_url("evil.example/x?y=1"), "")
        self.assertEqual(demo_url('a"onmouseover="x'), "")


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class LearnCase(unittest.TestCase):
    demo = True
    demo_link = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.crm = FakeCrm()
        for code, (title, perms) in learning.PROFILES.items():
            pid = run(self.crm.create_access_profile(title, perms))
            self.crm.profiles_[pid]["code"] = code
        run(self.crm.create_location(name="Павлюхина", city="Казань", address="", note=None))
        owner = run(self.crm.access_profile_by_code("owner"))
        run(self.crm.create_staff("demo", logic.hash_password("demo"), "Демо", "admin",
                                  owner["id"]))
        cfg = WebConfig(pg={}, secret="test-secret", admin_login="admin",
                        admin_password="", bot_token="",
                        storage_dir=Path(self.tmp.name) / "kyc", port=8080,
                        remind_before_days=2, demo=self.demo, demo_url=self.demo_link,
                        bike_photo_dir=Path(self.tmp.name) / "bikes",
                        doc_dir=Path(self.tmp.name) / "doctemplates")
        self.app = create_app(crm=self.crm, db=FakeBotDB(), cfg=cfg, bot=None)
        self.app.state.demo_limits = None
        self.client = self.client_from("10.0.0.1")

    def client_from(self, host):
        return TestClient(self.app, follow_redirects=False, client=(host, 50000))

    def start(self, track="admin", client=None):
        web = client or self.client
        r = web.post("/learn/start", data={"track": track})
        self.assertEqual(r.status_code, 303)
        return r, web

    def trainee(self, login):
        return run(self.crm.staff_by_login(login))


class TestTraineeEntry(LearnCase):
    def test_login_page_offers_both_tracks(self):
        text = self.client.get("/login").text
        tracks = re.findall(r'action="/learn/start">\s*<input type="hidden" name="track" '
                            r'value="(\w+)"', text)
        self.assertEqual(tracks, list(learning.TRACKS))
        self.assertIn("Обучение: администратор точки", text)
        self.assertIn("Обучение: мастер", text)

    def test_start_creates_personal_login_and_lets_in(self):
        r, web = self.start("tech")
        self.assertEqual(r.headers["location"], "/learn")
        page = web.get("/learn").text
        login, password = CREDS.search(page).groups()
        staff = self.trainee(login)
        self.assertEqual(learning.track_of(staff), "tech")
        self.assertEqual(staff["location"], "Павлюхина")
        kit = run(self.crm.bike_by_code(learning.kit_code(login)))
        self.assertEqual((kit["status"], kit["location"], kit["mileage_km"]),
                         ("available", "Павлюхина", learning.KIT_MILEAGE))
        self.assertTrue(logic.verify_password(password, staff["password_hash"]))
        self.assertNotIn(password, str(staff))
        # Пароль показан один раз: следующая страница его не повторяет.
        self.assertNotIn(password, web.get("/learn").text)
        # Карточка шага - на каждой странице, кроме самой /learn.
        service = web.get("/service").text
        self.assertIn("Обучение: мастер", service)
        self.assertIn("Шаг 1. Примите велосипед в ремонт", service)
        self.assertRegex(service, r'href="/learn"\s*>Обучение · 0/7<')
        # Вход под учебным логином снова ведёт к шагам.
        other = self.client_from("10.0.0.9")
        r = other.post("/login", data={"login": login, "password": password})
        self.assertEqual(r.headers["location"], "/learn")

    def test_start_replaces_current_session(self):
        self.assertEqual(self.client.post("/login", data={"login": "demo",
                                                          "password": "demo"}).status_code,
                         303)
        self.start("admin")
        text = self.client.get("/learn").text
        self.assertIn("Обучение: администратор точки", text)
        # Под учебным входом сотрудники и настройки закрыты.
        self.assertEqual(self.client.get("/company").status_code, 403)

    def test_foreign_page_cannot_start(self):
        """Форма с чужого сайта не заводит учебных входов: иначе его
        посетители заполнили бы стенд до ночи мимо предела на адрес."""
        before = len(self.crm.staff)
        r = self.client.post("/learn/start", data={"track": "admin"},
                             headers={"origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(len(self.crm.staff), before)
        r = self.client.post("/learn/start", data={"track": "admin"},
                             headers={"origin": "http://testserver"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(len(self.crm.staff), before + 1)

    def test_unknown_track_and_missing_profile(self):
        r = self.client.post("/learn/start", data={"track": "boss"})
        self.assertEqual(r.headers["location"], "/login")
        self.assertIn("Выберите, чему учиться", self.client.get("/login").text)
        for p in list(self.crm.profiles_.values()):
            if p.get("code") in learning.PROFILES:
                p["code"] = None
        before = len(self.crm.staff)
        self.client.post("/learn/start", data={"track": "admin"})
        self.assertEqual(len(self.crm.staff), before)
        self.assertIn("не настроено", self.client.get("/login").text)

    def test_limits_per_address_and_per_stand(self):
        for _ in range(learning.PER_ADDRESS):
            self.start("admin", client=self.client_from("10.0.0.5"))
        before = len(self.crm.staff)
        web = self.client_from("10.0.0.5")
        web.post("/learn/start", data={"track": "admin"})
        self.assertEqual(len(self.crm.staff), before)
        self.assertIn("больше не будет", web.get("/login").text)
        # С другого адреса - можно, пока стенд не заполнен.
        self.start("admin", client=self.client_from("10.0.0.6"))
        with mock.patch.object(learning, "PER_DAY", learning.PER_ADDRESS + 1):
            before = len(self.crm.staff)
            self.client_from("10.0.0.7").post("/learn/start", data={"track": "tech"})
            self.assertEqual(len(self.crm.staff), before)

    def test_parallel_starts_keep_the_address_limit(self):
        """Параллельные запросы с одного адреса не обходят предел: место
        занимается до первого ожидания, а не после заведения входа."""
        import asyncio

        import httpx

        async def burst():
            transport = httpx.ASGITransport(app=self.app, client=("10.0.0.9", 50000))
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://testserver") as web:
                return await asyncio.gather(*(
                    web.post("/learn/start", data={"track": "admin"})
                    for _ in range(learning.PER_ADDRESS * 2)))

        before = len(self.crm.staff)
        asyncio.run(burst())
        self.assertEqual(len(self.crm.staff) - before, learning.PER_ADDRESS)

    def test_step_is_counted_once_and_shown_once(self):
        _, web = self.start("admin")
        web.get("/learn")
        r = web.post("/clients", data={"full_name": "Учебный Клиент",
                                       "phone": "+7 000 012-34-56"})
        self.assertEqual(r.status_code, 303)
        card = web.get(r.headers["location"]).text
        self.assertIn("✓ Засчитано: Зарегистрируйте клиента", card)
        self.assertIn("Шаг 2. Выдайте ему велосипед", card)
        self.assertNotIn("✓ Засчитано", web.get("/clients").text)
        client = run(self.crm.client_by_phone("+70000123456"))
        self.assertTrue(client["created_by"].startswith("staff:learn-"))


class TestDemoPhones(LearnCase):
    def login(self):
        self.client.post("/login", data={"login": "demo", "password": "demo"})

    def test_real_numbers_refused_in_demo(self):
        self.login()
        for data in ({"full_name": "Настоящий", "phone": "+7 900 123-45-67"},
                     {"full_name": "Запасной", "phone": "+7 000 011-11-11",
                      "phone2": "8 912 345-67-89"}):
            r = self.client.post("/clients", data=data)
            self.assertIn("только вымышленные", self.client.get(r.headers["location"]).text)
        self.assertEqual(run(self.crm.clients()), [])
        r = self.client.post("/issue/client", data={"full_name": "Настоящий",
                                                    "phone": "+7 900 123-45-67"})
        self.assertEqual(run(self.crm.clients()), [])
        r = self.client.post("/clients", data={"full_name": "Вымышленный",
                                               "phone": "+7 000 011-11-11"})
        self.assertEqual(len(run(self.crm.clients())), 1)
        self.assertEqual(run(self.crm.clients())[0]["created_by"], "staff:demo")


class TestLivePanel(LearnCase):
    """Боевая панель: учебных входов не заводит, страница /learn только
    рассказывает, где обучение живёт."""

    demo = False

    def test_no_training_entry(self):
        self.assertNotIn("/learn/start", self.client.get("/login").text)
        r = self.client.post("/learn/start", data={"track": "admin"})
        self.assertTrue(r.headers["location"].startswith("/login"))
        self.client.post("/login", data={"login": "demo", "password": "demo"})
        before = len(self.crm.staff)
        self.assertEqual(self.client.post("/learn/start",
                                          data={"track": "admin"}).status_code, 404)
        self.assertEqual(len(self.crm.staff), before)
        page = self.client.get("/learn").text
        self.assertIn("не поднят", page)
        self.assertNotIn('action="/learn/start"', page)
        self.assertIn('href="/learn"', self.client.get("/staff").text)

    def test_real_numbers_allowed(self):
        self.client.post("/login", data={"login": "demo", "password": "demo"})
        self.client.post("/clients", data={"full_name": "Настоящий",
                                           "phone": "+7 900 123-45-67"})
        self.assertEqual(len(run(self.crm.clients())), 1)


class TestLivePanelWithDemo(LearnCase):
    demo = False
    demo_link = "https://demo.mybike-kzn.ru"

    def test_link_for_newcomers(self):
        self.client.post("/login", data={"login": "demo", "password": "demo"})
        page = self.client.get("/learn").text
        self.assertIn('href="https://demo.mybike-kzn.ru/learn"', page)
        self.assertIn("ссылка для новичка", self.client.get("/staff").text)


if __name__ == "__main__":
    unittest.main()
