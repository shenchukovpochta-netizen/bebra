"""Доход по статьям и платёжный календарь (app/crm/cashflow.py), блоки
«Критично» и «Доход по статьям» на сводке."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import cashflow, logic

try:
    from tests.test_web import HAVE_WEB, WebCase, run
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False
    WebCase = unittest.TestCase                        # type: ignore[misc,assignment]

TODAY = date(2026, 10, 5)


class TestIncome(unittest.TestCase):
    def test_articles_by_point(self):
        money = {"Павлюхина": {"paid": D(30000), "charged": D(28000),
                               "charged_fines": D(29000), "refunded": D(0)},
                 None: {"paid": D(1000), "charged": D(0), "charged_fines": D(0)}}
        repairs = {"Павлюхина": {"external": D(5000), "renters": D(2000), "orders": 3},
                   "Адоратского": {"external": D(1500), "renters": D(0), "orders": 1}}
        got = cashflow.income(money, repairs, ["Павлюхина", "Адоратского"])
        rows = {r["location"]: r for r in got["rows"]}
        self.assertEqual([r["location"] for r in got["rows"]],
                         ["Павлюхина", "Адоратского", None])
        self.assertEqual((rows["Павлюхина"]["total"], rows["Павлюхина"]["fines"]),
                         (D("37000.00"), D("1000.00")))
        self.assertEqual(got["total"]["total"], D("39500.00"))
        self.assertEqual(got["total"]["rent"], D("31000.00"),
                         "аренда - те же платежи журнала, что в трёх числах")
        self.assertEqual(got["total"]["share"]["repair_external"], 16.5)

    def test_no_orphan_row_when_empty(self):
        got = cashflow.income({}, {}, ["Павлюхина"])
        self.assertEqual([r["location"] for r in got["rows"]], ["Павлюхина"])
        self.assertIsNone(got["total"]["share"]["rent"])


class TestCalendar(unittest.TestCase):
    def test_add_months(self):
        self.assertEqual(cashflow.add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(cashflow.add_months(date(2026, 11, 15), 3), date(2027, 2, 15))

    def test_expected_rent(self):
        rentals = [
            {"id": 1, "full_name": "Иван", "price": D(3000), "period_days": 7,
             "billed_until": TODAY + timedelta(days=2), "billing": "auto"},
            {"id": 2, "full_name": "Сдаёт", "price": D(3000), "period_days": 7,
             "billed_until": TODAY, "intent": "return"},
            {"id": 3, "full_name": "Ручная", "price": D(3000), "period_days": 7,
             "billed_until": TODAY, "billing": "manual"},
            {"id": 4, "full_name": "Розыск", "price": D(3000), "period_days": 7,
             "billed_until": TODAY, "search_at": datetime(2026, 10, 1, tzinfo=UTC)},
            # отстал биллинг: «оплачено до» вчера - ждём сегодня
            {"id": 5, "full_name": "Вчера", "price": D(500), "period_days": 30,
             "billed_until": TODAY - timedelta(days=1)},
        ]
        got = cashflow.expected_rent(rentals, start=TODAY, end=TODAY + timedelta(days=13))
        self.assertEqual([(i["title"], i["day"]) for i in got],
                         [("Иван", TODAY + timedelta(days=2)),
                          ("Иван", TODAY + timedelta(days=9)), ("Вчера", TODAY)])

    def test_planned_repeat_and_overdue(self):
        items = [{"id": 1, "due_on": date(2026, 9, 10), "title": "Аренда помещения",
                  "amount": D(40000), "direction": "out", "repeat_months": 1},
                 {"id": 2, "due_on": date(2026, 10, 1), "title": "Налог",
                  "amount": D(5000), "direction": "out", "repeat_months": 0},
                 {"id": 3, "due_on": date(2026, 10, 7), "title": "Готово",
                  "amount": D(1), "direction": "out", "repeat_months": 0,
                  "done_at": datetime(2026, 10, 2, tzinfo=UTC)}]
        got = cashflow.planned(items, start=TODAY, end=date(2026, 11, 30))
        self.assertEqual([(i["title"], i["day"], i["overdue"]) for i in got],
                         [("Аренда помещения", date(2026, 10, 10), False),
                          ("Аренда помещения", date(2026, 11, 10), False),
                          ("Налог", TODAY, True)])

    def test_calendar_gap(self):
        rentals = [{"id": 1, "full_name": "Иван", "price": D(3000), "period_days": 7,
                    "billed_until": TODAY + timedelta(days=3), "location": "Павлюхина"}]
        plans = [{"id": 1, "due_on": TODAY + timedelta(days=1), "title": "Зарплата",
                  "amount": D(10000), "direction": "out", "repeat_months": 0}]
        cal = cashflow.calendar(rentals, plans, start=TODAY, days=7, opening=D(8000),
                                debts=D(4500))
        self.assertEqual(cal["gap"], TODAY + timedelta(days=1))
        self.assertEqual(cal["days"][3]["balance"], D("1000.00"))
        self.assertEqual((cal["total"]["rent"], cal["total"]["plan_out"], cal["net"]),
                         (D("3000.00"), D("10000.00"), D("-7000.00")))
        self.assertEqual(cal["debts"], D("4500.00"), "долги отдельно, в итог не входят")
        other = cashflow.calendar(rentals, plans, start=TODAY, days=7,
                                  location="Адоратского")
        self.assertEqual(other["total"]["rent"], D("0.00"), "аренда чужой точки не видна")
        self.assertEqual(other["total"]["plan_out"], D("10000.00"), "общий расход - всем")
        self.assertIsNone(other["gap"], "без остатка разрыва не определить")
        week = cashflow.week_ahead(cal)
        self.assertEqual((week["rent"], week["count"]), (D("3000.00"), 1))

    def test_parse_plan(self):
        got, problem = cashflow.parse_plan({"title": "Аренда", "due_on": "2026-10-10",
                                            "amount": "40 000", "direction": "out",
                                            "repeat_months": "1"}, today=TODAY)
        self.assertIsNone(problem)
        self.assertEqual((got["amount"], got["repeat_months"]), (D("40000.00"), 1))
        self.assertIn("больше нуля", cashflow.parse_plan(
            {"title": "x", "due_on": "2026-10-10", "amount": "-5"}, today=TODAY)[1])
        self.assertIn("дата", cashflow.parse_plan(
            {"title": "x", "due_on": "завтра", "amount": "5"}, today=TODAY)[1])


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestCashPages(WebCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def test_calendar_page_and_plan(self):
        run(self.crm.create_rental(client_id=self.client_id, bike_id=self.bike_id,
                                   tariff_id=self.tariff_id, tariff_name="Неделя",
                                   period_days=7, price=D(3000), billing="auto",
                                   started_on=date.today() - timedelta(days=5),
                                   contract_no=None, created_by="t"))
        self.login()
        r = self.client.post("/finance/calendar", data={
            "title": "Аренда помещения", "due_on": date.today().isoformat(),
            "amount": "40000", "direction": "out", "repeat_months": "1"})
        self.assertEqual(r.status_code, 303)
        page = self.get_ok("/finance/calendar?start=10000")
        self.assertIn("Аренда помещения", page)
        self.assertIn("Кассовый разрыв", page)
        self.assertIn("Иванов Иван", page, "аренда ждёт следующий период")
        self.assertEqual(self.client.get("/finance/calendar?start=NaN&days=999")
                         .status_code, 200, "мусор в адресе - не 500")
        plan = next(iter(self.crm.cash_plan_.values()))
        self.client.post(f"/finance/calendar/{plan['id']}/delete")
        self.assertEqual(self.crm.cash_plan_, {})

    def test_dashboard_critical_and_income(self):
        run(self.crm.create_task(title="Закупить запчасти", by="t",
                                 due_on=date.today() - timedelta(days=2)))
        self.login()
        page = self.get_ok("/")
        self.assertIn("Критично", page)
        self.assertIn("Просроченные поручения", page)
        self.assertIn("Доход по статьям", page)
        self.assertIn("сторонний ремонт", page)

    def test_rights(self):
        profile = run(self.crm.access_profile_by_code("tech"))
        run(self.crm.create_staff("mech", logic.hash_password("password-1"), "Механик",
                                  "manager", profile["id"]))
        self.login("mech", "password-1")
        self.assertEqual(self.client.get("/finance/calendar").status_code, 403)
        self.assertEqual(self.client.post("/finance/calendar", data={
            "title": "x", "due_on": "2026-10-10", "amount": "1"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
