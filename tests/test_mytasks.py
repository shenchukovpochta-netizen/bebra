"""«Мои задачи» (app/crm/mytasks.py), профили «только задачи» и срок
доступа сотрудника: чистые правила и панель на FakeCrm."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import logic, mytasks

try:
    from tests.test_web import HAVE_WEB, WebCase, run
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False
    WebCase = unittest.TestCase                        # type: ignore[misc,assignment]

TODAY = date(2026, 9, 30)
P1, P2 = "Павлюхина", "Адоратского"


def perms(code):
    return next(p[2] for p in (*logic.BUILT_IN_PROFILES, *logic.TASK_PROFILES)
                if p[0] == code)


def person(code, **extra):
    return {"id": 7, "login": "x", "perms": perms(code), **extra}


def rental(rid, name, location, left, **extra):
    return {"id": rid, "full_name": name, "phone": f"+7999000000{rid}",
            "bike_code": f"B-{rid}", "location": location,
            "summary": {"days_left": left, "covered_until": TODAY + timedelta(days=left),
                        "debt": D(1500) if left < 0 else D(0)}, **extra}


def order(oid, *, tech=None, location=P1, days=1, status="in_work"):
    return {"id": oid, "no": f"РЕМ-{oid:06d}", "bike_code": f"B-{oid}",
            "bike_model": "Kugoo", "status": status, "tech_id": tech,
            "location": location, "complaint": "не тянет",
            "opened_at": datetime.combine(TODAY - timedelta(days=days), time(10),
                                          tzinfo=UTC)}


def codes(groups):
    return [g["code"] for g in groups]


class TestMyTasks(unittest.TestCase):
    def test_mechanic_sees_his_orders_and_free_ones_of_his_point(self):
        staff = person("tasks_tech", location=P1)
        groups = mytasks.my_tasks(
            staff, orders=[order(1, tech=7), order(2, tech=8), order(3), order(4, location=P2),
                           order(5, tech=7, status="done"), order(6, location=None)],
            expiring=[rental(1, "Иванов", P1, -2)], today=TODAY)
        by = {g["code"]: g for g in groups}
        self.assertEqual(set(by), {"orders_mine", "orders_free"},
                         "аренды механику не показываются: раздел закрыт")
        self.assertEqual([i["url"] for i in by["orders_mine"]["rows"]], ["/orders/1"])
        # без техника: своя точка и наряды без точки, но не чужая точка
        self.assertEqual([i["url"] for i in by["orders_free"]["rows"]],
                         ["/orders/3", "/orders/6"])

    def test_stuck_order_is_hot_and_first(self):
        staff = person("tasks_tech", location=P1)
        groups = mytasks.my_tasks(staff, orders=[order(1, tech=7, days=1),
                                                 order(2, tech=7, days=20)], today=TODAY)
        mine = groups[0]
        self.assertEqual((mine["code"], mine["level"]), ("orders_mine", "hot"))
        self.assertEqual([i["url"] for i in mine["rows"]], ["/orders/2", "/orders/1"])
        self.assertTrue(mine["rows"][0]["hot"])
        self.assertIn("дольше срока", mine["rows"][0]["sub"])

    def test_operator_sees_his_point_only(self):
        staff = person("tasks_operator", location=P1)
        groups = mytasks.my_tasks(
            staff, expiring=[rental(1, "Иванов", P1, -2), rental(2, "Петров", P2, -1),
                             rental(3, "Сидоров", P1, 1), rental(4, "Без точки", None, 0)],
            bookings=[{"id": 1, "client_id": 1, "full_name": "Заявкин", "status": "new",
                       "wanted_on": TODAY, "location_name": P1},
                      {"id": 2, "client_id": 2, "full_name": "Чужой", "status": "new",
                       "wanted_on": TODAY, "location_name": P2},
                      {"id": 3, "client_id": 3, "full_name": "Завтрашний", "status": "new",
                       "wanted_on": TODAY + timedelta(days=1), "location_name": P1}],
            alerts=[{"id": 1, "tracker_id": 11, "bike_code": "B-1", "kind": "moving",
                     "level": "urgent", "state": "new", "bike_location": P1},
                    {"id": 2, "tracker_id": 12, "bike_code": "B-2", "kind": "offline",
                     "level": "yellow", "state": "new", "bike_location": P2},
                    {"id": 3, "tracker_id": 13, "bike_code": "B-3", "kind": "offline",
                     "level": "yellow", "state": "work", "bike_location": P1}],
            orders=[order(1)], shift_open=False, today=TODAY,
            booking_url=lambda b: f"/issue?booking={b['id']}")
        by = {g["code"]: g for g in groups}
        self.assertNotIn("orders_free", by, "сервис оператору закрыт")
        self.assertEqual([i["title"] for i in by["overdue"]["rows"]],
                         ["Иванов", "Без точки"])
        self.assertIn("без точки", by["overdue"]["rows"][1]["sub"])
        self.assertEqual([i["title"] for i in by["soon"]["rows"]], ["Сидоров"])
        self.assertEqual([i["url"] for i in by["bookings"]["rows"]], ["/issue?booking=1"])
        self.assertEqual([i["url"] for i in by["alerts"]["rows"]], ["/trackers/11"])
        self.assertEqual(by["cash"]["title"], "Касса «Павлюхина» не открыта")
        # горящее выше тёплого
        self.assertLess(codes(groups).index("overdue"), codes(groups).index("soon"))
        self.assertLess(codes(groups).index("bookings"), codes(groups).index("cash"))

    def test_groups_go_to_who_acts(self):
        """Механику встроенного профиля аренды открыты посмотреть - звонить
        должникам ему не поручают; свои наряды видны и с правом смотреть,
        а взять чужой - только с правом менять сервис."""
        rows = [rental(1, "Иванов", P1, -2)]
        tech = mytasks.my_tasks(person("tech", location=P1), expiring=rows,
                                orders=[order(1, tech=7), order(2)], today=TODAY)
        self.assertEqual(codes(tech), ["orders_mine", "orders_free"])
        viewer = {"id": 7, "perms": {"sections": {"service": "view", "rentals": "view"}},
                  "location": P1}
        only = mytasks.my_tasks(viewer, expiring=rows, orders=[order(1, tech=7), order(2)],
                                today=TODAY)
        self.assertEqual(codes(only), ["orders_mine"])

    def test_money_only_with_finance(self):
        rows = [rental(1, "Иванов", P1, -2)]
        without = mytasks.my_tasks(person("tasks_operator", location=P1), expiring=rows,
                                   today=TODAY)
        self.assertNotIn("₽", without[0]["rows"][0]["sub"])
        manager = mytasks.my_tasks(person("manager", location=P1), expiring=rows,
                                   today=TODAY)
        self.assertIn("долг 1", manager[0]["rows"][0]["sub"])

    def test_no_point_means_every_point(self):
        groups = mytasks.my_tasks(person("tasks_operator"),
                                  expiring=[rental(1, "А", P1, -1), rental(2, "Б", P2, -1)],
                                  shift_open=False, today=TODAY)
        self.assertEqual([i["title"] for i in groups[0]["rows"]], ["А", "Б"])
        self.assertNotIn("cash", codes(groups), "касса - только своей точки")

    def test_search_and_claims(self):
        groups = mytasks.my_tasks(
            person("tasks_operator", location=P1),
            search={"searching": [{"id": 1, "full_name": "Вор", "location": P1,
                                   "theft": True, "search_at": TODAY}],
                    "candidates": [{"id": 2, "full_name": "Кандидат", "location": P2,
                                    "overdue_days": 9}]},
            claims=[{"id": 1, "full_name": "Платил"}], today=TODAY)
        by = {g["code"]: g for g in groups}
        self.assertEqual([i["title"] for i in by["search"]["rows"]], ["Вор"])
        self.assertIn("пора признавать потерю", by["search"]["rows"][0]["sub"])
        self.assertEqual(by["claims"]["count"], 1)

    def test_long_group_is_cut(self):
        rows = [rental(i, f"К{i}", P1, 1) for i in range(1, mytasks.GROUP_LIMIT + 6)]
        group = mytasks.my_tasks(person("tasks_operator", location=P1), expiring=rows,
                                 today=TODAY)[0]
        self.assertEqual((len(group["rows"]), group["more"], group["count"]),
                         (mytasks.GROUP_LIMIT, 5, mytasks.GROUP_LIMIT + 5))
        self.assertEqual(group["url"], "/rentals", "без сводки - список аренд")


class TestProfilesAndTerms(unittest.TestCase):
    def test_task_profiles_hide_the_whole_network(self):
        for code, name, p, built_in in logic.TASK_PROFILES:
            staff = {"perms": p}
            self.assertEqual(logic.normalize_perms(p), p, code)
            self.assertFalse(built_in, "владелец правит их, как свои")
            for hidden in ("dashboard", "finance", "reports", "staff", "settings",
                           "tariffs", "mailing", "inbox", "franchise", "import"):
                self.assertFalse(logic.can_view(staff, hidden), f"{code}: {hidden}")
            self.assertEqual(logic.home_for(staff), "/my", code)
            self.assertIn("только задачи", name)
        operator = {"perms": perms("tasks_operator")}
        self.assertTrue(logic.can_edit(operator, "issue"))
        self.assertTrue(logic.can_edit(operator, "rentals"))
        self.assertFalse(logic.can_view(operator, "service"))
        tech = {"perms": perms("tasks_tech")}
        self.assertTrue(logic.can_edit(tech, "service"))
        self.assertFalse(logic.can_view(tech, "clients"))

    def test_home_keeps_the_old_rules(self):
        self.assertEqual(logic.home_for({"perms": perms("tech")}), "/")
        self.assertEqual(logic.home_for({"perms": {"sections": {"bikes": "edit"}}}), "/bikes")
        self.assertEqual(logic.home_for({"perms": {"sections": {"service": "edit"}}}), "/my")

    def test_schema_seeds_the_same_profiles(self):
        schema = (Path(__file__).resolve().parent.parent / "schema.sql").read_text("utf-8")
        import json
        import re
        found = {code: (name, json.loads(body)) for code, name, body in re.findall(
            r"\('(tasks_\w+)',\s*'([^']*)',\s*'(\{.*?\})'::jsonb", schema)}
        self.assertEqual(found, {code: (name, p) for code, name, p, _ in
                                 logic.TASK_PROFILES})

    def test_term_from_form(self):
        end = time(23, 59, 59)
        self.assertEqual(logic.check_staff_term("", "", today=TODAY).value, None)
        self.assertEqual(logic.check_staff_term("none", "", today=TODAY).value, None)
        self.assertEqual(logic.check_staff_term("day", "", today=TODAY).value,
                         datetime.combine(TODAY, end).astimezone())
        self.assertEqual(logic.check_staff_term("week", "", today=TODAY).value,
                         datetime.combine(TODAY + timedelta(days=7), end).astimezone())
        # дата главнее кнопки
        self.assertEqual(logic.check_staff_term("week", "2026-10-02", today=TODAY).value,
                         datetime.combine(date(2026, 10, 2), end).astimezone())
        for term, until in (("year", ""), ("", "вчера"), ("", "2026-09-29")):
            self.assertFalse(logic.check_staff_term(term, until, today=TODAY).ok,
                             (term, until))
        # форма правки: пусто - «ничего не выбрал», а не «снять срок»
        self.assertFalse(logic.check_staff_term("", "", today=TODAY, keep_empty=True).ok)
        self.assertIsNone(logic.check_staff_term("none", "", today=TODAY,
                                                 keep_empty=True).value)

    def test_expired(self):
        now = datetime(2026, 9, 30, 12, tzinfo=UTC)
        self.assertFalse(logic.staff_expired({}, now=now))
        self.assertFalse(logic.staff_expired({"expires_at": now + timedelta(minutes=1)},
                                             now=now))
        self.assertTrue(logic.staff_expired({"expires_at": now}, now=now))
        self.assertTrue(logic.staff_expired({"expires_at": now - timedelta(days=1)},
                                            now=now))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestMyTasksPage(WebCase):
    def setUp(self):
        super().setUp()
        crm = self.crm
        for name in (P1, P2):
            run(crm.create_location(name=name, city="Казань", address="", note=None))
        tariff = run(crm.create_tariff("Неделя", 7, D(3000), None))
        today = date.today()
        self.clients = {}
        for n, point in ((1, P1), (2, P2)):
            bike = run(crm.create_bike(code=f"B-{n}", model="Kugoo V3", location=point))
            client = run(crm.create_client(full_name=f"Клиент {point}",
                                           phone=f"+7999000000{n}"))
            self.clients[point] = client
            run(crm.create_rental(client_id=client, bike_id=bike, tariff_id=tariff,
                                  tariff_name="Неделя", period_days=7, price=D(3000),
                                  billing="auto", started_on=today - timedelta(days=9),
                                  contract_no=None, created_by="t", location=point))
            rid = max(crm.rentals_)
            run(crm.update_rental(rid, billed_until=today - timedelta(days=2)))
        mech = run(crm.access_profile_by_code("tasks_tech"))
        oper = run(crm.access_profile_by_code("tasks_operator"))
        self.mech = run(crm.create_staff("mech", logic.hash_password("password-1"),
                                         "Механик", "manager", mech["id"], location=P1))
        run(crm.create_staff("oper", logic.hash_password("password-1"), "Оператор",
                             "manager", oper["id"], location=P1))
        self.bike3 = run(crm.create_bike(code="B-3", model="Kugoo V3", location=P1))
        bike4 = run(crm.create_bike(code="B-4", model="Kugoo V3", location=P2))
        self.mine = run(crm.create_work_order(
            bike_id=self.bike3, payer="own", client_id=None, complaint="тормоза",
            object_note=None, tech_id=self.mech, estimate=D(0), created_by="t"))
        self.other = run(crm.create_work_order(
            bike_id=bike4, payer="own", client_id=None, complaint="свет",
            object_note=None, tech_id=None, estimate=D(0), created_by="t"))

    def test_operator_gets_tasks_instead_of_dashboard(self):
        r = self.login("oper", "password-1")
        self.assertEqual(r.headers["location"], "/my")
        self.assertEqual(self.client.get("/").status_code, 403)
        page = self.get_ok("/my")
        self.assertIn("Клиент Павлюхина", page)
        self.assertNotIn("Клиент Адоратского", page)
        self.assertIn("Касса «Павлюхина» не открыта", page)
        self.assertNotIn("₽", page, "денег без права на финансы нет")
        self.assertNotIn("РЕМ-", page, "наряды оператору не показываются")
        self.assertIn('href="/rentals/', page)
        self.assertIn('href="/my"', page, "в меню")
        self.assertNotIn('href="/reports"', page)

    def test_mechanic_gets_his_orders(self):
        self.login("mech", "password-1")
        page = self.get_ok("/my")
        mine = run(self.crm.work_order(self.mine))["no"]
        other = run(self.crm.work_order(self.other))["no"]
        self.assertIn(mine, page)
        self.assertNotIn(other, page, "наряд чужой точки без техника - не его")
        self.assertNotIn("Клиент Павлюхина", page)
        self.assertEqual(self.client.get("/clients").status_code, 403)

    def test_owner_without_point_sees_every_point(self):
        self.login()
        page = self.get_ok("/my")
        self.assertIn("Клиент Павлюхина", page)
        self.assertIn("Клиент Адоратского", page)
        self.assertIn("Своя точка не задана", page)
        self.assertIn('href="/my"', self.get_ok("/"), "ссылка на сводке")


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestStaffTerm(WebCase):
    def add(self, **data):
        manager = run(self.crm.access_profile_by_code("manager"))
        r = self.client.post("/staff", data={"login": "temp", "name": "Подменный",
                                             "profile_id": manager["id"], **data})
        self.assertEqual(r.status_code, 303)
        return run(self.crm.staff_by_login("temp"))

    def test_generated_password_shown_once_and_works(self):
        self.login()
        staff = self.add(term="day")
        page = self.get_ok("/staff")
        found = __import__("re").search(r"Пароль для temp: (\S+) —", page)
        self.assertIsNotNone(found)
        self.assertNotIn(found.group(1), self.get_ok("/staff"), "показан один раз")
        self.assertEqual(staff["expires_at"],
                         datetime.combine(date.today(), time(23, 59, 59)).astimezone())
        other = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(
            self.app, follow_redirects=False)
        r = other.post("/login", data={"login": "temp", "password": found.group(1)})
        self.assertEqual(r.status_code, 303)

    def test_expired_login_is_refused_and_session_dies(self):
        self.login()
        staff = self.add(password="password-1", term="week")
        self.client.post("/logout")
        self.assertEqual(self.login("temp", "password-1").status_code, 303)
        self.assertEqual(self.client.get("/me").status_code, 200)
        run(self.crm.set_staff_expires(staff["id"],
                                       datetime.now(UTC) - timedelta(minutes=1)))
        r = self.client.get("/me")
        self.assertEqual(r.status_code, 303, "открытая сессия выбита")
        self.assertTrue(r.headers["location"].startswith("/login"))
        r = self.login("temp", "password-1")
        self.assertEqual(r.status_code, 403)
        self.assertIn("Срок доступа истёк", r.text)
        # неверный пароль - обычный ответ: срок не выдаёт, что логин есть
        r = self.login("temp", "wrong-pass")
        self.assertEqual(r.status_code, 401)

    def test_owner_extends_and_removes_term(self):
        self.login()
        staff = self.add(password="password-1", until=(date.today()
                                                        + timedelta(days=3)).isoformat())
        self.assertEqual(staff["expires_at"].date(), date.today() + timedelta(days=3))
        self.assertIn("до ", self.get_ok("/staff"))
        # «—» и пустая дата - ничего не выбрано: срок остаётся, а не снимается
        self.client.post(f"/staff/{staff['id']}/term", data={"term": "", "until": ""})
        self.assertIn("выберите срок или дату", self.get_ok("/staff"))
        self.assertEqual(run(self.crm.staff_by_id(staff["id"]))["expires_at"].date(),
                         date.today() + timedelta(days=3))
        self.client.post(f"/staff/{staff['id']}/term", data={"term": "none"})
        self.assertIsNone(run(self.crm.staff_by_id(staff["id"]))["expires_at"])
        self.client.post(f"/staff/{staff['id']}/term", data={"until": "2000-01-01"})
        self.assertIn("дата уже прошла", self.get_ok("/staff"))
        self.assertIsNone(run(self.crm.staff_by_id(staff["id"]))["expires_at"])
        me = run(self.crm.staff_by_login("admin"))
        self.client.post(f"/staff/{me['id']}/term", data={"term": "day"})
        self.assertIsNone(run(self.crm.staff_by_id(me["id"]))["expires_at"],
                          "свой срок не ставят")

    def test_new_password_by_panel(self):
        self.login()
        staff = self.add(password="password-1")
        self.client.post(f"/staff/{staff['id']}/password", data={"password": ""})
        found = __import__("re").search(r"Новый пароль для temp: (\S+) —",
                                        self.get_ok("/staff"))
        self.assertTrue(logic.verify_password(
            found.group(1), run(self.crm.staff_by_id(staff["id"]))["password_hash"]))

    def test_expired_staff_cannot_swap_from_the_group(self):
        from app.handlers import ops
        sid = run(self.crm.create_staff("swapper", logic.hash_password("password-1"),
                                        "С", "admin"))
        self.crm.staff[sid]["tg_id"] = 4242
        cfg = SimpleNamespace(admins=set())
        self.assertTrue(run(ops._may_swap(self.crm, cfg, 4242)))
        run(self.crm.set_staff_expires(sid, datetime.now(UTC) - timedelta(seconds=1)))
        self.assertFalse(run(ops._may_swap(self.crm, cfg, 4242)))


if __name__ == "__main__":
    unittest.main()
