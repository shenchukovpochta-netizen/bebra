"""План месяца по точке и неполные месяцы в таблицах трёх чисел.

План точки лежит в строке справочника, а не в настройках под её именем:
переименование - каскад по тексту имени, и план обязан его пережить.
Общий план, не заданный явно, - сумма планов открытых точек, когда они
есть у каждой; заданный руками главнее. Выполнение считается тем же
ровным темпом, что и план месяца на сводке.

Неполный месяц - текущий или тот, где началась история (у сети - журнал
статусов, у точки - журнал мест): предоплата на четыре дня аренды даёт
чек 600-770 ₽, и без пометки его сравнивают с полными месяцами. Формулы
не меняются - меняется только подпись.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import logic  # noqa: E402

try:
    from tests import test_points_web as tpw
    from tests import test_web as tw
    HAVE_WEB = tw.HAVE_WEB and tpw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

try:
    import asyncpg
    import pgserver

    from app.crm.db import CrmDB
    from app.db import Database, _init_connection
    HAVE_PG = True
except ImportError:                                    # pragma: no cover
    HAVE_PG = False

D = Decimal
MSK = timezone(timedelta(hours=3))
PAV, ADO, DEK = "Павлюхина", "Адоратского", "Декабристов"
SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"


def place(name, rented=None, check=None, active=True, **over):
    return {"id": hash(name) % 1000, "name": name, "active": active,
            "plan_rented": rented, "plan_check": check, **over}


class TestPointPlanLogic(unittest.TestCase):
    def test_point_plan_uses_the_common_check_when_own_is_empty(self):
        self.assertIsNone(logic.point_plan(None, check=500))
        self.assertIsNone(logic.point_plan(place(PAV), check=500), "плана нет")
        self.assertIsNone(logic.point_plan(place(PAV, "ой"), check=500))
        self.assertIsNone(logic.point_plan(place(PAV, -1), check=500))
        self.assertIsNone(logic.point_plan(place(PAV, 10, active=False), check=500),
                          "у закрытой точки плана нет")
        own = logic.point_plan(place(PAV, 10, D("600")), check=500)
        self.assertEqual((own["rented"], own["check"], own["own_check"], own["per_day"]),
                         (10, D("600.00"), True, D("6000.00")))
        common = logic.point_plan(place(ADO, 4), check=D(500))
        self.assertEqual((common["check"], common["own_check"], common["per_day"]),
                         (D("500.00"), False, D("2000.00")))
        zero = logic.point_plan(place(DEK, 0), check=500)
        self.assertEqual(zero["per_day"], D(0), "ноль велосипедов - честный план")

    def test_sum_needs_every_open_point(self):
        places = [place(PAV, 3, D(600)), place(ADO, 4), place(DEK, active=False)]
        got = logic.plan_from_points(places, check=D(500))
        self.assertEqual((got["rented"], got["per_day"], got["count"]),
                         (7, D("3800.00"), 2), "закрытая точка не в счёт")
        self.assertEqual(got["check"], D("542.86"), "средний по деньгам точек")
        self.assertIsNone(logic.plan_from_points(places + [place("Новая")], check=500),
                          "открытая точка без плана - суммы нет")
        self.assertIsNone(logic.plan_from_points([], check=500))
        self.assertIsNone(logic.plan_from_points([place(PAV, 3, active=False)],
                                                 check=500))

    def test_month_plan_explicit_wins_then_points_then_fleet(self):
        places = [place(PAV, 3, D(600)), place(ADO, 4)]
        explicit = logic.month_plan({"plan_rented": "100", "plan_check": "550"},
                                    fleet=165, places=places)
        self.assertEqual((explicit["source"], explicit["rented"], explicit["check"],
                          explicit["per_day"]),
                         ("settings", 100, D("550.00"), D("55000.00")))
        self.assertEqual(explicit["points"]["per_day"], D("4000.00"),
                         "сумма точек видна и при явном плане - для сверки; "
                         "у Адоратского чек общий, 550")
        for raw in ({}, {"plan_rented": ""}, {"plan_rented": "ой"}):
            summed = logic.month_plan(raw, fleet=165, places=places)
            self.assertEqual((summed["source"], summed["rented"], summed["per_day"]),
                             ("points", 7, D("3800.00")), raw)
            self.assertEqual(summed["base_check"], logic.CHECK_TARGET,
                             "форма правит общий чек, а не средний точек")
        fleet = logic.month_plan({}, fleet=165, places=places + [place("Новая")])
        self.assertEqual((fleet["source"], fleet["rented"]), ("default", 148))
        self.assertEqual(logic.month_plan({}, fleet=165)["source"], "default",
                         "без справочника - как было")

    def test_progress_of_the_sum_matches_the_points_to_the_kopeck(self):
        """7 велосипедов по среднему 542,86 - это 3 800,02 в день, а точки
        дают 3 800,00: план суммы считается от денег точек в день."""
        plan = logic.month_plan({}, fleet=10, places=[place(PAV, 3, D(600)),
                                                     place(ADO, 4)])
        got = logic.plan_progress(plan, {"revenue": D(57000)}, days_in_month=30,
                                  days_passed=15)
        self.assertEqual((got["target"], got["pace"]), (D("114000.00"), D("57000.00")))
        self.assertTrue(got["ahead"])
        self.assertEqual(got["percent"], 50.0)

    def test_points_plan_columns(self):
        report = logic.points_rows(
            [place(PAV, 2), place(ADO), place(DEK, 1, D(700))], bikes=[], days={},
            money={PAV: {"paid": D(15000)}, ADO: {"paid": D(9000)},
                   DEK: {"paid": D(4000)}, None: {"paid": D(500)}})
        got = logic.points_plan(report, check=D(500), days=30, passed=10)
        plans = {r["key"]: r["plan"] for r in got["rows"]}
        self.assertEqual(got["planned"], 2)
        self.assertIsNone(plans[ADO])
        self.assertIsNone(plans[None], "у «без точки» плана не бывает")
        self.assertEqual((plans[PAV]["target"], plans[PAV]["pace"], plans[PAV]["percent"]),
                         (D("30000.00"), D("10000.00"), 50.0))
        self.assertTrue(plans[PAV]["ahead"])
        self.assertFalse(plans[DEK]["ahead"], "4 000 при темпе 7 000")
        total = got["total"]["plan"]
        self.assertEqual((total["target"], total["fact"]), (D("51000.00"), D("19000.00")),
                         "итого плана - только точки с планом")
        self.assertEqual(got["total"]["paid"], D(28500), "итог отчёта не тронут")
        bare = logic.points_plan(logic.points_rows([place(PAV)], bikes=[], days={},
                                                   money={}),
                                 check=D(500), days=30, passed=30)
        self.assertEqual(bare["planned"], 0)
        self.assertIsNone(bare["total"]["plan"])

    def test_report_period_knows_its_days(self):
        now = datetime(2026, 9, 4, 15, 30, tzinfo=MSK)
        month = logic.report_period({"month": "2026-09"}, now=now)
        self.assertEqual((month["days"], month["passed"]), (30, 4))
        past = logic.report_period({"month": "2026-08"}, now=now)
        self.assertEqual((past["days"], past["passed"]), (31, 31))
        custom = logic.report_period({"since": "2026-08-01", "until": "2026-08-10"},
                                     now=now)
        self.assertEqual((custom["days"], custom["passed"]), (10, 10))
        window = logic.report_period({}, now=now)
        self.assertEqual((window["days"], window["passed"]),
                         (logic.POINTS_PERIOD_DAYS, logic.POINTS_PERIOD_DAYS))

    def test_plan_month_of_the_point_page(self):
        today = date(2026, 9, 4)
        now = datetime(2026, 9, 4, 15, 30, tzinfo=MSK)
        chosen = logic.plan_month(logic.report_period({"month": "2026-08"}, now=now),
                                  today=today)
        self.assertEqual((chosen["first"], chosen["passed"], chosen["is_current"]),
                         (date(2026, 8, 1), 31, False))
        window = logic.plan_month(logic.report_period({}, now=now), today=today)
        self.assertEqual((window["first"], window["passed"]), (date(2026, 9, 1), 4),
                         "у «30 дней» своего месяца нет - текущий")


class TestPartialMonths(unittest.TestCase):
    NOW = datetime(2026, 9, 4, 15, 30, tzinfo=MSK)

    def windows(self):
        return {m["month"]: m for m in logic.month_windows(self.NOW, 3)}

    def test_current_month_is_partial(self):
        m = self.windows()[date(2026, 9, 1)]
        got = logic.month_coverage(m["since"], m["until"])
        self.assertEqual((got["partial"], got["days"], got["of"], got["current"]),
                         (True, 4, 30, True), "на 4-е число в счёте четыре дня")
        self.assertIsNone(got["from"])

    def test_full_past_month(self):
        m = self.windows()[date(2026, 8, 1)]
        got = logic.month_coverage(m["since"], m["until"],
                                   start=datetime(2026, 7, 10, tzinfo=MSK))
        self.assertEqual((got["partial"], got["days"], got["of"]), (False, 31, 31))

    def test_month_where_history_starts(self):
        windows = self.windows()
        # 17.07 22:30 UTC - это уже 18.07 по Москве: сутки - по часам панели.
        start = datetime(2026, 7, 17, 22, 30, tzinfo=UTC)
        july = logic.month_coverage(windows[date(2026, 7, 1)]["since"],
                                    windows[date(2026, 7, 1)]["until"], start=start)
        self.assertEqual((july["partial"], july["days"], july["from"], july["current"]),
                         (True, 14, date(2026, 7, 18), False))
        # История началась в текущем месяце: оба повода, дни - с её начала.
        now = logic.month_coverage(windows[date(2026, 9, 1)]["since"], self.NOW,
                                   start=datetime(2026, 9, 2, 9, tzinfo=MSK))
        self.assertEqual((now["partial"], now["days"], now["from"]),
                         (True, 3, date(2026, 9, 2)))

    def test_month_before_history_is_empty_not_partial(self):
        m = logic.month_windows(self.NOW, 6)[-1]              # апрель
        got = logic.month_coverage(m["since"], m["until"],
                                   start=datetime(2026, 7, 18, tzinfo=MSK))
        self.assertEqual((got["partial"], got["days"]), (False, 0))

    def test_history_from_midnight_of_the_first_is_full(self):
        m = self.windows()[date(2026, 8, 1)]
        got = logic.month_coverage(m["since"], m["until"], start=m["since"])
        self.assertFalse(got["partial"])
        # С 9 утра первого числа - все 31 сутки в счёте, но месяц первый:
        # предоплаты на старте раздувают его чек (демо: 613 ₽ против 510).
        late = logic.month_coverage(m["since"], m["until"],
                                    start=m["since"] + timedelta(hours=9))
        self.assertEqual((late["partial"], late["days"], late["from"]),
                         (True, 31, date(2026, 8, 1)))

    def test_point_months_carry_coverage_without_changing_numbers(self):
        windows = logic.month_windows(self.NOW, 2)
        days = {PAV: {"rented": D(4), "available": D(0)}}
        money = {PAV: {"paid": D(3000)}}
        months = [{**m, "days": days, "money": money} for m in windows]
        starts = {PAV: datetime(2026, 8, 20, 12, tzinfo=MSK)}
        got = logic.point_months(months, PAV, starts=starts)
        self.assertEqual(got[0]["avg_check"], D("750.00"),
                         "чек тот же: формулы не меняются, меняется подпись")
        self.assertEqual((got[0]["coverage"]["partial"], got[0]["coverage"]["days"]),
                         (True, 4))
        self.assertEqual((got[1]["coverage"]["days"], got[1]["coverage"]["from"]),
                         (12, date(2026, 8, 20)), "август - с открытия точки")
        old = logic.point_months([{"month": date(2026, 9, 1), "days": days,
                                   "money": money}], PAV)
        self.assertNotIn("coverage", old[0], "без окна месяца пометке не из чего")


class TestHistoryStarts(unittest.TestCase):
    T = datetime(2026, 7, 1, 10, tzinfo=UTC)

    def at(self, days):
        return self.T + timedelta(days=days)

    def test_first_row_reaches_back_to_the_status_start(self):
        status = [
            {"id": 1, "bike_id": 1, "to_status": "available", "changed_at": self.at(0)},
            {"id": 2, "bike_id": 2, "to_status": "available", "changed_at": self.at(5)},
            {"id": 3, "bike_id": 3, "to_status": "available", "changed_at": self.at(7)},
            {"id": 4, "bike_id": 2, "to_status": "rented", "changed_at": self.at(9)},
        ]
        places = [
            # Журнал мест велосипеда 1 начался позже его статусов (внедрение):
            # первая строка тянется назад - Павлюхина с нулевого дня.
            {"id": 1, "bike_id": 1, "to_location": PAV, "changed_at": self.at(3)},
            {"id": 2, "bike_id": 2, "to_location": None, "changed_at": self.at(5)},
            {"id": 3, "bike_id": 2, "to_location": DEK, "changed_at": self.at(12)},
            {"id": 4, "bike_id": 1, "to_location": ADO, "changed_at": self.at(20)},
        ]
        got = logic.history_starts(status, places)
        self.assertEqual(got["status"], self.at(0))
        self.assertEqual(got["points"], {PAV: self.at(0), None: self.at(5),
                                         DEK: self.at(12), ADO: self.at(20)},
                         "велосипед 3 без журнала мест - «без точки», но позже")
        self.assertEqual(logic.history_starts([], []), {"status": None, "points": {}})


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestPointPlanInPanel(tpw.PointsWebCase if HAVE_WEB else unittest.TestCase):
    def test_plan_is_saved_shown_and_cleared(self):
        self.story()
        r = self.client.post(f"/plan/points/{self.pav}",
                             data={"plan_rented": "2", "plan_check": ""})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], f"/reports/points/{self.pav}")
        row = next(p for p in tw.run(self.crm.locations()) if p["id"] == self.pav)
        self.assertEqual((row["plan_rented"], row["plan_check"]), (2, None))
        page = self.get_ok(f"/reports/points/{self.pav}")
        self.assertIn("План месяца", page)
        self.assertIn("плана выполнено", page)
        self.assertIn("общий чек плана", page)
        span = logic.month_bounds(date.today().replace(day=1), today=date.today())
        self.assertIn(logic.money(D(1000) * span["days"]), page, "2 × 500 × дни месяца")
        self.assertIn(logic.money(D(15000)), page)
        self.client.post(f"/plan/points/{self.pav}",
                         data={"plan_rented": "3", "plan_check": "650"})
        row = next(p for p in tw.run(self.crm.locations()) if p["id"] == self.pav)
        self.assertEqual((row["plan_rented"], row["plan_check"]), (3, D(650)))
        self.assertIn("план месяца: 3 в аренде", self.get_ok("/locations"))
        self.client.post(f"/plan/points/{self.pav}", data={"plan_rented": ""})
        row = next(p for p in tw.run(self.crm.locations()) if p["id"] == self.pav)
        self.assertIsNone(row["plan_rented"])
        self.assertIn("Плана у точки нет", self.get_ok(f"/reports/points/{self.pav}"))

    def test_bad_plan_is_refused_and_unknown_point_is_404(self):
        r = self.client.post(f"/plan/points/{self.pav}", data={"plan_rented": "много"})
        self.assertIn("целое число от 0 до 9999", self.get_ok(r.headers["location"]))
        r = self.client.post(f"/plan/points/{self.pav}",
                             data={"plan_rented": "2", "plan_check": "-5"})
        self.assertEqual(r.status_code, 303)
        row = next(p for p in tw.run(self.crm.locations()) if p["id"] == self.pav)
        self.assertIsNone(row["plan_rented"], "отказ ничего не записал")
        self.assertEqual(self.client.post("/plan/points/99999",
                                          data={"plan_rented": "1"}).status_code, 404)

    def test_plan_survives_rename(self):
        self.client.post(f"/plan/points/{self.dek}", data={"plan_rented": "5"})
        self.client.post(f"/locations/{self.dek}/rename", data={"name": "Декабристов 1"})
        row = next(p for p in tw.run(self.crm.locations()) if p["id"] == self.dek)
        self.assertEqual((row["name"], row["plan_rented"]), ("Декабристов 1", 5))
        self.assertIn("5 велосипедов в аренде", self.get_ok(f"/reports/points/{self.dek}"))

    def test_plan_is_money_only(self):
        """План - деньги: без права на финансы его не видно и не правится,
        даже с правом на отчёты и справочники."""
        profile = tw.run(self.crm.create_access_profile(
            "Отчёты и точки", {"sections": {"reports": "view", "settings": "edit"}}))
        tw.run(self.crm.create_staff("rep", logic.hash_password("rep-pass-123"),
                                     name="Аналитик", role="manager", profile_id=profile))
        tw.run(self.crm.update_location(self.pav, plan_rented=2))
        self.client.post("/logout")
        self.login("rep", "rep-pass-123")
        self.assertNotIn("План месяца", self.get_ok(f"/reports/points/{self.pav}"))
        self.assertNotIn("план месяца", self.get_ok("/locations"))
        self.assertNotIn(">План<", self.get_ok("/reports/points"))
        self.assertEqual(self.client.post(f"/plan/points/{self.pav}",
                                          data={"plan_rented": "9"}).status_code, 403)

    def test_report_columns_only_with_a_plan(self):
        self.story()
        self.assertNotIn("Выпол&shy;нено", self.get_ok("/reports/points"))
        self.assertNotIn("План", self.table("/reports/points.csv")["Точка"])
        tw.run(self.crm.update_location(self.pav, plan_rented=2))
        tw.run(self.crm.update_location(self.dek, plan_rented=1, plan_check=D(700)))
        self.assertIn("Выпол&shy;нено", self.get_ok("/reports/points"))
        rows = self.table("/reports/points.csv")
        head = rows["Точка"]

        def cell(name, column):
            return rows[name][head.index(column)]

        # 30 дней: план точки в день × 30, выручка точки к нему.
        self.assertEqual((cell(PAV, "План"), cell(PAV, "Выполнено, %")),
                         ("30000,00", "50.0"))
        self.assertEqual((cell(DEK, "План"), cell(DEK, "Выполнено, %")),
                         ("21000,00", "57.1"))
        self.assertEqual(cell(ADO, "План"), "", "у точки без плана - пусто")
        self.assertEqual(cell("ИТОГО", "План"), "51000,00")
        # Период-месяц: план месяца, выполнение к нему.
        month = self.table(f"/reports/points.csv?month={date.today():%Y-%m}")
        days = logic.month_bounds(date.today().replace(day=1), today=date.today())["days"]
        self.assertEqual(month[PAV][month["Точка"].index("План")],
                         f"{1000 * days},00")

    def test_dashboard_sums_point_plans_until_set_explicitly(self):
        for point, rented in ((self.pav, 2), (self.ado, 3)):
            tw.run(self.crm.update_location(point, plan_rented=rented))
        self.assertNotIn("сумма планов", self.get_ok("/"),
                         "у Декабристов плана нет - суммы нет")
        tw.run(self.crm.update_location(self.dek, plan_rented=1, plan_check=D(700)))
        page = self.get_ok("/")
        self.assertIn("6 велосипедов в аренде по", page)
        self.assertIn("сумма планов 3 точек", page)
        self.assertIn('placeholder="сумма точек: 6"', page)
        # Сохранение норм с пустым полем велосипедов сумму не замораживает.
        self.client.post("/plan", data={"plan_rented": "", "plan_check": "500",
                                        "plan_repair": "2"})
        settings = tw.run(self.crm.settings())
        self.assertEqual((settings["plan_rented"], settings["plan_repair"]), ("", "2"))
        self.assertIn("сумма планов 3 точек", self.get_ok("/"))
        # Заданный руками общий план главнее, расхождение видно.
        self.client.post("/plan", data={"plan_rented": "10", "plan_check": "500"})
        page = self.get_ok("/")
        self.assertIn("10 велосипедов в аренде по", page)
        self.assertNotIn("сумма планов", page)
        self.assertIn("Планы точек в сумме — 6 велосипедов", page)

    def plan_form(self):
        """Поля формы плана на сводке - как их отправит браузер."""
        page = self.get_ok("/")
        form = page[page.index('action="/plan"'):]
        form = form[:form.index("</form>")]
        return dict(re.findall(r'name="(plan_\w+)" value="([^"]*)"', form))

    def test_unset_plan_survives_saving_the_form_as_rendered(self):
        """Общий план не задан, план есть не у всех точек: поле пустое, а
        умолчание от парка - подсказкой. Иначе сохранение норм записало бы
        его явным планом, и сумма точек не заступила бы никогда."""
        for point, rented in ((self.pav, 2), (self.ado, 3)):
            tw.run(self.crm.update_location(point, plan_rented=rented))
        self.client.post("/plan", data={"plan_rented": "", "plan_check": "500"})
        form = self.plan_form()
        self.assertEqual(form["plan_rented"], "", "умолчание от парка не в значении")
        self.assertRegex(self.get_ok("/"), r'placeholder="от парка: \d+"')
        self.client.post("/plan", data={**form, "plan_repair": "2"})
        self.assertEqual(tw.run(self.crm.settings())["plan_rented"], "")
        tw.run(self.crm.update_location(self.dek, plan_rented=1))
        self.assertIn("сумма планов 3 точек", self.get_ok("/"))
        # Заданный руками план стоит в поле значением: его и правят.
        self.client.post("/plan", data={**form, "plan_rented": "10"})
        self.assertEqual(self.plan_form()["plan_rented"], "10")

    def test_service_norm_is_the_dashboard_norm(self):
        """План сети - сумма точек: норма «у клиента» на сервисе та же, что
        на сводке, а не умолчание от парка."""
        self.story()
        for point in (self.pav, self.ado, self.dek):
            tw.run(self.crm.update_location(point, plan_rented=20))
        norm = "норма 60 · "
        self.assertIn(norm, self.get_ok("/"))
        self.assertIn(norm, self.get_ok("/service"))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestPartialMonthsInPanel(tpw.PointsWebCase if HAVE_WEB else unittest.TestCase):
    def test_current_month_is_marked_everywhere(self):
        self.story()
        mark = f"неполный · {date.today().day} дн."
        self.assertIn(mark, self.get_ok("/reports"))
        self.assertIn(mark, self.get_ok("/reports/points"))
        self.assertIn(mark, self.get_ok(f"/reports/points/{self.pav}"))

    def test_month_where_a_point_opened_and_history_started(self):
        """Точка открылась 11-го числа прошлого месяца: её ячейка прошлого
        месяца неполная, у соседней точки - полная. Журнал статусов сети
        начался тогда же у первого велосипеда - неполон и месяц отчёта."""
        self.story()
        first = (date.today().replace(day=1) - timedelta(days=1)).replace(day=1)
        opened = datetime.combine(first + timedelta(days=10),
                                  datetime.min.time()).astimezone() + timedelta(hours=12)
        # Остальные точки - задолго до всех шести месяцев таблицы.
        early = datetime.combine(first - timedelta(days=200),
                                 datetime.min.time()).astimezone()
        for row in self.crm.status_log_ + self.crm.location_log_:
            row["changed_at"] = opened if row["bike_id"] == self.d1 else early
        page = self.get_ok("/reports/points")
        self.assertIn(f"неполный · с {opened.date():%d.%m}", page)
        point = self.get_ok(f"/reports/points/{self.dek}")
        self.assertIn(f"неполный · с {opened.date():%d.%m}", point)
        self.assertNotIn("неполный · с", self.get_ok(f"/reports/points/{self.pav}"),
                         "Павлюхина работала весь месяц")
        for row in self.crm.status_log_:
            row["changed_at"] = opened
        self.assertIn(f"неполный · с {opened.date():%d.%m}",
                      self.get_ok("/reports"))


@unittest.skipUnless(HAVE_PG, "pgserver или asyncpg не установлены")
class TestPointPlanOnPostgres(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tz_before = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Moscow"
        time.tzset()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()
        cls.tmp.cleanup()
        if cls.tz_before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = cls.tz_before
        time.tzset()

    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(self.pg.get_uri(), min_size=1, max_size=3,
                                              init=_init_connection)
        await self.pool.execute("drop schema if exists crm cascade; "
                                "drop schema if exists bot cascade")
        await Database(self.pool).apply_schema(SCHEMA)
        self.crm = CrmDB(self.pool)

    async def asyncTearDown(self):
        await self.pool.close()

    async def point(self, name):
        return next(p for p in await self.crm.locations() if p["name"] == name)

    async def test_plan_lives_in_the_row_and_survives_rename_and_reapply(self):
        pav = await self.point(PAV)
        self.assertIsNone(pav["plan_rented"])
        await self.crm.update_location(pav["id"], plan_rented=12, plan_check=D("550"))
        self.assertEqual(await self.crm.rename_location(pav["id"], "Павлюхина 97А"), "ok")
        await Database(self.pool).apply_schema(SCHEMA)
        row = await self.point("Павлюхина 97А")
        self.assertEqual((row["plan_rented"], row["plan_check"]), (12, D("550.00")))
        plan = logic.point_plan(row, check=D(500))
        self.assertEqual(plan["per_day"], D("6600.00"))
        await self.crm.update_location(pav["id"], plan_rented=None, plan_check=None)
        self.assertIsNone((await self.point("Павлюхина 97А"))["plan_rented"])
        for bad in ({"plan_rented": -1}, {"plan_check": D(0)}):
            with self.assertRaises(asyncpg.CheckViolationError, msg=bad):
                await self.crm.update_location(pav["id"], **bad)

    def test_demo_point_plans_add_up_to_the_demo_plan(self):
        """Демо показывает планы точек, и сводка с общим планом не спорит:
        строки расхождения на показе быть не должно."""
        from app.demo import seed
        self.assertEqual(sum(seed.POINT_PLANS.values()), int(seed.PLAN["plan_rented"]))

    async def test_history_starts_match_the_pure_mirror(self):
        """База и logic.history_starts на тех же журналах - одно и то же:
        первая строка места тянется к началу статусов, велосипед без
        журнала мест - «без точки», переименование уносит ключ с собой."""
        empty = await self.crm.history_starts()
        self.assertEqual(empty, {"status": None, "points": {}})
        old = await self.crm.create_bike(code="O-1", model="M", location=PAV)
        bare = await self.crm.create_bike(code="N-1", model="M")
        await self.crm.update_bike(bare, location=ADO, by="staff:a")
        lone = await self.crm.create_bike(code="L-1", model="M", location=ADO)
        # Статусы старше журнала мест (велосипед до внедрения точек) и
        # велосипед вовсе без журнала мест.
        await self.pool.execute(
            "update crm.bike_status_log set changed_at = changed_at - interval '40 days' "
            "where bike_id = $1", old)
        await self.pool.execute("delete from crm.bike_location_log where bike_id = $1",
                                lone)
        await self.pool.execute(
            "update crm.bike_status_log set changed_at = changed_at - interval '3 days' "
            "where bike_id = $1", lone)
        self.assertEqual(await self.crm.rename_location((await self.point(PAV))["id"],
                                                        "Сокол"), "ok")
        got = await self.crm.history_starts()
        status = [dict(r) for r in await self.pool.fetch(
            "select id, bike_id, to_status, changed_at from crm.bike_status_log")]
        places = [dict(r) for r in await self.pool.fetch(
            "select id, bike_id, to_location, changed_at from crm.bike_location_log")]
        self.assertEqual(got, logic.history_starts(status, places))
        first_old = min(r["changed_at"] for r in status if r["bike_id"] == old)
        self.assertEqual(got["points"]["Сокол"], first_old)
        self.assertEqual(got["status"], first_old)
        self.assertNotIn(PAV, got["points"])
        self.assertEqual(got["points"][None],
                         min(r["changed_at"] for r in status if r["bike_id"] == lone),
                         "без журнала мест - «без точки» с первого статуса")


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
