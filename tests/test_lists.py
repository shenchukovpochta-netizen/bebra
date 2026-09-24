"""Мелочи списков: выгрузка, «без техники», дни на складе, трек за период.

Ничего из этого не меняет данные — поэтому здесь проверяется ровно одно:
что видно на экране, то и уезжает файлом, и что счётчики считают то, что
обещают подписью.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def line_of(html: str) -> list:
    """Линия трека из страницы: она уезжает в скрипт как `var line = …`."""
    import json
    import re
    got = re.search(r"var line = (\[.*?\]);", html, re.S)
    return json.loads(got.group(1)) if got else []


class TestListLogic(unittest.TestCase):
    def test_days_on_stock_and_stale(self):
        now = datetime.now(UTC)
        parts = [{"id": 1, "title": "Камера", "min_stock": 5},
                 {"id": 2, "title": "Колодки", "min_stock": 0},
                 {"id": 3, "title": "Зеркало", "min_stock": 0}]
        moved = {1: now - timedelta(days=3),
                 2: now - timedelta(days=logic.STOCK_STALE_DAYS + 10)}
        rows = {r["id"]: r for r in logic.part_rows(
            parts, {1: 10, 2: 4, 3: 0}, moved, today=now.date())}
        self.assertEqual(rows[1]["days_on_stock"], 3)
        self.assertFalse(rows[1]["stale"])
        self.assertTrue(rows[2]["stale"], "лежит дольше квартала — деньги на полке")
        self.assertIsNone(rows[3]["days_on_stock"], "движений не было вовсе")
        self.assertEqual(logic.stock_summary(rows.values())["stale"], 1)

    def test_empty_shelf_is_not_stale(self):
        now = datetime.now(UTC)
        rows = logic.part_rows([{"id": 1, "title": "Камера", "min_stock": 0}],
                               {1: 0},
                               {1: now - timedelta(days=200)}, today=now.date())
        self.assertFalse(rows[0]["stale"],
                         "пустая полка не лежит — лежать нечему")

    def test_track_periods(self):
        today = date(2026, 9, 17)
        self.assertEqual(logic.track_period("today", today=today),
                         (today, today))
        self.assertEqual(logic.track_period("yesterday", today=today),
                         (date(2026, 9, 16), date(2026, 9, 16)))
        self.assertEqual(logic.track_period("week", today=today),
                         (date(2026, 9, 11), today))
        self.assertEqual(logic.track_period("чушь", today=today), (today, today),
                         "непонятный вид — сегодня, а не пустой экран")

    def test_custom_period_is_clamped_to_the_journal(self):
        today = date(2026, 9, 17)
        self.assertEqual(
            logic.track_period("custom", today=today, since=date(2026, 9, 10),
                               until=date(2026, 9, 12)),
            (date(2026, 9, 10), date(2026, 9, 12)))
        # Журнал позиций живёт месяц: просить больше нечего.
        first, last = logic.track_period("custom", today=today,
                                         since=date(2020, 1, 1),
                                         until=date(2030, 1, 1))
        self.assertEqual(first, today - timedelta(days=31))
        self.assertEqual(last, today)
        # Перевёрнутый период — к умолчанию, а не к пустоте.
        self.assertEqual(
            logic.track_period("custom", today=today, since=date(2026, 9, 12),
                               until=date(2026, 9, 10)), (today, today))

    def test_track_line_drops_gps_jumps(self):
        rows = [{"recorded_at": 2, "lat": 55.80, "lon": 49.10},
                {"recorded_at": 1, "lat": 55.79, "lon": 49.09},
                {"recorded_at": 3, "lat": 10.0, "lon": 10.0},
                {"recorded_at": 4, "lat": 55.81, "lon": 49.11}]
        line = logic.track_line(rows)
        self.assertEqual(line[0], [55.79, 49.09], "линия идёт от старой точки")
        self.assertNotIn([10.0, 10.0], line, "перескок спутника — не поездка")
        self.assertEqual(len(line), 3)

    def test_track_line_survives_empty_coordinates(self):
        self.assertEqual(logic.track_line([]), [])
        self.assertEqual(
            logic.track_line([{"recorded_at": 1, "lat": None, "lon": 49.0}]), [])


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestExports(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def csv(self, path):
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200, path)
        self.assertIn("text/csv", r.headers["content-type"])
        self.assertTrue(r.text.startswith("﻿"), "BOM нужен Excel")
        return r.text

    def test_bikes_export_follows_the_filter(self):
        _run(self.crm.create_bike(code="B-Z", model="Другая"))
        everything = self.csv("/bikes.csv")
        self.assertIn("B-1", everything)
        self.assertIn("B-Z", everything)
        only = self.csv("/bikes.csv?q=B-Z")
        self.assertIn("B-Z", only)
        self.assertNotIn("B-1", only, "выгружают то, что видят")

    def test_rentals_export_opens(self):
        text = self.csv("/rentals.csv?status=all")
        self.assertIn("Аренда", text)

    def test_orders_export_opens(self):
        _run(self.crm.create_work_order(
            bike_id=self.bike_id, payer="own", client_id=None,
            complaint="стук", object_note=None, tech_id=None, estimate=D(0),
            created_by="тест"))
        text = self.csv("/orders.csv")
        self.assertIn("РЕМ-000001", text)

    def test_parts_export_has_days_on_stock(self):
        _run(self.crm.create_part(title="Камера", node="tube_tire", unit="шт",
                                  cost=D(100), price=D(300), min_stock=5,
                                  model=None, note=None))
        self.assertIn("Дней на складе", self.csv("/parts.csv"))

    def test_export_needs_the_section(self):
        """Кладовщик без доступа к парку выгрузку парка не получит."""
        profile = _run(self.crm.create_access_profile(
            "Только склад", {"sections": {"inventory": "edit"}}))
        _run(self.crm.create_staff("sklad", logic.hash_password("sklad-pass-1"),
                                   name="Кладовщик", role="manager",
                                   profile_id=profile))
        self.client.post("/logout")
        self.login("sklad", "sklad-pass-1")
        self.assertEqual(self.client.get("/bikes.csv").status_code, 403)
        self.assertEqual(self.client.get("/parts.csv").status_code, 200)


class TestRentalListLogic(unittest.TestCase):
    ROW = {"id": 7, "full_name": "Иванов Иван", "phone": "+7 (917) 000-11-22",
           "bike_code": "B-1", "bike_model": "Kugoo V3", "contract_no": "АВ-2026-000001"}

    def test_search_matches_every_field_and_digits_in_the_phone(self):
        for q in ("иван", "kugoo", "b-1", "АВ-2026", "7", "9170001", "917 000 11"):
            self.assertEqual(len(logic.rental_search([self.ROW], q)), 1, q)
        self.assertEqual(logic.rental_search([self.ROW], "Петров"), [])
        self.assertEqual(logic.rental_search([self.ROW], "8"), [],
                         "чужая цифра не цепляет ни телефон, ни номер")
        self.assertEqual(len(logic.rental_search([self.ROW], "  ")), 1, "пусто - все")

    def test_running_days_count_from_issue_to_today_or_closing(self):
        self.assertEqual(logic.rental_days({"started_on": date(2026, 9, 10)},
                                           today=date(2026, 9, 18)), 8)
        self.assertEqual(logic.rental_days({"started_on": date(2026, 9, 18)},
                                           today=date(2026, 9, 18)), 0, "выдана сегодня")
        self.assertEqual(logic.rental_days({"started_on": date(2026, 9, 1),
                                            "closed_on": date(2026, 9, 5)},
                                           today=date(2026, 9, 18)), 4)
        self.assertEqual(logic.rental_days({}), 0)

    def test_manual_reminder_kind_follows_the_days_left(self):
        self.assertEqual(logic.manual_reminder_kind({"active": True, "days_left": -2}),
                         logic.REMIND_OVERDUE)
        self.assertEqual(logic.manual_reminder_kind({"active": True, "days_left": 0}),
                         logic.REMIND_DUE)
        self.assertEqual(logic.manual_reminder_kind({"active": True, "days_left": 9}),
                         logic.REMIND_SOON, "оператор жмёт, когда решил, - шлём «через 9 дней»")
        self.assertIsNone(logic.manual_reminder_kind({"active": False, "days_left": -2}))
        self.assertIsNone(logic.manual_reminder_kind(None))

    def test_overdue_days_come_from_the_summary(self):
        self.assertEqual(logic.overdue_days({"active": True, "days_left": -3}), 3)
        self.assertEqual(logic.overdue_days({"active": True, "days_left": 2}), 0)
        self.assertEqual(logic.overdue_days({"active": False, "days_left": -9}), 0,
                         "закрытая аренда не просрочена")
        self.assertEqual(logic.overdue_days(None), 0)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestRentalViews(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.rental_id = _run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id,
            tariff_id=self.tariff_id, tariff_name="Неделя", period_days=7,
            price=D(3000), billing="weekly", started_on=date(2026, 9, 1),
            contract_no="АВ-1", created_by="тест"))

    def test_debt_total_counts_a_client_once(self):
        # Баланс клиентский и приходит в каждой его аренде: на вкладке
        # «Все» у клиента закрытая и идущая аренды и 3 000 долга - в
        # подвале 3 000, а не 6 000.
        _run(self.crm.update_rental(self.rental_id, status="closed"))
        second = _run(self.crm.create_bike(code="B-2", model="Kugoo V3"))
        _run(self.crm.create_rental(
            client_id=self.client_id, bike_id=second, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="weekly",
            started_on=date(2026, 9, 1), contract_no="АВ-2", created_by="тест"))
        _run(self.crm.add_ledger(client_id=self.client_id, kind="charge",
                                 amount=D(-3000), created_by="тест"))
        text = self.get_ok("/rentals?status=all")
        self.assertIn("долг 3", text)
        self.assertNotIn("долг 6", text)

    def test_paid_sort_follows_the_shown_date(self):
        other = _run(self.crm.create_client(full_name="Петров Пётр",
                                            phone="+79990000001"))
        second = _run(self.crm.create_bike(code="B-2", model="Kugoo V3"))
        # Петров начислен дальше, но оплачено у него меньше: граница
        # начисления впереди, а «оплачено до» - раньше, чем у Иванова.
        rid = _run(self.crm.create_rental(
            client_id=other, bike_id=second, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="weekly",
            started_on=date(2026, 9, 1), contract_no="АВ-2", created_by="тест"))
        _run(self.crm.update_rental(rid, billed_until=date.today() + timedelta(days=14)))
        _run(self.crm.add_ledger(client_id=other, kind="charge", amount=D(-9000),
                                 created_by="тест"))
        _run(self.crm.update_rental(self.rental_id, billed_until=date.today()))
        text = self.get_ok("/rentals?sort=paid&dir=asc")
        self.assertLess(text.index("Петров"), text.index("Иванов"))

    def test_nobike_shows_rentals_without_equipment(self):
        text = self.get_ok("/rentals?view=nobike")
        self.assertNotIn("АВ-1", text)
        _run(self.crm.update_rental(self.rental_id, bike_id=None))
        self.assertIn("Иванов", self.get_ok("/rentals?view=nobike"),
                      "аренда идёт, а велосипеда на руках нет")

    def test_debt_view_counts_only_debtors(self):
        self.assertNotIn("Иванов", self.get_ok("/rentals?view=debt"))
        _run(self.crm.add_ledger(client_id=self.client_id,
                                 rental_id=self.rental_id, kind="charge",
                                 amount=D(-3000)))
        self.assertIn("Иванов", self.get_ok("/rentals?view=debt"))

    def test_search_finds_by_name_phone_bike_and_contract(self):
        for q in ("иванов", "9990000000", "+7 999", "B-1", "АВ-1", str(self.rental_id)):
            self.assertIn("Иванов", self.get_ok(f"/rentals?q={q}"), q)
        self.assertNotIn("Иванов", self.get_ok("/rentals?q=Петров"))
        page = self.get_ok("/rentals?q=Петров")
        self.assertIn('value="Петров"', page, "строка поиска остаётся в поле")

    def test_overdue_view_and_column(self):
        """Аренда с 1 сентября на неделю - просрочена: сроки давно вышли."""
        page = self.get_ok("/rentals?view=overdue")
        self.assertIn("Иванов", page)
        self.assertIn("Просрочка ·", page, "чип считает просроченные")
        self.assertIn("просрочено 1", page, "подвал считает по всему найденному")
        fresh = _run(self.crm.create_client(full_name="Петров Пётр",
                                            phone="+79990000001"))
        bike = _run(self.crm.create_bike(code="B-2", model="Kugoo V3"))
        _run(self.crm.create_rental(
            client_id=fresh, bike_id=bike, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="weekly",
            started_on=date.today(), contract_no="АВ-2", created_by="тест"))
        page = self.get_ok("/rentals?view=overdue")
        self.assertIn("Иванов", page)
        self.assertNotIn("Петров", page, "свежая аренда не просрочена")
        self.assertEqual(self.client.get("/rentals?sort=overdue&dir=desc").status_code, 200)

    def test_repair_view_shows_rentals_with_an_open_order(self):
        self.assertNotIn("Иванов", self.get_ok("/rentals?view=repair"))
        _run(self.crm.create_work_order(
            bike_id=self.bike_id, payer="own", client_id=None, complaint="стук",
            object_note=None, tech_id=None, estimate=D(0), created_by="т"))
        page = self.get_ok("/rentals?view=repair")
        self.assertIn("Иванов", page)
        self.assertIn("В ремонте · 1", page)
        self.assertIn("открыт наряд", page, "в строке видно, что велосипед в сервисе")

    def test_export_carries_days_and_overdue(self):
        text = self.client.get("/rentals.csv").text
        self.assertIn("Идёт, дн.", text)
        self.assertIn("Просрочка, дн.", text)
        self.assertIn("Иванов", self.client.get("/rentals.csv?q=иванов").text)
        self.assertNotIn("Иванов", self.client.get("/rentals.csv?q=петров").text)

    # ─── карточка аренды: оплата, наряд, суток, напоминание ───

    def test_card_shows_days_and_offers_an_order(self):
        page = self.get_ok(f"/rentals/{self.rental_id}")
        self.assertIn("сут.", page)
        self.assertIn(f"/orders/new?bike={self.bike_id}", page, "наряд открывается с аренды")
        self.assertIn("Принять оплату", page)
        _run(self.crm.create_work_order(
            bike_id=self.bike_id, payer="own", client_id=None, complaint="стук",
            object_note=None, tech_id=None, estimate=D(0), created_by="т"))
        page = self.get_ok(f"/rentals/{self.rental_id}")
        self.assertIn("наряд РЕМ-", page, "открытый наряд виден тегом")
        self.assertNotIn(f"/orders/new?bike={self.bike_id}", page)

    def test_payment_from_the_card_lands_in_the_ledger_and_returns_there(self):
        r = self.client.post(f"/clients/{self.client_id}/ledger", data={
            "kind": "payment", "amount": "3000", "method": "cash",
            "note": "с карточки аренды", "next": f"/rentals/{self.rental_id}"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], f"/rentals/{self.rental_id}")
        rows = [x for x in _run(self.crm.ledger_of(self.client_id, 50))
                if x.get("kind") == "payment"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rental_id"], self.rental_id)
        # Чужой адрес в next не уводит с панели.
        r = self.client.post(f"/clients/{self.client_id}/ledger", data={
            "kind": "payment", "amount": "100", "method": "cash", "note": "",
            "next": "//evil.example/x"})
        self.assertEqual(r.headers["location"], f"/clients/{self.client_id}")

    def test_remind_button_sends_the_reminder_now(self):
        self.db.users[5001] = {"tg_id": 5001, "lang": None}
        r = self.client.post(f"/rentals/{self.rental_id}/remind",
                             data={"next": "/rentals"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/rentals")
        self.assertTrue(self.bot.sent, "клиенту ушло сообщение")
        chat_id, text = self.bot.sent[-1][0], self.bot.sent[-1][1]
        self.assertEqual(chat_id, 5001)
        self.assertIn("B-1", text)
        log = _run(self.crm.notice_log(limit=10))
        self.assertEqual(log[0]["code"], "rent_overdue", "аренда с 1 сентября просрочена")
        self.assertEqual(log[0]["detail"], "вручную")
        self.assertIn("напоминание отправлено", self.get_ok("/rentals"))
        # Расписание в тот же день второго не пришлёт.
        rental = _run(self.crm.rental(self.rental_id))
        self.assertEqual(rental["notified_on"], date.today())
        self.assertEqual(rental["notified_kind"], "overdue")

    def test_remind_needs_telegram_and_an_active_rental(self):
        _run(self.crm.update_client(self.client_id, tg_id=None))
        self.client.post(f"/rentals/{self.rental_id}/remind", data={})
        self.assertIn("нет в боте", self.get_ok(f"/rentals/{self.rental_id}"))
        self.assertEqual(self.bot.sent, [])
        _run(self.crm.update_client(self.client_id, tg_id=5001))
        _run(self.crm.update_rental(self.rental_id, status="closed"))
        self.client.post(f"/rentals/{self.rental_id}/remind", data={})
        self.assertIn("не идёт", self.get_ok(f"/rentals/{self.rental_id}"))
        self.assertEqual(self.bot.sent, [])

    def test_list_has_the_bell_only_for_clients_in_the_bot(self):
        self.assertIn(f"/rentals/{self.rental_id}/remind", self.get_ok("/rentals"))
        _run(self.crm.update_client(self.client_id, tg_id=None))
        self.assertNotIn(f"/rentals/{self.rental_id}/remind", self.get_ok("/rentals"))

    def test_tabs_are_on_the_page(self):
        text = self.get_ok("/rentals")
        self.assertIn("Без техники", text)
        self.assertIn("/rentals.csv", text)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestTrackPage(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.bike_id = _run(self.crm.create_bike(code="B-7", model="Kugoo"))
        self.tracker_id = _run(self.crm.create_tracker(
            device_id="1007", alias="Трекер 7", bike_id=self.bike_id))

    def point(self, when, lat, lon):
        _run(self.crm.save_tracker_state({
            "device_id": "1007", "alias": "Трекер 7", "lat": lat, "lon": lon,
            "speed": D(10), "voltage": D("12.6"), "recorded_at": when,
            "course": None, "gsm_level": None, "alarm": False}))

    def test_period_tabs_and_line(self):
        now = datetime.now().astimezone()
        self.point(now - timedelta(hours=1), 55.79, 49.09)
        self.point(now - timedelta(days=2), 55.70, 49.00)
        today = line_of(self.get_ok(f"/trackers/{self.tracker_id}?range=today"))
        self.assertIn([55.79, 49.09], today, "точка за сегодня попала в линию")
        self.assertNotIn([55.70, 49.00], today,
                         "позавчерашняя точка в «сегодня» не идёт")
        week = line_of(self.get_ok(f"/trackers/{self.tracker_id}?range=week"))
        self.assertIn([55.70, 49.00], week)

    def test_custom_period(self):
        now = datetime.now().astimezone()
        self.point(now - timedelta(days=3), 55.75, 49.05)
        day = (now - timedelta(days=3)).date().isoformat()
        got = line_of(self.get_ok(
            f"/trackers/{self.tracker_id}?range=custom&since={day}&until={day}"))
        self.assertEqual(got, [[55.75, 49.05]])


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
