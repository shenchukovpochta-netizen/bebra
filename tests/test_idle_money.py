"""Деньги простоя: простой в рублях, а не только в процентах.

Процент простоя — метрика для отчёта, рубль — довод для решения.
«В ремонте 7» ничего не говорит, «в ремонте 7, это −3 500 ₽ каждые
сутки» говорит всё.

Деньги здесь — оценка по цели среднего чека, а не факт: велосипед,
который стоит, не заработал ничего, и «сколько бы он принёс» —
единственный честный способ это назвать. Цель, а не фактический чек:
потери показывают расстояние до цели, а не подстраиваются под слабый
месяц.
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


class TestIdleCost(unittest.TestCase):
    def test_days_times_the_target(self):
        self.assertEqual(logic.idle_cost(20), 20 * logic.CHECK_TARGET)

    def test_zero_and_none(self):
        self.assertEqual(logic.idle_cost(0), D(0))
        self.assertEqual(logic.idle_cost(None), D(0))

    def test_whole_roubles(self):
        got = logic.idle_cost(D("1.5"), rate=D("333.33"))
        self.assertEqual(got, D(500), "копейки в оценке - точность, которой нет")
        self.assertEqual(got.as_tuple().exponent, 0)

    def test_rate_can_be_overridden(self):
        self.assertEqual(logic.idle_cost(2, rate=D(100)), D(200))


class TestServiceDeskMoney(unittest.TestCase):
    def rows(self):
        today = date(2026, 9, 17)
        bikes = [{"id": 1, "code": "B-1", "status": "repair", "idle_days": 20},
                 {"id": 2, "code": "B-2", "status": "repair", "idle_days": 3}]
        return logic.service_rows(bikes, {}, today=today)

    def test_every_row_carries_its_loss(self):
        rows = {r["code"]: r for r in self.rows()}
        self.assertEqual(rows["B-1"]["lost"], 20 * logic.CHECK_TARGET)
        self.assertEqual(rows["B-2"]["lost"], 3 * logic.CHECK_TARGET)

    def test_summary_adds_them_up_and_names_the_daily_rate(self):
        summary = logic.service_summary(self.rows())
        self.assertEqual(summary["days"], 23)
        self.assertEqual(summary["lost"], 23 * logic.CHECK_TARGET)
        self.assertEqual(summary["per_day"], 2 * logic.CHECK_TARGET,
                         "два велосипеда стоят - столько стоят каждые сутки")

    def test_empty_desk_costs_nothing(self):
        summary = logic.service_summary([])
        self.assertEqual(summary["lost"], D(0))
        self.assertEqual(summary["per_day"], D(0))


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestIdleMoneyPages(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def stand(self, days):
        """Поставить велосипед в ремонт задним числом.

        Двигаем обе записи журнала: простой считается по последней смене
        статуса, и если оставить запись о заведении на «сейчас», она
        окажется свежее ремонта.
        """
        _run(self.crm.update_bike(self.bike_id, status="repair", by="тест"))
        now = datetime.now(UTC)
        for row in self.crm.status_log_:
            if row["bike_id"] != self.bike_id:
                continue
            row["changed_at"] = (now - timedelta(days=days)
                                 if row["to_status"] == "repair"
                                 else now - timedelta(days=days + 10))

    def test_bike_card_names_the_money(self):
        self.stand(10)
        text = self.get_ok(f"/bikes/{self.bike_id}")
        self.assertIn("Простаивает 10 сут.", text)
        self.assertIn(tw.logic.money(logic.idle_cost(10)).split(" ")[0], text)

    def test_rented_bike_has_no_idle_line(self):
        text = self.get_ok(f"/bikes/{self.bike_id}")
        self.assertNotIn("Простаивает", text,
                         "свободный только что заведённый - не простой в деньгах")

    def test_service_desk_shows_the_daily_rate(self):
        self.stand(4)
        text = self.get_ok("/service")
        self.assertIn("в день, пока стоят", text)
        self.assertIn("Потеряно", text)

    def test_dashboard_lists_who_stands(self):
        self.stand(7)
        text = self.get_ok("/")
        self.assertIn("Стоят дольше всех", text)
        self.assertIn("B-1", text)

    def test_money_is_hidden_from_staff_without_finance(self):
        self.stand(7)
        profile = _run(self.crm.create_access_profile(
            "Механик без денег", {"sections": {"service": "edit", "bikes": "edit"}}))
        _run(self.crm.create_staff("mehanik", logic.hash_password("mehanik-pass"),
                                   name="Механик", role="tech", profile_id=profile))
        self.client.post("/logout")
        self.login("mehanik", "mehanik-pass")
        text = self.get_ok("/service")
        self.assertNotIn("Потеряно", text)
        self.assertNotIn("в день, пока стоят", text)
        self.assertNotIn("Простаивает", self.get_ok(f"/bikes/{self.bike_id}"))


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
