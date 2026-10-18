"""Сводка владельца: «Критично» группами (клиенты, задачи, проблемы),
план-факт месяца, платёжный календарь, парк и точки за месяц - и ни одного
списка дел точки. Намерение клиента («продлит», «сдаёт», «завтра») - в
карточке аренды, прогноз свободных и потери в рублях - в блоке парка.
Чистая логика и панель через TestClient (обвязка из tests/test_web.py).
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import cashflow, logic  # noqa: E402
from app.crm import dashboard as dash  # noqa: E402

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
                 "price": D(3000), "period_days": 7, "intent": "return",
                 "intent_until": TODAY + timedelta(days=1)},
                {"status": "active", "bike_id": 3,
                 "billed_until": TODAY + timedelta(days=1), "balance": D(0),
                 "price": D(3000), "period_days": 7, "intent": "renew",
                 "intent_until": TODAY + timedelta(days=1)},
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
        # журнал статусов только что начат: минуты парка, деньги за месяц
        fresh = logic.fleet_losses(logic.fleet_metrics(
            {"rented": D("0.002"), "available": D("0.001")}, D(38500)))
        self.assertIsNone(fresh["efficiency_percent"])

    def test_loss_per_day_counts_idle_bikes(self):
        out = logic.loss_per_day({"available": 7, "repair": 5, "maintenance": 1,
                                  "rented": 150, "lost": 25})
        self.assertEqual(out["idle"], 13)
        self.assertEqual(out["amount"], D(6500))
        self.assertEqual(out["by_status"]["reserved"], 0)


class TestDashboardLogic(unittest.TestCase):
    """Сводка - картина, а не список дел (app/crm/dashboard.py)."""

    def tasks(self):
        expiring = [{**row(1, -2), "full_name": "Должник"},
                    {**row(3, 1), "full_name": "Завтра"}]
        search = {"candidates": [{"full_name": "Пропал"}],
                  "searching": [{"full_name": "Украли", "theft": True}]}
        orders = [{"no": "РЕМ-000001", "status": "in_work",
                   "opened_at": datetime(2026, 9, 1, tzinfo=UTC)}]
        alerts = [{"bike_code": "B-1", "level": "urgent", "state": "new"},
                  {"bike_code": "B-2", "level": "yellow", "state": "new"}]
        bookings = [{"full_name": "Новичок", "wanted_on": TODAY, "status": "new"}]
        return logic.today_tasks(expiring=expiring, search=search, orders=orders,
                                 claims=[{"full_name": "Платил"}], bookings=bookings,
                                 alerts=alerts, today=TODAY)

    EXTRA = ({"code": "tasks_late", "title": "Просроченные поручения", "count": 1,
              "url": "/tasks", "level": "hot", "names": ["Закупка"]},
             {"code": "inbox", "title": "Ждут ответа в сообщениях", "count": 2,
              "url": "/incoming", "level": "warn", "names": []})

    def test_critical_is_clients_tasks_problems(self):
        """Клиенты, задачи, проблемы - и ничего из дел точки: «истекает на
        днях», заявки на выдачу и «Я оплатил» живут в «Задачах дня» и во
        «Входящих»."""
        out = dash.critical(self.tasks(), self.EXTRA, can=lambda section: True)
        by = {g["code"]: [r["code"] for r in g["rows"]] for g in out["groups"]}
        self.assertEqual(list(by), ["clients", "tasks", "problems"])
        self.assertEqual(by["clients"], ["overdue", "theft", "search"])
        self.assertEqual(by["tasks"], ["tasks_late"])
        self.assertEqual(by["problems"], ["inbox", "orders", "alerts_urgent", "alerts"])
        self.assertEqual(out["count"], 8)
        rows = {r["code"]: r for g in out["groups"] for r in g["rows"]}
        self.assertTrue(rows["overdue"]["hot"])
        self.assertFalse(rows["inbox"]["hot"])
        self.assertEqual(rows["overdue"]["url"], logic.EXPIRING_URL,
                         "списка на сводке нет - ведёт в аренды по «оплачено до»")
        self.assertEqual(dash.critical([], can=lambda section: True),
                         {"groups": [], "count": 0})

    def test_critical_row_needs_its_section(self):
        out = dash.critical(self.tasks(), self.EXTRA,
                            can=lambda section: section == "trackers")
        self.assertEqual([[r["code"] for r in g["rows"]] for g in out["groups"]],
                         [["tasks_late"], ["alerts_urgent", "alerts"]],
                         "поручения видны всем, остальное - с правом на раздел")

    def test_fleet_advice_needs_view_and_edit_for_a_form(self):
        tasks = [{"code": "transfer", "title": "Перевезти 2 на Адоратского", "count": 2,
                  "url": "/reports/points#transfer", "level": "warn", "names": []},
                 {"code": "idle_promo", "title": "Kugoo простаивает", "count": 3,
                  "url": "/promos/new", "level": "info", "edit": True, "names": []},
                 {"code": "overdue", "title": "x", "count": 1, "url": "/", "level": "hot"}]
        everything = dash.fleet_advice(tasks, can_view=lambda s: True, can_edit=lambda s: True)
        self.assertEqual([t["code"] for t in everything], ["transfer", "idle_promo"])
        look = dash.fleet_advice(tasks, can_view=lambda s: True, can_edit=lambda s: False)
        self.assertEqual([t["code"] for t in look], ["transfer"],
                         "предложение скидки - тому, кто заведёт акцию")
        self.assertEqual(dash.fleet_advice(tasks, can_view=lambda s: s == "promos",
                                           can_edit=lambda s: True)[0]["code"], "idle_promo")

    def test_fleet_tiles_add_reserve_and_spare_and_keep_out_of_service_apart(self):
        counts = {"rented": 6, "available": 2, "repair": 1, "reserved": 1, "new": 2,
                  "lost": 3, "sold": 1}
        park = dash.fleet_tiles(counts, {"repair": 1}, spare=1)
        self.assertEqual([t["code"] for t in park["tiles"]],
                         ["rented", "repair", "available", "maintenance", "reserved",
                          "new", "spare"])
        tiles = {t["code"]: t for t in park["tiles"]}
        self.assertEqual(park["operational"], 10)
        self.assertEqual(tiles["reserved"]["percent"], 10.0)
        self.assertEqual(tiles["rented"]["percent"], 60.0,
                         "проценты - от операционного парка, потерянные не в счёт")
        self.assertEqual(tiles["spare"]["value"], 1)
        self.assertIsNone(tiles["spare"]["norm"], "нормы нет - просто число")
        self.assertEqual(tiles["repair"]["norm"], 1)
        self.assertEqual(park["out"], [{"code": "lost", "title": "Утерян", "value": 3},
                                       {"code": "sold", "title": "Продан", "value": 1},
                                       {"code": "written_off", "title": "Списан",
                                        "value": 0}])
        # норма подменных задана - плитка из logic.fleet_tiles, без дубля
        park = dash.fleet_tiles(counts, {"spare": 2}, spare=1)
        spares = [t for t in park["tiles"] if t["code"] == "spare"]
        self.assertEqual((len(spares), spares[0]["norm"], spares[0]["bad"]), (1, 2, True))

    def test_fleet_by_model(self):
        bikes = [{"model": "Kugoo V3", "status": "rented"},
                 {"model": "Kugoo V3", "status": "rented", "spare": False},
                 {"model": "Kugoo V3", "status": "available", "spare": True},
                 {"model": "Kugoo V3", "status": "maintenance"},
                 {"model": "Monster", "status": "repair"},
                 {"model": "Monster", "status": "reserved"},
                 {"model": "Monster", "status": "lost"},
                 {"model": "Новая", "status": "new"},
                 {"model": "Старая", "status": "written_off"}]
        out = dash.fleet_by_model(bikes)
        self.assertEqual([r["model"] for r in out["rows"]], ["Kugoo V3", "Monster", "Новая"],
                         "больше парк - выше; только списанные - уже не парк")
        kugoo, monster, fresh = out["rows"]
        self.assertEqual((kugoo["fleet"], kugoo["rented"], kugoo["free"], kugoo["repair"],
                          kugoo["spare"], kugoo["load"]), (4, 2, 1, 1, 1, 50))
        self.assertEqual((monster["fleet"], monster["repair"], monster["reserved"],
                          monster["load"]), (2, 1, 1, 0))
        self.assertEqual((fresh["fleet"], fresh["new"], fresh["load"]), (0, 1, None),
                         "на сборке в парк не входит - загрузки нет")
        self.assertEqual((out["total"]["fleet"], out["total"]["rented"],
                          out["total"]["load"]), (6, 2, 33))

    def test_change_and_income_compare(self):
        self.assertEqual(dash.change(D(150), D(100)),
                         {"now": D("150.00"), "prev": D("100.00"), "diff": D("50.00"),
                          "up": True, "percent": 50.0})
        self.assertIsNone(dash.change(D(10), 0)["percent"], "прошлого не было - без %")
        self.assertEqual(dash.change(D(80), D(100))["percent"], -20.0)
        now = cashflow.income({"Павлюхина": {"paid": D(3000)}},
                              {"Павлюхина": {"external": D(1000), "renters": D(0)}},
                              ["Павлюхина"])
        prev = cashflow.income({"Павлюхина": {"paid": D(2000)}}, {}, ["Павлюхина"])
        rows = {r["code"]: r for r in dash.income_compare(now, prev)}
        self.assertEqual(list(rows), ["rent", "repair_external", "repair_renters", "total"])
        self.assertEqual((rows["rent"]["diff"], rows["rent"]["percent"], rows["rent"]["share"]),
                         (D("1000.00"), 50.0, 75.0))
        self.assertIsNone(rows["repair_external"]["percent"])
        self.assertEqual((rows["total"]["now"], rows["total"]["prev"]),
                         (D("4000.00"), D("2000.00")))

    def test_calendar_strip_and_short_money(self):
        self.assertEqual([dash.short_money(x) for x in (D(950), D(40600), D(126400),
                                                        D(1250000), D(3000))],
                         ["950", "40,6к", "126,4к", "1,3 млн", "3к"])
        rentals = [{"id": 1, "full_name": "А", "status": "active", "billing": "auto",
                    "price": D(3000), "period_days": 7, "billed_until": TODAY,
                    "location": None}]
        plans = [{"id": 1, "due_on": TODAY + timedelta(days=1), "amount": D(6000),
                  "direction": "out", "title": "Аренда помещения", "repeat_months": 0}]
        cal = cashflow.calendar(rentals, plans, start=TODAY, days=3, opening=D(2000))
        strip = dash.calendar_strip(cal)
        self.assertEqual([(d["came"], d["in_height"], d["out_height"]) for d in strip],
                         [(D("3000.00"), 50, 0), (D("0.00"), 0, 100), (D("0.00"), 0, 0)])
        self.assertEqual([(d["negative"], d["gap"]) for d in strip],
                         [(False, False), (True, True), (True, False)],
                         "остаток 5 000 минус 6 000 - разрыв на второй день")
        blind = dash.calendar_strip(cashflow.calendar(rentals, plans, start=TODAY, days=3))
        self.assertFalse(any(d["negative"] for d in blind),
                         "без остатка на сегодня минуса не бывает - его не с чего считать")

    def test_opening_balance_from_the_address(self):
        self.assertEqual(cashflow.parse_opening("120 000"), D("120000.00"))
        self.assertEqual(cashflow.parse_opening("-5000,5"), D("-5000.50"))
        for junk in ("", None, "NaN", "1e20", "много", "Infinity"):
            self.assertIsNone(cashflow.parse_opening(junk), junk)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestDashboardPage(tw.WebCase):
    """Сводка владельца: «Критично», план-факт, календарь, парк, точки -
    и ни одного списка дел точки."""

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

    def mark(self, intent, nxt=None):
        return self.client.post(f"/rentals/{self.rental_id}/intent",
                                data={"intent": intent,
                                      "next": nxt or f"/rentals/{self.rental_id}"})

    def history(self, days):
        """История статусов и денег началась `days` дней назад: стрелка
        «прошлый месяц» ведёт до неё."""
        for x in self.crm.status_log_ + self.crm.location_log_:
            x["changed_at"] -= timedelta(days=days)

    def test_no_task_lists_on_the_dashboard(self):
        """Задачи точки - в «Задачах дня» и во «Входящих», на сводке их нет:
        ни карточки «Задачи на сегодня», ни «Истекает аренда», ни заявок на
        зачисление, ни кнопки выдачи."""
        other = tw.run(self.crm.create_client(full_name="Петров Пётр",
                                              phone="+79990000002"))
        tw.run(self.crm.create_booking(client_id=other, model="Kugoo V3", tariff_id=None,
                                       location_id=None, wanted_on=date.today()))
        tw.run(self.crm.create_claim(self.client_id, D(3000)))
        page = self.get_ok("/")
        for gone in ("Задачи на сегодня", "Истекает аренда", "Заявки на зачисление",
                     "заявок на зачисление", "⚡ Быстрая выдача", "↩ сдаёт",
                     "Заявки на выдачу сегодня", "в задачах на сегодня"):
            self.assertNotIn(gone, page)
        # Просрочка - в «Критично», в группе клиентов, и ведёт в аренды.
        self.assertIn("Критично — 2.", page, "просрочка и кандидат в розыск")
        self.assertIn(">Клиенты</span>", page)
        self.assertIn(f'class="task hot hc-overdue" href="{logic.EXPIRING_URL}"', page)
        self.assertIn("Иванов Иван", page)
        # Заявка и «Я оплатил» никуда не делись - они во «Входящих».
        feed = self.get_ok("/incoming/feed")
        self.assertIn("Петров Пётр", feed)
        self.assertIn("«Я оплатил»", feed)
        self.assertIn("Иванов Иван", self.get_ok(logic.EXPIRING_URL))

    def test_critical_has_tasks_and_problems(self):
        tw.run(self.crm.create_task(title="Закупить запчасти", by="t",
                                    due_on=date.today() - timedelta(days=2)))
        page = self.get_ok("/")
        self.assertIn(">Задачи</span>", page)
        self.assertIn("Просроченные поручения", page)
        self.assertIn("Закупить запчасти", page)
        self.assertNotIn("Критичного нет", page)

    def test_calm_day(self):
        crm = self.crm
        crm.rentals_.clear()
        crm.ledger_.clear()
        tw.run(crm.update_bike(self.bike_id, status="available"))
        page = self.get_ok("/")
        self.assertIn("Критичного нет", page)
        self.assertNotIn("hero-group", page)

    def test_plan_fact_folds_the_form_and_compares_months(self):
        page = self.get_ok("/")
        self.assertIn("План-факт месяца", page)
        self.assertIn("% плана", page)
        self.assertIn("осталось собрать", page)
        self.assertIn('class="pace"', page, "черта ровного темпа на полосе плана")
        self.assertIn("к прошлому месяцу", page)
        self.assertIn("Доход по статьям", page)
        self.assertIn("Сторонний ремонт", page)
        self.assertIn("Ремонт арендаторам", page)
        self.assertIn("Деньги по дням", page)
        # Форма плана - под «Изменить план», закрытой.
        self.assertIn('<details class="more" id="plan-form">', page)
        self.assertIn("<summary>Изменить план</summary>", page)
        self.assertLess(page.index("Изменить план"), page.index('name="plan_rented"'))
        self.assertNotIn("План на месяц", page)

    def test_month_switch_goes_back_to_the_start_of_history(self):
        first = date.today().replace(day=1)
        prev = (first - timedelta(days=1)).replace(day=1)
        page = self.get_ok("/")
        self.assertNotIn(f'href="/?month={prev:%Y-%m}#plan"', page,
                         "история началась в этом месяце - назад некуда")
        self.history(70)
        page = self.get_ok("/")
        self.assertIn(f'href="/?month={prev:%Y-%m}#plan"', page)
        self.assertIn("неполный", page, "текущий месяц помечен")
        page = self.get_ok(f"/?month={prev:%Y-%m}")
        self.assertIn(prev.strftime("%m.%Y"), page)
        self.assertIn(f'href="/?month={first:%Y-%m}#plan"', page, "вперёд - в текущий")
        self.assertIn("пришло за месяц", page)
        self.assertEqual(self.client.get("/?month=0001-01").status_code, 200)

    def test_payment_calendar_on_the_dashboard(self):
        r = self.client.post("/finance/calendar", data={
            "title": "Аренда помещения", "due_on": date.today().isoformat(),
            "amount": "40000", "direction": "out", "repeat_months": "1"})
        self.assertEqual(r.status_code, 303)
        page = self.get_ok("/")
        self.assertIn("Платёжный календарь", page)
        self.assertIn("Аренда помещения", page)
        self.assertIn("ждём за аренду", page)
        self.assertIn("Накопительно", page, "без остатка - итог дней накопительно")
        self.assertIn('href="/finance/calendar"', page)
        self.assertNotIn("Кассовый разрыв", page)
        page = self.get_ok("/?start=10000")
        self.assertIn("Кассовый разрыв", page)
        self.assertIn('href="/finance/calendar?start=10000"', page)
        self.assertNotIn("Кассовый разрыв", self.get_ok("/?start=мусор"))

    def test_fleet_block(self):
        tw.run(self.crm.create_bike(code="B-2", model="Monster", status="repair"))
        tw.run(self.crm.create_bike(code="B-3", model="Monster", status="lost"))
        page = self.get_ok("/")
        self.assertIn("Велопарк", page)
        self.assertIn("По моделям", page)
        self.assertIn('href="/bikes?q=Kugoo%20V3&status=rented"', page)
        self.assertIn('href="/bikes?status=lost">Утерян: <b>1</b>', page)
        self.assertIn("вне оборота", page)
        self.assertIn("Свободных сейчас <b>0</b>", page)
        self.assertIn("теряем ≈", page)
        self.assertIn("потеряно на простое", page)
        self.assertIn("Три числа", page)

    def test_points_by_month(self):
        self.history(70)
        pav = tw.run(self.crm.create_location(name="Павлюхина", city="Казань",
                                              address="ул. Павлюхина, 1", note=None))
        tw.run(self.crm.create_location(name="Адоратского", city="Казань",
                                        address="ул. Адоратского, 1", note=None))
        tw.run(self.crm.create_bike(code="P-1", model="Kugoo V3", location="Павлюхина"))
        first = date.today().replace(day=1)
        prev = (first - timedelta(days=1)).replace(day=1)
        page = self.get_ok("/")
        self.assertIn('id="points"', page)
        block = page[page.index('id="points"'):]
        self.assertIn("Итого", block)
        self.assertIn(logic.NO_POINT_TITLE, block,
                      "велосипед без точки - своей строкой, иначе сумма не сойдётся")
        self.assertIn(f'href="/reports/points/{pav}?month={first:%Y-%m}"', block)
        self.assertIn(f'href="/reports/points?month={first:%Y-%m}"', block)
        self.assertIn(f'href="/?month={prev:%Y-%m}#points"', block)
        self.assertIn("неполный", block[:block.index("</h2>")])
        self.assertNotIn("По точкам за 30 дней", page)
        block = self.get_ok(f"/?month={prev:%Y-%m}")
        block = block[block.index('id="points"'):]
        self.assertIn(f'href="/reports/points?month={prev:%Y-%m}"', block)
        self.assertNotIn("неполный", block[:block.index("</h2>")],
                         "прошлый месяц внутри истории - полный")

    def test_money_blocks_need_finance(self):
        profile = tw.run(self.crm.access_profile_by_code("tech"))
        tw.run(self.crm.create_staff("mech", logic.hash_password("password-1"), "Механик",
                                     "manager", profile["id"]))
        self.client.post("/logout")
        self.login("mech", "password-1")
        page = self.get_ok("/")
        self.assertIn("Велопарк", page)
        for money in ("План-факт месяца", "Платёжный календарь", "Должники",
                      "теряем ≈", "Изменить план"):
            self.assertNotIn(money, page)

    def test_intent_on_the_card_moves_the_forecast(self):
        r = self.mark("return")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], f"/rentals/{self.rental_id}")
        card = self.get_ok(f"/rentals/{self.rental_id}")
        self.assertIn("сдаёт</span>", card)
        self.assertIn("к завтрашнему дню <b>1</b>", self.get_ok("/"))
        rental = tw.run(self.crm.rental(self.rental_id))
        self.assertEqual(rental["intent"], "return")
        self.assertEqual(rental["intent_by"], "staff:admin")
        self.assertIsNotNone(rental["intent_at"])
        self.mark("renew")
        card = self.get_ok(f"/rentals/{self.rental_id}")
        self.assertIn("Клиент сказал", card)
        self.assertIn("продлит</span>", card)
        # Сказал «продлю» - велосипед не вернётся, и в прогнозе его нет.
        self.assertIn("к завтрашнему дню <b>0</b>", self.get_ok("/"))

    def test_snooze_on_the_card_hides_the_overdue_until_tomorrow(self):
        card = self.get_ok(f"/rentals/{self.rental_id}")
        self.assertIn("⏰ завтра", card)
        self.mark("snooze")
        self.assertNotIn("hc-overdue", self.get_ok("/"))
        self.assertEqual(tw.run(self.crm.rental(self.rental_id))["snooze_until"],
                         date.today() + timedelta(days=1))
        card = self.get_ok(f"/rentals/{self.rental_id}")
        self.assertNotIn("⏰ завтра", card)
        self.assertIn(">снять</button>", card)
        self.mark("clear")
        self.assertIn("hc-overdue", self.get_ok("/"))

    def test_intent_goes_stale_after_payment(self):
        self.mark("renew")
        self.assertIn("продлит</span>", self.get_ok(f"/rentals/{self.rental_id}"))
        # клиент заплатил за период: «оплачено до» сдвинулось, отметка снята сама
        tw.run(self.crm.add_ledger(client_id=self.client_id, kind="payment", amount=D(3000)))
        self.assertNotIn("продлит</span>", self.get_ok(f"/rentals/{self.rental_id}"))

    def test_intent_needs_active_rental_and_local_next(self):
        r = self.mark("renew", nxt="https://evil.example/")
        self.assertEqual(r.headers["location"], f"/rentals/{self.rental_id}")
        r = self.client.post(f"/rentals/{self.rental_id}/intent",
                             data={"intent": "fly", "next": "/"})
        self.assertIn("Неизвестное действие", self.get_ok("/"))
        tw.run(self.crm.close_rental(self.rental_id, closed_on=date.today(), note=None))
        r = self.mark("renew", nxt="/")
        self.assertIn("не идёт", self.get_ok("/"))

    def test_reports_show_losses_column(self):
        # В сводке отчётов - четыре главные колонки, КПД и дни - на полной
        # странице по месяцам.
        self.assertIn(">Потери</th>", self.get_ok("/reports"))
        page = self.get_ok("/reports/months")
        self.assertIn(">Потери</th>", page)
        self.assertIn(">КПД</th>", page)


if __name__ == "__main__":
    unittest.main()
