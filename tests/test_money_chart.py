"""График денег по дням месяца.

Помесячных чисел мало: по ним не видно, в какой день всё пошло не так.
Столбики «пришло за день», пунктир плана и накопленный долг отвечают
на этот вопрос на одном экране.

Правило, которое здесь стерегут: долг показан накопительным и ниже нуля
не опускается. Разовый провал ничего не значит, а линия, которая ползёт
вверх весь месяц, — это и есть «копим долги»; переплата же — не
отрицательный долг, а деньги вперёд, и рисовать её провалом нечестно.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
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
TODAY = date(2026, 9, 4)


def days(*values):
    """(пришло, начислено) по дням сентября с первого."""
    return [{"day": date(2026, 9, i + 1), "paid": D(p), "charged": D(c)}
            for i, (p, c) in enumerate(values)]


class TestMoneyChart(unittest.TestCase):
    def test_sums_only_the_days_that_passed(self):
        chart = logic.money_chart(
            days((1000, 0), (2000, 0), (0, 0), (500, 0), (9999, 0), (9999, 0)),
            today=TODAY)
        self.assertEqual(chart["paid"], D(3500), "будущие дни не в счёт")
        self.assertEqual(chart["days_passed"], 4)

    def test_future_days_stay_in_the_row(self):
        chart = logic.money_chart(days((1000, 0), (0, 0), (0, 0), (0, 0),
                                       (0, 0), (0, 0)), today=TODAY)
        self.assertEqual(len(chart["days"]), 6,
                         "месяц ещё идёт - обрезать его значит делать вид, "
                         "что он кончился")
        self.assertTrue(chart["days"][-1]["future"])
        self.assertFalse(chart["days"][3]["future"])

    def test_debt_is_cumulative(self):
        chart = logic.money_chart(
            days((0, 3000), (0, 3000), (5000, 0), (0, 0)), today=TODAY)
        got = [d["debt"] for d in chart["days"]]
        self.assertEqual(got, [D(3000), D(6000), D(1000), D(1000)])
        self.assertEqual(chart["debt"], D(1000))

    def test_overpayment_is_not_a_negative_debt(self):
        chart = logic.money_chart(days((9000, 1000), (0, 0)), today=TODAY)
        self.assertEqual(chart["debt"], D(0),
                         "деньги вперёд - не отрицательный долг")
        self.assertEqual(chart["days"][0]["debt"], D(0))

    def test_plan_marks_the_good_days(self):
        chart = logic.money_chart(days((1000, 0), (3000, 0), (0, 0), (2000, 0)),
                                  plan_per_day=D(2000), today=TODAY)
        self.assertEqual([d["over"] for d in chart["days"]],
                         [False, True, False, True])
        self.assertEqual(chart["over_days"], 2)

    def test_average_and_best_day(self):
        chart = logic.money_chart(days((1000, 0), (3000, 0), (0, 0), (0, 0)),
                                  today=TODAY)
        self.assertEqual(chart["avg"], D(1000), "по всем прошедшим дням")
        self.assertEqual(chart["avg_worked"], D(2000),
                         "по дням с оплатами - иначе месяц выглядит вдвое хуже")
        self.assertEqual(chart["best"]["day"], date(2026, 9, 2))

    def test_heights_are_percents_of_the_peak(self):
        chart = logic.money_chart(days((1000, 0), (4000, 0)), today=TODAY)
        self.assertEqual(chart["top"], D(4000))
        self.assertEqual(chart["days"][1]["height"], 100)
        self.assertEqual(chart["days"][0]["height"], 25)

    def test_empty_month_does_not_divide_by_zero(self):
        chart = logic.money_chart([], today=TODAY)
        self.assertEqual(chart["paid"], D(0))
        self.assertEqual(chart["avg"], D(0))
        self.assertIsNone(chart["best"])

    def test_cumulative_view(self):
        rows = logic.cumulative(logic.money_chart(
            days((1000, 0), (2000, 0), (0, 0), (500, 0), (9999, 0)),
            today=TODAY)["days"])
        self.assertEqual([r["total"] for r in rows],
                         [D(1000), D(3000), D(3000), D(3500), D(3500)])
        self.assertEqual(rows[-1]["height"], 100)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestChartOnDashboard(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def test_chart_is_on_the_page(self):
        text = self.get_ok("/")
        self.assertIn("Деньги по дням", text)
        self.assertIn("накопительно", text)

    def test_payment_shows_up(self):
        tw.run(self.crm.add_ledger(client_id=self.client_id, rental_id=None,
                                   kind="payment", amount=D(4200)))
        text = self.get_ok("/")
        self.assertIn("Итого пришло", text)
        self.assertIn("4 200", text)

    def test_cumulative_switch(self):
        tw.run(self.crm.add_ledger(client_id=self.client_id, rental_id=None,
                                   kind="payment", amount=D(1000)))
        self.assertIn("Деньги по дням", self.get_ok("/?chart=cumulative"))

    def test_debt_line_appears_only_with_debt(self):
        self.assertNotIn("накопленный долг", self.get_ok("/"))
        tw.run(self.crm.add_ledger(client_id=self.client_id, rental_id=None,
                                   kind="charge", amount=D(-3000)))
        self.assertIn("накопленный долг", self.get_ok("/"))


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
