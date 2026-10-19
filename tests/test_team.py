"""Команда (app/crm/team.py): роль сотрудника по доступу, показатели и
план своей роли (мастер - ремонт парка и сторонний, в срок, работы и
выручка ремонта; администратор - выдачи, продления, сдачи, клиенты,
деньги), факт из своих таблиц, темп и расчёт зарплаты по условиям роли;
страницы «Команда» и карточка."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import logic, service, team

try:
    from tests.test_web import HAVE_WEB, WebCase, run
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False
    WebCase = unittest.TestCase                        # type: ignore[misc,assignment]

TODAY = date(2026, 10, 15)


def perms(code):
    """Права встроенной роли или роли «только задачи» по коду."""
    for got, _name, value, _built in logic.BUILT_IN_PROFILES + logic.TASK_PROFILES:
        if got == code:
            return {"profile_code": code, "perms": value}
    raise KeyError(code)


class TestRoles(unittest.TestCase):
    """Роль в «Команде» - по доступу, а не по колонке staff.role."""

    def test_roles_of(self):
        self.assertEqual(team.roles_of(perms("owner")), (), "владелец - не исполнитель")
        self.assertEqual(team.roles_of(perms("manager")), ("admin",))
        self.assertEqual(team.roles_of(perms("tech")), ("master",))
        self.assertEqual(team.roles_of(perms("tasks_operator")), ("admin",))
        self.assertEqual(team.roles_of(perms("tasks_tech")), ("master",))
        both = {"perms": {"sections": {"service": "edit", "issue": "edit"}}}
        self.assertEqual(team.roles_of(both), ("master", "admin"))
        self.assertEqual(team.roles_of({"perms": {"sections": {"cash": "edit"}}}),
                         ("admin",), "касса - работа администратора точки")
        self.assertEqual(team.roles_of({"perms": {"sections": {"service": "view",
                                                               "rentals": "view"}}}),
                         (), "только смотрит - ни мастер, ни администратор")
        self.assertEqual(team.role_words(("master", "admin")), "мастер и администратор")

    def test_metrics_and_terms_by_role(self):
        self.assertEqual([m.fact for m in team.metrics_for(("master",))],
                         ["orders_own", "orders_ext", "orders_works", "repair_revenue",
                          "tasks"])
        self.assertEqual([m.fact for m in team.metrics_for(("admin",))],
                         ["issues", "extensions", "returns", "new_clients", "payments",
                          "tasks"])
        self.assertEqual([m.fact for m in team.metrics_for(())], ["tasks"])
        self.assertEqual(len(team.metrics_for(("admin", "master"))), len(team.METRICS),
                         "обе роли - все показатели, мастер первым")
        self.assertEqual(team.metrics_for(("admin", "master"))[0].fact, "orders_own")
        self.assertEqual(team.terms_for(("master",)), ("salary_base", "order_pct"))
        self.assertEqual(team.terms_for(("admin",)),
                         ("salary_base", "per_issue", "revenue_pct"))
        self.assertEqual(team.terms_for(()), ("salary_base",))

    def test_db_columns_match_metrics(self):
        """Колонки плана в базе - те же, что показатели команды: новое
        число без колонки молча не сохранилось бы."""
        try:
            from app.crm import db
        except ImportError:                            # pragma: no cover
            self.skipTest("asyncpg не установлен")
        self.assertEqual(set(db.STAFF_PERIOD_PLAN_FIELDS), set(team.PERIOD_FIELDS))
        self.assertEqual(db.STAFF_PLAN_FIELDS, frozenset(team.PLAN_FIELDS))
        schema = (Path(__file__).resolve().parent.parent / "schema.sql").read_text("utf-8")
        for key in team.PERIOD_FIELDS:
            for table in ("staff_plans", "staff_period_plans"):
                self.assertTrue(f"alter table crm.{table} add column if not exists {key} "
                                in schema or f"  {key} " in schema, f"{table}.{key}")


class TestTeamRules(unittest.TestCase):
    def test_month_of(self):
        self.assertEqual(team.month_of("2026-09", today=TODAY), date(2026, 9, 1))
        self.assertEqual(team.month_of("2027-01", today=TODAY), date(2026, 10, 1),
                         "будущий месяц - текущий")
        self.assertEqual(team.month_of("мусор", today=TODAY), date(2026, 10, 1))
        self.assertEqual(team.month_of("0001-05", today=TODAY), date(2026, 10, 1),
                         "полгода назад от 0001 года - переполнение даты")
        self.assertEqual(team.month_name(date(2026, 10, 1)), "октябрь 2026")

    def test_salary_by_role(self):
        """Условие чужой роли не начисляется: мастеру - % от суммы работ,
        администратору - ставка за выдачу и % от принятых денег."""
        terms = {"salary_base": D("20000"), "per_issue": D("300"), "order_pct": D("30"),
                 "revenue_pct": D("2")}
        facts = {"issues": 10, "orders_works": D("10000"), "payments": D("40000"),
                 "repair_revenue": D("10000"), "tasks_pay": D("500")}
        admin = team.salary(terms, facts, ("admin",))
        self.assertEqual((admin["issues"], admin["orders"], admin["revenue"], admin["tasks"]),
                         (D("3000.00"), D("0.00"), D("800.00"), D("500.00")),
                         "2 % от принятых денег; выручка ремонта - не его")
        self.assertEqual(admin["total"], D("24300.00"))
        master = team.salary(terms, facts, ("master",))
        self.assertEqual((master["issues"], master["orders"], master["revenue"]),
                         (D("0.00"), D("3000.00"), D("0.00")))
        self.assertEqual(master["total"], D("23500.00"))
        self.assertEqual(team.salary(terms, facts, ())["total"], D("20500.00"),
                         "без роли - оклад и задачи")
        both = team.salary(terms, facts, ("master", "admin"))
        self.assertEqual(both["total"], D("27300.00"))
        self.assertEqual(team.salary(None, {})["total"], D("0.00"))

    def test_on_time(self):
        self.assertIsNone(team.on_time({"orders": 0}), "нарядов нет - не «0 %»")
        got = team.on_time({"orders": 3, "orders_on_time": 2})
        self.assertEqual((got["percent"], got["done"], got["of"], got["late"]),
                         (67, 2, 3, 1))

    def test_progress_pace(self):
        plan = {"plan_issues": 30, "plan_revenue": D("100000")}
        facts = {"issues": 10, "payments": D("60000")}
        rows = {m["key"]: m for m in team.progress(plan, facts, days=30, passed=15,
                                                   roles=("admin",))}
        self.assertEqual((rows["issues"]["percent"], rows["issues"]["pace"]), (33, 15))
        self.assertFalse(rows["issues"]["ahead"], "10 из 30 к середине месяца - отстаёт")
        self.assertTrue(rows["payments"]["ahead"], "план денег администратора - принятые")
        self.assertIsNone(rows["returns"]["plan"], "без плана - просто число")
        self.assertNotIn("orders_own", rows, "показателей мастера у администратора нет")
        self.assertEqual(set(rows), {"issues", "extensions", "returns", "new_clients",
                                     "payments", "tasks"})

    def test_rows_skip_idle_disabled(self):
        people = [{"id": 1, "name": "Анна", "active": True, **perms("manager")},
                  {"id": 2, "name": "Ушёл", "active": False, **perms("manager")},
                  {"id": 3, "name": "Ушёл с фактом", "active": False, **perms("tech")}]
        facts = {3: {"orders": 2, "orders_own": 2}}
        got = team.rows(people, facts, {1: {"plan_issues": 10, "own": False}},
                        days=31, passed=15)
        self.assertEqual([r["name"] for r in got], ["Анна", "Ушёл с фактом"])
        self.assertTrue(got[0]["inherited"], "условия прошлого месяца помечены")

    def test_sections_by_role(self):
        """Мастера и администраторы - своими таблицами со своими колонками;
        обе роли - в обеих; владелец ни в одной, а сделанное им - строкой."""
        people = [{"id": 1, "name": "Анна", **perms("manager")},
                  {"id": 2, "name": "Булат", **perms("tech")},
                  {"id": 3, "name": "Вера",
                   "perms": {"sections": {"service": "edit", "issue": "edit"}}},
                  {"id": 4, "name": "Владелец", **perms("owner")},
                  {"id": 5, "name": "Бухгалтер", "perms": {"sections": {"finance": "view"}}}]
        facts = {1: {"issues": 3, "payments": D("9000"), "returns": 1},
                 2: {"orders": 4, "orders_own": 3, "orders_ext": 1, "orders_on_time": 3,
                     "orders_works": D("6000")},
                 3: {"orders": 1, "orders_own": 1, "orders_on_time": 0, "issues": 1},
                 4: {"payments": D("500"), "issues": 1}}
        plans = {2: {"plan_orders_own": 6, "plan_orders_ext": 2}}
        team_rows = team.rows(people, facts, plans, days=30, passed=15)
        got = {s["code"]: s for s in team.sections(team_rows)}
        self.assertEqual(list(got), ["master", "admin", "other"])
        self.assertEqual([r["name"] for r in got["master"]["rows"]], ["Булат", "Вера"])
        self.assertEqual([r["name"] for r in got["admin"]["rows"]], ["Анна", "Вера"])
        self.assertEqual([r["name"] for r in got["other"]["rows"]], ["Бухгалтер"])
        self.assertEqual([m.fact for m in got["other"]["metrics"]], ["tasks"])
        bulat = got["master"]["rows"][0]
        self.assertEqual([c["key"] for c in bulat["cells"]],
                         ["orders_own", "orders_ext", "orders_works", "repair_revenue",
                          "tasks"])
        self.assertEqual(bulat["percent"], 50, "парк 3 из 6 и сторонний 1 из 2")
        self.assertTrue(got["master"]["rows"][1]["both"])
        self.assertEqual(got["master"]["total"]["orders_own"], 4)
        self.assertEqual(got["master"]["total"]["on_time"]["percent"], 60)
        self.assertEqual(got["admin"]["total"]["payments"], D("9000.00"),
                         "деньги владельца в таблицу администраторов не идут")
        owners = team.owners(team_rows)
        self.assertEqual([o["name"] for o in owners], ["Владелец"])
        self.assertEqual([d["title"] for d in owners[0]["done"]], ["Выдачи", "Принял денег"])
        total = team.totals(team_rows)
        self.assertEqual((total["issues"], total["orders"], total["payments"]),
                         (4, 5, D("9000.00")), "Вера считается один раз, владелец - нет")
        with_roles = [r for r in team_rows if r["name"] != "Бухгалтер"]
        self.assertNotIn("other", {s["code"] for s in team.sections(with_roles)},
                         "«Остальные» - только когда такие есть")

    def test_parse_plan(self):
        got, problem = team.parse_plan({"plan_issues": "20", "salary_base": "25 000",
                                        "order_pct": "30,5"})
        self.assertIsNone(problem)
        self.assertEqual((got["plan_issues"], got["salary_base"], got["order_pct"],
                          got["plan_orders_own"], got["per_issue"]),
                         (20, D("25000.00"), D("30.50"), None, D("0.00")))
        self.assertIn("0 до 100", team.parse_plan({"revenue_pct": "120"})[1])
        self.assertIn("цифрами", team.parse_plan({"plan_issues": "много"})[1])

    def test_parse_period_plan(self):
        """Неделя и день - только числа плана роли, условий оплаты нет;
        пустое поле - «как в плане месяца» (None), а не ноль."""
        keys = team.form_keys("week", True, ("admin",))
        got, problem = team.parse_plan({"plan_issues": "5", "salary_base": "99999"}, keys)
        self.assertIsNone(problem)
        self.assertEqual(got, {"plan_issues": 5, "plan_extensions": None,
                               "plan_returns": None, "plan_clients": None,
                               "plan_revenue": None, "plan_tasks": None})
        self.assertEqual(team.parse_plan({"plan_tasks": "-1"}, keys)[1],
                         "Числа плана - не меньше нуля.")

    def test_parse_plan_whole_counts(self):
        """Штуки - целыми: «1,3» не усекается молча до 1 (доля месяца бывает
        дробной, свой план - нет), «5,0» - это 5; деньги - с копейками."""
        got, problem = team.parse_plan({"plan_tasks": "1,3"}, team.PERIOD_FIELDS)
        self.assertIsNone(got)
        self.assertEqual(problem, "Штуки плана - целым числом.")
        self.assertIsNotNone(team.parse_plan({"plan_orders_ext": "2.5"})[1], "и у месяца")
        got, problem = team.parse_plan({"plan_issues": "5,0", "plan_works": "1500,5"},
                                       team.PERIOD_FIELDS)
        self.assertIsNone(problem)
        self.assertEqual((got["plan_issues"], got["plan_works"]), (5, D("1500.50")))

    def test_form_keys_by_role(self):
        self.assertEqual(team.form_keys("month", True, ("master",)),
                         ("plan_orders_own", "plan_orders_ext", "plan_works",
                          "plan_repair_revenue", "plan_tasks", "salary_base", "order_pct"))
        self.assertEqual(team.form_keys("month", True, ("admin",)),
                         ("plan_issues", "plan_extensions", "plan_returns", "plan_clients",
                          "plan_revenue", "plan_tasks", "salary_base", "per_issue",
                          "revenue_pct"))
        self.assertEqual(team.form_keys("day", False, ("master",)),
                         ("plan_orders_own", "plan_orders_ext", "plan_tasks"))
        self.assertEqual(team.form_keys("month", False, ()), ("plan_tasks",))

    def test_keep_saved(self):
        """Форма несёт не все числа (рубли без права, чужая роль), а запись
        пишет строку целиком: недостающее - прежнее из базы."""
        saved = {"plan_issues": 10, "plan_revenue": D("90000"), "salary_base": D("20000"),
                 "per_issue": D("300"), "order_pct": D("0"), "revenue_pct": D("2"),
                 "plan_orders_own": 7, "own": False, "staff_id": 1}
        got = team.keep_saved("month", {"plan_issues": 12, "plan_tasks": None}, saved)
        self.assertEqual((got["plan_issues"], got["plan_revenue"], got["salary_base"],
                          got["revenue_pct"], got["plan_orders_own"], got["plan_tasks"]),
                         (12, D("90000"), D("20000"), D("2"), 7, None))
        self.assertEqual(set(got), set(team.PLAN_FIELDS), "строка целиком, без служебных")
        got = team.keep_saved("month", {"plan_issues": 1}, None)
        self.assertEqual((got["plan_revenue"], got["salary_base"]), (None, D("0.00")),
                         "плана не было: денег не планируется, условия - ноль")
        got = team.keep_saved("week", {"plan_issues": 3}, {"plan_revenue": D("50000")})
        self.assertEqual((got["plan_issues"], got["plan_revenue"]), (3, D("50000")))
        self.assertEqual(set(got), set(team.PERIOD_FIELDS), "у недели условий нет")


class TestPeriodPlans(unittest.TestCase):
    """План недели и дня: доля плана месяца по дням, свой план главнее."""

    OCT, SEP = date(2026, 10, 1), date(2026, 9, 1)

    def test_period_start_and_span(self):
        self.assertEqual(team.period_start("week", "2026-10-15", today=TODAY),
                         date(2026, 10, 12), "неделя - с понедельника")
        self.assertEqual(team.period_start("week", "мусор", today=TODAY), date(2026, 10, 12))
        self.assertEqual(team.period_start("day", "0001-01-01", today=TODAY), TODAY,
                         "стрелка назад от первого дня эры - переполнение")
        self.assertEqual(team.period_start("day", "2026-10-14", today=TODAY),
                         date(2026, 10, 14))
        self.assertEqual(team.period_start("day", "2027-05-01", today=TODAY), TODAY,
                         "далёкое будущее - сегодня")
        self.assertEqual(team.period_start("day", "2026-11-20", today=TODAY),
                         date(2026, 11, 20), "ближнее будущее планируют заранее")
        self.assertEqual(team.period_kind("week"), "week")
        self.assertEqual(team.period_kind("year"), "month")
        week = team.period_span("week", date(2026, 10, 12), today=TODAY)
        self.assertEqual((week["days"], week["passed"], week["is_current"]), (7, 4, True))
        self.assertEqual(week["title"], "12–18 октября")
        edge = team.period_span("week", date(2026, 9, 28), today=TODAY)
        self.assertEqual((edge["passed"], edge["title"]), (7, "28 сентября – 4 октября"))
        ahead = team.period_span("day", date(2026, 10, 20), today=TODAY)
        self.assertEqual((ahead["passed"], ahead["future"]), (0, True))
        self.assertEqual(team.period_span("day", TODAY, today=TODAY)["title"],
                         "четверг, 15 октября")
        self.assertEqual(team.period_title("week", date(2026, 12, 28),
                                           today=date(2027, 1, 2)),
                         "28 декабря 2026 – 3 января 2027", "через Новый год - год у обеих")
        self.assertEqual(team.period_title("week", date(2026, 12, 28), today=TODAY),
                         "28 декабря 2026 – 3 января 2027")
        self.assertEqual(team.period_title("week", date(2025, 10, 13), today=TODAY),
                         "13–19 октября 2025")
        self.assertEqual(team.period_months("week", date(2026, 9, 28)), [self.SEP, self.OCT])

    def test_month_share_by_days(self):
        plans = {self.OCT: {"plan_issues": 62, "plan_revenue": D("155000"),
                            "plan_tasks": 40, "plan_orders_own": None}}
        week = team.month_share(plans, date(2026, 10, 12), 7)
        self.assertEqual((week["plan_issues"], week["plan_revenue"], week["plan_orders_own"]),
                         (D("14.0"), D("35000.00"), None))
        day = team.month_share(plans, TODAY, 1)
        self.assertEqual((day["plan_issues"], day["plan_tasks"]), (D("2.0"), D("1.3")),
                         "40 задач в октябре - 1,3 в день, а не «1»")
        self.assertEqual(team.month_share({}, TODAY, 1)["plan_issues"], None)

    def test_week_across_month_boundary(self):
        """Неделя 28.09-04.10: три дня берут долю сентября, четыре - октября."""
        plans = {self.SEP: {"plan_issues": 30, "plan_orders_own": 15},
                 self.OCT: {"plan_issues": 62}}
        got = team.month_share(plans, date(2026, 9, 28), 7)
        self.assertEqual(got["plan_issues"], D("11.0"), "3 × 1 + 4 × 2")
        self.assertEqual(got["plan_orders_own"], D("1.5"), "в октябре ремонт не планируется")

    def test_own_plan_wins_per_metric(self):
        derived = {"plan_issues": D("9.0"), "plan_returns": D("3.5"), "plan_tasks": D("2.0"),
                   "plan_revenue": None}
        plan = team.period_plan({"plan_issues": 5, "plan_returns": None, "plan_tasks": 0,
                                 "plan_revenue": None}, derived)
        self.assertEqual((plan["plan_issues"], plan["plan_returns"], plan["plan_tasks"],
                          plan["plan_revenue"]), (5, D("3.5"), 0, None))
        self.assertEqual(plan["sources"], {"plan_issues": "own", "plan_returns": "month",
                                           "plan_tasks": "own"})
        rows = {m["key"]: m for m in team.progress(plan, {"issues": 2}, days=7, passed=4,
                                                   roles=("admin",))}
        self.assertIsNone(rows["tasks"]["percent"], "свой ноль - не планируется")
        self.assertEqual((rows["issues"]["source"], rows["returns"]["source"]),
                         ("own", "month"))
        self.assertIsNone(team.period_plan(None, {"plan_issues": None}))

    def test_period_plans_for_team(self):
        own = {2: {"plan_issues": 7}}
        months = {self.OCT: {1: {"plan_issues": 31, "own": True},
                             3: {"salary_base": D("20000"), "plan_issues": None}}}
        got = team.period_plans("day", TODAY, own, months)
        self.assertEqual(got[1]["plan_issues"], D("1.0"))
        self.assertEqual(got[1]["sources"], {"plan_issues": "month"})
        self.assertEqual(got[2]["plan_issues"], 7, "без плана месяца - только свой")
        self.assertNotIn(3, got, "одни условия оплаты - не план")
        people = [{"id": 1, "name": "Анна", **perms("manager")},
                  {"id": 2, "name": "Борис", **perms("manager")}]
        team_rows = team.rows(people, {1: {"issues": 1}}, got, days=1, passed=1,
                              open_day=True)
        by_name = {r["name"]: r for r in team_rows}
        self.assertEqual((by_name["Анна"]["percent"], by_name["Анна"]["source"]),
                         (100, "month"))
        self.assertTrue(by_name["Анна"]["ahead"])
        self.assertFalse(by_name["Борис"]["behind"], "день идёт: 0 из 7 - ещё не отставание")

    def test_row_green_only_when_all_on_pace(self):
        """Идущий день: выдача сделана, денег ноль - не зелёный, хоть и не
        красный; зелёный - только когда в темпе всё запланированное."""
        plans = {1: {"plan_issues": D("1.0"), "plan_revenue": D("2903")}}
        who = [{"id": 1, "name": "Волков", **perms("manager")}]
        row = team.rows(who, {1: {"issues": 1}}, plans, days=1, passed=1, open_day=True)[0]
        self.assertEqual(row["percent"], 50)
        self.assertEqual((row["ahead"], row["behind"]), (False, False))
        row = team.rows(who, {1: {"issues": 1, "payments": D("3000")}}, plans,
                        days=1, passed=1, open_day=True)[0]
        self.assertTrue(row["ahead"])
        row = team.rows([{"id": 1, "name": "Без плана"}], {}, {}, days=1, passed=1)[0]
        self.assertFalse(row["ahead"])

    def test_progress_fractional_and_pace(self):
        plan = {"plan_tasks": D("1.3")}
        row = {m["key"]: m for m in team.progress(plan, {"tasks": 1}, days=1, passed=1,
                                                  open_day=True)}["tasks"]
        self.assertEqual((row["plan_text"], row["percent"], row["ahead"]), ("1,3", 77, None))
        row = {m["key"]: m for m in team.progress(plan, {"tasks": 1}, days=1,
                                                  passed=1)}["tasks"]
        self.assertFalse(row["ahead"], "прошедший день: не сделано - отстал")
        row = {m["key"]: m for m in team.progress({"plan_issues": 10}, {}, days=7,
                                                  passed=0)}["issues"]
        self.assertIsNone(row["ahead"], "неделя не началась - темпа нет")
        self.assertEqual(row["fact_text"], "0")

    def test_history(self):
        months = team.history_months(date(2026, 10, 1))
        self.assertEqual((len(months), months[0], months[-1]),
                         (6, date(2026, 5, 1), date(2026, 10, 1)))
        data = [(m, None, {}) for m in months[:3]]
        data += [(months[3], {"plan_orders_own": 10, "own": True}, {"orders_own": 12}),
                 (months[4], None, {}),
                 (months[5], {"plan_orders_own": 10, "own": False,
                              "salary_base": D("1000")},
                  {"orders_own": 3, "orders": 4, "orders_on_time": 3})]
        got = team.history(data, today=TODAY, roles=("master",))
        self.assertEqual([h["first"] for h in got],
                         [date(2026, 10, 1), date(2026, 9, 1), date(2026, 8, 1)],
                         "новые сверху; до первой работы - не показаны, пустой между - да")
        self.assertEqual((got[2]["metrics"]["orders_own"]["percent"],
                          got[2]["metrics"]["orders_own"]["ahead"]), (120, True))
        self.assertNotIn("issues", got[0]["metrics"], "колонки - своей роли")
        self.assertEqual(got[0]["on_time"]["percent"], 75)
        self.assertTrue(got[0]["current"] and got[0]["inherited"])
        self.assertEqual(got[0]["pay"]["total"], D("1000.00"))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestTeamPages(WebCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def make_anna(self):
        profile = run(self.crm.access_profile_by_code("manager"))
        return run(self.crm.create_staff("anna", logic.hash_password("password-1"), "Анна",
                                         "manager", profile["id"], location="Адоратского"))

    def make_bulat(self):
        profile = run(self.crm.access_profile_by_code("tech"))
        return run(self.crm.create_staff("bulat", logic.hash_password("password-3"),
                                         "Булат", "manager", profile["id"]))

    def test_team_month_and_member(self):
        anna = self.make_anna()
        run(self.crm.create_rental(client_id=self.client_id, bike_id=self.bike_id,
                                   tariff_id=self.tariff_id, tariff_name="Неделя",
                                   period_days=7, price=D(3000), billing="auto",
                                   started_on=date.today(), contract_no=None,
                                   created_by="staff:anna"))
        run(self.crm.add_ledger(client_id=self.client_id, kind="payment", amount=D(3000),
                                created_by="staff:anna"))
        run(self.crm.add_ledger(client_id=self.client_id, kind="payment", amount=D(999),
                                created_by="bank"))
        task = run(self.crm.create_task(title="Напомнить о продлении", by="staff:admin",
                                        assignee_id=anna, pay=D(200)))
        run(self.crm.finish_task(task, anna))
        self.login()
        r = self.client.post(f"/team/{anna}/plan", data={
            "month": f"{date.today():%Y-%m}", "plan_issues": "10", "salary_base": "20000",
            "per_issue": "300", "revenue_pct": "10", "order_pct": "50",
            "plan_orders_own": "99"})
        self.assertEqual(r.status_code, 303)
        plan = run(self.crm.staff_plans(date.today().replace(day=1)))[anna]
        self.assertEqual((plan["order_pct"], plan["plan_orders_own"]), (D(0), None),
                         "условия и план чужой роли из запроса не читаются")
        page = self.get_ok("/team")
        self.assertIn('id="role-admin"', page)
        self.assertIn("Анна", page)
        self.assertIn('1 <small class="muted">/ 10</small>', page, "выдача против плана")
        self.assertIn("Мастеров нет", page, "пустая таблица ролей - объяснением")
        # 20000 + 300 + 10 % от 3000 + 200 за задачу; платёж банка не её
        self.assertIn("20 800 ₽", page)
        card = self.get_ok(f"/team/{anna}")
        self.assertIn("Напомнить о продлении", card)
        self.assertIn("План и условия на", card)
        self.assertIn('name="plan_extensions"', card)
        self.assertNotIn('name="plan_orders_own"', card, "форма - по роли")
        self.assertNotIn('name="order_pct"', card, "% от работ - мастеру")
        self.assertNotIn("Ремонт парка", card)
        # следующий месяц живёт по этим условиям, пока их не поменяют
        nxt = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
        plans = run(self.crm.staff_plans(nxt))
        self.assertEqual((plans[anna]["salary_base"], plans[anna]["own"]),
                         (D("20000.00"), False))
        # сотрудник видит свой месяц в «Задачах дня»
        self.client.post("/logout")
        self.login("anna", "password-1")
        my = self.get_ok("/my")
        self.assertIn("Мой ", my)
        self.assertIn("20 800 ₽", my)
        self.assertIn("продления", my, "показатели роли - и без плана")
        self.assertEqual(self.client.get("/team").status_code, 403, "Команда - не ей")

    def test_master_facts_and_page(self):
        """Мастер: наш и сторонний ремонт порознь, в срок - по сроку
        ремонта, выручка - оплаченные клиентом наряды; форма - его роли, а
        план администратора у того же человека сохранение не стирает."""
        bulat = self.make_bulat()
        now = datetime.now(UTC)
        own = run(self.crm.create_work_order(
            bike_id=self.bike_id, payer="own", client_id=None, complaint="колодки",
            object_note=None, tech_id=bulat, estimate=D(0), created_by="staff:admin"))
        ext = run(self.crm.create_work_order(
            bike_id=None, payer="client", client_id=self.client_id, complaint="мотор",
            object_note="Самокат Kugoo", tech_id=bulat, estimate=D(0),
            created_by="staff:admin"))
        run(self.crm.update_work_order(own, status="done", total=D(1500), cost=D(500),
                                       opened_at=now - timedelta(days=1), closed_at=now))
        run(self.crm.update_work_order(ext, status="done", total=D(3000), cost=D(1000),
                                       opened_at=now - timedelta(days=6), closed_at=now,
                                       paid_at=now))
        # свой ремонт без цены клиенту: сумма работ - ноль, а не минус 700
        free = run(self.crm.create_work_order(
            bike_id=self.bike_id, payer="own", client_id=None, complaint="камера",
            object_note=None, tech_id=bulat, estimate=D(0), created_by="staff:admin"))
        run(self.crm.update_work_order(free, status="done", total=D(0), cost=D(700),
                                       opened_at=now - timedelta(days=1), closed_at=now))
        since = now - timedelta(days=1)
        facts = run(self.crm.team_facts(since, now + timedelta(minutes=1)))[bulat]
        self.assertEqual((facts["orders"], facts["orders_own"], facts["orders_ext"],
                          facts["orders_on_time"], facts["orders_works"],
                          facts["repair_revenue"]),
                         (3, 2, 1, 2, D(3000), D(3000)))
        facts = run(self.crm.team_facts(since, now + timedelta(minutes=1),
                                        norm_days=10))[bulat]
        self.assertEqual(facts["orders_on_time"], 3, "общий срок из настройки")
        first = date.today().replace(day=1)
        run(self.crm.set_staff_plan(bulat, first, by="t", plan_issues=5))
        self.login()
        r = self.client.post(f"/team/{bulat}/plan", data={
            "month": f"{first:%Y-%m}", "plan_orders_own": "20", "plan_orders_ext": "4",
            "salary_base": "30000", "order_pct": "30"})
        self.assertEqual(r.status_code, 303)
        plan = run(self.crm.staff_plans(first))[bulat]
        self.assertEqual((plan["plan_orders_own"], plan["plan_orders_ext"],
                          plan["plan_issues"], plan["order_pct"]),
                         (20, 4, 5, D("30.00")), "план другой роли не стёрт")
        page = self.get_ok("/team")
        self.assertIn('id="role-master"', page)
        self.assertIn("Сторонний ремонт", page)
        self.assertIn('2 <small class="muted">/ 20</small>', page)
        self.assertIn('<b class="bad">67 %</b>', page, "в срок 2 из 3")
        card = self.get_ok(f"/team/{bulat}")
        self.assertIn("Ремонт парка", card)
        self.assertIn("Самокат Kugoo", card)
        self.assertIn("+3 дн.", card, "6 суток при сроке 3")
        self.assertIn('name="plan_orders_ext"', card)
        self.assertNotIn('name="plan_issues"', card)
        self.assertNotIn('name="per_issue"', card)
        self.assertIn("% от суммы работ", card)
        self.assertNotIn("За выдачи", card, "начисление - условия его роли")
        # 30000 + 30 % от 3000
        self.assertIn("30 900 ₽", card)
        self.client.post("/logout")
        self.login("bulat", "password-3")
        my = self.get_ok("/my")
        self.assertIn("ремонт парка", my)
        self.assertIn("в срок ремонта", my)

    def test_admin_extensions_returns_clients(self):
        """Администратор: продление - платёж по идущей аренде, начатой
        раньше; два платежа за день - одно продление; сдача - закрытие с
        возвратом техники; новые клиенты - заведённые им карточки."""
        anna = self.make_anna()
        start = date.today() - timedelta(days=3)
        rid = run(service.open_rental(
            self.crm, client=run(self.crm.client(self.client_id)),
            bike=run(self.crm.bike(self.bike_id)), tariff=run(self.crm.tariff(self.tariff_id)),
            started_on=start, contract_no=None, by="staff:anna"))
        for amount in (D(1000), D(2000)):
            run(self.crm.add_ledger(client_id=self.client_id, kind="payment", amount=amount,
                                    rental_id=rid, created_by="staff:anna"))
        run(self.crm.create_client(full_name="Петров Пётр", phone="+79990000001",
                                   created_by="staff:anna"))
        run(service.close_rental(self.crm, run(self.crm.rental(rid)),
                                 closed_on=date.today(), note=None, by="staff:anna"))
        now = datetime.now(UTC)
        facts = run(self.crm.team_facts(now - timedelta(hours=1),
                                        now + timedelta(minutes=1)))[anna]
        self.assertEqual((facts["issues"], facts["extensions"], facts["returns"],
                          facts["new_clients"], facts["payments"]),
                         (1, 1, 1, 1, D(3000)))
        # платёж в день начала аренды - выдача, не продление
        other = run(self.crm.create_client(full_name="Сидоров", phone="+79990000002"))
        bike2 = run(self.crm.create_bike(code="B-2", model="Kugoo V3"))
        rid2 = run(service.open_rental(
            self.crm, client=run(self.crm.client(other)), bike=run(self.crm.bike(bike2)),
            tariff=run(self.crm.tariff(self.tariff_id)), started_on=date.today(),
            contract_no=None, by="staff:anna"))
        run(self.crm.add_ledger(client_id=other, kind="payment", amount=D(3000),
                                rental_id=rid2, created_by="staff:anna"))
        # потеря - не сдача
        run(service.declare_theft(self.crm, run(self.crm.rental(rid2)), note=None,
                                  by="staff:anna"))
        facts = run(self.crm.team_facts(now - timedelta(hours=1),
                                        datetime.now(UTC) + timedelta(minutes=1)))[anna]
        self.assertEqual((facts["extensions"], facts["returns"]), (1, 1))
        self.login()
        card = self.get_ok(f"/team/{anna}")
        self.assertIn("Сдачи", card)
        self.assertIn("Петров Пётр", card)
        self.assertIn("из них продлений 2", card, "оба платежа помечены продлением")

    def test_owner_not_in_tables(self):
        """Владелец не исполнитель: ни в таблице ролей, ни плана; сделанное
        им самим - строкой под таблицами. План ему не сохраняется."""
        self.make_anna()
        run(self.crm.add_ledger(client_id=self.client_id, kind="payment", amount=D(700),
                                created_by="staff:admin"))
        owner = run(self.crm.staff_by_login("admin"))
        self.login()
        page = self.get_ok("/team")
        rows = page.split('id="role-admin"')[1].split("</section>")[0]
        self.assertNotIn(f'href="/team/{owner["id"]}', rows)
        self.assertIn("сам: принял денег — 700 ₽", page)
        card = self.get_ok(f"/team/{owner['id']}")
        self.assertIn("Владелец — не исполнитель", card)
        self.assertNotIn('action="/team/', card, "форм плана нет")
        r = self.client.post(f"/team/{owner['id']}/plan", data={"plan_tasks": "3"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(run(self.crm.staff_plans(date.today().replace(day=1))), {})

    def test_week_and_day_plans(self):
        """Неделя и день: без своего плана - доля месяца с подписью, свой
        главнее; отказ формы - та же страница с набранным; пустая форма
        возвращает план к доле месяца."""
        anna = self.make_anna()
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        self.login()
        self.client.post(f"/team/{anna}/plan", data={
            "month": f"{today:%Y-%m}", "plan_issues": "62", "plan_tasks": "31"})
        page = self.get_ok("/team?period=week")
        self.assertIn('class="on" aria-current="page">Неделя', page)
        self.assertIn("из плана месяца", page)
        self.assertNotIn("Начислено", page, "начисления - только за месяц")
        r = self.client.post(f"/team/{anna}/plan", data={
            "period": "week", "week": today.isoformat(), "month": f"{today:%Y-%m}",
            "day": today.isoformat(), "plan_issues": "5"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("#plan-week", r.headers["location"])
        own = run(self.crm.staff_period_plans("week", monday))[anna]
        self.assertEqual((own["plan_issues"], own["plan_tasks"]), (5, None),
                         "неделя записана с понедельника, пустое - из месяца")
        page = self.get_ok("/team?period=week")
        self.assertIn('0 <small class="muted">/ 5</small>', page)
        self.assertIn("свой + из месяца", page, "выдачи свои, задачи - доля месяца")
        card = self.get_ok(f"/team/{anna}?week={monday.isoformat()}")
        self.assertIn("Аналитика помесячно", card)
        self.assertIn('name="plan_issues" inputmode="numeric" value="5"', card)
        # пустое поле задач - доля месяца (подсказка без числа: дробь в поле
        # звала бы её ввести), у сдач месяца плана нет - и подсказки нет
        self.assertIn('name="plan_tasks" inputmode="numeric" value="" '
                      'placeholder="из плана месяца"', card)
        self.assertIn('name="plan_returns" inputmode="numeric" value="">', card)
        # ошибка ввода - та же карточка, сообщение у формы дня и набранное
        r = self.client.post(f"/team/{anna}/plan", data={
            "period": "day", "day": today.isoformat(), "plan_issues": "много"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Числа плана - цифрами.", r.text)
        self.assertIn('value="много"', r.text)
        self.assertEqual(run(self.crm.staff_period_plans("day", today)), {})
        # дробь в штуках не усекается молча
        r = self.client.post(f"/team/{anna}/plan", data={
            "period": "day", "day": today.isoformat(), "plan_tasks": "1,3"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Штуки плана - целым числом.", r.text)
        self.assertIn('value="1,3"', r.text)
        self.assertEqual(run(self.crm.staff_period_plans("day", today)), {})
        r = self.client.post(f"/team/{anna}/plan", data={"period": "day",
                                                          "day": today.isoformat(),
                                                          "plan_issues": "3"})
        self.assertEqual(run(self.crm.staff_period_plans("day", today))[anna]["plan_issues"], 3)
        day = self.get_ok(f"/team?period=day&day={today.isoformat()}")
        self.assertIn("свой + из месяца", day)
        # пустая форма недели - строка снята, план снова доля месяца
        self.client.post(f"/team/{anna}/plan", data={"period": "week",
                                                     "week": monday.isoformat()})
        self.assertEqual(run(self.crm.staff_period_plans("week", monday)), {})
        title = team.period_title("week", monday, today=date.today())
        self.assertIn(f'<div class="flash ok">План недели {title} - снова из плана месяца.',
                      self.get_ok(f"/team/{anna}"))
        # ошибка в форме месяца - тоже на месте, с набранными условиями
        r = self.client.post(f"/team/{anna}/plan", data={"month": f"{today:%Y-%m}",
                                                          "salary_base": "20000",
                                                          "revenue_pct": "150"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Процент - от 0 до 100.", r.text)
        self.assertIn('value="20000"', r.text)
        # сотрудник видит сегодня и неделю в «Задачах дня», планы не правит
        self.client.post("/logout")
        self.login("anna", "password-1")
        my = self.get_ok("/my")
        self.assertIn("Сегодня", my)
        self.assertIn("Эта неделя", my)
        self.assertIn("из плана месяца", my, "неделя без своего плана - доля месяца")
        r = self.client.post(f"/team/{anna}/plan", data={"period": "week",
                                                          "plan_issues": "100"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(run(self.crm.staff_period_plans("week", monday)), {})

    def test_week_view_without_finance(self):
        """Неделя и день без права на «Финансы»: рублей нет, как у месяца, -
        ни в таблицах, ни в формах; сохранение штук рубли и условия не стирает."""
        anna = self.make_anna()
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        run(self.crm.set_staff_period_plan(anna, "week", monday, by="t",
                                           plan_revenue=D("50000")))
        run(self.crm.set_staff_plan(anna, today.replace(day=1), by="t", plan_issues=20,
                                    plan_revenue=D("155000"), salary_base=D("20000"),
                                    revenue_pct=D("2")))
        profile = run(self.crm.access_profile_by_code("manager"))
        sections = {**profile["perms"]["sections"], "staff": "edit", "finance": ""}
        pid = run(self.crm.create_access_profile("Кадры", {"sections": sections,
                                                           "actions": {}}))
        run(self.crm.create_staff("kadry", logic.hash_password("password-2"), "Кадры",
                                  "manager", pid))
        self.assertEqual(self.login("kadry", "password-2").status_code, 303)
        page = self.get_ok("/team?period=week")
        self.assertIn("Анна", page)
        self.assertNotIn("Принял денег", page)
        card = self.get_ok(f"/team/{anna}")
        self.assertNotIn("Принял денег", card, "ни в неделе, ни в аналитике")
        self.assertNotIn("Начислено", card)
        for field in ("plan_revenue", "salary_base", "per_issue", "order_pct", "revenue_pct"):
            self.assertNotIn(f'name="{field}"', card, "денежных полей в формах нет")
        self.assertNotIn("50000", card)
        self.assertNotIn("155000", card)
        self.assertIn('name="plan_issues"', card, "штуки планировать можно")
        # сохранение недели и месяца: рубли и условия прежние, из запроса не берутся
        r = self.client.post(f"/team/{anna}/plan", data={
            "period": "week", "week": monday.isoformat(), "plan_issues": "4",
            "plan_revenue": "1"})
        self.assertEqual(r.status_code, 303)
        own = run(self.crm.staff_period_plans("week", monday))[anna]
        self.assertEqual((own["plan_issues"], own["plan_revenue"]), (4, D("50000")))
        r = self.client.post(f"/team/{anna}/plan", data={
            "month": f"{today:%Y-%m}", "plan_issues": "25", "salary_base": "0"})
        self.assertEqual(r.status_code, 303)
        plan = run(self.crm.staff_plans(today.replace(day=1)))[anna]
        self.assertEqual((plan["plan_issues"], plan["plan_revenue"], plan["salary_base"],
                          plan["revenue_pct"]), (25, D("155000"), D("20000"), D("2")))
        self.assertIn(f"План на {team.month_name(today.replace(day=1))} сохранён.",
                      self.get_ok(f"/team/{anna}"))

    def test_menu_and_export(self):
        self.make_anna()
        self.make_bulat()
        self.login()
        page = self.get_ok("/")
        self.assertIn("Команда", self.menu_labels(page))
        r = self.client.get("/team.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Начислено", r.text)
        self.assertIn("Ремонт парка", r.text)
        self.assertIn("администратор", r.text)
        self.assertIn("мастер", r.text)
        self.assertNotIn("Владелец,", r.text, "владельца в расчёте зарплаты нет")


if __name__ == "__main__":
    unittest.main()
