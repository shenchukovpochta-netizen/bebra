"""Задачи дня (app/crm/tasks.py): поручения сотрудникам, незакреплённые
задачи точки, «Взять» и «Сделано», доска команды."""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import logic, tasks

try:
    from tests.test_web import HAVE_WEB, WebCase, run
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False
    WebCase = unittest.TestCase                        # type: ignore[misc,assignment]

TODAY = date(2026, 10, 5)
OWNER = {"id": 1, "profile_code": "owner", "perms": {"sections": {"staff": "edit"}}}
ADMIN = {"id": 2, "location": "Адоратского", "profile_code": "manager",
         "perms": {"sections": {"rentals": "edit"}}}
MASTER = {"id": 3, "location": "Павлюхина", "profile_code": "tech",
          "perms": {"sections": {"service": "edit"}}}


class TestTaskRules(unittest.TestCase):
    def test_due_label(self):
        self.assertEqual(tasks.due_label({"status": "open", "due_on": TODAY - timedelta(days=2)},
                                         today=TODAY), ("просрочена на 2 дн.", True))
        self.assertEqual(tasks.due_label({"status": "open", "due_on": TODAY}, today=TODAY),
                         ("сегодня", True))
        self.assertEqual(tasks.due_label({"status": "open", "due_on": TODAY + timedelta(1)},
                                         today=TODAY), ("завтра", False))
        self.assertEqual(tasks.due_label({"status": "done", "due_on": TODAY - timedelta(5)},
                                         today=TODAY), ("", False), "сделанная не горит")

    def test_parse_form(self):
        fields, problem = tasks.parse_form(
            {"title": "  закупить   запчасти ", "assignee": "me", "due_on": "2026-10-06",
             "pay": "500"}, staff=ADMIN, today=TODAY)
        self.assertIsNone(problem)
        self.assertEqual(fields["title"], "закупить запчасти")
        self.assertEqual(fields["assignee_id"], 2)
        self.assertNotIn("pay", fields, "цену ставит только тот, кто ведёт команду")
        fields, _ = tasks.parse_form({"title": "ремонт АКБ", "assignee": "none",
                                      "pay": "1 500,50"}, staff=OWNER, today=TODAY)
        self.assertEqual((fields["assignee_id"], fields["pay"]), (None, D("1500.50")))
        self.assertEqual(tasks.parse_form({"title": " "}, staff=OWNER, today=TODAY)[1],
                         "Напишите, что сделать.")
        self.assertIn("прошёл", tasks.parse_form({"title": "x", "due_on": "2026-09-01"},
                                                 staff=OWNER, today=TODAY)[1])
        self.assertIn("число", tasks.parse_form({"title": "x", "pay": "много"},
                                                staff=OWNER, today=TODAY)[1])

    def test_rights(self):
        task = {"status": "open", "assignee_id": 3, "created_by_id": 1}
        self.assertTrue(tasks.may_finish(MASTER, task))
        self.assertTrue(tasks.may_finish(OWNER, task))
        self.assertFalse(tasks.may_finish(ADMIN, task), "чужую закрывает исполнитель")
        self.assertFalse(tasks.may_change(MASTER, task), "править - автор или руководитель")
        self.assertTrue(tasks.may_take(ADMIN, {**task, "assignee_id": None}))
        self.assertFalse(tasks.may_take(ADMIN, task))

    def test_my_lists_by_point(self):
        rows = [{"id": 1, "status": "open", "title": "мне", "assignee_id": 3},
                {"id": 2, "status": "open", "title": "ремонт АКБ", "assignee_id": None,
                 "location": "Павлюхина"},
                {"id": 3, "status": "open", "title": "чужая точка", "assignee_id": None,
                 "location": "Адоратского"},
                {"id": 4, "status": "open", "title": "вся сеть", "assignee_id": None},
                {"id": 5, "status": "done", "title": "сделана", "assignee_id": 3}]
        got = tasks.my_lists(MASTER, rows, today=TODAY)
        self.assertEqual([t["id"] for t in got["mine"]], [1])
        self.assertEqual([t["id"] for t in got["free"]], [2, 4])
        self.assertTrue(got["free"][0]["can_take"])

    def test_overview(self):
        people = [{"id": 2, "name": "Админ", "location": "Адоратского"},
                  {"id": 3, "name": "Марат", "location": "Павлюхина"}]
        rows = [{"id": 1, "status": "open", "title": "a", "assignee_id": 3,
                 "location": "Павлюхина", "pay": D(300), "due_on": TODAY},
                {"id": 2, "status": "open", "title": "b", "assignee_id": None,
                 "location": "Адоратского"}]
        view = tasks.overview(rows, people, staff=ADMIN, today=TODAY)
        self.assertEqual((view["open"], view["hot"]), (2, 1))
        self.assertEqual(view["people"][0]["name"], "Марат")
        self.assertIsNone(view["people"][0]["tasks"][0]["pay"], "цена - без права скрыта")
        only = tasks.overview(rows, people, staff=OWNER, today=TODAY, location="Адоратского")
        self.assertEqual([t["id"] for t in only["free"]], [2])
        self.assertEqual([p["name"] for p in only["people"]], ["Админ"])


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestTaskPages(WebCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def add_staff(self, login, code, location=None, tg_id=None):
        profile = run(self.crm.access_profile_by_code(code))
        sid = run(self.crm.create_staff(login, logic.hash_password("password-1"),
                                        login.title(), "manager", profile["id"],
                                        location=location))
        if tg_id:
            run(self.crm.link_staff_tg(sid, tg_id, login))
        return sid

    def as_(self, login):
        self.client.post("/logout")
        self.login(login, "password-1")

    def test_assign_take_finish(self):
        marat = self.add_staff("marat", "tech", location="Павлюхина", tg_id=7001)
        self.add_staff("anna", "manager", location="Адоратского")
        self.login()
        r = self.client.post("/tasks", data={"title": "Закупить запчасти", "assignee": "me",
                                             "back": "/tasks"})
        self.assertEqual(r.headers["location"], "/tasks")
        self.client.post("/tasks", data={"title": "Напомнить о продлении",
                                         "assignee": str(marat), "pay": "200"})
        sent = [text for chat, text in self.bot.sent if chat == 7001]
        self.assertTrue(any("Напомнить о продлении" in t for t in sent),
                        "исполнитель узнаёт сообщением")
        self.client.post("/tasks", data={"title": "Ремонт АКБ", "assignee": "none",
                                         "location": "Павлюхина"})
        board = self.get_ok("/tasks")
        for word in ("Закупить запчасти", "Напомнить о продлении", "Ремонт АКБ", "Marat"):
            self.assertIn(word, board)
        free = next(t for t in self.crm.tasks_.values() if t["title"] == "Ремонт АКБ")
        # Марат видит своё и незакреплённое своей точки, берёт и закрывает
        self.as_("marat")
        my = self.get_ok("/my")
        self.assertIn("Напомнить о продлении", my)
        self.assertIn("Ремонт АКБ", my)
        self.assertNotIn("Закупить запчасти", my)
        self.assertNotIn("200", my.split("Мои поручения")[1].split("</section>")[0],
                         "цена задачи - тому, кто ведёт команду")
        self.client.post(f"/tasks/{free['id']}/take", data={"back": "/my"})
        self.assertEqual(free["assignee_id"], marat)
        # Анна не может закрыть чужую
        self.as_("anna")
        self.client.post(f"/tasks/{free['id']}/done")
        self.assertEqual(free["status"], "open")
        self.client.post(f"/tasks/{free['id']}/take")
        self.assertEqual(free["assignee_id"], marat, "взятую второй раз не взять")
        self.as_("marat")
        self.client.post(f"/tasks/{free['id']}/done", data={"back": "/my"})
        self.assertEqual((free["status"], free["done_by"]), ("done", marat))
        self.assertIn("Ремонт АКБ", self.get_ok("/tasks").split("Сделано сегодня")[1])
        # отменить чужую нельзя, автор - может
        self.client.post(f"/tasks/{free['id']}/reopen")
        self.assertEqual(free["status"], "done")
        self.client.post("/logout")
        self.login()
        self.client.post(f"/tasks/{free['id']}/reopen")
        self.assertEqual((free["status"], free["done_by"]), ("open", None))

    def test_edit_and_back_is_local(self):
        self.login()
        r = self.client.post("/tasks", data={"title": "x", "back": "https://evil.example"})
        self.assertEqual(r.headers["location"], "/my")
        task = next(iter(self.crm.tasks_.values()))
        self.client.post(f"/tasks/{task['id']}", data={"title": "Позвонить поставщику",
                                                        "assignee": "none", "pay": "350"})
        self.assertEqual((task["title"], task["assignee_id"], task["pay"]),
                         ("Позвонить поставщику", None, D("350.00")))
        self.assertIn("Позвонить поставщику", self.get_ok(f"/tasks/{task['id']}"))
        self.assertEqual(self.client.get("/tasks/999999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
