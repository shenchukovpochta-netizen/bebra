"""Логика CRM: деньги, периоды, «оплачено до», напоминания, проверки форм."""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import logic  # noqa: E402

D = Decimal
TODAY = date(2026, 9, 13)


class TestMoney(unittest.TestCase):
    def test_format(self):
        self.assertEqual(logic.money(3000), "3 000 ₽")
        self.assertEqual(logic.money(D("11000.00")), "11 000 ₽")
        self.assertEqual(logic.money(D("428.5")), "428,50 ₽")
        self.assertEqual(logic.money(D("-3000")), "−3 000 ₽")
        self.assertEqual(logic.money(0), "0 ₽")
        self.assertEqual(logic.money(None), "—")

    def test_signed(self):
        self.assertEqual(logic.money_signed(3000), "+3 000 ₽")
        self.assertEqual(logic.money_signed(-3000), "−3 000 ₽")
        self.assertEqual(logic.money_signed(0), "0 ₽")

    def test_parse(self):
        self.assertEqual(logic.parse_money("3000"), D("3000.00"))
        self.assertEqual(logic.parse_money("3 000 ₽"), D("3000.00"))
        self.assertEqual(logic.parse_money("3000,50"), D("3000.50"))
        self.assertEqual(logic.parse_money("3000.5"), D("3000.50"))
        self.assertEqual(logic.parse_money("-500"), D("-500.00"))
        for bad in ("", None, "abc", "3.000.00", "1e5", "12345678901"):
            self.assertIsNone(logic.parse_money(bad), bad)

    def test_sign_by_kind(self):
        self.assertEqual(logic.signed_amount("payment", D("-100")), D("100"))
        self.assertEqual(logic.signed_amount("charge", D("100")), D("-100"))
        self.assertEqual(logic.signed_amount("fine", D("100")), D("-100"))
        self.assertEqual(logic.signed_amount("refund", D("100")), D("-100"))
        self.assertEqual(logic.signed_amount("adjust", D("-100")), D("-100"))
        self.assertEqual(logic.signed_amount("adjust", D("100")), D("100"))

    def test_balance(self):
        rows = [{"amount": D("3000")}, {"amount": D("-3000")}, {"amount": D("500.5")}]
        self.assertEqual(logic.balance(rows), D("500.50"))
        self.assertEqual(logic.balance([]), D("0"))


class TestCoverage(unittest.TestCase):
    """Неделя за 3 000, начислено по 20.09 (billed_until = 20.09)."""

    BILLED = date(2026, 9, 20)

    def until(self, bal):
        return logic.covered_until(self.BILLED, D(bal), D("3000"), 7)

    def test_zero_balance_is_paid_through_billed_until(self):
        self.assertEqual(self.until(0), self.BILLED)

    def test_prepayment_adds_whole_periods(self):
        self.assertEqual(self.until(3000), self.BILLED + timedelta(days=7))
        self.assertEqual(self.until(6500), self.BILLED + timedelta(days=14))
        # неполный период вперёд не считается оплаченным
        self.assertEqual(self.until(2999), self.BILLED)

    def test_debt_takes_periods_back(self):
        self.assertEqual(self.until(-1), self.BILLED - timedelta(days=7))
        self.assertEqual(self.until(-3000), self.BILLED - timedelta(days=7))
        self.assertEqual(self.until(-3001), self.BILLED - timedelta(days=14))

    def test_free_rental(self):
        self.assertEqual(logic.covered_until(self.BILLED, D(0), D(0), 7), self.BILLED)
        self.assertEqual(logic.covered_until(self.BILLED, D(-100), D(0), 7),
                         self.BILLED - timedelta(days=7))

    def test_days_left(self):
        self.assertEqual(logic.days_left(TODAY + timedelta(days=2), today=TODAY), 2)
        self.assertEqual(logic.days_left(TODAY - timedelta(days=3), today=TODAY), -3)
        self.assertIsNone(logic.days_left(None, today=TODAY))


class TestDuePeriods(unittest.TestCase):
    def test_new_rental_gets_first_period_today(self):
        periods = logic.due_periods(TODAY, 7, today=TODAY)
        self.assertEqual(periods, [(TODAY, TODAY + timedelta(days=7))])

    def test_nothing_due_before_billed_until(self):
        self.assertEqual(logic.due_periods(TODAY + timedelta(days=1), 7, today=TODAY), [])

    def test_catches_up_missed_periods(self):
        start = TODAY - timedelta(days=15)
        periods = logic.due_periods(start, 7, today=TODAY)
        self.assertEqual(len(periods), 3)
        self.assertEqual(periods[0][0], start)
        self.assertEqual(periods[-1][1], start + timedelta(days=21))
        # периоды стыкуются без дыр
        for (_, end), (nxt, _) in zip(periods, periods[1:], strict=False):
            self.assertEqual(end, nxt)

    def test_daily_tariff(self):
        periods = logic.due_periods(TODAY - timedelta(days=2), 1, today=TODAY)
        self.assertEqual(len(periods), 3)

    def test_runaway_is_capped(self):
        periods = logic.due_periods(date(1990, 1, 1), 1, today=TODAY)
        self.assertEqual(len(periods), logic.MAX_PERIOD_DAYS)

    def test_period_label(self):
        self.assertEqual(logic.period_label(date(2026, 9, 14), date(2026, 9, 21)),
                         "14.09 – 20.09.2026")
        self.assertEqual(logic.period_label(date(2026, 9, 14), date(2026, 9, 15)),
                         "14.09.2026")
        self.assertEqual(logic.period_label(None, None), "")


class TestReminders(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(logic.reminder_kind(2, before_days=2), "soon")
        self.assertIsNone(logic.reminder_kind(3, before_days=2))
        self.assertIsNone(logic.reminder_kind(1, before_days=2))
        self.assertEqual(logic.reminder_kind(0, before_days=2), "due")
        self.assertEqual(logic.reminder_kind(-1, before_days=2), "overdue")
        self.assertIsNone(logic.reminder_kind(-2, before_days=2))
        self.assertEqual(logic.reminder_kind(-3, before_days=2), "overdue")
        self.assertEqual(logic.reminder_kind(-7, before_days=2), "overdue")
        self.assertIsNone(logic.reminder_kind(-10, before_days=2))
        self.assertEqual(logic.reminder_kind(-14, before_days=2), "overdue")
        self.assertIsNone(logic.reminder_kind(None, before_days=2))

    def test_zero_before_days_never_soon(self):
        self.assertIsNone(logic.reminder_kind(0, before_days=0) == "soon" or None)

    def rental(self, **over):
        base = {"status": "active", "billed_until": TODAY + timedelta(days=2),
                "balance": D(0), "price": D(3000), "period_days": 7,
                "notified_on": None}
        base.update(over)
        return base

    def test_reminder_due_uses_coverage(self):
        self.assertEqual(logic.reminder_due(self.rental(), before_days=2, today=TODAY),
                         "soon")
        # предоплата на неделю вперёд - напоминать нечего
        self.assertIsNone(logic.reminder_due(self.rental(balance=D(3000)),
                                             before_days=2, today=TODAY))
        # долг: не оплачено с прошлой недели -> просрочка 5 дней -> не день напоминания
        self.assertIsNone(logic.reminder_due(self.rental(balance=D(-100)),
                                             before_days=2, today=TODAY))

    def test_one_reminder_per_day(self):
        self.assertIsNone(logic.reminder_due(self.rental(notified_on=TODAY),
                                             before_days=2, today=TODAY))
        self.assertIsNone(logic.reminder_due(self.rental(status="closed"),
                                             before_days=2, today=TODAY))

    def test_digest(self):
        rows = [
            self.rental(full_name="Иванов", bike_code="B-1"),
            self.rental(full_name="Петров", billed_until=TODAY - timedelta(days=4),
                        balance=D(-3000)),
            self.rental(full_name="Сидоров", balance=D(9000)),
            self.rental(full_name="Закрытый", status="closed"),
        ]
        text = logic.digest(rows, today=TODAY, before_days=2)
        self.assertIn("Долги", text)
        self.assertIn("Петров — долг 3 000 ₽", text)
        self.assertIn("(11 дн.)", text)
        self.assertIn("Иванов · B-1 — платёж", text)
        self.assertNotIn("Сидоров", text)
        self.assertNotIn("Закрытый", text)
        self.assertEqual(logic.digest([], today=TODAY, before_days=2), "")

    def test_digest_escapes_html(self):
        """Сводка уходит с parse_mode=HTML: «<» в имени ломало бы всё сообщение."""
        rows = [self.rental(full_name="Иванов <брат> & Co", bike_code="<1>",
                            billed_until=TODAY - timedelta(days=4), balance=D(-3000))]
        text = logic.digest(rows, today=TODAY, before_days=2)
        self.assertIn("Иванов &lt;брат&gt; &amp; Co · &lt;1&gt;", text)
        self.assertNotIn("<брат>", text)

    def test_first_amount(self):
        """Первое число, а не склейка всех цифр строки."""
        self.assertEqual(logic.first_amount("3000 qr 14.09"), D("3000.00"))
        self.assertEqual(logic.first_amount("3 000 р (за 7 дней)"), D("3000.00"))
        self.assertEqual(logic.first_amount("1500 сбп + 1500 нал"), D("1500.00"))
        self.assertEqual(logic.first_amount("3000 2 недели"), D("3000.00"))
        self.assertEqual(logic.first_amount("1500 14.09"), D("1500.00"))
        self.assertEqual(logic.first_amount("12 500"), D("12500.00"))
        self.assertEqual(logic.first_amount("1 500 000 р"), D("1500000.00"))
        self.assertEqual(logic.first_amount(2500), D("2500.00"))
        self.assertIsNone(logic.first_amount("бесплатно"))
        self.assertIsNone(logic.first_amount(None))
        self.assertEqual(logic.rental_from_issue({"rent_price": "3000 qr 14.09"}, None, None,
                                                 today=TODAY)["price"], D("3000.00"))


class TestChecks(unittest.TestCase):
    def test_amount(self):
        self.assertEqual(logic.check_amount("3 000").value, D("3000.00"))
        self.assertFalse(logic.check_amount("0").ok)
        self.assertFalse(logic.check_amount("-5").ok)
        self.assertTrue(logic.check_amount("-5", allow_negative=True).ok)
        self.assertFalse(logic.check_amount("99999999").ok)
        self.assertFalse(logic.check_amount("много").ok)

    def test_name_and_code(self):
        self.assertEqual(logic.check_name("  Иванов   Иван ").value, "Иванов Иван")
        self.assertFalse(logic.check_name("").ok)
        self.assertFalse(logic.check_name("<b>x</b>").ok)
        self.assertFalse(logic.check_name("x" * 200).ok)
        self.assertEqual(logic.check_code("b-012").value, "B-012")
        self.assertFalse(logic.check_code("").ok)
        self.assertFalse(logic.check_code("код<>").ok)

    def test_period_and_date(self):
        self.assertEqual(logic.check_period("7").value, 7)
        self.assertFalse(logic.check_period("0").ok)
        self.assertFalse(logic.check_period("400").ok)
        self.assertFalse(logic.check_period("неделя").ok)
        self.assertEqual(logic.check_date("2026-09-13").value, TODAY)
        self.assertEqual(logic.check_date("13.09.2026").value, TODAY)
        self.assertEqual(logic.check_date("", default=TODAY).value, TODAY)
        self.assertFalse(logic.check_date("").ok)
        self.assertFalse(logic.check_date("вчера").ok)

    def test_choice_login_password(self):
        self.assertTrue(logic.check_choice("sbp", logic.METHODS).ok)
        self.assertFalse(logic.check_choice("bitcoin", logic.METHODS).ok)
        self.assertEqual(logic.check_login(" Admin ").value, "admin")
        self.assertFalse(logic.check_login("a").ok)
        self.assertFalse(logic.check_login("ад мин").ok)
        self.assertTrue(logic.check_password("secret-123").ok)
        self.assertFalse(logic.check_password("short").ok)

    def test_note(self):
        self.assertIsNone(logic.check_note("  ").value)
        self.assertFalse(logic.check_note("x" * 3000).ok)


class TestPasswords(unittest.TestCase):
    def test_roundtrip(self):
        stored = logic.hash_password("correct horse")
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertTrue(logic.verify_password("correct horse", stored))
        self.assertFalse(logic.verify_password("wrong", stored))
        self.assertFalse(logic.verify_password("correct horse", None))
        self.assertFalse(logic.verify_password("correct horse", "md5$x$y"))
        self.assertFalse(logic.verify_password("correct horse", "мусор"))

    def test_salted(self):
        self.assertNotEqual(logic.hash_password("a"), logic.hash_password("a"))

    def test_generated_password_is_long_enough(self):
        self.assertTrue(logic.check_password(logic.generate_password()).ok)


class TestSummary(unittest.TestCase):
    def test_no_rental(self):
        s = logic.rental_summary(None, D("-200"), today=TODAY)
        self.assertFalse(s["active"])
        self.assertEqual(s["debt"], D("200"))
        self.assertTrue(s["overdue"])
        self.assertEqual(logic.topup_hint(s), D("200"))

    def test_active(self):
        rental = {"status": "active", "billed_until": TODAY + timedelta(days=3),
                  "price": D(3000), "period_days": 7, "tariff_name": "Неделя",
                  "bike_model": "Kugoo V3", "bike_code": "B-7"}
        s = logic.rental_summary(rental, D(0), today=TODAY)
        self.assertTrue(s["active"])
        self.assertEqual(s["days_left"], 3)
        self.assertEqual(s["bike"], "Kugoo V3 B-7")
        self.assertEqual(logic.topup_hint(s), D(3000))
        debt = logic.rental_summary(rental, D(-500), today=TODAY)
        self.assertTrue(debt["overdue"])
        self.assertEqual(logic.topup_hint(debt), D(500))


class TestIssueSync(unittest.TestCase):
    def test_from_issue_form(self):
        issue = {"rent_price": "3000 qr", "rent_term": "03.08 - 10.08",
                 "bike_model": "Kugoo V3 Pro", "vin_frame": "KG123456789",
                 "vin_motor": "M-1"}
        r = logic.rental_from_issue(issue, date(2026, 8, 3), date(2026, 8, 10),
                                    today=TODAY)
        self.assertEqual(r["price"], D("3000.00"))
        self.assertEqual(r["period_days"], 7)
        self.assertEqual(r["billing"], "manual")
        self.assertEqual(r["started_on"], date(2026, 8, 3))
        self.assertEqual(r["frame_no"], "KG123456789")
        self.assertIn("03.08 - 10.08", r["tariff_name"])

    def test_defaults_without_dates(self):
        r = logic.rental_from_issue({"rent_price": "—"}, None, None, today=TODAY)
        self.assertEqual(r["price"], D(0))
        self.assertEqual(r["period_days"], 7)
        self.assertEqual(r["started_on"], TODAY)
        self.assertIsNone(r["frame_no"])

    def test_bike_code(self):
        self.assertEqual(logic.bike_code_from_frame("KG-123456789", "X"), "АВТО-456789")
        self.assertEqual(logic.bike_code_from_frame(None, "Kugoo V3"), "АВТО-KUGOOV3")
        self.assertEqual(logic.bike_code_from_frame(None, None), "АВТО-BIKE")



class TestFleetMetrics(unittest.TestCase):
    """Три числа: простой, чек, амортизация - формулы из CLAUDE.md."""

    def test_amortization_splits_frame_and_battery(self):
        bike = {"purchase_price": D("47000"), "service_months": 24, "residual_price": D("5000"),
                "battery_price": D("9000"), "battery_count": 2, "battery_service_months": 15,
                "status": "available"}
        # рама (47000-5000)/24 = 1750; АКБ 9000*2/15 = 1200
        self.assertEqual(logic.amortization_month(bike), D("2950.00"))
        self.assertIsNone(logic.amortization_month({"purchase_price": None}))
        # без цены АКБ - только рама; отрицательной амортизации не бывает
        self.assertEqual(logic.amortization_month({"purchase_price": D("1000"),
                                                   "residual_price": D("5000"),
                                                   "service_months": 10}), D("0.00"))
        lost = dict(bike, status="lost")
        self.assertEqual(logic.amortization_total([bike, lost, dict(bike, status="repair")]),
                         D("5900.00"))

    def test_days_by_status_from_log(self):
        from datetime import UTC, datetime
        t = lambda d, h=0: datetime(2026, 9, d, h, tzinfo=UTC)  # noqa: E731
        log = [
            {"bike_id": 1, "to_status": "available", "changed_at": t(1)},
            {"bike_id": 1, "to_status": "rented", "changed_at": t(3)},
            {"bike_id": 1, "to_status": "available", "changed_at": t(10)},
            {"bike_id": 2, "to_status": "repair", "changed_at": t(5, 12)},
            {"bike_id": 3, "to_status": "lost", "changed_at": t(1)},
        ]
        days = logic.days_by_status(log, t(1), t(11))
        self.assertEqual(days["rented"], D(7))
        self.assertEqual(days["available"], D(3))            # 2 до аренды + 1 после
        self.assertEqual(days["repair"], D("5.5"))
        self.assertEqual(days["lost"], D(10))
        # окно уже журнала: обрезается с обеих сторон
        days = logic.days_by_status(log, t(4), t(6))
        self.assertEqual(days["rented"], D(2))
        self.assertEqual(days["repair"], D("0.5"))
        self.assertNotIn("available", days)

    def test_fleet_metrics_targets(self):
        m = logic.fleet_metrics({"rented": D(60), "available": D(5), "repair": D(3),
                                 "lost": D(30)}, D("30000"))
        self.assertEqual(m["operational_days"], D(68))       # потерянные не в знаменателе
        self.assertEqual(m["idle_percent"], 11.8)
        self.assertFalse(m["idle_ok"])
        self.assertEqual(m["avg_check"], D("500.00"))
        self.assertTrue(m["check_ok"])
        empty = logic.fleet_metrics({}, D(0))
        self.assertIsNone(empty["idle_percent"])
        self.assertIsNone(empty["avg_check"])
        self.assertFalse(empty["idle_ok"])
        good = logic.fleet_metrics({"rented": D(95), "available": D(5)}, D("50000"))
        self.assertTrue(good["idle_ok"])
        self.assertEqual(good["idle_percent"], 5.0)


if __name__ == "__main__":
    unittest.main()
