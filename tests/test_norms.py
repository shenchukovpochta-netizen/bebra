"""Нормы парка и график «в ремонте по дням».

Плитка без нормы — это просто число: «в ремонте 7» ничего не значит, пока
не сказано, сколько это нормально. Норма превращает число в решение.

Второе правило здесь: считается среднее за сутки, а не «сколько было в
полночь». Велосипед, заехавший в ремонт в обед и уехавший к вечеру, —
это полдня простоя, и округлять его до нуля или до единицы одинаково
неверно.
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


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TestPlanNorms(unittest.TestCase):
    def test_defaults_come_from_the_fleet(self):
        plan = logic.month_plan({}, fleet=200)
        self.assertEqual(plan["rented"], 180, "простой меньше 10 %")
        self.assertEqual(plan["repair"], 10, "половина допустимого простоя")
        self.assertEqual(plan["spare"], 4)

    def test_tiny_fleet_still_gets_a_norm(self):
        plan = logic.month_plan({}, fleet=1)
        self.assertEqual(plan["repair"], 1, "ноль в ремонте - не норма, а мечта")
        self.assertEqual(plan["spare"], 2)

    def test_owner_settings_win(self):
        plan = logic.month_plan({"plan_repair": "5", "plan_spare": "3"}, fleet=200)
        self.assertEqual(plan["repair"], 5)
        self.assertEqual(plan["spare"], 3)


class TestFleetTiles(unittest.TestCase):
    def counts(self, **over):
        row = {"rented": 77, "repair": 7, "available": 12, "maintenance": 1,
               "new": 2, "lost": 25, "sold": 3}
        row.update(over)
        return row

    def test_percent_is_of_the_operational_fleet(self):
        tiles = {t["code"]: t for t in logic.fleet_tiles(self.counts())}
        # 77+7+12+1 = 97 операционных; new, lost и sold в знаменатель не идут
        self.assertEqual(tiles["rented"]["percent"], 79.4)
        self.assertNotIn("lost", tiles)

    def test_repair_over_the_norm_is_bad(self):
        plan = {"rented": 85, "repair": 5, "spare": 4}
        tiles = {t["code"]: t for t in logic.fleet_tiles(self.counts(), plan)}
        self.assertTrue(tiles["repair"]["over"])
        self.assertTrue(tiles["repair"]["bad"])
        self.assertEqual(tiles["repair"]["diff"], 2)

    def test_rented_under_the_norm_is_bad_the_other_way(self):
        plan = {"rented": 85, "repair": 5, "spare": 4}
        tiles = {t["code"]: t for t in logic.fleet_tiles(self.counts(), plan)}
        self.assertTrue(tiles["rented"]["under"])
        self.assertEqual(tiles["rented"]["diff"], -8)
        self.assertFalse(tiles["rented"]["over"],
                         "«сверх нормы» на аренде было бы издевательством")

    def test_spare_tile_appears_only_with_a_norm(self):
        without = {t["code"] for t in logic.fleet_tiles(self.counts(), {})}
        self.assertNotIn("spare", without)
        tiles = {t["code"]: t for t in logic.fleet_tiles(
            self.counts(), {"spare": 4}, spare=2)}
        self.assertEqual(tiles["spare"]["value"], 2)
        self.assertTrue(tiles["spare"]["under"])

    def test_empty_fleet_does_not_divide(self):
        tiles = logic.fleet_tiles({})
        self.assertTrue(all(t["percent"] is None for t in tiles))


class TestRepairChart(unittest.TestCase):
    def days(self):
        return {date(2026, 9, 1): D("5.0"), date(2026, 9, 2): D("4.4"),
                date(2026, 9, 3): D("6.6"), date(2026, 9, 4): D("0")}

    def test_fractions_are_rounded_to_whole_bikes(self):
        chart = logic.repair_chart(self.days(), norm=5)
        values = [d["value"] for d in chart["days"]]
        self.assertEqual(values, [5, 4, 7, 0], "решение принимают по целым")

    def test_over_the_norm_is_marked(self):
        chart = logic.repair_chart(self.days(), norm=5)
        self.assertEqual([d["over"] for d in chart["days"]],
                         [False, False, True, False])
        self.assertEqual(chart["over_days"], 1)
        self.assertEqual(chart["ok_days"], 3)
        self.assertEqual(chart["peak"], 7)

    def test_heights_are_percents_of_the_peak(self):
        chart = logic.repair_chart(self.days(), norm=5)
        self.assertEqual(chart["top"], 7)
        self.assertEqual(chart["days"][2]["height"], 100)
        self.assertEqual(chart["days"][3]["height"], 0)

    def test_without_a_norm_nothing_is_over(self):
        chart = logic.repair_chart(self.days())
        self.assertEqual(chart["over_days"], 0)

    def test_empty_month_does_not_divide_by_zero(self):
        chart = logic.repair_chart({}, norm=5)
        self.assertEqual(chart["days"], [])
        self.assertEqual(chart["peak"], 0)
        self.assertEqual(chart["top"], 5)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestNormPages(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def test_plan_saves_the_norms(self):
        self.client.post("/plan", data={"plan_rented": "150", "plan_check": "500",
                                        "plan_repair": "8", "plan_spare": "5"})
        settings = _run(self.crm.settings())
        self.assertEqual(settings["plan_repair"], "8")
        self.assertEqual(settings["plan_spare"], "5")

    def test_dashboard_shows_the_norm(self):
        self.client.post("/plan", data={"plan_rented": "150", "plan_check": "500",
                                        "plan_repair": "1", "plan_spare": "2"})
        _run(self.crm.update_bike(self.bike_id, status="repair", by="тест"))
        _run(self.crm.create_bike(code="B-2", model="Kugoo V3", status="repair"))
        text = self.get_ok("/")
        self.assertIn("норма 1", text)
        self.assertIn("сверх", text, "два в ремонте при норме один")

    def test_service_desk_has_the_chart(self):
        _run(self.crm.update_bike(self.bike_id, status="repair", by="тест"))
        text = self.get_ok("/service")
        self.assertIn("В ремонте по дням", text)
        self.assertIn("Парк против нормы", text)
        self.assertIn("суток в норме", text)

    def test_bad_norm_is_refused(self):
        self.client.post("/plan", data={"plan_rented": "150", "plan_check": "500",
                                        "plan_repair": "много", "plan_spare": "2"})
        self.assertNotIn("plan_repair", _run(self.crm.settings()))


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
