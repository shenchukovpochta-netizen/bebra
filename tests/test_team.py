"""Команда (app/crm/team.py): план месяца сотрудника, факт из своих
таблиц, темп и расчёт зарплаты; страницы «Команда» и карточка."""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import logic, team

try:
    from tests.test_web import HAVE_WEB, WebCase, run
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False
    WebCase = unittest.TestCase                        # type: ignore[misc,assignment]

TODAY = date(2026, 10, 15)


class TestTeamRules(unittest.TestCase):
    def test_month_of(self):
        self.assertEqual(team.month_of("2026-09", today=TODAY), date(2026, 9, 1))
        self.assertEqual(team.month_of("2027-01", today=TODAY), date(2026, 10, 1),
                         "будущий месяц - текущий")
        self.assertEqual(team.month_of("мусор", today=TODAY), date(2026, 10, 1))
        self.assertEqual(team.month_of("0001-05", today=TODAY), date(2026, 10, 1),
                         "полгода назад от 0001 года - переполнение даты")
        self.assertEqual(team.month_name(date(2026, 10, 1)), "октябрь 2026")

    def test_salary(self):
        terms = {"salary_base": D("20000"), "per_issue": D("300"), "order_pct": D("30"),
                 "revenue_pct": D("2")}
        facts = {"issues": 10, "orders_works": D("10000"), "payments": D("40000"),
                 "client_orders_total": D("10000"), "tasks_pay": D("500")}
        pay = team.salary(terms, facts)
        self.assertEqual(pay["issues"], D("3000.00"))
        self.assertEqual(pay["orders"], D("3000.00"))
        self.assertEqual(pay["revenue"], D("1000.00"), "2 % от платежей и клиентских нарядов")
        self.assertEqual(pay["tasks"], D("500.00"))
        self.assertEqual(pay["total"], D("27500.00"))
        self.assertEqual(team.salary(None, {})["total"], D("0.00"))

    def test_progress_pace(self):
        plan = {"plan_issues": 30, "plan_revenue": D("100000")}
        facts = {"issues": 10, "payments": D("60000")}
        rows = {m["key"]: m for m in team.progress(plan, facts, days=30, passed=15)}
        self.assertEqual((rows["issues"]["percent"], rows["issues"]["pace"]), (33, 15))
        self.assertFalse(rows["issues"]["ahead"], "10 из 30 к середине месяца - отстаёт")
        self.assertTrue(rows["revenue"]["ahead"])
        self.assertIsNone(rows["orders"]["plan"], "без плана - просто число")

    def test_rows_skip_idle_disabled(self):
        people = [{"id": 1, "name": "Анна", "active": True},
                  {"id": 2, "name": "Ушёл", "active": False},
                  {"id": 3, "name": "Ушёл с фактом", "active": False}]
        facts = {3: {"issues": 2}}
        got = team.rows(people, facts, {1: {"plan_issues": 10, "own": False}},
                        days=31, passed=15)
        self.assertEqual([r["name"] for r in got], ["Анна", "Ушёл с фактом"])
        self.assertTrue(got[0]["inherited"], "условия прошлого месяца помечены")

    def test_parse_plan(self):
        got, problem = team.parse_plan({"plan_issues": "20", "salary_base": "25 000",
                                        "order_pct": "30,5"})
        self.assertIsNone(problem)
        self.assertEqual((got["plan_issues"], got["salary_base"], got["order_pct"],
                          got["plan_orders"], got["per_issue"]),
                         (20, D("25000.00"), D("30.50"), None, D("0.00")))
        self.assertIn("0 до 100", team.parse_plan({"revenue_pct": "120"})[1])
        self.assertIn("цифрами", team.parse_plan({"plan_issues": "много"})[1])

    def test_parse_period_plan(self):
        """Неделя и день - только четыре числа плана, условий оплаты нет;
        пустое поле - «как в плане месяца» (None), а не ноль."""
        got, problem = team.parse_plan({"plan_issues": "5", "salary_base": "99999"},
                                       team.PERIOD_FIELDS)
        self.assertIsNone(problem)
        self.assertEqual(got, {"plan_issues": 5, "plan_orders": None, "plan_tasks": None,
                               "plan_revenue": None})
        self.assertEqual(team.parse_plan({"plan_tasks": "-1"}, team.PERIOD_FIELDS)[1],
                         "Числа плана - не меньше нуля.")


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
        self.assertEqual(team.period_months("week", date(2026, 9, 28)), [self.SEP, self.OCT])

    def test_month_share_by_days(self):
        plans = {self.OCT: {"plan_issues": 62, "plan_revenue": D("155000"),
                            "plan_tasks": 40, "plan_orders": None}}
        week = team.month_share(plans, date(2026, 10, 12), 7)
        self.assertEqual((week["plan_issues"], week["plan_revenue"], week["plan_orders"]),
                         (D("14.0"), D("35000.00"), None))
        day = team.month_share(plans, TODAY, 1)
        self.assertEqual((day["plan_issues"], day["plan_tasks"]), (D("2.0"), D("1.3")),
                         "40 задач в октябре - 1,3 в день, а не «1»")
        self.assertEqual(team.month_share({}, TODAY, 1)["plan_issues"], None)

    def test_week_across_month_boundary(self):
        """Неделя 28.09-04.10: три дня берут долю сентября, четыре - октября."""
        plans = {self.SEP: {"plan_issues": 30, "plan_orders": 15},
                 self.OCT: {"plan_issues": 62}}
        got = team.month_share(plans, date(2026, 9, 28), 7)
        self.assertEqual(got["plan_issues"], D("11.0"), "3 × 1 + 4 × 2")
        self.assertEqual(got["plan_orders"], D("1.5"), "в октябре наряды не планируются")

    def test_own_plan_wins_per_metric(self):
        derived = {"plan_issues": D("9.0"), "plan_orders": D("3.5"), "plan_tasks": D("2.0"),
                   "plan_revenue": None}
        plan = team.period_plan({"plan_issues": 5, "plan_orders": None, "plan_tasks": 0,
                                 "plan_revenue": None}, derived)
        self.assertEqual((plan["plan_issues"], plan["plan_orders"], plan["plan_tasks"],
                          plan["plan_revenue"]), (5, D("3.5"), 0, None))
        self.assertEqual(plan["sources"], {"plan_issues": "own", "plan_orders": "month",
                                           "plan_tasks": "own"})
        rows = {m["key"]: m for m in team.progress(plan, {"issues": 2}, days=7, passed=4)}
        self.assertIsNone(rows["tasks"]["percent"], "свой ноль - не планируется")
        self.assertEqual((rows["issues"]["source"], rows["orders"]["source"]),
                         ("own", "month"))
        self.assertIsNone(team.period_plan(None, {"plan_issues": None}))

    def test_period_plans_for_team(self):
        own = {2: {"plan_issues": 7, "plan_orders": None, "plan_tasks": None,
                   "plan_revenue": None}}
        months = {self.OCT: {1: {"plan_issues": 31, "own": True},
                             3: {"salary_base": D("20000"), "plan_issues": None}}}
        got = team.period_plans("day", TODAY, own, months)
        self.assertEqual(got[1]["plan_issues"], D("1.0"))
        self.assertEqual(got[1]["sources"], {"plan_issues": "month"})
        self.assertEqual(got[2]["plan_issues"], 7, "без плана месяца - только свой")
        self.assertNotIn(3, got, "одни условия оплаты - не план")
        team_rows = team.rows([{"id": 1, "name": "Анна"}, {"id": 2, "name": "Борис"}],
                              {1: {"issues": 1}}, got, days=1, passed=1, open_day=True)
        by_name = {r["name"]: r for r in team_rows}
        self.assertEqual((by_name["Анна"]["percent"], by_name["Анна"]["source"]),
                         (100, "month"))
        self.assertTrue(by_name["Анна"]["ahead"])
        self.assertFalse(by_name["Борис"]["behind"], "день идёт: 0 из 7 - ещё не отставание")

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
        data += [(months[3], {"plan_issues": 10, "own": True}, {"issues": 12}),
                 (months[4], None, {}),
                 (months[5], {"plan_issues": 10, "own": False, "salary_base": D("1000")},
                  {"issues": 3})]
        got = team.history(data, today=TODAY)
        self.assertEqual([h["first"] for h in got],
                         [date(2026, 10, 1), date(2026, 9, 1), date(2026, 8, 1)],
                         "новые сверху; до первой работы - не показаны, пустой между - да")
        self.assertEqual((got[2]["metrics"]["issues"]["percent"],
                          got[2]["metrics"]["issues"]["ahead"]), (120, True))
        self.assertTrue(got[0]["current"] and got[0]["inherited"])
        self.assertEqual(got[0]["pay"]["total"], D("1000.00"))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestTeamPages(WebCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def test_team_month_and_member(self):
        profile = run(self.crm.access_profile_by_code("manager"))
        anna = run(self.crm.create_staff("anna", logic.hash_password("password-1"), "Анна",
                                         "manager", profile["id"], location="Адоратского"))
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
            "per_issue": "300", "revenue_pct": "10"})
        self.assertEqual(r.status_code, 303)
        page = self.get_ok("/team")
        self.assertIn("Анна", page)
        self.assertIn('1 <small class="muted">/ 10</small>', page, "выдача против плана")
        # 20000 + 300 + 10 % от 3000 + 200 за задачу; платёж банка не её
        self.assertIn("20 800 ₽", page)
        card = self.get_ok(f"/team/{anna}")
        self.assertIn("Напомнить о продлении", card)
        self.assertIn("План и условия на", card)
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
        self.assertEqual(self.client.get("/team").status_code, 403, "Команда - не ей")

    def make_anna(self):
        profile = run(self.crm.access_profile_by_code("manager"))
        return run(self.crm.create_staff("anna", logic.hash_password("password-1"), "Анна",
                                         "manager", profile["id"], location="Адоратского"))

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
        self.assertIn("из плана месяца", card)
        # ошибка ввода - та же карточка, сообщение у формы дня и набранное
        r = self.client.post(f"/team/{anna}/plan", data={
            "period": "day", "day": today.isoformat(), "plan_issues": "много"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Числа плана - цифрами.", r.text)
        self.assertIn('value="много"', r.text)
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
        self.assertIn("снова из плана месяца", self.get_ok(f"/team/{anna}"))
        # ошибка в форме месяца - тоже на месте, с набранными условиями
        r = self.client.post(f"/team/{anna}/plan", data={"month": f"{today:%Y-%m}",
                                                          "salary_base": "20000",
                                                          "order_pct": "150"})
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
        """Неделя и день без права на «Финансы»: рублей нет, как у месяца."""
        anna = self.make_anna()
        run(self.crm.set_staff_period_plan(anna, "week",
                                           date.today() - timedelta(days=date.today().weekday()),
                                           by="t", plan_revenue=D("50000")))
        profile = run(self.crm.access_profile_by_code("manager"))
        sections = {**profile["perms"]["sections"], "staff": "edit", "finance": ""}
        pid = run(self.crm.create_access_profile("Кадры", {"sections": sections,
                                                           "actions": {}}))
        run(self.crm.create_staff("kadry", logic.hash_password("password-2"), "Кадры",
                                  "manager", pid))
        self.assertEqual(self.login("kadry", "password-2").status_code, 303)
        page = self.get_ok("/team?period=week")
        self.assertIn("Анна", page)
        self.assertNotIn("Принёс денег", page)
        card = self.get_ok(f"/team/{anna}")
        self.assertNotIn("Принёс денег", card, "ни в неделе, ни в аналитике")
        self.assertNotIn("Начислено", card)

    def test_menu_and_export(self):
        self.login()
        page = self.get_ok("/")
        self.assertIn("Команда", self.menu_labels(page))
        r = self.client.get("/team.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Начислено", r.text)


if __name__ == "__main__":
    unittest.main()
