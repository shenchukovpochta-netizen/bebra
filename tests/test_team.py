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

    def test_menu_and_export(self):
        self.login()
        page = self.get_ok("/")
        self.assertIn("Команда", self.menu_labels(page))
        r = self.client.get("/team.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Начислено", r.text)


if __name__ == "__main__":
    unittest.main()
