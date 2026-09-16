"""Окупаемость по моделям: что модель принесла и что съела.

Отчёт отвечает на вопрос «какую модель брать дальше»: платежи и начисления
привязаны к модели через аренду, ремонт и амортизация вычтены. Проверяется
в том числе платёж без аренды - зачисление по заявке из бота: его привязка
к модели восстанавливается по аренде клиента на день платежа.
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


def bike(model: str = "Truck+", **over) -> dict:
    row = {"model": model, "status": "available", "purchase_price": D(47000),
           "residual_price": D(5000), "service_months": 24,
           "battery_price": D(9000), "battery_count": 2,
           "battery_service_months": 15}
    row.update(over)
    return row


class TestPaybackLogic(unittest.TestCase):
    def test_margin_counts_repairs_and_amortization(self):
        money = {"Truck+": {"paid": D(20000), "charged": D(24000),
                            "repair_cost": D(3000), "works": D(0),
                            "rented_days": D(40)}}
        rows = logic.payback_rows([bike(), bike()], money, days=30)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["bikes"], 2)
        # рама (47000-5000)/24 + АКБ 9000*2/15 = 1750 + 1200 = 2950 в месяц
        # на два велосипеда за 30 дней: 2950 * 2 * 30 / 30.44
        self.assertEqual(row["amortization"], D("5814.72"))
        self.assertEqual(row["margin"], D("11185.28"))
        self.assertEqual(row["margin_percent"], 55.9)
        self.assertEqual(row["check_per_day"], D("500.00"))

    def test_client_paid_repairs_go_to_the_plus(self):
        """Наш велосипед, починенный за счёт клиента: деньги модели в плюс."""
        money = {"Truck+": {"paid": D(0), "charged": D(0), "repair_cost": D(2000),
                            "works": D(2000), "rented_days": D(0)}}
        row = logic.payback_rows([bike(purchase_price=None)], money, days=30)[0]
        self.assertEqual(row["works"], D("2000.00"))
        self.assertEqual(row["margin"], D("0.00"))
        self.assertIsNone(row["check_per_day"])

    def test_retired_bikes_do_not_eat_amortization(self):
        """Проданный и списанный больше не наши - и не амортизируются."""
        rows = logic.payback_rows([bike(status="sold"), bike(status="written_off")],
                                  {}, days=30)
        self.assertEqual(rows[0]["bikes"], 2)
        self.assertEqual(rows[0]["priced"], 0)
        self.assertEqual(rows[0]["amortization"], D(0))

    def test_amortization_scales_to_the_period(self):
        half = logic.payback_rows([bike()], {}, days=15)[0]["amortization"]
        full = logic.payback_rows([bike()], {}, days=30)[0]["amortization"]
        self.assertAlmostEqual(float(full / half), 2.0, places=2)

    def test_model_without_money_still_shows_its_cost(self):
        """Модель, которая ничего не принесла, - главная строка отчёта."""
        row = logic.payback_rows([bike("Kugoo V3")], {}, days=30)[0]
        self.assertEqual(row["paid"], D(0))
        self.assertLess(row["margin"], D(0))
        self.assertIsNone(row["margin_percent"])

    def test_rows_are_sorted_by_margin(self):
        money = {"A": {"paid": D(10000), "rented_days": D(10)},
                 "B": {"paid": D(30000), "rented_days": D(10)}}
        rows = logic.payback_rows([bike("A", purchase_price=None),
                                   bike("B", purchase_price=None)], money, days=30)
        self.assertEqual([r["model"] for r in rows], ["B", "A"])

    def test_total_repeats_the_row_arithmetic(self):
        money = {"A": {"paid": D(10000), "repair_cost": D(1000), "rented_days": D(20)},
                 "B": {"paid": D(30000), "repair_cost": D(5000), "rented_days": D(40)}}
        rows = logic.payback_rows([bike("A", purchase_price=None),
                                   bike("B", purchase_price=None)], money, days=30)
        total = logic.payback_total(rows)
        self.assertEqual(total["paid"], D(40000))
        self.assertEqual(total["margin"], D(34000))
        self.assertEqual(total["check_per_day"], D("666.67"))
        self.assertEqual(logic.payback_total([])["margin"], D(0))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestPaybackInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.since = date.today().replace(day=1)
        self.until = date.today()

    def money(self):
        start = datetime.combine(self.since, datetime.min.time(), tzinfo=UTC)
        end = datetime.combine(self.until + timedelta(days=1), datetime.min.time(),
                               tzinfo=UTC)
        return tw.run(self.crm.model_money(start, end))

    def rent(self, *, pay: Decimal | None = None, with_rental: bool = True):
        rid = tw.run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="auto",
            started_on=self.since, contract_no="АВ-1", created_by="staff:admin"))
        # Начисление в журнале отрицательное: это долг клиента.
        tw.run(self.crm.charge_period(rid, self.client_id, amount=-D(3000),
                                      period_from=self.since,
                                      period_to=self.since + timedelta(days=7),
                                      note=None, created_by="staff:admin"))
        if pay is not None:
            tw.run(self.crm.add_ledger(
                client_id=self.client_id, rental_id=rid if with_rental else None,
                kind="payment", amount=pay, method="sbp", note=None,
                created_by="staff:admin"))
        return rid

    def test_payment_is_attributed_to_the_model(self):
        self.rent(pay=D(3000))
        money = self.money()
        self.assertEqual(money["Kugoo V3"]["paid"], D(3000))
        self.assertEqual(money["Kugoo V3"]["charged"], D(3000))

    def test_payment_without_rental_still_finds_the_model(self):
        """Зачисление по заявке из бота: rental_id пуст, модель - по аренде."""
        self.rent(pay=D(2500), with_rental=False)
        self.assertEqual(self.money()["Kugoo V3"]["paid"], D(2500))

    def test_prepayment_before_the_rental_finds_the_model(self):
        """Заплатил вперёд, велосипед выдали позже - деньги всё равно модели."""
        rid = tw.run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="auto",
            started_on=self.until, contract_no="АВ-1", created_by="staff:admin"))
        del rid
        tw.run(self.crm.add_ledger(
            client_id=self.client_id, rental_id=None, kind="payment", amount=D(3000),
            method="sbp", note=None, created_by="staff:admin",
            created_at=datetime.combine(self.since, datetime.min.time(), tzinfo=UTC)))
        self.assertEqual(self.money()["Kugoo V3"]["paid"], D(3000))

    def test_repairs_and_client_works_land_on_the_model(self):
        self.rent()
        tw.run(self.crm.create_repair(self.bike_id,
                                      items=[{"node": "brake_pads", "parts_cost": D(800),
                                              "labor_cost": D(400), "note": None}],
                                      note=None, created_by="staff:admin"))
        order_id = tw.run(self.crm.create_work_order(
            bike_id=self.bike_id, payer="client", client_id=self.client_id,
            complaint=None, object_note=None, tech_id=None, estimate=D(0),
            created_by="staff:admin"))
        tw.run(self.crm.update_work_order(order_id, status="done", total=D(1500),
                                          cost=D(500), closed_at=datetime.now(UTC)))
        money = self.money()
        self.assertEqual(money["Kugoo V3"]["repair_cost"], D(1200))
        self.assertEqual(money["Kugoo V3"]["works"], D(1500))

    def test_report_and_csv_render(self):
        self.rent(pay=D(3000))
        page = self.get_ok("/reports/payback")
        self.assertIn("Kugoo V3", page)
        self.assertIn("ИТОГО", page)
        r = self.client.get("/reports/payback.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Модель", r.text)
        self.assertIn("Kugoo V3", r.text)

    def test_broken_dates_fall_back_to_the_month(self):
        self.rent(pay=D(3000))
        page = self.get_ok("/reports/payback?since=не+дата&until=тоже")
        self.assertIn("Kugoo V3", page)

    def test_report_is_money_only(self):
        """Механику парк виден, деньги парка - нет."""
        profile = tw.run(self.crm.access_profile_by_code("tech"))
        tw.run(self.crm.create_staff("petr", logic.hash_password("password-1"),
                                     "Пётр", "manager", profile["id"]))
        self.client.post("/logout")
        self.login("petr", "password-1")
        self.assertEqual(self.client.get("/reports/payback").status_code, 403)
        self.assertEqual(self.client.get("/reports/payback.csv").status_code, 403)


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
