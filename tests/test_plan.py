"""План месяца и прогноз освобождения велосипедов.

План считается от трёх чисел, а не выдумывается: столько парк даёт, если
держать простой в норме и чек на цели. Прогноз - по «оплачено до» каждой
идущей аренды: сказавший «продлю» из него выпадает.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
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
TODAY = date(2026, 9, 17)


def rental(days_left: int, **over) -> dict:
    row = {"status": "active", "bike_id": 1, "balance": D(0), "price": D(3000),
           "period_days": 7, "intent": None,
           "billed_until": TODAY + timedelta(days=days_left)}
    row.update(over)
    return row


class TestPlanLogic(unittest.TestCase):
    def test_default_plan_comes_from_the_three_numbers(self):
        plan = logic.month_plan(None, fleet=165)
        # простой меньше 10 % - значит в аренде держим 90 % парка
        self.assertEqual(plan["rented"], 149 if 165 * 0.9 > 148.5 else 148)
        self.assertEqual(plan["check"], logic.CHECK_TARGET)

    def test_saved_plan_wins_and_garbage_does_not(self):
        plan = logic.month_plan({"plan_rented": "120", "plan_check": "600"}, fleet=165)
        self.assertEqual((plan["rented"], plan["check"]), (120, D("600.00")))
        broken = logic.month_plan({"plan_rented": "ой", "plan_check": "0"}, fleet=100)
        self.assertEqual(broken["rented"], 90)
        self.assertEqual(broken["check"], logic.CHECK_TARGET)

    def test_progress_compares_with_an_even_pace(self):
        plan = {"rented": 100, "check": D(500), "fleet": 165}
        progress = logic.plan_progress(plan, {"revenue": D(750000)},
                                       days_in_month=30, days_passed=15)
        self.assertEqual(progress["target"], D(1500000))
        self.assertEqual(progress["pace"], D(750000))
        self.assertTrue(progress["ahead"])
        self.assertEqual(progress["percent"], 50.0)
        self.assertEqual(progress["left"], D(750000))
        self.assertEqual(progress["days_left"], 15)
        self.assertEqual(progress["need_rented"], 100)

    def test_progress_at_the_end_of_the_month_asks_for_nothing(self):
        plan = {"rented": 10, "check": D(500), "fleet": 10}
        progress = logic.plan_progress(plan, {"revenue": D(0)},
                                       days_in_month=30, days_passed=30)
        self.assertEqual(progress["days_left"], 0)
        self.assertEqual(progress["need_rented"], 0, "делить на ноль дней нельзя")

    def test_overfulfilled_plan_needs_nobody(self):
        plan = {"rented": 10, "check": D(500), "fleet": 10}
        progress = logic.plan_progress(plan, {"revenue": D(999999)},
                                       days_in_month=30, days_passed=10)
        self.assertEqual(progress["left"], D(0))
        self.assertEqual(progress["need_rented"], 0)


class TestForecastLogic(unittest.TestCase):
    def test_renewal_intent_leaves_the_forecast(self):
        rows = [rental(0), rental(1, intent="renew"), rental(1, intent="return"),
                rental(5)]
        soon = logic.freeing_soon(rows, today=TODAY)
        self.assertEqual({k: len(v) for k, v in soon.items()},
                         {"0": 1, "1": 1, "2": 0, "3": 0})

    def test_overdue_counts_as_today(self):
        soon = logic.freeing_soon([rental(-7, balance=D(-3000))], today=TODAY)
        self.assertEqual(len(soon["0"]), 1, "просроченного ждут уже сегодня")

    def test_those_who_said_return_come_first(self):
        rows = [rental(1), rental(1, intent="return")]
        soon = logic.freeing_soon(rows, today=TODAY)
        self.assertTrue(soon["1"][0]["returning"])

    def test_summary_accumulates(self):
        soon = logic.freeing_soon([rental(0), rental(1), rental(1)], today=TODAY)
        self.assertEqual(logic.forecast_summary(5, soon),
                         {"now": 5, "0": 6, "1": 8, "2": 8, "3": 8})

    def test_rentals_without_a_bike_are_skipped(self):
        soon = logic.freeing_soon([rental(0, bike_id=None)], today=TODAY)
        self.assertEqual(len(soon["0"]), 0)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestPlanInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def test_dashboard_shows_the_plan(self):
        page = self.get_ok("/")
        self.assertIn("План на месяц", page)
        self.assertIn("плана выполнено", page)

    def test_plan_is_saved(self):
        r = self.client.post("/plan", data={"plan_rented": "120",
                                            "plan_check": "600"})
        self.assertEqual(r.status_code, 303)
        settings = tw.run(self.crm.settings())
        self.assertEqual(settings["plan_rented"], "120")
        plan = logic.month_plan(settings, fleet=10)
        self.assertEqual((plan["rented"], plan["check"]), (120, D("600.00")))
        self.assertIn("120 велосипедов в аренде по 600", self.get_ok("/"))

    def test_bad_plan_is_refused(self):
        r = self.client.post("/plan", data={"plan_rented": "сто", "plan_check": "600"})
        self.assertIn("Велосипедов в аренде", self.get_ok(r.headers["location"]))
        self.assertEqual(tw.run(self.crm.settings()).get("plan_rented"), None)

    def test_plan_is_money_only(self):
        profile = tw.run(self.crm.access_profile_by_code("tech"))
        tw.run(self.crm.create_staff("petr", logic.hash_password("password-1"),
                                     "Пётр", "manager", profile["id"]))
        self.client.post("/logout")
        self.login("petr", "password-1")
        self.assertNotIn("План на месяц", self.get_ok("/"))
        self.assertEqual(self.client.post("/plan", data={"plan_rented": "1"}).status_code,
                         403)

    def test_issue_wizard_shows_what_frees_up(self):
        tw.run(tw.service.open_rental(
            self.crm, client=tw.run(self.crm.client(self.client_id)),
            bike=tw.run(self.crm.bike(self.bike_id)),
            tariff=tw.run(self.crm.tariff(self.tariff_id)),
            started_on=date.today() - timedelta(days=6), contract_no="АВ-1",
            by="staff:admin"))
        other = tw.run(self.crm.create_client(full_name="Петров Пётр",
                                              phone="+79995554433"))
        tw.run(self.crm.create_bike(code="B-2", model="Kugoo V3"))
        page = self.get_ok(f"/issue?client={other}")
        self.assertIn("Освобождается по срокам", page)


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
