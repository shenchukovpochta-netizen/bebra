"""Веб-панель CRM через TestClient: вход и права, каждая страница
рендерится, формы меняют данные, уведомления уходят через бот-заглушку.
База - tests/fake_crm.py, бот - список отправленных сообщений.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from fastapi.testclient import TestClient

    from app.crm import logic
    from app.web.app import create_app, ensure_admin
    from app.web.config import WebConfig
    from tests.fake_crm import FakeCrm
    HAVE_WEB = True
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal


class FakeBotDB:
    """bot.users в памяти - панели нужен только get_user."""

    def __init__(self) -> None:
        self.users: dict[int, dict] = {}

    async def get_user(self, tg_id):
        row = self.users.get(tg_id)
        return dict(row) if row else None


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


def run(coro):
    import asyncio
    return asyncio.run(coro)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class WebCase(unittest.TestCase):
    def setUp(self):
        self.crm = FakeCrm()
        self.db = FakeBotDB()
        self.bot = FakeBot()
        self.cfg = WebConfig(pg={}, secret="test-secret", admin_login="admin",
                             admin_password="admin-pass-123", bot_token="",
                             storage_dir=Path("/tmp/kyc"), port=8080,
                             remind_before_days=2)
        run(ensure_admin(self.crm, self.cfg))
        self.app = create_app(crm=self.crm, db=self.db, cfg=self.cfg, bot=self.bot)
        self.client = TestClient(self.app, follow_redirects=False)

    def login(self, login="admin", password="admin-pass-123"):
        return self.client.post("/login", data={"login": login, "password": password})

    def get_ok(self, path):
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200, f"{path}: {r.status_code}")
        return r.text

    def seed(self):
        """Клиент с Telegram, велосипед, тариф."""
        self.client_id = run(self.crm.create_client(full_name="Иванов Иван",
                                                    phone="+79990000000", tg_id=5001))
        self.bike_id = run(self.crm.create_bike(code="B-1", model="Kugoo V3"))
        self.tariff_id = run(self.crm.create_tariff("Неделя", 7, D(3000), None))


class TestAuth(WebCase):
    def test_first_admin_is_created_once(self):
        self.assertEqual(run(self.crm.staff_count()), 1)
        run(ensure_admin(self.crm, self.cfg))
        self.assertEqual(run(self.crm.staff_count()), 1)
        admin = run(self.crm.staff_by_login("admin"))
        self.assertTrue(logic.verify_password("admin-pass-123", admin["password_hash"]))

    def test_generated_password_when_none_configured(self):
        crm = FakeCrm()
        cfg = WebConfig(pg={}, secret="s", admin_login="boss", admin_password="",
                        bot_token="", storage_dir=Path("/tmp"), port=1, remind_before_days=2)
        generated = run(ensure_admin(crm, cfg))
        self.assertTrue(generated and len(generated) >= 8)
        self.assertTrue(logic.verify_password(generated,
                                              run(crm.staff_by_login("boss"))["password_hash"]))

    def test_anonymous_is_redirected_to_login(self):
        r = self.client.get("/clients")
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/login?next=%2Fclients"))
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_login_and_logout(self):
        r = self.login()
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/")
        self.assertIn("Сводка", self.get_ok("/"))
        self.client.post("/logout")
        self.assertEqual(self.client.get("/").status_code, 303)

    def test_wrong_password(self):
        r = self.login(password="nope")
        self.assertEqual(r.status_code, 401)
        self.assertIn("Неверный логин или пароль", r.text)

    def test_next_must_be_local(self):
        r = self.client.post("/login", data={"login": "admin", "password": "admin-pass-123",
                                             "next": "https://evil.example/"})
        self.assertEqual(r.headers["location"], "/")

    def test_disabled_staff_cannot_login(self):
        sid = run(self.crm.create_staff("ivan", logic.hash_password("password-1"),
                                        "Иван", "manager"))
        run(self.crm.set_staff_active(sid, False))
        self.assertEqual(self.login("ivan", "password-1").status_code, 401)

    def test_manager_cannot_manage_staff(self):
        run(self.crm.create_staff("ivan", logic.hash_password("password-1"), "Иван", "manager"))
        self.login("ivan", "password-1")
        page = self.get_ok("/staff")
        self.assertNotIn("Добавить", page)
        r = self.client.post("/staff", data={"login": "x", "password": "password-2"})
        self.assertEqual(r.status_code, 403)

    def test_admin_adds_staff_and_changes_password(self):
        self.login()
        self.client.post("/staff", data={"login": "Ivan", "name": "Иван",
                                         "password": "password-1", "role": "manager"})
        staff = run(self.crm.staff_by_login("ivan"))
        self.assertIsNotNone(staff)
        self.client.post(f"/staff/{staff['id']}/password", data={"password": "password-2"})
        self.assertTrue(logic.verify_password(
            "password-2", run(self.crm.staff_by_id(staff["id"]))["password_hash"]))
        self.client.post("/me/password", data={"old": "admin-pass-123", "new": "admin-pass-456"})
        self.assertTrue(logic.verify_password(
            "admin-pass-456", run(self.crm.staff_by_login("admin"))["password_hash"]))


class TestPages(WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def test_every_page_renders_empty(self):
        for path in ("/", "/clients", "/clients/new", "/bikes", "/bikes/new", "/rentals",
                     "/rentals/new", "/tariffs", "/finance", "/claims", "/reports",
                     "/staff", f"/clients/{self.client_id}", f"/bikes/{self.bike_id}"):
            self.get_ok(path)

    def test_missing_objects_are_404(self):
        for path in ("/clients/999", "/bikes/999", "/rentals/999"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_client_create_and_edit(self):
        r = self.client.post("/clients", data={"full_name": " Петров  Пётр ",
                                               "phone": "8 900 111 22 33",
                                               "status": "active", "note": ""})
        self.assertEqual(r.status_code, 303)
        client = run(self.crm.client_by_phone("+79001112233"))
        self.assertEqual(client["full_name"], "Петров Пётр")
        # дубль по телефону отклоняется
        self.client.post("/clients", data={"full_name": "Дубль", "phone": "+79001112233"})
        self.assertEqual(len(self.crm.clients_), 2)
        page = self.get_ok("/clients/new")
        self.assertIn("уже у клиента", page)
        # правка + чёрный список
        self.client.post(f"/clients/{client['id']}/edit",
                         data={"full_name": "Петров Пётр", "phone": "+79001112233",
                               "status": "blacklist", "note": "не платит"})
        self.assertEqual(run(self.crm.client(client["id"]))["status"], "blacklist")
        self.assertIn("Чёрный список", self.get_ok("/clients?status=blacklist"))

    def test_client_search(self):
        page = self.get_ok("/clients?q=Иван")
        self.assertIn("Иванов Иван", page)
        self.assertNotIn("Иванов Иван", self.get_ok("/clients?q=Сидор"))

    def test_ledger_entry_notifies_client(self):
        self.client.post(f"/clients/{self.client_id}/ledger",
                         data={"kind": "payment", "amount": "3 000", "method": "cash",
                               "note": "наличными"})
        self.assertEqual(run(self.crm.client_balance(self.client_id)), D(3000))
        self.assertTrue(self.bot.sent and self.bot.sent[-1][0] == 5001)
        self.assertIn("3 000 ₽ зачислен", self.bot.sent[-1][1])
        self.client.post(f"/clients/{self.client_id}/ledger",
                         data={"kind": "fine", "amount": "500", "note": "крыло"})
        self.assertEqual(run(self.crm.client_balance(self.client_id)), D(2500))
        self.client.post(f"/clients/{self.client_id}/ledger",
                         data={"kind": "adjust", "amount": "-100", "note": "скидка наоборот"})
        self.assertEqual(run(self.crm.client_balance(self.client_id)), D(2400))
        # плохая сумма - запись не создаётся, ошибка на странице
        self.client.post(f"/clients/{self.client_id}/ledger",
                         data={"kind": "payment", "amount": "много"})
        self.assertIn("Сумма: число", self.get_ok(f"/clients/{self.client_id}"))
        page = self.get_ok(f"/clients/{self.client_id}")
        self.assertIn("+3 000 ₽", page)
        self.assertIn("−500 ₽", page)

    def test_rental_lifecycle(self):
        r = self.client.post("/rentals", data={"client_id": self.client_id,
                                               "bike_id": self.bike_id,
                                               "tariff_id": self.tariff_id,
                                               "started_on": date.today().isoformat(),
                                               "billing": "auto"})
        self.assertEqual(r.status_code, 303)
        rental = run(self.crm.active_rental_of(self.client_id))
        self.assertIsNotNone(rental)
        self.assertEqual(rental["billed_until"], date.today() + timedelta(days=7))
        self.assertEqual(run(self.crm.client_balance(self.client_id)), D(-3000))
        self.assertEqual(run(self.crm.bike(self.bike_id))["status"], "rented")
        self.assertIn("Аренда оформлена", self.bot.sent[-1][1])

        page = self.get_ok(r.headers["location"])
        self.assertIn("Kugoo V3", page)
        # первый период начислен и не оплачен: платёж за него - сегодня
        self.assertIn("платёж сегодня", page)
        self.assertIn("−3 000 ₽", page)

        # вторая аренда тому же клиенту невозможна
        self.client.post("/rentals", data={"client_id": self.client_id, "bike_id": "",
                                           "tariff_id": self.tariff_id})
        self.assertIn("уже идёт аренда", self.get_ok("/rentals/new"))

        # смена тарифа со следующего периода
        month = run(self.crm.create_tariff("Месяц", 30, D(11000), None))
        self.client.post(f"/rentals/{rental['id']}/tariff",
                         data={"tariff_id": month, "billing": "auto"})
        fresh = run(self.crm.rental(rental["id"]))
        self.assertEqual(fresh["period_days"], 30)
        self.assertEqual(fresh["price"], D(11000))

        # закрытие: велосипед в ремонт, клиент уведомлён
        self.client.post(f"/rentals/{rental['id']}/close",
                         data={"closed_on": "", "bike_status": "repair", "note": "царапина"})
        self.assertIsNone(run(self.crm.active_rental_of(self.client_id)))
        self.assertEqual(run(self.crm.bike(self.bike_id))["status"], "repair")
        self.assertIn("закрыта", self.bot.sent[-1][1])
        self.assertIn("царапина", self.get_ok(f"/rentals/{rental['id']}"))
        self.assertIn("Закрытые", self.get_ok("/rentals?status=closed"))

    def test_rental_needs_active_tariff_and_client(self):
        self.client.post("/rentals", data={"client_id": "", "tariff_id": ""})
        self.assertIn("Выберите клиента и тариф", self.get_ok("/rentals/new"))
        run(self.crm.update_client(self.client_id, status="blocked"))
        self.client.post("/rentals", data={"client_id": self.client_id,
                                           "tariff_id": self.tariff_id})
        self.assertIn("заблокирован", self.get_ok("/rentals/new"))

    def test_bike_create_status_and_log(self):
        self.client.post("/bikes", data={"code": "b-2", "model": "Truck+", "frame_no": "FR2",
                                         "battery_count": "2", "purchase_price": "45000",
                                         "purchased_on": "2026-05-01"})
        bike = run(self.crm.bike_by_code("B-2"))
        self.assertEqual(bike["purchase_price"], D(45000))
        self.assertEqual(bike["purchased_on"], date(2026, 5, 1))
        # дубли номера и рамы
        self.client.post("/bikes", data={"code": "B-2", "model": "X"})
        self.assertIn("уже занят", self.get_ok("/bikes/new"))
        self.client.post("/bikes", data={"code": "B-3", "model": "X", "frame_no": "FR2"})
        self.assertIn("номером рамы уже есть", self.get_ok("/bikes/new"))
        # статус и журнал
        self.client.post(f"/bikes/{bike['id']}/status", data={"status": "repair",
                                                              "note": "прокол"})
        self.assertEqual(run(self.crm.bike(bike["id"]))["status"], "repair")
        self.client.post(f"/bikes/{bike['id']}/log", data={"kind": "repair", "cost": "800",
                                                           "note": "камера"})
        page = self.get_ok(f"/bikes/{bike['id']}")
        self.assertIn("прокол", page)
        self.assertIn("камера", page)
        self.assertIn("800 ₽", page)
        # в аренде статус руками не меняется
        self.client.post("/rentals", data={"client_id": self.client_id, "bike_id": self.bike_id,
                                           "tariff_id": self.tariff_id})
        self.client.post(f"/bikes/{self.bike_id}/status", data={"status": "lost"})
        self.assertEqual(run(self.crm.bike(self.bike_id))["status"], "rented")

    def test_tariffs_edit_and_toggle(self):
        self.client.post("/tariffs", data={"name": "Месяц", "period_days": "30",
                                           "price": "11 000", "note": ""})
        self.assertEqual(len(self.crm.tariffs_), 2)
        self.client.post(f"/tariffs/{self.tariff_id}", data={"name": "Неделя+",
                                                             "period_days": "7",
                                                             "price": "3400", "note": "2 АКБ"})
        self.assertEqual(run(self.crm.tariff(self.tariff_id))["price"], D(3400))
        self.client.post(f"/tariffs/{self.tariff_id}", data={"action": "toggle"})
        self.assertFalse(run(self.crm.tariff(self.tariff_id))["active"])
        self.assertNotIn("Неделя+", self.get_ok("/rentals/new"))
        self.client.post("/tariffs", data={"name": "", "period_days": "7", "price": "1"})
        self.assertIn("заполните поле", self.get_ok("/tariffs"))

    def test_claims_confirm_and_reject(self):
        claim_id = run(self.crm.create_claim(self.client_id, D(3000)))
        other = run(self.crm.create_client(full_name="Второй", phone="+79995555555"))
        claim2 = run(self.crm.create_claim(other, None))
        page = self.get_ok("/claims")
        self.assertIn("Иванов Иван", page)
        self.assertIn("Второй", page)
        self.client.post(f"/claims/{claim_id}/confirm", data={"amount": "2500", "method": "sbp"})
        self.assertEqual(run(self.crm.client_balance(self.client_id)), D(2500))
        self.assertEqual(run(self.crm.claim(claim_id))["status"], "confirmed")
        self.assertIn("2 500 ₽ зачислен", self.bot.sent[-1][1])
        # повторно - уже обработана
        self.client.post(f"/claims/{claim_id}/confirm", data={"amount": "2500"})
        self.assertEqual(run(self.crm.client_balance(self.client_id)), D(2500))
        self.client.post(f"/claims/{claim2}/reject")
        self.assertEqual(run(self.crm.claim(claim2))["status"], "rejected")
        self.assertIn("Открытых заявок нет", self.get_ok("/claims"))

    def test_finance_filters_and_totals(self):
        run(self.crm.add_ledger(client_id=self.client_id, kind="payment", amount=D(3000)))
        run(self.crm.add_ledger(client_id=self.client_id, kind="charge", amount=D(-3000)))
        page = self.get_ok("/finance")
        self.assertIn("+3 000 ₽", page)
        self.assertIn("−3 000 ₽", page)
        page = self.get_ok("/finance?kind=payment")
        self.assertNotIn("−3 000 ₽", page)
        self.assertEqual(self.client.get("/finance?since=вчера").status_code, 303)

    def test_dashboard_and_reports_show_debtors(self):
        run(self.crm.add_ledger(client_id=self.client_id, kind="charge", amount=D(-3000)))
        self.assertIn("Иванов Иван", self.get_ok("/"))
        report = self.get_ok("/reports")
        self.assertIn("−3 000 ₽", report)
        self.assertIn("загрузка парка", report)

    def test_billing_run_button(self):
        started = (date.today() - timedelta(days=8)).isoformat()
        self.client.post("/rentals", data={"client_id": self.client_id, "bike_id": "",
                                           "tariff_id": self.tariff_id,
                                           "started_on": started})
        run(self.crm.add_ledger(client_id=self.client_id, kind="payment", amount=D(6000)))
        rental = run(self.crm.active_rental_of(self.client_id))
        # оформление начислило все периоды по сегодня: два
        self.assertEqual(rental["billed_until"],
                         date.today() - timedelta(days=8) + timedelta(days=14))
        r = self.client.post("/billing/run")
        self.assertEqual(r.status_code, 303)
        self.assertIn("Начислений сделано: 0", self.get_ok("/"))

    def test_contract_download_guards(self):
        self.assertEqual(self.client.get(f"/clients/{self.client_id}/contract").status_code, 404)
        self.db.users[5001] = {"tg_id": 5001, "contract_status": "signed",
                               "contract_path": "/etc/passwd"}
        # путь вне хранилища не отдаётся
        self.assertEqual(self.client.get(f"/clients/{self.client_id}/contract").status_code, 404)


if __name__ == "__main__":
    unittest.main()


class TestReviewFixes(WebCase):
    """Регрессии ревью: двойное зачисление, ручной биллинг, местное время,
    ссылка CSV, чужой велосипед в форме аренды, next с параметрами."""

    def test_double_confirm_writes_one_payment(self):
        self.seed()
        self.login()
        pid = run(self.crm.create_claim(self.client_id, D("2500")))
        self.assertEqual(self.client.post(f"/claims/{pid}/confirm",
                                          data={"amount": "2500"}).status_code, 303)
        r = self.client.post(f"/claims/{pid}/confirm", data={"amount": "2500"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("уже обработали", self.client.get("/claims").text)
        entries = run(self.crm.ledger_of(self.client_id))
        self.assertEqual([x["kind"] for x in entries], ["payment"])
        self.assertEqual(run(self.crm.ledger_totals()).get("payment"), D("2500"))
        self.assertEqual(run(self.crm.client_balance(self.client_id)), D("2500"))

    def test_manual_rental_flash_does_not_claim_a_charge(self):
        self.seed()
        self.login()
        r = self.client.post("/rentals", data={"client_id": self.client_id,
                                               "bike_id": self.bike_id,
                                               "tariff_id": self.tariff_id,
                                               "billing": "manual"})
        self.assertEqual(r.status_code, 303)
        page = self.client.get(r.headers["location"]).text
        self.assertIn("без начисления", page)
        self.assertNotIn("первый период начислен", page)
        self.assertEqual(run(self.crm.client_balance(self.client_id)), D(0))

    def test_unknown_bike_id_is_rejected(self):
        self.seed()
        self.login()
        r = self.client.post("/rentals", data={"client_id": self.client_id, "bike_id": "777",
                                               "tariff_id": self.tariff_id})
        self.assertEqual(r.headers["location"], "/rentals/new")
        self.assertIn("Такого велосипеда нет", self.client.get("/rentals/new").text)
        self.assertIsNone(run(self.crm.active_rental_of(self.client_id)))

    def test_timestamps_shown_in_local_time(self):
        from datetime import UTC, datetime

        from app.web import app as web_app
        moment = datetime(2026, 9, 13, 21, 30, tzinfo=UTC)
        local = moment.astimezone()
        expected = local.strftime("%d.%m.%Y %H:%M")
        self.assertEqual(web_app._dmy(moment), expected)
        self.assertEqual(web_app._cell(moment), expected)
        self.assertEqual(web_app._dmy(date(2026, 9, 13)), "13.09.2026")

    def test_csv_link_encodes_query(self):
        self.login()
        page = self.client.get("/clients", params={"q": "A&B#1"}).text
        self.assertIn("/clients.csv?q=A%26B%231&status=", page)

    def test_login_redirect_keeps_query(self):
        r = self.client.get("/clients?q=abc")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login?next=%2Fclients%3Fq%3Dabc")


class TestExtras(WebCase):
    """Троттлинг входа, экспорт CSV, поиск по цифрам телефона, будущая дата."""

    def test_login_is_throttled_after_failures(self):
        from app.web import app as web_app
        for _ in range(web_app.LOGIN_LIMIT):
            self.assertEqual(self.login(password="nope").status_code, 401)
        # даже верный пароль не пускает, пока окно не истекло
        r = self.login()
        self.assertEqual(r.status_code, 429)
        self.assertIn("Слишком много попыток", r.text)

    def test_throttle_is_per_login_not_per_address(self):
        """За туннелем и Caddy все приходят с одного адреса: чужие неудачи
        по другому логину не должны закрывать вход администратору."""
        from app.web import app as web_app
        run(self.crm.create_staff("ivan", logic.hash_password("password-1"), "Иван", "manager"))
        for _ in range(web_app.LOGIN_LIMIT):
            self.assertEqual(self.login("ivan", "nope").status_code, 401)
        self.assertEqual(self.login("ivan", "password-1").status_code, 429)
        self.assertEqual(self.login().status_code, 303)

    def test_successful_login_clears_failures(self):
        self.assertEqual(self.login(password="nope").status_code, 401)
        self.assertEqual(self.login().status_code, 303)
        self.client.post("/logout")
        self.assertEqual(self.login(password="nope").status_code, 401)
        self.assertEqual(self.login().status_code, 303)

    def test_csv_exports(self):
        self.login()
        self.seed()
        run(self.crm.add_ledger(client_id=self.client_id, kind="payment", amount=D("3000"),
                                method="sbp", note="перевод"))
        r = self.client.get("/finance.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers["content-type"])
        self.assertIn("attachment", r.headers["content-disposition"])
        text = r.content.decode("utf-8")
        self.assertTrue(text.startswith("﻿"))
        self.assertIn("Дата;Клиент;Вид;Сумма", text)
        self.assertIn("Иванов Иван;Платёж;3000,00;;СБП;перевод", text)
        r = self.client.get("/clients.csv?q=Иван")
        self.assertEqual(r.status_code, 200)
        text = r.content.decode("utf-8")
        self.assertIn("ФИО;Телефон;Статус", text)
        self.assertIn("Иванов Иван;+79990000000;Активен;есть;3000,00", text)
        self.assertNotIn("Иванов", self.client.get("/clients.csv?q=Сидор").text)
        # экспорт закрыт без входа
        self.client.post("/logout")
        self.assertEqual(self.client.get("/finance.csv").status_code, 303)

    def test_phone_search_ignores_formatting(self):
        self.login()
        self.seed()
        for q in ("900 000", "8 (999) 000-00-00", "+7 999 000 00 00", "9990000000"):
            self.assertIn("Иванов Иван", self.get_ok(f"/clients?q={q}"), q)
        self.assertNotIn("Иванов Иван", self.get_ok("/clients?q=123 456"))

    def test_future_start_is_not_charged_in_advance(self):
        self.login()
        self.seed()
        start = date.today() + timedelta(days=3)
        r = self.client.post("/rentals", data={"client_id": self.client_id,
                                               "bike_id": self.bike_id,
                                               "tariff_id": self.tariff_id,
                                               "started_on": start.isoformat()})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(run(self.crm.client_balance(self.client_id)), D(0))
        rental = run(self.crm.active_rental_of(self.client_id))
        self.assertEqual(rental["billed_until"], start)
        self.assertIn(f"начислится {start:%d.%m.%Y}", self.get_ok(r.headers["location"]))
