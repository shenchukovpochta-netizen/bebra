"""Веб-панель CRM через TestClient: вход и права, каждая страница
рендерится, формы меняют данные, уведомления уходят через бот-заглушку.
База - tests/fake_crm.py, бот - список отправленных сообщений.
"""

from __future__ import annotations

import re
import sys
import types
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.plain import plain  # noqa: E402

try:
    from fastapi.testclient import TestClient

    from app.crm import logic, service
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

    async def user_by_phone(self, phone):
        row = next((u for u in self.users.values() if u.get("phone") == phone), None)
        return dict(row) if row else None


class FakeBot:
    """Сообщения клиентам - с обычными пробелами в суммах (tests/plain.py)."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, plain(text)))

    async def get_me(self):
        return types.SimpleNamespace(username="mybike_test_bot")


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

    @staticmethod
    def menu_labels(page):
        """Пункты бокового меню страницы (app/web/nav.py) - подписи ссылок
        и раскрывающихся групп, по порядку."""
        return re.findall(r'class="nav-link[^"]*"[^>]*>(?:<svg[^>]*>.*?</svg>)?'
                          r'<span>([^<]+)</span>', page)

    def get_ok(self, path):
        """Страница с обычными пробелами в суммах (tests/plain.py): тексты
        в тестах пишутся так, как их читает человек."""
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200, f"{path}: {r.status_code}")
        return plain(r.text)

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
        # Не пустая база: пустую владельца встречает мастер (test_firstrun).
        self.seed()
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
        self.seed()
        r = self.client.post("/login", data={"login": "admin", "password": "admin-pass-123",
                                             "next": "https://evil.example/"})
        self.assertEqual(r.headers["location"], "/")

    def test_disabled_staff_cannot_login(self):
        sid = run(self.crm.create_staff("ivan", logic.hash_password("password-1"),
                                        "Иван", "manager"))
        run(self.crm.set_staff_active(sid, False))
        self.assertEqual(self.login("ivan", "password-1").status_code, 401)

    def test_manager_cannot_manage_staff(self):
        manager = run(self.crm.access_profile_by_code("manager"))
        run(self.crm.create_staff("ivan", logic.hash_password("password-1"), "Иван",
                                  "manager", manager["id"]))
        self.login("ivan", "password-1")
        self.assertEqual(self.client.get("/staff").status_code, 403)
        r = self.client.post("/staff", data={"login": "x", "password": "password-2"})
        self.assertEqual(r.status_code, 403)

    def test_staff_page_changes_role_by_button_only(self):
        """Стрелка на списке ролей в фокусе меняла значение и отправляла
        форму: одно нажатие клавиши давало «Владельца». Теперь - кнопкой."""
        self.login()
        run(self.crm.create_staff("ivan", logic.hash_password("password-1"), "Иван",
                                  "manager"))
        page = self.get_ok("/staff")
        self.assertNotIn("this.form.submit()", page)
        self.assertIn(">Сменить</button>", page)

    def test_add_link_opens_the_dialog_without_script(self):
        self.login()
        self.assertNotRegex(self.get_ok("/staff"), r'id="add-staff"[^>]*\bopen\b')
        self.assertRegex(self.get_ok("/staff?add=1"), r'id="add-staff"[^>]*\bopen\b')

    def test_no_default_role_is_full_access(self):
        """Удалили «Администратора» - в окне добавления не отмечено ничего,
        а не первая роль списка: полные права выбирают руками."""
        self.login()
        manager = run(self.crm.access_profile_by_code("manager"))
        run(self.crm.delete_access_profile(manager["id"]))
        owner = run(self.crm.access_profile_by_code("owner"))
        page = self.get_ok("/staff")
        self.assertNotRegex(page, rf'name="profile_id" value="{owner["id"]}"[^>]*checked')

    def test_admin_adds_staff_and_changes_password(self):
        self.login()
        manager = run(self.crm.access_profile_by_code("manager"))
        self.client.post("/staff", data={"login": "Ivan", "name": "Иван",
                                         "password": "password-1",
                                         "profile_id": manager["id"]})
        staff = run(self.crm.staff_by_login("ivan"))
        self.assertIsNotNone(staff)
        self.assertEqual(staff["profile_code"], "manager")
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
                     "/rentals/new", "/issue", "/tariffs", "/finance", "/claims", "/reports",
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

    def test_rental_start_far_from_today_is_refused(self):
        """Опечатка в годе (2025 вместо 2026) начислила бы разом все
        прошедшие периоды: дальше месяца в обе стороны - отказ."""
        for started in (date.today() - timedelta(days=logic.RENTAL_BACKDATE_DAYS + 1),
                        date.today() + timedelta(days=logic.RENTAL_AHEAD_DAYS + 1)):
            self.client.post("/rentals", data={"client_id": self.client_id,
                                               "bike_id": self.bike_id,
                                               "tariff_id": self.tariff_id,
                                               "started_on": started.isoformat(),
                                               "billing": "auto"})
            self.assertIn("проверьте год", self.get_ok("/rentals/new"))
        self.assertIsNone(run(self.crm.active_rental_of(self.client_id)))
        self.assertEqual(run(self.crm.client_balance(self.client_id)), D(0))
        page = self.get_ok("/rentals/new")
        self.assertIn('min="' + (date.today() - timedelta(
            days=logic.RENTAL_BACKDATE_DAYS)).isoformat() + '"', page)

    def test_manual_billing_needs_money_edit(self):
        """Ручное начисление - аренда без биллинга: администратору без
        права на записи в журнал её не завести и в неё не перевести."""
        manager = run(self.crm.access_profile_by_code("manager"))
        run(self.crm.create_staff("ivan", logic.hash_password("password-1"), "Иван",
                                  "manager", manager["id"]))
        self.login("ivan", "password-1")
        self.assertNotIn('name="billing"', self.get_ok("/rentals/new"))
        r = self.client.post("/rentals", data={"client_id": self.client_id,
                                               "bike_id": self.bike_id,
                                               "tariff_id": self.tariff_id,
                                               "started_on": date.today().isoformat(),
                                               "billing": "manual"})
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(run(self.crm.active_rental_of(self.client_id)))
        r = self.client.post("/rentals", data={"client_id": self.client_id,
                                               "bike_id": self.bike_id,
                                               "tariff_id": self.tariff_id,
                                               "started_on": date.today().isoformat()})
        self.assertEqual(r.status_code, 303)
        rental = run(self.crm.active_rental_of(self.client_id))
        self.assertEqual(rental["billing"], "auto")
        r = self.client.post(f"/rentals/{rental['id']}/tariff",
                             data={"tariff_id": self.tariff_id, "billing": "manual"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(run(self.crm.rental(rental["id"]))["billing"], "auto")

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
        # статус и журнал. Заведён велосипед «на сборке», и статус руками
        # там не меняется: в оборот его выпускает сверка.
        self.client.post(f"/bikes/{bike['id']}/status", data={"status": "repair"})
        self.assertEqual(run(self.crm.bike(bike["id"]))["status"], "new")
        run(self.crm.commission_bike(bike["id"], by="staff:admin"))
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

    def test_finance_journal_is_paged(self):
        """Журнал листается, как остальные списки: месяц в тысячу записей
        одной страницей весил за полмегабайта (подписи карточек телефона),
        а прежний предел в тысячу строк молча резал хвост. Подвал считает
        все найденные записи, сумма - только внутри одного вида."""
        before = len(run(self.crm.ledger(since=date.today().replace(day=1),
                                         until=date.today(), limit=100000)))
        for i in range(120):
            run(self.crm.add_ledger(client_id=self.client_id, kind="payment",
                                    amount=D(100 + i), note=f"платёж-{i:03d}"))
        page = self.get_ok("/finance")
        self.assertIn('class="list-foot"', page)
        self.assertIn(f"Итого {before + 120}", page)
        self.assertEqual(page.count("платёж-"), 50)
        self.assertIn("платёж-119", page)             # новые сверху
        self.assertNotIn("платёж-000", page)
        page = self.get_ok("/finance?rows=300")
        self.assertEqual(page.count("платёж-"), 120)
        page = self.get_ok("/finance?page=3")
        self.assertIn("платёж-000", page)
        self.assertIn("стр. 3 из", page)
        # Сортировка по сумме - по белому списку; чужое имя поля - как было.
        page = self.get_ok("/finance?sort=amount&dir=asc&kind=payment")
        self.assertIn("платёж-000", page)
        self.assertIn("на сумму +", page)             # один вид - есть сумма
        self.assertNotIn("на сумму", self.get_ok("/finance"))
        page = self.get_ok("/finance?sort=note&dir=asc")
        self.assertIn("платёж-119", page)

    def test_dashboard_and_reports_show_debtors(self):
        run(self.crm.add_ledger(client_id=self.client_id, kind="charge", amount=D(-3000)))
        self.assertIn("Иванов Иван", self.get_ok("/"))
        report = self.get_ok("/reports")
        self.assertIn("−3 000 ₽", report)
        self.assertIn("сейчас в аренде", report)

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

    def test_csv_neutralises_formulas(self):
        """Имя из бота «=HYPERLINK(...)» не должно стать формулой в Excel."""
        from app.web import app as web_app
        self.assertEqual(web_app._cell('=HYPERLINK("http://evil";"Иванов")'),
                         '="=HYPERLINK(""http://evil"";""Иванов"")"')
        self.assertEqual(web_app._cell("+79990000000"), '="+79990000000"')
        self.assertEqual(web_app._cell("Иванов Иван"), "Иванов Иван")
        self.assertEqual(web_app._cell(D("-3000")), "-3000,00")       # числа не трогаем
        cid = run(self.crm.create_client(full_name="Петров", phone="+79990000001"))
        run(self.crm.update_client(cid, note="=cmd|' /C calc'!A1"))
        self.login()
        body = self.client.get("/clients.csv").text
        self.assertNotIn('\n=cmd', body)
        self.assertIn('"=""+79990000001"""', body)

    def test_csv_link_encodes_query(self):
        self.login()
        page = self.client.get("/clients", params={"q": "A&B#1"}).text
        self.assertIn("/clients.csv?q=A%26B%231&status=", page)

    def test_login_redirect_keeps_query(self):
        r = self.client.get("/clients?q=abc")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login?next=%2Fclients%3Fq%3Dabc")


class TestOperatorInput(WebCase):
    """Проверка кода: ввод, на котором панель отвечала 500, - нулевой байт,
    номер несуществующей записи в форме, переименование в занятое имя,
    слова и бесконечность в числах, дата в десятитысячном году. На
    заглушке база не падает, поэтому проверяется, что до неё не доходит:
    ничего не записано и оператору сказано, что не так."""

    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def test_nul_byte_is_cut_from_query_and_form(self):
        """Postgres не хранит \\x00 в тексте: `%00` в поиске давал 500."""
        page = self.get_ok("/clients?q=Ива%00нов")
        self.assertIn("Иванов Иван", page, "поиск получил «Иванов», а не с нулём")
        self.get_ok("/bikes?q=B%00-1&status=%00")
        self.client.post("/bikes", data={"code": "N-1", "model": "Kugoo V3",
                                         "note": "до\x00бавлен"})
        bike = run(self.crm.bike_by_code("N-1"))
        self.assertEqual(bike["note"], "добавлен")
        self.assertEqual(self.client.get("/sign/ab%00cd").status_code, 404)

    def test_unknown_technician_is_refused(self):
        r = self.client.post("/orders", data={"payer": "own", "bike_id": str(self.bike_id),
                                              "tech_id": "999999", "complaint": "стук"})
        self.assertEqual(r.headers["location"], "/orders/new")
        self.assertIn("Такого сотрудника нет", self.get_ok("/orders/new"))
        self.assertEqual(run(self.crm.work_orders()), [])
        self.client.post("/orders", data={"payer": "own", "bike_id": str(self.bike_id),
                                          "complaint": "стук"})
        order = run(self.crm.work_orders())[0]
        self.client.post(f"/orders/{order['id']}/edit",
                         data={"status": order["status"], "tech_id": "999999"})
        self.assertIsNone(run(self.crm.work_order(order["id"]))["tech_id"])
        self.assertIn("Такого сотрудника нет", self.get_ok(f"/orders/{order['id']}"))

    def test_unknown_catalogue_and_supplier_ids_are_refused(self):
        # модель АКБ в карточке батареи
        self.client.post("/batteries", data={"code": "9510001", "model_id": "999999",
                                             "service_months": "15", "cycles": "0"})
        self.assertIsNone(run(self.crm.battery_by_code("9510001")))
        self.assertIn("такой нет в каталоге", self.get_ok("/batteries"))
        # клетка совместимости
        battery_model = run(self.crm.create_battery_model(
            title="48V", brand=None, voltage=48, capacity=None, price=D(0),
            service_months=15, note=None))
        self.client.post("/models/compat", data={"bike_model_id": "999999",
                                                 "battery_model_id": str(battery_model),
                                                 "mode": "fits"})
        self.assertEqual(run(self.crm.compat_pairs()), [])
        # поставщик закупки, прихода и заказа
        self.client.post("/assets", data={"codes": "Z-1", "model": "Kugoo V3",
                                          "supplier_id": "999999"})
        self.assertEqual(run(self.crm.purchases()), [])
        part = run(self.crm.create_part(title="Камера", node=None, unit="шт",
                                        cost=D(300), price=D(600), min_stock=0,
                                        model=None, note=None))
        for data in ({"supplier_id": "999999", "part_id_0": str(part), "qty_0": "2"},
                     {"part_id_0": "999999", "qty_0": "2"}):
            self.client.post("/parts/receipts", data=data)
            self.assertEqual(run(self.crm.part_docs(kind="receipt")), [], data)
        self.assertIn("обновите страницу", self.get_ok("/parts/receipts"))
        self.client.post("/part-orders/items", data={"part_id": str(part), "qty": "1"})
        order = run(self.crm.open_part_order())
        self.client.post(f"/part-orders/{order['id']}/status",
                         data={"status": "ordered", "supplier_id": "999999"})
        self.assertEqual(run(self.crm.part_order(order["id"]))["status"], order["status"],
                         "заказ не ушёл неизвестному поставщику")

    def test_rename_to_a_taken_name_is_a_message(self):
        run(self.crm.create_part(title="Камера", node=None, unit="шт", cost=D(1),
                                 price=D(1), min_stock=0, model=None, note=None))
        b = run(self.crm.create_part(title="Покрышка", node=None, unit="шт", cost=D(1),
                                     price=D(1), min_stock=0, model=None, note=None))
        r = self.client.post(f"/parts/{b}/edit", data={"title": "Камера", "unit": "шт",
                                                       "active": "1"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(run(self.crm.part(b))["title"], "Покрышка")
        self.assertIn("уже есть", self.get_ok(f"/parts/{b}"))
        types = [t for t in run(self.crm.work_types()) if t["active"]][:2]
        r = self.client.post(f"/work-types/{types[1]['id']}",
                             data={"title": types[0]["title"], "minutes": "10"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("уже есть", self.get_ok("/work-types"))
        run(self.crm.create_battery(code="A-1", by="t"))
        second = run(self.crm.create_battery(code="A-2", by="t"))
        r = self.client.post(f"/batteries/{second}/edit",
                             data={"code": "A-1", "service_months": "15", "cycles": "0"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(run(self.crm.battery(second))["code"], "A-2")
        self.assertIn("уже есть", self.get_ok(f"/batteries/{second}"))

    def test_words_and_overflow_in_numbers_are_a_message(self):
        run(self.crm.create_bike_model(title="Kugoo V3", brand=None, factory_title=None,
                                       battery_slots=1, note=None, speed_kmh=45))
        model = run(self.crm.bike_models())[0]
        r = self.client.post(f"/models/bikes/{model['id']}",
                             data={"weight_kg": "25 кг", "note": ""})
        self.assertEqual(r.status_code, 303)
        self.assertIn("Вес, кг: только число", self.get_ok("/models"))
        self.client.post(f"/models/bikes/{model['id']}",
                         data={"speed_kmh": "99999999999", "note": ""})
        self.assertIn("Скорость, км/ч: число от 0", self.get_ok("/models"))
        self.assertEqual(run(self.crm.bike_model(model["id"]))["speed_kmh"], 45,
                         "отказ не стирает записанное")
        self.client.post("/models/batteries", data={"title": "60V", "capacity": "9999999"})
        self.assertNotIn("60V", [m["title"] for m in run(self.crm.battery_models())])
        self.client.post("/batteries", data={"code": "9510002", "volts": "inf",
                                             "service_months": "15", "cycles": "0"})
        self.assertIsNone(run(self.crm.battery_by_code("9510002")))

    def test_dates_in_year_9999_are_refused(self):
        """Дата закрытия в 9999 году ложилась бесконечностью и ломала
        клиентов и риск, дата покупки - план замены и карточку батареи."""
        rental_id = run(service.open_rental(
            self.crm, client=run(self.crm.client(self.client_id)),
            bike=run(self.crm.bike(self.bike_id)),
            tariff=run(self.crm.tariff(self.tariff_id)), started_on=date.today(),
            contract_no=None, by="t"))
        self.client.post(f"/rentals/{rental_id}/close",
                         data={"closed_on": "31.12.9999", "bike_status": "available"})
        self.assertEqual(run(self.crm.rental(rental_id))["status"], "active")
        self.assertIn("ещё не наступила", self.get_ok(f"/rentals/{rental_id}"))
        self.client.post("/batteries", data={"code": "9510003",
                                             "purchased_on": "31.12.9998",
                                             "service_months": "15", "cycles": "0"})
        self.assertIsNone(run(self.crm.battery_by_code("9510003")))
        self.client.post("/bikes", data={"code": "N-2", "model": "Kugoo V3",
                                         "purchased_on": "31.12.9998"})
        self.assertIsNone(run(self.crm.bike_by_code("N-2")))
        self.client.post("/assets", data={"codes": "Z-2", "model": "Kugoo V3",
                                          "purchased_on": "31.12.9998"})
        self.assertEqual(run(self.crm.purchases()), [])
        self.get_ok("/batteries/plan")

    def test_check_photo_field_is_checked_before_the_disk(self):
        """Поле сверки - из формы и стоит в имени файла: «../x» писало
        снимок мимо папки раньше, чем сервис отказывал полю."""
        import dataclasses
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "bikes"
            app = create_app(crm=self.crm, db=self.db, bot=self.bot,
                             cfg=dataclasses.replace(self.cfg, bike_photo_dir=folder))
            client = TestClient(app, follow_redirects=False)
            client.post("/login", data={"login": "admin", "password": "admin-pass-123"})
            for field in ("../../escape", "nope"):
                r = client.post(f"/bikes/{self.bike_id}/check",
                                data={"field": field, "action": "check"},
                                files={"photo": ("a.jpg", b"\xff\xd8 jpeg", "image/jpeg")})
                self.assertEqual(r.status_code, 303, field)
            written = [p.name for p in Path(tmp).rglob("*") if p.is_file()]
            self.assertEqual(written, [])
            self.assertIn("Неизвестное поле паспорта", plain(client.get("/").text))

    def test_role_name_cannot_break_out_of_the_confirm(self):
        """Название роли шло внутрь confirm('…') в атрибуте: HTML-экранирование
        апострофа браузер снимает до JS, и «x');alert(1);//» становилось кодом."""
        pid = run(self.crm.create_access_profile(
            "x');alert(1);//", {"sections": {"bikes": "view"}, "actions": {}}))
        page = self.client.get(f"/profiles/{pid}").text
        self.assertNotIn("confirm('", page)
        self.assertIn('data-confirm="Удалить роль «x&#39;);alert(1);//»?"', page)
        self.assertIn('onsubmit="return confirm(this.dataset.confirm)"', page)

    def test_issue_leaves_another_clients_booking_alone(self):
        other = run(self.crm.create_client(full_name="Петров", phone="+79990000002"))
        booking = run(self.crm.create_booking(client_id=other, model="Kugoo V3",
                                              tariff_id=self.tariff_id, location_id=None,
                                              wanted_on=date.today()))
        r = self.client.post("/issue", data={"client_id": self.client_id,
                                             "tariff_id": self.tariff_id,
                                             "bike_id": self.bike_id,
                                             "booking_id": str(booking),
                                             "started_on": date.today().isoformat(),
                                             "pay_amount": "0", "mileage": "10"})
        self.assertEqual(r.status_code, 303)
        self.assertIsNotNone(run(self.crm.active_rental_of(self.client_id)))
        self.assertEqual(run(self.crm.booking(booking))["status"], "new",
                         "чужая заявка не закрыта этой выдачей")


class TestFleetMetricsPages(WebCase):
    """Три числа на сводке и в отчётах, амортизация, ремонт по узлам,
    точки, новые статусы."""

    def test_dashboard_three_numbers(self):
        self.seed()
        run(self.crm.create_bike(code="B-2", model="Truck+", status="repair",
                                 location="Павлюхина", purchase_price=D("48000"),
                                 residual_price=D("0"), service_months=24,
                                 battery_price=D("9000"), battery_count=2,
                                 battery_service_months=15))
        run(self.crm.create_bike(code="B-3", model="Truck+", status="lost"))
        self.login()
        page = self.get_ok("/")
        self.assertIn("Три числа", page)
        self.assertIn("операционный парк", page)
        self.assertIn(">2</b>", page)                        # B-1 и B-2, без потерянного
        self.assertIn("отложить на парк", page)
        self.assertIn("3 200 ₽", page)                       # 48000/24 + 9000*2/15
        self.assertIn("Павлюхина: свободных <b>0</b>, в ремонте и на ТО <b>1</b>", page)

    def test_money_on_the_page_does_not_wrap(self):
        """Сырая страница: сумма целиком на неразрывных пробелах - плитка
        сводки не рвёт «2 107 700» и не уносит «₽» на свою строку."""
        self.seed()
        self.login()
        run(self.crm.add_ledger(client_id=self.client_id, kind="payment",
                                amount=D("2107700")))
        for path in ("/", "/finance"):
            raw = self.client.get(path).text
            self.assertIn("2\u00a0107\u00a0700\u00a0₽", raw, path)
            self.assertNotIn("2 107 700 ₽", raw, path)

    def test_bike_form_amortization_and_location(self):
        self.login()
        r = self.client.post("/bikes", data={"code": "B-9", "model": "Truck+",
                                             "location": "Адоратского", "purchase_price": "47000",
                                             "service_months": "24", "residual_price": "5000",
                                             "battery_price": "9000", "battery_count": "2",
                                             "battery_service_months": "15"})
        self.assertEqual(r.status_code, 303)
        bike = run(self.crm.bike_by_code("B-9"))
        self.assertEqual((bike["location"], bike["service_months"], bike["residual_price"],
                          bike["battery_price"], bike["battery_service_months"]),
                         ("Адоратского", 24, D("5000"), D("9000"), 15))
        page = self.get_ok(f"/bikes/{bike['id']}")
        self.assertIn("2 950 ₽</b> в месяц", page)
        self.assertIn("История статусов", page)
        r = self.client.post("/bikes", data={"code": "B-10", "model": "T", "location": "Марс"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("Точка: недопустимое значение", self.client.get("/bikes/new").text)
        self.assertIsNone(run(self.crm.bike_by_code("B-10")))
        # фильтр по точке в списке, «не на точке» - отдельный фильтр
        self.assertIn("B-9", self.client.get("/bikes", params={"location": "Адоратского"}).text)
        self.assertNotIn("B-9", self.client.get("/bikes", params={"location": "Павлюхина"}).text)
        run(self.crm.create_bike(code="B-11", model="T"))
        page = self.client.get("/bikes", params={"location": "none"}).text
        self.assertIn("B-11", page)
        self.assertNotIn("B-9", page)
        # автор первой записи журнала статусов - тот, кто завёл велосипед.
        # Заводится он «на сборке»: сверка требуется по умолчанию, и в
        # выдачу велосипед попадёт только после ввода в эксплуатацию.
        log = run(self.crm.bike_status_log(bike["id"]))
        self.assertEqual((log[-1]["to_status"], log[-1]["changed_by"]),
                         ("new", "staff:admin"))

    def test_repair_by_node_and_report(self):
        self.seed()
        self.login()
        r = self.client.post(f"/bikes/{self.bike_id}/repair",
                             data={"node": "brake_pads", "parts_cost": "400",
                                   "labor_cost": "300", "note": "передние"})
        self.assertEqual(r.status_code, 303)
        entries = run(self.crm.bike_log(self.bike_id))
        self.assertEqual((entries[0]["kind"], entries[0]["cost"], entries[0]["note"]),
                         ("repair", D("700"), "Тормоза: колодки: передние"))
        from datetime import UTC, datetime
        self.assertEqual(run(self.crm.repair_stats(
            datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)))["by_node"][0]["n"],
            1)
        # ноль в стоимости - допустимо (работа своя, запчастей не было)
        r = self.client.post(f"/bikes/{self.bike_id}/repair",
                             data={"node": "wiring", "parts_cost": "0", "labor_cost": "0"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(len(run(self.crm.bike_log(self.bike_id))), 2)
        r = self.client.post(f"/bikes/{self.bike_id}/repair", data={"node": "warp_drive"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("Узел", self.client.get(f"/bikes/{self.bike_id}").text)
        self.assertEqual(len(run(self.crm.bike_log(self.bike_id))), 2)
        report = self.get_ok("/reports")
        self.assertIn("Тормоза: колодки", report)
        self.assertIn("Kugoo V3", report)
        self.assertIn("Три числа по месяцам", report)

    def test_status_history_and_new_statuses(self):
        self.seed()
        self.login()
        r = self.client.post(f"/bikes/{self.bike_id}/status",
                             data={"status": "maintenance", "note": "плановое ТО"})
        self.assertEqual(r.status_code, 303)
        log = run(self.crm.bike_status_log(self.bike_id))
        self.assertEqual((log[0]["from_status"], log[0]["to_status"], log[0]["changed_by"]),
                         ("available", "maintenance", "staff:admin"))
        page = self.get_ok(f"/bikes/{self.bike_id}")
        self.assertIn("На ТО", page)
        run(self.crm.update_bike(self.bike_id, status="available"))
        rid = run(service.open_rental(self.crm, client=run(self.crm.client(self.client_id)),
                                      bike=run(self.crm.bike(self.bike_id)),
                                      tariff=run(self.crm.tariff(self.tariff_id)),
                                      started_on=date.today(), contract_no=None, by="t"))
        r = self.client.post(f"/rentals/{rid}/close", data={"bike_status": "written_off",
                                                            "note": "рама лопнула"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(run(self.crm.bike(self.bike_id))["status"], "written_off")
        log = run(self.crm.bike_status_log(self.bike_id))
        self.assertEqual((log[0]["to_status"], log[0]["changed_by"]),
                         ("written_off", "staff:admin"))


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

    def test_unknown_or_disabled_login_costs_the_same_scrypt(self):
        """Без логина отказ приходил без scrypt - быстрее в десятки раз, и
        по времени ответа было видно, какой логин есть."""
        from unittest import mock

        from app.web import app as web_app
        run(self.crm.create_staff("gone", logic.hash_password("password-1"), "Ушёл",
                                  "manager"))
        gone = run(self.crm.staff_by_login("gone"))
        run(self.crm.set_staff_active(gone["id"], False))
        seen = []
        real = logic.verify_password

        def spy(password, stored):
            seen.append(stored)
            return real(password, stored)

        with mock.patch.object(web_app.logic, "verify_password", spy):
            self.assertEqual(self.login("nobody", "password-1").status_code, 401)
            self.assertEqual(self.login("gone", "password-1").status_code, 401)
        self.assertEqual(seen[0], web_app.DECOY_PASSWORD_HASH)
        self.assertEqual(seen[1], gone["password_hash"],
                         "отключённый проверяется своим хэшем, отказ - после")
        self.assertFalse(real("", web_app.DECOY_PASSWORD_HASH))

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
        # телефон начинается с «+» - отдаётся как формула-строка, чтобы Excel
        # не превратил его в число и не вычислял ничего из имён
        self.assertIn('Иванов Иван;"=""+79990000000""";Активен;есть;3000,00', text)
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
