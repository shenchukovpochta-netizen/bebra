"""Отчёты сервиса: по сотрудникам, траты по моделям, расход склада.

Три вопроса, на которые окупаемость по моделям не отвечает: кто из
техников сколько сделал, какая модель дороже всех обходится в запчастях
и что вообще уходит со склада.

Правило, которое здесь стерегут: сумма отчёта сходится с суммой нарядов.
Наряд без техника не теряется, он идёт строкой «не назначен» — иначе
расхождение заметят первым делом и отчёту перестанут верить.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
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


class TestTechRows(unittest.TestCase):
    def rows(self):
        return logic.tech_rows([
            {"tech": "Хомяков И.", "tech_id": 2, "orders": 4, "client_orders": 1,
             "total": D(8000), "cost": D(3000), "days": 6.0},
            {"tech": "не назначен", "tech_id": None, "orders": 1,
             "client_orders": 0, "total": D(1000), "cost": D(400), "days": 1.0},
        ])

    def test_works_are_the_order_minus_parts(self):
        row = self.rows()[0]
        self.assertEqual(row["works"], D(5000),
                         "видно, сколько наработал руками, а не сколько "
                         "прошло железа")

    def test_averages(self):
        row = self.rows()[0]
        self.assertEqual(row["avg_days"], D("1.5"))
        self.assertEqual(row["avg_total"], D(2000))

    def test_no_orders_no_division(self):
        row = logic.tech_rows([{"tech": "Новый", "orders": 0, "total": D(0),
                                "cost": D(0), "days": 0}])[0]
        self.assertEqual(row["avg_days"], D(0))
        self.assertEqual(row["avg_total"], D(0))

    def test_total_matches_the_sum_of_orders(self):
        total = logic.tech_total(self.rows())
        self.assertEqual(total["orders"], 5)
        self.assertEqual(total["total"], D(9000))
        self.assertEqual(total["works"], D(5600))
        self.assertEqual(total["avg_total"], D(1800))


class TestModelParts(unittest.TestCase):
    def test_sign_is_dropped_here_too(self):
        """В движениях расход отрицательный: «модель съела −4 500 ₽»
        читается хуже, чем «4 500 ₽»."""
        row = logic.model_parts_rows(
            [{"model": "Kugoo V3", "orders": 5, "qty": -9, "cost": D(-4500)}],
            [], days=30)[0]
        self.assertEqual(row["cost"], D(4500))
        self.assertEqual(row["qty"], 9)

    def test_cost_per_bike_and_day(self):
        rows = logic.model_parts_rows(
            [{"model": "Kugoo V3", "orders": 5, "qty": -9, "cost": D(-4500)}],
            [{"model": "Kugoo V3", "status": "available"},
             {"model": "Kugoo V3", "status": "rented"},
             {"model": "Kugoo V3", "status": "lost"}],
            days=30)
        row = rows[0]
        self.assertEqual(row["bikes"], 2, "выбывшие в парк не считаются")
        self.assertEqual(row["per_bike"], D(2250))
        self.assertEqual(row["per_bike_day"], D("75.00"))

    def test_model_without_bikes_has_no_per_bike(self):
        row = logic.model_parts_rows(
            [{"model": "чужая техника", "orders": 2, "qty": 3, "cost": D(900)}],
            [], days=30)[0]
        self.assertIsNone(row["per_bike"])
        self.assertIsNone(row["per_bike_day"])

    def test_expensive_first(self):
        rows = logic.model_parts_rows(
            [{"model": "A", "cost": D(100)}, {"model": "B", "cost": D(900)}],
            [], days=1)
        self.assertEqual([r["model"] for r in rows], ["B", "A"])


class TestSpendRows(unittest.TestCase):
    def test_sign_is_dropped(self):
        row = logic.spend_rows([{"id": 1, "title": "Камера", "node": "tube_tire",
                                 "unit": "шт", "qty": -7, "cost": D(-2100),
                                 "orders": 3}])[0]
        self.assertEqual(row["qty"], 7)
        self.assertEqual(row["cost"], D(2100), "знак человеку ничего не добавляет")
        self.assertEqual(row["node_title"], logic.REPAIR_NODES["tube_tire"])

    def test_total(self):
        total = logic.spend_total(logic.spend_rows([
            {"id": 1, "title": "Камера", "qty": -7, "cost": D(-2100), "orders": 3},
            {"id": 2, "title": "Колодки", "qty": -2, "cost": D(-800), "orders": 1}]))
        self.assertEqual(total["qty"], 9)
        self.assertEqual(total["cost"], D(2900))
        self.assertEqual(total["titles"], 2)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestReportPages(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.tech_id = _run(self.crm.create_staff(
            "homyakov", logic.hash_password("homyakov-pass"), name="Хомяков И.",
            role="tech", profile_id=None))
        self.part_id = _run(self.crm.create_part(
            title="Камера", node="tube_tire", unit="шт", cost=D(300),
            price=D(600), min_stock=2, model=None, note=None))

    def closed_order(self, *, total=None, cost=None, tech=True):
        total = D(2000) if total is None else total
        cost = D(500) if cost is None else cost
        order_id = _run(self.crm.create_work_order(
            bike_id=self.bike_id, payer="own", client_id=None,
            complaint="стук", object_note=None,
            tech_id=self.tech_id if tech else None,
            estimate=D(0), created_by="тест"))
        _run(self.crm.update_work_order(
            order_id, status="done", total=total, cost=cost,
            closed_at=datetime.now(UTC)))
        return order_id

    def test_techs_report(self):
        self.closed_order()
        text = self.get_ok("/reports/techs")
        self.assertIn("Хомяков И.", text)
        self.assertIn("Выработка техников", text)

    def test_unassigned_order_is_not_lost(self):
        self.closed_order(total=D(1000), cost=D(0), tech=False)
        self.assertIn("не назначен", self.get_ok("/reports/techs"))

    def test_open_order_is_not_counted(self):
        _run(self.crm.create_work_order(
            bike_id=self.bike_id, payer="own", client_id=None, complaint="стук",
            object_note=None, tech_id=self.tech_id, estimate=D(0),
            created_by="тест"))
        rows = _run(self.crm.tech_work(
            datetime.now(UTC) - timedelta(days=1), datetime.now(UTC) + timedelta(days=1)))
        self.assertEqual(rows, [], "открытый наряд ещё ничего не сделал")

    def test_techs_csv(self):
        self.closed_order()
        r = self.client.get("/reports/techs.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers["content-type"])
        self.assertIn("Хомяков И.", r.text)
        self.assertIn("ИТОГО", r.text)

    def test_model_parts_and_spend(self):
        order_id = self.closed_order()
        _run(self.crm.add_part_move(part_id=self.part_id, kind="order", qty=-3,
                                    cost=D(300), order_id=order_id,
                                    created_by="тест"))
        text = self.get_ok("/reports/model-parts")
        self.assertIn("Kugoo V3", text)
        spend = self.get_ok("/reports/spend")
        self.assertIn("Камера", spend)
        self.assertIn("900", spend)

    def test_spend_csv(self):
        order_id = self.closed_order()
        _run(self.crm.add_part_move(part_id=self.part_id, kind="order", qty=-3,
                                    cost=D(300), order_id=order_id,
                                    created_by="тест"))
        r = self.client.get("/reports/spend.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Камера", r.text)

    def test_reports_need_their_section(self):
        profile = _run(self.crm.create_access_profile(
            "Только клиенты", {"sections": {"clients": "view"}}))
        _run(self.crm.create_staff("menedzher", logic.hash_password("menedzher-1"),
                                   name="Менеджер", role="manager",
                                   profile_id=profile))
        self.client.post("/logout")
        self.login("menedzher", "menedzher-1")
        self.assertEqual(self.client.get("/reports/techs").status_code, 403)
        self.assertEqual(self.client.get("/reports/spend").status_code, 403)

    def test_tabs_are_linked(self):
        text = self.get_ok("/reports")
        self.assertIn("/reports/techs", text)
        self.assertIn("/reports/model-parts", text)
        self.assertIn("/reports/spend", text)


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
