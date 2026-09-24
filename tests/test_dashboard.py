"""Сводка оператора: виджет «истекает аренда» с намерением клиента,
прогноз свободных и потери в рублях. Чистая логика и панель через TestClient
(обвязка из tests/test_web.py).
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

    from app.crm import service
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal
TODAY = date(2026, 9, 16)


def row(rental_id: int, left: int, **extra) -> dict:
    """Строка сводки: аренда с посчитанным «оплачено до»."""
    until = TODAY + timedelta(days=left)
    return {"id": rental_id, "summary": {"active": True, "days_left": left,
                                         "covered_until": until},
            "intent": None, "intent_until": None, "snooze_until": None, **extra}


class TestOperatorLogic(unittest.TestCase):
    def test_intent_is_valid_only_for_the_same_paid_until(self):
        r = row(1, 0, intent="renew", intent_until=TODAY)
        self.assertEqual(logic.intent_state(r, r["summary"], today=TODAY)["intent"], "renew")
        # клиент заплатил - «оплачено до» уехало, старое «продлит» устарело
        moved = {**r["summary"], "covered_until": TODAY + timedelta(days=7)}
        self.assertIsNone(logic.intent_state(r, moved, today=TODAY)["intent"])
        self.assertIsNone(logic.intent_state(row(2, 0, intent="что-то"),
                                             row(2, 0)["summary"], today=TODAY)["intent"])
        snoozed = row(3, 0, snooze_until=TODAY + timedelta(days=1))
        self.assertTrue(logic.intent_state(snoozed, snoozed["summary"], today=TODAY)["snoozed"])
        expired = row(4, 0, snooze_until=TODAY)
        self.assertFalse(logic.intent_state(expired, expired["summary"], today=TODAY)["snoozed"])

    def test_today_tasks_are_ranked_and_named(self):
        expiring = [{**row(1, -2), "full_name": "Должник"},
                    {**row(2, 0), "full_name": "Сегодня"},
                    {**row(3, 1), "full_name": "Завтра"}]
        search = {"candidates": [{"full_name": "Пропал"}],
                  "searching": [{"full_name": "Украли", "theft": True},
                                {"full_name": "Ищем", "theft": False}]}
        orders = [{"no": "РЕМ-000001", "status": "in_work",
                   "opened_at": datetime(2026, 9, 1, tzinfo=UTC)},
                  {"no": "РЕМ-000002", "status": "in_work",
                   "opened_at": datetime(2026, 9, 24, tzinfo=UTC)}]
        bookings = [{"full_name": "Новичок", "wanted_on": TODAY, "status": "new"},
                    {"full_name": "Позже", "wanted_on": TODAY + timedelta(days=2),
                     "status": "new"},
                    {"full_name": "Закрыта", "wanted_on": TODAY, "status": "done"}]
        alerts = [{"bike_code": "B-1", "level": "urgent", "state": "new"},
                  {"bike_code": "B-2", "level": "yellow", "state": "new"},
                  {"bike_code": "B-3", "level": "urgent", "state": "working"}]
        tasks = logic.today_tasks(expiring=expiring, search=search, orders=orders,
                                  claims=[{"full_name": "Платил"}], bookings=bookings,
                                  alerts=alerts, today=TODAY)
        codes = [t["code"] for t in tasks]
        self.assertEqual(codes[:6], ["overdue", "theft", "search", "bookings",
                                     "alerts_urgent", "soon"][:6])
        self.assertEqual([t["level"] for t in tasks][:5], ["hot"] * 5)
        by = {t["code"]: t for t in tasks}
        self.assertEqual(by["overdue"]["count"], 2)
        self.assertEqual(by["overdue"]["names"], ["Должник", "Сегодня"])
        self.assertEqual(by["soon"]["names"], ["Завтра"])
        self.assertEqual(by["orders"]["count"], 1, "только застрявший наряд")
        self.assertEqual(by["orders"]["names"], ["РЕМ-000001"])
        self.assertEqual(by["bookings"]["names"], ["Новичок"])
        self.assertEqual(by["bookings_later"]["level"], "info")
        self.assertEqual(by["alerts_urgent"]["names"], ["B-1"])
        self.assertEqual(by["alerts"]["count"], 1, "тревога в работе - не новая")
        self.assertEqual(by["claims"]["level"], "warn")
        self.assertEqual(logic.today_tasks(today=TODAY), [], "нечего - пусто")

    def test_expiring_filters_and_orders(self):
        rows = [row(1, 5), row(2, 1), row(3, -3), row(4, 0),
                row(5, 0, snooze_until=TODAY + timedelta(days=1)),
                {"id": 6, "summary": {"active": False, "days_left": None}}]
        out = logic.expiring(rows, today=TODAY, before_days=2)
        self.assertEqual([r["id"] for r in out], [3, 4, 2],
                         "просроченные первыми, отложенные скрыты")
        self.assertEqual(out[0]["intent_label"], "")

    def test_forecast_counts_what_frees_up_in_the_next_days(self):
        """Прогноз считается по «оплачено до», а «продлю» из него выпадает."""
        rows = [{"status": "active", "bike_id": 1, "billed_until": TODAY,
                 "balance": D(0), "price": D(3000), "period_days": 7, "intent": None},
                {"status": "active", "bike_id": 2,
                 "billed_until": TODAY + timedelta(days=1), "balance": D(0),
                 "price": D(3000), "period_days": 7, "intent": "return"},
                {"status": "active", "bike_id": 3,
                 "billed_until": TODAY + timedelta(days=1), "balance": D(0),
                 "price": D(3000), "period_days": 7, "intent": "renew"},
                {"status": "active", "bike_id": 4,
                 "billed_until": TODAY - timedelta(days=5), "balance": D(-3000),
                 "price": D(3000), "period_days": 7, "intent": None}]
        soon = logic.freeing_soon(rows, today=TODAY, horizon=3)
        self.assertEqual({k: len(v) for k, v in soon.items()},
                         {"0": 2, "1": 1, "2": 0, "3": 0})
        self.assertEqual(logic.forecast_summary(12, soon),
                         {"now": 12, "0": 14, "1": 15, "2": 15, "3": 15})

    def test_fleet_losses_from_metrics(self):
        metrics = logic.fleet_metrics(
            {"rented": D(80), "available": D(10), "repair": D(6), "maintenance": D(4)},
            D(30000))
        losses = logic.fleet_losses(metrics)
        self.assertEqual(losses["rate"], D(500))
        self.assertEqual(losses["by_status"]["available"], D(5000))
        self.assertEqual(losses["by_status"]["repair"], D(3000))
        self.assertEqual(losses["lost"], D(10000))
        self.assertEqual(losses["potential"], D(50000))
        self.assertEqual(losses["earned"], D(30000))
        self.assertEqual(losses["efficiency_percent"], 60.0)
        # дробные велосипеде-дни - целые рубли
        part = logic.fleet_losses(logic.fleet_metrics({"available": D("1.5")}, 0))
        self.assertEqual(part["lost"], D(750))
        self.assertEqual(logic.fleet_losses(
            logic.fleet_metrics({"repair": D("0.333")}, 0))["lost"], D(167))
        empty = logic.fleet_losses(logic.fleet_metrics({}, 0))
        self.assertEqual(empty["lost"], D(0))
        self.assertIsNone(empty["efficiency_percent"])

    def test_loss_per_day_counts_idle_bikes(self):
        out = logic.loss_per_day({"available": 7, "repair": 5, "maintenance": 1,
                                  "rented": 150, "lost": 25})
        self.assertEqual(out["idle"], 13)
        self.assertEqual(out["amount"], D(6500))
        self.assertEqual(out["by_status"]["reserved"], 0)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestExpiringWidget(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        # аренда неделю назад без оплаты: первый период начислен, просрочка
        client = tw.run(self.crm.client(self.client_id))
        bike = tw.run(self.crm.bike(self.bike_id))
        tariff = tw.run(self.crm.tariff(self.tariff_id))
        self.rental_id = tw.run(service.open_rental(
            self.crm, client=client, bike=bike, tariff=tariff,
            started_on=date.today() - timedelta(days=7), contract_no=None, by="t"))

    def mark(self, intent, nxt="/"):
        return self.client.post(f"/rentals/{self.rental_id}/intent",
                                data={"intent": intent, "next": nxt})

    def test_today_tasks_card_leads_with_the_overdue(self):
        page = self.get_ok("/")
        self.assertIn("Задачи на сегодня", page)
        self.assertIn("Просрочка или платёж сегодня", page)
        self.assertIn('class="task hot" href="/"', page)
        self.assertIn("Иванов Иван", page)
        # заявка из кабинета на сегодня - в тот же список
        client = tw.run(self.crm.create_client(full_name="Петров Пётр",
                                               phone="+79990000002"))
        tw.run(self.crm.create_booking(client_id=client, model="Kugoo V3", tariff_id=None,
                                       location_id=None, wanted_on=date.today()))
        page = self.get_ok("/")
        self.assertIn("Заявки на выдачу сегодня", page)
        self.assertIn('href="/bookings"', page)

    def test_widget_lists_overdue_with_phone_and_actions(self):
        page = self.get_ok("/")
        self.assertIn("Истекает аренда", page)
        self.assertIn('href="tel:+79990000000"', page)
        self.assertIn("просрочка", page)
        self.assertIn("↩ сдаёт", page)
        self.assertIn("Свободных сейчас <b>0</b>", page)
        self.assertIn("теряем ≈", page)
        self.assertIn("потеряно на простое", page)

    def test_intent_is_shown_and_takes_renewals_out_of_the_forecast(self):
        r = self.mark("return")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/")
        page = self.get_ok("/")
        self.assertIn("сдаёт</span>", page)
        self.assertIn("к завтрашнему дню <b>1</b>", page)
        rental = tw.run(self.crm.rental(self.rental_id))
        self.assertEqual(rental["intent"], "return")
        self.assertEqual(rental["intent_by"], "staff:admin")
        self.assertIsNotNone(rental["intent_at"])

        self.mark("renew", nxt=f"/rentals/{self.rental_id}")
        card = self.get_ok(f"/rentals/{self.rental_id}")
        self.assertIn("Клиент сказал", card)
        self.assertIn("продлит</span>", card)
        # Сказал «продлю» - велосипед не вернётся, и в прогнозе его нет.
        self.assertIn("к завтрашнему дню <b>0</b>", self.get_ok("/"))

    def test_snooze_hides_until_tomorrow_and_clear_restores(self):
        self.mark("snooze")
        page = self.get_ok("/")
        self.assertNotIn("↩ сдаёт", page)
        self.assertIn("отложено до завтра", page)
        self.assertEqual(tw.run(self.crm.rental(self.rental_id))["snooze_until"],
                         date.today() + timedelta(days=1))
        self.mark("clear")
        self.assertIn("↩ сдаёт", self.get_ok("/"))

    def test_intent_goes_stale_after_payment(self):
        self.mark("renew")
        self.assertIn("продлит</span>", self.get_ok("/"))
        # клиент заплатил за период: «оплачено до» сдвинулось, отметка снята сама
        tw.run(self.crm.add_ledger(client_id=self.client_id, kind="payment", amount=D(3000)))
        page = self.get_ok("/")
        self.assertNotIn("продлит</span>", page)
        self.assertIn("↩ сдаёт", page, "строка осталась: платёж сегодня")

    def test_intent_needs_active_rental_and_local_next(self):
        r = self.mark("renew", nxt="https://evil.example/")
        self.assertEqual(r.headers["location"], f"/rentals/{self.rental_id}")
        r = self.client.post(f"/rentals/{self.rental_id}/intent",
                             data={"intent": "fly", "next": "/"})
        self.assertIn("Неизвестное действие", self.get_ok("/"))
        tw.run(self.crm.close_rental(self.rental_id, closed_on=date.today(), note=None))
        r = self.mark("renew")
        self.assertIn("не идёт", self.get_ok("/"))

    def test_reports_show_losses_column(self):
        page = self.get_ok("/reports")
        self.assertIn("<th>Потери</th>", page)
        self.assertIn("<th>КПД</th>", page)


if __name__ == "__main__":
    unittest.main()
