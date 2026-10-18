"""Экраны «в одном окне»: «Входящие» одной лентой (app/crm/incoming.py),
сводка клиентов за всё время и отчёты одним окном."""

from __future__ import annotations

import re
import sys
import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import incoming, logic

try:
    from tests.test_web import HAVE_WEB, WebCase, run
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False
    WebCase = unittest.TestCase                        # type: ignore[misc,assignment]

TODAY = date(2026, 9, 30)


def at(days_ago):
    return datetime(2026, 9, 30, 12, tzinfo=UTC) - timedelta(days=days_ago)


class TestIncomingRows(unittest.TestCase):
    def test_one_feed_longest_wait_first(self):
        rows = incoming.incoming_rows(
            threads=[{"id": 1, "who": "Азиз", "preview": "когда забрать", "status": "work",
                      "channel_label": "WhatsApp", "waiting_since": at(1),
                      "waiting_hours": 0},
                     {"id": 2, "who": "Олег", "preview": "есть велосипед?", "status": "new",
                      "channel_label": "Авито", "waiting_since": at(0)},
                     # отвечен три дня назад: в ленте, но ниже всех и без «ждёт с»
                     {"id": 3, "who": "Отвечен", "preview": "спасибо", "status": "work",
                      "channel_label": "Telegram", "waiting_since": None,
                      "last_in_at": at(3)}],
            bookings=[{"id": 5, "client_id": 3, "full_name": "Заявкин", "status": "new",
                       "wanted_on": TODAY, "created_at": at(2), "model": "Kugoo"},
                      {"id": 6, "client_id": 4, "full_name": "Завтра", "status": "new",
                       "wanted_on": TODAY + timedelta(days=2), "created_at": at(3)},
                      {"id": 7, "client_id": 4, "full_name": "Снята", "status": "cancelled",
                       "wanted_on": TODAY, "created_at": at(3)}],
            claims=[{"id": 9, "full_name": "Платил", "amount_hint": D(3500),
                     "created_at": at(0)}],
            today=TODAY, booking_url=lambda b: f"/issue?booking={b['id']}")
        self.assertEqual([(r["kind"], r["who"]) for r in rows],
                         [("booking", "Заявкин"), ("message", "Азиз"),
                          ("message", "Олег"), ("claim", "Платил"),
                          ("booking", "Завтра"), ("message", "Отвечен")])
        self.assertEqual(rows[0]["url"], "/issue?booking=5")
        self.assertNotIn("₽", rows[3]["what"], "сумма - только с «Финансами»")
        self.assertIsNone(rows[-1]["since"], "отвеченный не «ждёт»")
        self.assertEqual(incoming.counts(rows),
                         {"message": 3, "booking": 2, "claim": 1, "all": 6})
        with_money = incoming.incoming_rows(
            claims=[{"id": 9, "full_name": "Платил", "amount_hint": D(3500)}],
            money_ok=True, today=TODAY)
        self.assertIn("3", with_money[0]["what"])

    def test_parts_follow_the_profile(self):
        owner = {"perms": logic.BUILT_IN_PROFILES[0][2]}
        manager = {"perms": logic.BUILT_IN_PROFILES[1][2]}
        tech = {"perms": logic.BUILT_IN_PROFILES[2][2]}
        self.assertEqual(incoming.visible_kinds(owner), ["message", "booking", "claim"])
        self.assertEqual(incoming.visible_kinds(manager), ["booking", "claim"],
                         "переписка - только владельцу")
        self.assertEqual(incoming.visible_kinds(tech), [])


class TestClientGroups(unittest.TestCase):
    ROWS = [
        {"id": 1, "rental_id": 10, "rentals_count": 3, "balance": D(0),
         "paid_total": D(9000), "rented_days": 30},
        {"id": 2, "rental_id": None, "rentals_count": 1, "balance": D(-500),
         "paid_total": D(3000), "rented_days": 7},
        {"id": 3, "rental_id": None, "rentals_count": 0, "balance": D(0),
         "paid_total": D(0), "rented_days": 0},
        {"id": 4, "rental_id": 11, "rentals_count": 1, "balance": D(-100),
         "paid_total": D(0), "rented_days": 2},
    ]

    def test_groups(self):
        pick = {g: [r["id"] for r in self.ROWS if logic.client_in_group(r, g)]
                for g in logic.CLIENT_GROUPS}
        self.assertEqual(pick, {"all": [1, 2, 3, 4], "active": [1, 4], "former": [2],
                                "never": [3], "debt": [2, 4], "bought": [], "repair": []})

    def test_bought_repair_and_kinds(self):
        bought = {"id": 5, "rentals_count": 1, "bought": True}
        fixer = {"id": 6, "rentals_count": 0, "external_repairs": 2,
                 "repair_objects": "Самокат Kugoo M4 | АКБ 60В от самоката"}
        self.assertTrue(logic.client_in_group(bought, "bought"))
        self.assertFalse(logic.client_in_group(fixer, "bought"))
        self.assertTrue(logic.client_in_group(fixer, "repair"))
        self.assertEqual(logic.client_kinds(fixer), {"scooter", "battery"},
                         "«АКБ от самоката» - это АКБ")
        self.assertEqual(logic.client_kinds(bought), {"bike"}, "арендатор - велосипед")
        self.assertEqual(logic.tech_kinds("трицикл | что-то своё"), {"tricycle", "other"})
        # вид словами главнее марки: Kugoo делает и самокаты, и велосипеды
        self.assertEqual(logic.tech_kinds("Электровелосипед Kugoo V3 Pro, свой у курьера"),
                         {"bike"})
        self.assertEqual(logic.tech_kinds("Электросамокат Kugoo G2 Pro"), {"scooter"})
        self.assertEqual(logic.tech_kinds("Kugoo M4 | Ninebot Max"), {"scooter"},
                         "одна марка - самокат")
        self.assertEqual(logic.top_values(["A", "B", "A", None, "C", "B", "A"], limit=2),
                         ["A", "B"])

    def test_groups_in_owners_words_and_order(self):
        # Слова и порядок - из записи владельца; коды прежние: на них
        # стоят сохранённые фильтры и закладки (?group=active).
        self.assertEqual(list(logic.CLIENT_GROUPS.items()), [
            ("all", "Все клиенты"), ("active", "Арендаторы вело"),
            ("former", "Сдали вело"), ("bought", "Купили технику"),
            ("repair", "Ремонтировались у нас"), ("debt", "Должники"),
            ("never", "Ни разу не брали")])

    def test_kind_in_every_group(self):
        """Вид техники понятен в любой группе: арендатор, сдавший и
        купивший - велосипед нашего парка, ремонтировавшийся - что приносил."""
        renter = {"rental_id": 10, "rentals_count": 1}
        former = {"rental_id": None, "rentals_count": 2}
        buyer = {"rental_id": None, "rentals_count": 1, "bought": True,
                 "bought_model": "Kugoo V3"}
        fixer = {"rentals_count": 0, "external_repairs": 1,
                 "repair_objects": "Трицикл грузовой"}
        silent = {"rentals_count": 0, "external_repairs": 1, "repair_objects": None}
        both = {"rental_id": 11, "rentals_count": 1, "external_repairs": 1,
                "repair_objects": "АКБ 48В"}
        nobody = {"rentals_count": 0}
        for row, group in ((renter, "active"), (former, "former"), (buyer, "bought"),
                           (fixer, "repair"), (silent, "repair")):
            self.assertTrue(logic.client_in_group(row, group), group)
        self.assertEqual(logic.client_kind_titles(renter), ["Велосипед"])
        self.assertEqual(logic.client_kind_titles(former), ["Велосипед"])
        self.assertEqual(logic.client_kind_titles(buyer), ["Велосипед"])
        self.assertEqual(logic.client_kind_titles(fixer), ["Трицикл"])
        self.assertEqual(logic.client_kind_titles(silent), ["Другое"],
                         "наряд без слов в объекте - «другое», а не «ничего»")
        self.assertEqual(logic.client_kind_titles(both), ["Велосипед", "АКБ"],
                         "наш парк первым")
        self.assertEqual(logic.client_kind_titles(nobody), [])
        counts = logic.client_kind_counts([renter, former, buyer, fixer, silent, both,
                                           nobody])
        self.assertEqual(list(counts), ["bike", "scooter", "tricycle", "battery", "other"])
        self.assertEqual(counts, {"bike": 4, "scooter": 0, "tricycle": 1, "battery": 1,
                                  "other": 1})

    def test_group_titles_for_export(self):
        row = {"rental_id": None, "rentals_count": 1, "bought": True,
               "balance": D(-100)}
        self.assertEqual(logic.client_group_titles(row, logic.CLIENT_GROUPS),
                         ["Сдали вело", "Купили технику", "Должники"])
        no_money = {k: v for k, v in logic.CLIENT_GROUPS.items() if k != "debt"}
        self.assertEqual(logic.client_group_titles(row, no_money),
                         ["Сдали вело", "Купили технику"])

    def test_tiles(self):
        tiles = logic.client_tiles(self.ROWS)
        self.assertEqual((tiles["all"], tiles["active"], tiles["former"], tiles["never"],
                          tiles["debt"]), (4, 2, 1, 1, 2))
        self.assertEqual((tiles["paid"], tiles["debt_sum"], tiles["days"]),
                         (D(12000), D(600), 39))


class TestHeadline(unittest.TestCase):
    TZ = timezone(timedelta(hours=3))

    def span(self, now=None, **params):
        now = now or datetime(2026, 9, 30, 15, tzinfo=self.TZ)
        return logic.report_prev_span(logic.report_period(params, now=now), now=now)

    def test_prev_span(self):
        # идущий месяц - то же прошедшее время прошлого, а не целый август
        cur = self.span(month="2026-09")
        self.assertEqual((cur["start"], cur["end"], cur["label"]),
                         (datetime(2026, 8, 1, tzinfo=self.TZ),
                          datetime(2026, 8, 30, 15, tzinfo=self.TZ), "01.08 — 30.08"))
        # закончившийся месяц - целый прошлый: сентябрь против всего августа
        done = self.span(month="2026-09", now=datetime(2026, 10, 5, 12, tzinfo=self.TZ))
        self.assertEqual((done["since"], done["until"], done["label"]),
                         (date(2026, 8, 1), date(2026, 8, 31), "08.2026"))
        full = self.span(month="2026-03")         # весь февраль, в март не вылезает
        self.assertEqual((full["since"], full["until"], full["label"]),
                         (date(2026, 2, 1), date(2026, 2, 28), "02.2026"))
        days = self.span()
        self.assertEqual(days["until"], date(2026, 8, 31))
        self.assertAlmostEqual(days["days"], 30)
        own = self.span(since="2026-09-01", until="2026-09-10")
        self.assertEqual((own["since"], own["until"]), (date(2026, 8, 22), date(2026, 8, 31)))
        # «сегодня до 15:00» - со «вчера до 15:00», а не со всеми вчерашними сутками
        today = self.span(since="2026-09-30", until="2026-09-30")
        self.assertEqual((today["start"], today["end"]),
                         (datetime(2026, 9, 29, tzinfo=self.TZ),
                          datetime(2026, 9, 29, 15, tzinfo=self.TZ)))

    def figures(self, idle, check, revenue, **extra):
        return {"metrics": {"idle_percent": idle, "avg_check": check, "revenue": revenue,
                            "operational_days": D(4800)}, "days": 30,
                "issued": 200, "renewals": 400, **extra}

    def test_rows_and_trend(self):
        now = self.figures(9.1, D(520), D(2400000), debt=D(70000), debtors=9,
                           new_clients=70, orders=90, integrity=2)
        prev = self.figures(11.0, D(500), D(2300000), new_clients=75, orders=90)
        rows = {r["code"]: r for r in logic.report_headline(now, prev, can=lambda s: True)}
        self.assertEqual(rows["fleet"]["now"], "160 шт.")
        idle = rows["idle"]
        self.assertEqual((idle["now"], idle["prev"], idle["goal"]), ("9.1 %", "11.0 %", "< 10 %"))
        self.assertEqual((idle["up"], idle["better"], idle["ok"]), (False, True, True),
                         "простой упал - это лучше")
        self.assertEqual((rows["clients"]["up"], rows["clients"]["better"]), (False, False))
        self.assertIsNone(rows["orders"]["up"], "без изменений - без стрелки")
        self.assertEqual(rows["issued"]["now"], "200 / 400")
        self.assertEqual((rows["debt"]["prev"], rows["debt"]["ok"]), ("", False))
        self.assertIn("9 чел.", rows["debt"]["now"])
        self.assertEqual(rows["integrity"]["now"], "2")

    def test_rows_follow_rights(self):
        now = self.figures(9.1, D(520), D(1), debt=D(0))
        rows = logic.report_headline(now, None, can=lambda s: s != "finance")
        codes = [r["code"] for r in rows]
        self.assertEqual(codes, ["fleet", "idle", "issued"],
                         "деньги - с «Финансами», закрытых разделов нет")
        self.assertTrue(all(r["prev"] == "—" for r in rows))


class TestReportHelpers(unittest.TestCase):
    def test_channel_totals_in_period(self):
        rows = [{"channel": "avito", "created_at": at(1)},
                {"channel": "avito", "created_at": at(2)},
                {"channel": "2gis", "created_at": at(0)},
                {"channel": None, "created_at": at(0)},
                {"channel": "avito", "created_at": at(40)}]
        self.assertEqual(logic.channel_totals(rows, since=TODAY - timedelta(days=7),
                                              until=TODAY),
                         [("avito", 2), ("", 1), ("2gis", 1)])

    def test_feedback_stats(self):
        stats = logic.feedback_stats([{"channel": "tg", "score": 5},
                                      {"channel": "tg", "score": 2},
                                      {"channel": "tg", "score": None}])
        self.assertEqual((stats["asked"], stats["answered"], stats["low"]), (3, 2, 1))
        self.assertEqual(stats["avg"], D("3.5"))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestOneWindowPages(WebCase):
    def setUp(self):
        super().setUp()
        self.seed()
        crm = self.crm
        run(crm.create_booking(client_id=self.client_id, model="Kugoo V3", tariff_id=None,
                               location_id=None, wanted_on=date.today()))
        run(crm.create_claim(self.client_id, D(3000)))
        self.other = run(crm.create_client(full_name="Бывший Клиент",
                                           phone="+79990000002"))
        run(crm.create_client(full_name="Новенький", phone="+79990000003"))
        # бывший: одна закрытая аренда и долг
        run(crm.create_rental(client_id=self.other, bike_id=self.bike_id,
                              tariff_id=self.tariff_id, tariff_name="Неделя", period_days=7,
                              price=D(3000), billing="auto",
                              started_on=date.today() - timedelta(days=20),
                              contract_no=None, created_by="t"))
        rid = max(crm.rentals_)
        run(crm.add_ledger(client_id=self.other, kind="charge", amount=D(-3000),
                            rental_id=rid))
        run(crm.close_rental(rid, closed_on=date.today() - timedelta(days=13), note=None))
        # действующий
        bike2 = run(crm.create_bike(code="B-2", model="Kugoo V3"))
        run(crm.create_rental(client_id=self.client_id, bike_id=bike2,
                              tariff_id=self.tariff_id, tariff_name="Неделя", period_days=7,
                              price=D(3000), billing="auto",
                              started_on=date.today() - timedelta(days=3),
                              contract_no=None, created_by="t"))
        run(crm.add_ledger(client_id=self.client_id, kind="payment", amount=D(3000)))

    def as_profile(self, login, code):
        profile = run(self.crm.access_profile_by_code(code))
        run(self.crm.create_staff(login, logic.hash_password("password-1"), login,
                                  "manager", profile["id"]))
        self.client.post("/logout")
        self.login(login, "password-1")

    def test_incoming_one_menu_item(self):
        self.login()
        board = self.get_ok("/incoming")
        self.assertIn("Воронка", board)
        self.assertIn("Иванов Иван", board, "идущая аренда - сделка «В аренде»")
        page = self.get_ok("/incoming/feed")
        self.assertIn("Иванов Иван", page)
        self.assertIn("нажал «Я оплатил»", page)
        self.assertRegex(page, r"Лента · 2")
        menu = page.split("<main>")[0]
        self.assertIn('href="/incoming"', menu)
        for gone in ('href="/bookings"', 'href="/claims"', 'href="/inbox"'):
            self.assertNotIn(gone, menu, "три пункта меню стали одним")
        for path in ("/bookings", "/claims", "/inbox"):
            self.assertIn('href="/incoming"', self.get_ok(path).split("<main>")[1],
                          f"{path}: вкладки общего раздела")

    def test_incoming_by_profile(self):
        self.as_profile("oper", "manager")
        page = self.get_ok("/incoming")
        self.assertNotIn('href="/inbox"', page, "переписка оператору закрыта")
        self.assertIn("Иванов Иван", page)
        self.as_profile("mech", "tech")
        self.assertEqual(self.client.get("/incoming").status_code, 403)
        self.assertNotIn('href="/incoming"', self.get_ok("/me"))

    def test_clients_bought_repair_model(self):
        from app.crm import service
        fixer = run(self.crm.create_client(full_name="Самокатчик", phone="+79990000005"))
        run(service.open_order(self.crm, bike=None, payer="client",
                               client=run(self.crm.client(fixer)), complaint="мотор",
                               object_note="Самокат Kugoo M4", tech_id=None, estimate=D(0),
                               by="t"))
        # «Бывший Клиент» выкупил свой велосипед: последняя аренда - его
        self.crm.bikes_[self.bike_id]["status"] = "sold"
        self.login()
        repair = self.get_ok("/clients?group=repair")
        self.assertIn("Самокатчик", repair)
        self.assertNotIn("Иванов Иван", repair)
        self.assertIn("Самокат Kugoo M4", repair)
        bought = self.get_ok("/clients?group=bought")
        self.assertIn("Бывший Клиент", bought)
        self.assertNotIn("Самокатчик", bought)
        scooters = self.get_ok("/clients?kind=scooter")
        self.assertIn("Самокатчик", scooters)
        self.assertNotIn("Иванов Иван", scooters)
        model = self.get_ok("/clients?model=Kugoo+V3")
        self.assertIn("Иванов Иван", model)
        self.assertNotIn("Самокатчик", model)
        self.assertIn("Самокатчик", self.get_ok("/clients?model=чужая"),
                      "неизвестная модель - фильтра нет")

    def test_clients_summary(self):
        self.login()
        page = self.get_ok("/clients")
        self.assertIn("база за всё время", page)
        found = re.findall(r'group=(\w+)" (?:class="on")?\s*>([^<]*) · (\d+)<', page)
        self.assertEqual(found, [("all", "Все клиенты", "3"),
                                 ("active", "Арендаторы вело", "1"),
                                 ("former", "Сдали вело", "1"),
                                 ("bought", "Купили технику", "0"),
                                 ("repair", "Ремонтировались у нас", "0"),
                                 ("debt", "Должники", "1"),
                                 ("never", "Ни разу не брали", "1")],
                         "вкладки - словами владельца и в его порядке")
        tiles = page.split('class="tiles"')[1].split("</div>")[0]
        order = [tiles.index(w) for w in ("все клиенты", "арендаторы вело", "сдали вело",
                                          "купили технику", "ремонтировались у нас",
                                          "должники", "ни разу не брали")]
        self.assertEqual(order, sorted(order), "плитки - те же слова в том же порядке")
        active = self.get_ok("/clients?group=active")
        self.assertIn("Иванов Иван", active)
        self.assertNotIn("Бывший Клиент", active)
        former = self.get_ok("/clients?group=former")
        self.assertIn("Бывший Клиент", former)
        self.assertIn("оплатили за всё время", page)
        csv = self.client.get("/clients.csv?group=never").text
        self.assertIn("Новенький", csv)
        self.assertNotIn("Бывший Клиент", csv)
        self.assertIn("Оплатил за всё время", csv)

    def fixer(self):
        """Клиент, чинивший у нас свой самокат: наряд на чужую технику."""
        from app.crm import service
        fixer = run(self.crm.create_client(full_name="Самокатчик", phone="+79990000005"))
        run(service.open_order(self.crm, bike=None, payer="client",
                               client=run(self.crm.client(fixer)), complaint="мотор",
                               object_note="Самокат Kugoo M4", tech_id=None, estimate=D(0),
                               by="t"))
        return fixer

    def test_kinds_switch_and_column(self):
        self.fixer()
        self.login()
        page = self.get_ok("/clients")
        kinds = page.split('class="tabs seg tech-kinds"')[1].split("</nav>")[0]
        self.assertEqual(re.findall(r">([^<]+) · (\d+)</a>", kinds),
                         [("Вся техника", "4"), ("Велосипед", "2"), ("Самокат", "1")],
                         "виды со счётчиками, пустые не показываются")
        self.assertIn("<th>Вид техники</th>", page)
        self.assertIn('data-label="Вид техники">Самокат', page)
        self.assertIn('data-label="Вид техники">Велосипед', page)
        repair = self.get_ok("/clients?group=repair")
        kinds = repair.split('class="tabs seg tech-kinds"')[1].split("</nav>")[0]
        self.assertEqual(re.findall(r">([^<]+) · (\d+)</a>", kinds),
                         [("Вся техника", "1"), ("Самокат", "1")],
                         "счётчики - внутри группы")
        self.assertIn("group=repair&kind=scooter", kinds)
        scooters = self.get_ok("/clients?group=all&kind=scooter")
        self.assertIn("Самокатчик", scooters)
        self.assertNotIn("Бывший Клиент", scooters)
        # вкладка группы помнит выбранный вид
        self.assertIn("&kind=scooter&group=repair", scooters)

    def test_renter_who_bought_keeps_current_model(self):
        """Выкупил велосипед раньше и снова в аренде на другом: под видом -
        модель, на которой ездит сейчас, и «купил …» своей строкой."""
        buyer = run(self.crm.create_client(full_name="Выкупивший", phone="+79990000007"))
        sold = run(self.crm.create_bike(code="B-3", model="Monster"))
        run(self.crm.create_rental(client_id=buyer, bike_id=sold,
                                   tariff_id=self.tariff_id, tariff_name="Неделя",
                                   period_days=7, price=D(3000), billing="auto",
                                   started_on=date.today() - timedelta(days=60),
                                   contract_no=None, created_by="t"))
        run(self.crm.close_rental(max(self.crm.rentals_),
                                  closed_on=date.today() - timedelta(days=40), note=None))
        self.crm.bikes_[sold]["status"] = "sold"
        now = run(self.crm.create_bike(code="B-4", model="Kugoo V3"))
        run(self.crm.create_rental(client_id=buyer, bike_id=now,
                                   tariff_id=self.tariff_id, tariff_name="Неделя",
                                   period_days=7, price=D(3000), billing="auto",
                                   started_on=date.today() - timedelta(days=2),
                                   contract_no=None, created_by="t"))
        self.login()
        page = self.get_ok("/clients?group=active")
        row = next(r for r in page.split("<tr>") if "Выкупивший" in r)
        cell = row.split('data-label="Вид техники">')[1].split("</td>")[0]
        self.assertIn('<small class="muted">Kugoo V3</small>', cell, "модель идущей аренды")
        self.assertIn('<small class="muted">купил Monster</small>', cell)
        former = self.get_ok("/clients?group=former")
        row = next(r for r in former.split("<tr>") if "Бывший Клиент" in r)
        self.assertIn("брал ", row.split('data-label="Вид техники">')[1].split("</td>")[0])
        # как заведена карточка - те же слова, что в выгрузке (logic.CLIENT_SOURCES)
        self.assertIn("<dt>Заведён</dt><dd>в панели · ", self.get_ok(f"/clients/{self.other}"))

    def test_export_is_the_whole_base(self):
        """«Все клиенты» в Excel/CSV - вся база, а не страница списка, с
        группами, видом техники, моделью, долгом и каналом."""
        self.fixer()
        run(self.crm.update_client(self.other, channel="avito"))
        for i in range(55):
            run(self.crm.create_client(full_name=f"Клиент {i:02}", phone=f"+7990100{i:04}"))
        self.login()
        page = self.get_ok("/clients")
        self.assertIn("/clients.xlsx?", page)
        self.assertEqual(page.count('data-label="Клиент"'), 50, "на странице - 50")
        lines = self.client.get("/clients.csv?group=all").content.decode(
            "utf-8-sig").splitlines()
        self.assertEqual(len(lines), 1 + 3 + 1 + 55, "в файле - все")
        head = lines[0].split(";")
        for col in ("ФИО", "Телефон", "Группы", "Вид техники", "Модель (последняя аренда)",
                    "Первая аренда", "Последний день аренды", "Оплатил за всё время",
                    "Долг", "Откуда узнал", "Как заведён"):
            self.assertIn(col, head)
        self.assertNotIn("Риск", head, "оценка риска выключена - колонки нет")
        rows = {line.split(";")[0]: dict(zip(head, line.split(";"), strict=True))
                for line in lines[1:]}
        former = rows["Бывший Клиент"]
        self.assertEqual((former["Группы"], former["Вид техники"], former["Долг"],
                          former["Откуда узнал"], former["Как заведён"]),
                         ("Сдали вело, Должники", "Велосипед", "3000,00", "Авито",
                          "в панели"))
        self.assertEqual(former["Модель (последняя аренда)"], "Kugoo V3")
        fixer = rows["Самокатчик"]
        self.assertEqual((fixer["Группы"], fixer["Вид техники"], fixer["Что чинил у нас"]),
                         ("Ремонтировались у нас, Ни разу не брали", "Самокат",
                          "Самокат Kugoo M4"))
        self.assertEqual(rows["Иванов Иван"]["Группы"], "Арендаторы вело")

    def test_clients_summary_hides_money(self):
        self.as_profile("oper", "manager")
        manager = run(self.crm.access_profile_by_code("manager"))
        # Копия прав, а не правка на месте: словарь встроенного профиля общий
        # на процесс, и вырезанные «Финансы» уехали бы в соседние тесты.
        stored = self.crm.profiles_[manager["id"]]
        sections = {k: v for k, v in stored["perms"]["sections"].items() if k != "finance"}
        stored["perms"] = {**stored["perms"], "sections": sections}
        page = self.get_ok("/clients")
        self.assertNotIn("оплатили за всё время", page)
        self.assertNotIn("Оплатил всего", page)
        self.assertNotIn("должник", page.lower(), "долг - это деньги")
        debt = self.get_ok("/clients?group=debt")
        self.assertIn("Новенький", debt, "без «Финансов» группа должников - просто все")
        self.assertIn("Бывший Клиент", self.get_ok("/clients?sort=rentals&dir=desc"))

    def test_reports_one_window(self):
        self.login()
        page = self.get_ok("/reports")
        for block in ("всё в одном окне", "Главное", "Деньги за период", "По точкам",
                      "Тарифы", "Окупаемость по моделям", "Что купить", "Клиенты",
                      "Сервис: техники", "Расход склада", "Три числа по месяцам",
                      "Должники"):
            self.assertIn(block, page, block)
        self.assertIn("/reports/points?since=", page, "подробнее - тот же период")
        head = page.split('class="headline')[1].split("</table>")[0]
        for row in ("Простой", "Средний чек в день", "Поступило от клиентов",
                    "Выдач / продлений", "Новых клиентов", "Долг клиентов сейчас"):
            self.assertIn(row, head, row)
        self.assertIn("&lt; 10 %", head)
        # свёрнутые блоки - не больше четырёх колонок
        for block in page.split("<details")[1:]:
            for table in block.split("<table")[1:]:
                header = table.split("</tr>")[0]
                self.assertLessEqual(header.count("<th"), 4, header[:200])
        # «Главное» и «Итого» по точкам - одно окно и одна выручка
        paid = re.search(r"Поступило от клиентов</td>\s*<td[^>]*><b>([^<]+)</b>", page)
        total = re.search(r'<tr class="total"><td>Итого</td>.*?<td class="num">([^<]+)</td>'
                          r"\s*</tr>", page, re.S)
        self.assertEqual(paid.group(1).strip(), total.group(1).strip())
        self.assertEqual(paid.group(1).strip(), "3 000 ₽")
        month = date.today().strftime("%Y-%m")
        page = self.get_ok(f"/reports?month={month}")
        prev = date.today().replace(day=1) - timedelta(days=1)
        self.assertIn(f"Прошлый · 01.{prev:%m} — ", page, "идущий месяц - по сегодняшнее")
        past = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        before = (prev.replace(day=1) - timedelta(days=1)).strftime("%m.%Y")
        self.assertIn(f"Прошлый · {before}", self.get_ok(f"/reports?month={past}"),
                      "закончившийся месяц - против целого прошлого")

    def test_reports_one_window_follows_rights(self):
        self.as_profile("mech", "tech")
        page = self.get_ok("/reports")
        self.assertIn("Сервис: техники", page)
        self.assertIn("Нарядов закрыто", page)
        for money_block in ("Деньги за период", "Окупаемость по моделям", "Что купить",
                            "Средний чек", "Поступило", "Долг клиентов"):
            self.assertNotIn(money_block, page, money_block)
        self.assertNotIn("Откуда новые клиенты", page)
        months = self.get_ok("/reports/months")
        self.assertIn("Дней аренды", months)
        self.assertNotIn("КПД", months)


if __name__ == "__main__":
    unittest.main()
