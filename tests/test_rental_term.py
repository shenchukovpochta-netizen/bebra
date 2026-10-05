"""Правка срока аренды (logic.rental_term_problem, db.change_rental_term):
начало и следующее начисление, журнал правок, ничего не начисляется
второй раз."""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import logic, service

try:
    from tests.test_web import HAVE_WEB, WebCase, run
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False
    WebCase = unittest.TestCase                        # type: ignore[misc,assignment]

TODAY = date(2026, 10, 5)
ACTIVE = {"status": "active"}


class TestTermRules(unittest.TestCase):
    def check(self, start, billed, charged=None, rental=ACTIVE):
        return logic.rental_term_problem(rental, started_on=start, billed_until=billed,
                                         charged_until=charged, today=TODAY)

    def test_forward_is_free_days(self):
        self.assertIsNone(self.check(TODAY, TODAY + timedelta(days=10),
                                     charged=TODAY + timedelta(days=7)))

    def test_charged_period_is_not_cut(self):
        why = self.check(TODAY, TODAY + timedelta(days=5), charged=TODAY + timedelta(days=7))
        self.assertIn("по 11.10.2026", why)
        self.assertIn("не раньше 12.10.2026", why)

    def test_dates_sanity(self):
        self.assertIn("раньше начала", self.check(TODAY, TODAY - timedelta(days=1)))
        self.assertIn("год", self.check(TODAY + timedelta(days=60), TODAY + timedelta(days=67)))
        self.assertIn("дальше 92 дней", self.check(TODAY, TODAY + timedelta(days=200)))
        self.assertIn("идущей", self.check(TODAY, TODAY, rental={"status": "closed"}))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestTermPage(WebCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.login()
        self.started = date.today() - timedelta(days=2)
        self.rid = run(service.open_rental(
            self.crm, client=run(self.crm.client(self.client_id)),
            bike=run(self.crm.bike(self.bike_id)), tariff=run(self.crm.tariff(self.tariff_id)),
            started_on=self.started, contract_no=None, by="t"))

    def post(self, **data):
        return self.client.post(f"/rentals/{self.rid}/term", data=data)

    def test_shift_forward_and_journal(self):
        rental = run(self.crm.rental(self.rid))
        self.assertEqual(rental["billed_until"], self.started + timedelta(days=7))
        page = self.get_ok(f"/rentals/{self.rid}")
        self.assertIn("Изменить срок", page)
        new = self.started + timedelta(days=10)
        self.post(started_on=self.started.isoformat(), billed_until=new.isoformat(),
                  note="3 дня за ремонт велосипеда")
        rental = run(self.crm.rental(self.rid))
        self.assertEqual(rental["billed_until"], new)
        self.assertEqual(run(self.crm.client_balance(self.client_id)), D(-3000),
                         "деньги не тронуты - сдвиг это дни без списания")
        page = self.get_ok(f"/rentals/{self.rid}")
        self.assertIn("3 дня за ремонт велосипеда", page)
        self.assertIn("следующее начисление", page)

    def test_cannot_cut_charged_or_skip_reason(self):
        self.post(started_on=self.started.isoformat(),
                  billed_until=(self.started + timedelta(days=3)).isoformat(),
                  note="ошибка")
        self.assertEqual(run(self.crm.rental(self.rid))["billed_until"],
                         self.started + timedelta(days=7))
        self.post(started_on=self.started.isoformat(),
                  billed_until=(self.started + timedelta(days=9)).isoformat(), note="  ")
        self.assertEqual(run(self.crm.rental(self.rid))["billed_until"],
                         self.started + timedelta(days=7), "без причины не сохраняется")

    def test_start_date_fix(self):
        earlier = self.started - timedelta(days=1)
        self.post(started_on=earlier.isoformat(),
                  billed_until=(self.started + timedelta(days=7)).isoformat(),
                  note="выдали вчера, оформили сегодня")
        self.assertEqual(run(self.crm.rental(self.rid))["started_on"], earlier)
        [change] = run(self.crm.rental_changes(self.rid))
        self.assertEqual((change["field"], change["old_value"], change["new_value"]),
                         ("started_on", self.started, earlier))

    def test_needs_money_edit(self):
        profile = run(self.crm.access_profile_by_code("manager"))
        run(self.crm.create_staff("oper", logic.hash_password("password-1"), "Оператор",
                                  "manager", profile["id"]))
        self.client.post("/logout")
        self.login("oper", "password-1")
        staff = run(self.crm.staff_by_login("oper"))
        if logic.can_act(staff, "money_edit"):
            self.skipTest("у профиля оператора есть право на журнал")
        r = self.post(started_on=self.started.isoformat(),
                      billed_until=(self.started + timedelta(days=9)).isoformat(),
                      note="x")
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
