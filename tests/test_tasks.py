"""Задачи дня (app/crm/tasks.py): поручения сотрудникам, незакреплённые
задачи точки, «Взять» и «Сделано», доска команды."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import logic, tasks

try:
    from app.crm import notify
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
        self.assertIn("прошла", tasks.parse_form({"title": "x", "due_on": "2026-09-01"},
                                                 staff=OWNER, today=TODAY)[1])
        self.assertIn("Оплата исполнителю - число",
                      tasks.parse_form({"title": "x", "pay": "много"}, staff=OWNER,
                                       today=TODAY)[1])
        for bad in ("NaN", "-1", "1e9"):
            self.assertIn("от нуля", tasks.parse_form({"title": "x", "pay": bad},
                                                      staff=OWNER, today=TODAY)[1], bad)

    def test_two_dates(self):
        """«Дата постановки» (пусто - сегодня, можно в будущем) и «Дата
        выполнения» (необязательна, не раньше постановки); в правке
        неизменённая прошедшая дата проходит."""
        fields, _ = tasks.parse_form({"title": "x"}, staff=ADMIN, today=TODAY)
        self.assertEqual((fields["start_on"], fields["due_on"]), (TODAY, None))
        fields, _ = tasks.parse_form({"title": "x", "start_on": "2026-10-09",
                                      "due_on": "2026-10-12"}, staff=ADMIN, today=TODAY)
        self.assertEqual((fields["start_on"], fields["due_on"]),
                         (date(2026, 10, 9), date(2026, 10, 12)))
        self.assertEqual(tasks.parse_form({"title": "x", "start_on": "2026-10-09",
                                           "due_on": "2026-10-08"}, staff=ADMIN,
                                          today=TODAY)[1],
                         "Дата выполнения раньше даты постановки.")
        self.assertIn("постановки уже прошла",
                      tasks.parse_form({"title": "x", "start_on": "2026-10-01"}, staff=ADMIN,
                                       today=TODAY)[1])
        self.assertEqual(tasks.parse_form({"title": "x", "start_on": "завтра"}, staff=ADMIN,
                                          today=TODAY)[1], "Дата постановки - дата.")
        old = {"start_on": date(2026, 9, 20), "due_on": date(2026, 9, 25)}
        fields, problem = tasks.parse_form({"title": "переименовали", "start_on": "2026-09-20",
                                            "due_on": "2026-09-25"}, staff=ADMIN,
                                           today=TODAY, task=old)
        self.assertIsNone(problem, "просроченную можно переименовать")
        self.assertIn("уже прошла", tasks.parse_form(
            {"title": "x", "start_on": "2026-09-20", "due_on": "2026-09-26"}, staff=ADMIN,
            today=TODAY, task=old)[1], "новую прошедшую дату - нет")

    def test_planned_tasks(self):
        """Дата постановки впереди: в «Задачах дня» задачи нет ни у
        исполнителя, ни в общих; автор видит её в своих запланированных,
        доска - в «Запланированных»."""
        later = TODAY + timedelta(days=3)
        rows = [{"id": 1, "status": "open", "title": "сегодня", "assignee_id": 3,
                 "start_on": TODAY, "created_by_id": 2},
                {"id": 2, "status": "open", "title": "потом", "assignee_id": 3,
                 "start_on": later, "due_on": later, "created_by_id": 2},
                {"id": 3, "status": "open", "title": "потом всем", "assignee_id": None,
                 "location": "Павлюхина", "start_on": TODAY + timedelta(days=1),
                 "created_by_id": 2}]
        master = tasks.my_lists(MASTER, rows, today=TODAY)
        self.assertEqual([t["id"] for t in master["mine"]], [1])
        self.assertEqual((master["free"], master["planned"]), ([], []))
        author = tasks.my_lists(ADMIN, rows, today=TODAY)
        self.assertEqual([t["id"] for t in author["planned"]], [3, 2], "по дате постановки")
        self.assertEqual(author["planned"][0]["start_text"], "с завтра")
        self.assertEqual(author["planned"][1]["start_text"], f"с {later:%d.%m}")
        people = [{"id": 3, "name": "Марат", "location": "Павлюхина"}]
        view = tasks.overview(rows, people, staff=OWNER, today=TODAY)
        self.assertEqual(([t["id"] for t in view["planned"]], view["open"]), ([3, 2], 1))
        self.assertEqual([t["id"] for t in view["people"][0]["tasks"]], [1])
        board = tasks.points_board(rows, people, staff=OWNER, today=TODAY)
        self.assertEqual(board["free"], [], "в «Задачах дня» владельца - тоже нет")
        self.assertEqual(sum(len(p["tasks"]) for g in board["points"] for p in g["people"]), 1)
        # в день постановки - обычная задача
        on_day = tasks.my_lists(MASTER, rows, today=later)
        self.assertEqual([t["id"] for t in on_day["mine"]], [2, 1], "сегодняшняя горит первой")
        self.assertEqual(tasks.due_label(rows[1], today=later + timedelta(days=2)),
                         ("просрочена на 2 дн.", True), "просрочка - по дате выполнения")

    def test_assignees(self):
        """«Кому»: все действующие, кроме себя, без фильтра по точке и роли;
        подпись - имя, роль, точка; истёкший доступ - не в списке, а
        исполнитель правимой задачи остаётся."""
        people = [{"id": 1, "name": "Владелец", "profile_name": "Владелец"},
                  {"id": 3, "name": "Булат", "profile_name": "Мастер", "location": "Павлюхина"},
                  {"id": 4, "name": "Анна", "profile_name": "Администратор: только задачи",
                   "location": "Адоратского"},
                  {"id": 5, "name": "Склад", "profile_name": "Мастер"},
                  {"id": 6, "name": "Ушёл", "profile_name": "Мастер",
                   "expires_at": datetime(2020, 1, 1, tzinfo=UTC)}]
        got = tasks.assignees(people, staff=OWNER)
        self.assertEqual([o["label"] for o in got],
                         ["Склад · Мастер", "Анна · Администратор: только задачи · Адоратского",
                          "Булат · Мастер · Павлюхина"])
        self.assertNotIn(1, [o["id"] for o in got], "себе - «мне»")
        self.assertEqual([o["id"] for o in tasks.assignees(people, staff=MASTER)], [1, 5, 4],
                         "мастер ставит и владельцу, и другой точке")
        kept = tasks.assignees(people, staff=OWNER,
                               task={"assignee_id": 6, "assignee_name": "Ушёл"})
        self.assertEqual(kept[-1], {"id": 6, "where": "", "label": "Ушёл · нет доступа"})

    def test_board_counts_with_work(self):
        people = [{"id": 2, "name": "Админ", "location": "Адоратского"}]
        rows = [{"id": 1, "status": "open", "title": "a", "assignee_id": 2,
                 "start_on": TODAY, "due_on": TODAY}]
        work = {"Адоратского": [{"code": "overdue", "count": 4, "hot": 4},
                                {"code": "soon", "count": 3, "hot": 0}],
                None: [{"code": "claims", "count": 1, "hot": 0}]}
        board = tasks.points_board(rows, people, staff=OWNER, today=TODAY,
                                   points=["Павлюхина", "Адоратского"], work=work)
        by = {g["key"]: g for g in board["points"]}
        self.assertEqual(by["Адоратского"]["counts"],
                         "1 сотрудник · 7 рабочих задач · 1 поручение · горит 5")
        self.assertEqual(by["Павлюхина"]["counts"], "0 сотрудников · задач нет")
        self.assertEqual([g["code"] for g in board["work_free"]], ["claims"])
        self.assertEqual(tasks.counts_text(0, 0, 2, work=1), "2 сотрудника · 1 рабочая задача")

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

    def test_points_board(self):
        """Владельцу: группа - точка сотрудника (порядок справочника, потом
        прочие, «Без точки» последней), сам он не в группах; общие -
        незакреплённые, переключатель режет группы и общие."""
        people = [{"id": 1, "name": "Владелец"},
                  {"id": 2, "name": "Админ", "location": "Адоратского"},
                  {"id": 3, "name": "Марат", "location": "Павлюхина"},
                  {"id": 4, "name": "Подменный"},
                  {"id": 5, "name": "Закрытая", "location": "Старая"}]
        rows = [{"id": 1, "status": "open", "title": "своя", "assignee_id": 1},
                {"id": 2, "status": "open", "title": "марату", "assignee_id": 3,
                 "location": "Адоратского", "due_on": TODAY},
                {"id": 3, "status": "open", "title": "марату 2", "assignee_id": 3},
                {"id": 4, "status": "open", "title": "уволенному", "assignee_id": 9,
                 "assignee_name": "Ушёл"},
                {"id": 5, "status": "open", "title": "общая П", "assignee_id": None,
                 "location": "Павлюхина"},
                {"id": 6, "status": "open", "title": "общая сети", "assignee_id": None},
                {"id": 7, "status": "done", "title": "сделана", "assignee_id": 3}]
        board = tasks.points_board(rows, people, staff=OWNER, today=TODAY,
                                   points=["Павлюхина", "Адоратского", "Победы"])
        self.assertEqual([g["title"] for g in board["points"]],
                         ["Павлюхина", "Адоратского", "Победы", "Старая", "Без точки"])
        by = {g["key"]: g for g in board["points"]}
        self.assertEqual([p["name"] for p in by["Павлюхина"]["people"]], ["Марат"])
        self.assertEqual([t["id"] for t in by["Павлюхина"]["people"][0]["tasks"]], [2, 3],
                         "все задачи человека - у его точки, а не у точки задачи")
        self.assertEqual(by["Павлюхина"]["counts"], "1 сотрудник · 2 задачи · горит 1")
        self.assertEqual(by["Победы"]["people"], [], "открытая точка без людей видна")
        self.assertEqual(by["Победы"]["counts"], "0 сотрудников · задач нет")
        self.assertEqual([p["name"] for p in by["none"]["people"]], ["Ушёл", "Подменный"],
                         "без точки и отключённый исполнитель")
        everyone = [p["id"] for g in board["points"] for p in g["people"]]
        self.assertNotIn(1, everyone, "свои задачи - в «Задачах владельца»")
        self.assertEqual([t["id"] for t in board["free"]], [5, 6])
        self.assertEqual((board["place"], board["place_title"], board["point"]), ("", "", ""))
        self.assertEqual(board["choices"][0], ("Павлюхина", "Павлюхина"))
        self.assertEqual(board["choices"][-1], ("none", "Без точки"))
        one = tasks.points_board(rows, people, staff=OWNER, today=TODAY,
                                 points=["Павлюхина", "Адоратского"], location="Адоратского")
        self.assertEqual([g["title"] for g in one["points"]], ["Адоратского"])
        self.assertEqual([t["id"] for t in one["free"]], [6], "чужая точка - нет, сеть - да")
        self.assertEqual((one["place_title"], one["point"]), ("Адоратского", "Адоратского"))
        self.assertEqual(len(one["choices"]), 4, "переключатель помнит все точки")
        none = tasks.points_board(rows, people, staff=OWNER, today=TODAY, location="none")
        self.assertEqual([t["id"] for t in none["free"]], [5, 6],
                         "люди без точки видят все незакреплённые, как в своих /my")
        self.assertEqual((none["place_title"], none["point"]), ("Без точки", ""))
        alien = tasks.points_board(rows, people, staff=OWNER, today=TODAY, location="<b>")
        self.assertEqual((alien["place"], len(alien["points"])), ("", 4))

    def test_plural(self):
        for n, word in ((1, "1 задача"), (2, "2 задачи"), (5, "5 задач"), (11, "11 задач"),
                        (21, "21 задача"), (14, "14 задач"), (22, "22 задачи")):
            self.assertEqual(tasks.plural(n, "задача", "задачи", "задач"), word)

    def test_safe_back(self):
        self.assertEqual(tasks.safe_back("/my"), "/my")
        self.assertEqual(tasks.safe_back("/tasks?location=none"), "/tasks?location=none")
        self.assertEqual(tasks.safe_back("/my?location=Павлюхина&next=//evil.example"),
                         "/my?location=%D0%9F%D0%B0%D0%B2%D0%BB%D1%8E%D1%85%D0%B8%D0%BD%D0%B0")
        for bad in ("https://evil.example", "//evil.example/my", "/clients", None, "",
                    "/my/../clients", "/clients#common"):
            self.assertEqual(tasks.safe_back(bad), "/my", bad)
        # якорь блока - только свой: кнопка возвращает туда, где нажали
        self.assertEqual(tasks.safe_back("/my?location=none#common"), "/my?location=none#common")
        self.assertEqual(tasks.safe_back("/my#owner"), "/my#owner")
        self.assertEqual(tasks.safe_back("/my#x\"><script>"), "/my")
        self.assertEqual(tasks.safe_back("/tasks?location=a#b#common"), "/tasks?location=a")
        self.assertEqual(tasks.back_url("/my", "Павлюхина", "points"),
                         "/my?location=%D0%9F%D0%B0%D0%B2%D0%BB%D1%8E%D1%85%D0%B8%D0%BD%D0%B0"
                         "#points")


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

    def test_anyone_assigns_anyone(self):
        """Ставит задачу любой: администратор «только задачи» - мастеру
        другой точки и владельцу; оплату за задачу - только руководитель.
        В «Кому» - все действующие с ролью и точкой; ссылка завести
        человека - тому, кто ведёт «Сотрудников»."""
        bulat = self.add_staff("bulat", "tasks_tech", location="Павлюхина", tg_id=7002)
        self.add_staff("dina", "tasks_operator", location="Адоратского")
        owner = run(self.crm.staff_by_login("admin"))
        self.as_("dina")
        my = self.get_ok("/my")
        self.assertIn("+ Поставить задачу", my)
        self.assertIn(f'<option value="{bulat}">Bulat · Мастер: только задачи · Павлюхина'
                      "</option>", my)
        self.assertIn(f'<option value="{owner["id"]}">', my, "и владельцу")
        self.assertNotIn("Заведите его", my, "заводить людей - не её право")
        self.assertNotIn("Оплата исполнителю", my)
        self.client.post("/tasks", data={"title": "Подкачать колёса", "assignee": str(bulat),
                                         "pay": "900", "back": "/my"})
        task = next(t for t in self.crm.tasks_.values() if t["title"] == "Подкачать колёса")
        self.assertEqual((task["assignee_id"], task["pay"]), (bulat, None))
        self.assertTrue(any("Подкачать колёса" in text for chat, text in self.bot.sent
                            if chat == 7002), "мастер узнаёт сразу")
        self.client.post("/tasks", data={"title": "Проверить кассу", "assignee": str(owner["id"])})
        self.assertTrue(any(t["title"] == "Проверить кассу" and t["assignee_id"] == owner["id"]
                            for t in self.crm.tasks_.values()))
        self.as_("bulat")
        self.assertIn("Подкачать колёса", self.get_ok("/my"))
        self.client.post("/logout")
        self.login()
        page = self.get_ok("/my")
        self.assertIn("Нет нужного человека? Заведите его в", page)
        self.assertIn("Оплата исполнителю за задачу, ₽", page)
        self.assertIn("сверх оклада", page)
        self.assertIn("Дата постановки", page)
        self.assertIn("Дата выполнения", page)

    def test_planned_task_waits_for_its_day(self):
        """Дата постановки впереди: исполнитель не видит задачу и не получает
        сообщения до её дня; доска - в «Запланированных», автор - у себя.
        В день постановки бот говорит утром, один раз."""
        marat = self.add_staff("marat", "tech", location="Павлюхина", tg_id=7001)
        self.login()
        today = date.today()
        later = today + timedelta(days=2)
        r = self.client.post("/tasks", data={
            "title": "Инвентаризация склада", "assignee": str(marat), "back": "/my",
            "start_on": later.isoformat(), "due_on": (later + timedelta(days=1)).isoformat()})
        self.assertEqual(r.status_code, 303)
        task = next(iter(self.crm.tasks_.values()))
        self.assertEqual((task["start_on"], task["notified_at"]), (later, None))
        self.assertFalse([1 for chat, _ in self.bot.sent if chat == 7001],
                         "сообщение - в день постановки, а не сейчас")
        board = self.get_ok("/tasks")
        planned = board.split('id="planned"')[1].split("</section>")[0]
        self.assertIn("Инвентаризация склада", planned)
        self.assertIn(f"с {later:%d.%m}", planned)
        self.assertNotIn("Инвентаризация склада", board.split('id="planned"')[0])
        mine = self.get_ok("/my")
        self.assertIn("Запланировано мной", mine)
        self.assertNotIn("Инвентаризация склада",
                         mine.split('id="points"')[1].split('id="planned"')[0])
        card = self.get_ok(f"/tasks/{task['id']}")
        self.assertIn("Дата постановки", card)
        self.assertIn(f"{later:%d.%m.%Y}", card)
        self.as_("marat")
        self.assertNotIn("Инвентаризация склада", self.get_ok("/my"))
        # бот: накануне и ночью молчит, утром дня постановки - один раз
        evening = datetime.combine(later, time(23, 0))
        self.assertEqual(run(notify.announce_tasks(self.bot, self.crm, now=evening)), 0)
        before = datetime.combine(later - timedelta(days=1), time(10, 0))
        self.assertEqual(run(notify.announce_tasks(self.bot, self.crm, now=before)), 0)
        morning = datetime.combine(later, time(9, 15))
        self.assertEqual(run(notify.announce_tasks(self.bot, self.crm, now=morning)), 1)
        said = [text for chat, text in self.bot.sent if chat == 7001]
        self.assertTrue(said and "Инвентаризация склада" in said[-1])
        self.assertIn("Дата выполнения", said[-1])
        self.assertIn("От: Владелец", said[-1], "от кого - имя автора")
        self.assertEqual(run(notify.announce_tasks(self.bot, self.crm, now=morning)), 0,
                         "второй раз не говорит")

    def test_edit_moves_planned_to_today(self):
        """Запланированную перенесли на сегодня - исполнитель узнаёт сразу;
        новому исполнителю запланированной - в день постановки."""
        marat = self.add_staff("marat", "tech", location="Павлюхина", tg_id=7001)
        anna = self.add_staff("anna", "manager", location="Адоратского", tg_id=7003)
        self.login()
        today = date.today()
        later = today + timedelta(days=3)
        self.client.post("/tasks", data={"title": "Покрасить стойку", "assignee": str(marat),
                                         "start_on": later.isoformat()})
        task = next(iter(self.crm.tasks_.values()))
        self.client.post(f"/tasks/{task['id']}", data={
            "title": "Покрасить стойку", "assignee": str(anna), "start_on": later.isoformat()})
        self.assertEqual((task["assignee_id"], task["notified_at"]), (anna, None))
        self.assertFalse([1 for chat, _ in self.bot.sent if chat in (7001, 7003)])
        self.client.post(f"/tasks/{task['id']}", data={
            "title": "Покрасить стойку", "assignee": str(anna), "start_on": today.isoformat()})
        self.assertIsNotNone(task["notified_at"])
        self.assertTrue(any("Покрасить стойку" in text for chat, text in self.bot.sent
                            if chat == 7003))
        r = self.client.post(f"/tasks/{task['id']}", data={
            "title": "Покрасить стойку", "assignee": str(anna), "start_on": today.isoformat(),
            "due_on": (today - timedelta(days=5)).isoformat()})
        self.assertEqual(r.status_code, 303)
        self.assertIsNone(task["due_on"], "прошедшая дата выполнения не принимается")

    def test_former_staff_not_told(self):
        """Исполнителя отключили или его доступ истёк до дня постановки:
        ни бот утром, ни правка с переносом на сегодня ему не пишут -
        задача отмечена сказанной и в очереди не висит."""
        marat = self.add_staff("marat", "tech", location="Павлюхина", tg_id=7001)
        anna = self.add_staff("anna", "manager", location="Адоратского", tg_id=7003)
        self.login()
        today = date.today()
        later = today + timedelta(days=2)
        for title, who in (("Сдать ключи", marat), ("Сверить кассу", anna),
                           ("Вернуть пропуск", marat)):
            self.client.post("/tasks", data={"title": title, "assignee": str(who),
                                             "start_on": later.isoformat()})
        keys, cash, badge = (next(t for t in self.crm.tasks_.values() if t["title"] == name)
                             for name in ("Сдать ключи", "Сверить кассу", "Вернуть пропуск"))
        run(self.crm.set_staff_active(marat, False))
        run(self.crm.set_staff_expires(anna, datetime.now(UTC) - timedelta(minutes=1)))
        # правка: исполнитель прежний («нет доступа»), постановку - на сегодня
        self.client.post(f"/tasks/{badge['id']}", data={
            "title": "Вернуть пропуск", "assignee": str(marat), "start_on": today.isoformat()})
        self.assertEqual((badge["start_on"], badge["assignee_id"]), (today, marat))
        self.assertIsNotNone(badge["notified_at"])
        morning = datetime.combine(later, time(9, 15))
        self.assertEqual(run(notify.announce_tasks(self.bot, self.crm, now=morning)), 0)
        self.assertIsNotNone(keys["notified_at"], "отметка без сообщения")
        self.assertIsNotNone(cash["notified_at"])
        self.assertFalse([1 for chat, _ in self.bot.sent if chat in (7001, 7003)],
                         "бывшим сотрудникам задачи не пишем")
        # постановщик не может выбрать истёкшего и отключённого
        self.client.post("/tasks", data={"title": "Пересчёт", "assignee": str(anna)})
        self.assertFalse([1 for t in self.crm.tasks_.values() if t["title"] == "Пересчёт"])

    def test_edit_by_assignee_and_race(self):
        """Запланированную переносит на сегодня сам исполнитель - ему не
        пишут ни панель, ни бот; бот успел сказать раньше, чем панель
        сохранила перенос, - второго сообщения нет (отметка условием в
        UPDATE)."""
        self.add_staff("marat", "tasks_tech", location="Павлюхина", tg_id=7001)
        anna = self.add_staff("anna", "manager", location="Адоратского", tg_id=7003)
        owner = run(self.crm.staff_by_login("admin"))
        run(self.crm.link_staff_tg(owner["id"], 7000, "admin"))
        today = date.today()
        later = today + timedelta(days=3)
        self.as_("marat")
        self.client.post("/tasks", data={"title": "Заказать цепи",
                                         "assignee": str(owner["id"]),
                                         "start_on": later.isoformat()})
        self.client.post("/tasks", data={"title": "Обзвонить должников",
                                         "assignee": str(anna),
                                         "start_on": later.isoformat()})
        chains, calls = (next(t for t in self.crm.tasks_.values() if t["title"] == name)
                         for name in ("Заказать цепи", "Обзвонить должников"))
        self.assertIsNone(chains["notified_at"])
        self.client.post("/logout")
        self.login()
        self.client.post(f"/tasks/{chains['id']}", data={
            "title": "Заказать цепи", "assignee": "me", "start_on": today.isoformat()})
        self.assertEqual(chains["start_on"], today)
        self.assertIsNotNone(chains["notified_at"], "правил сам - уже знает")
        noon = datetime.combine(today, time(12, 0))
        self.assertEqual(run(notify.announce_tasks(self.bot, self.crm, now=noon)), 0)
        self.assertFalse([1 for chat, _ in self.bot.sent if chat == 7000])
        # гонка: бот сказал Анне, пока владелец сохранял перенос на сегодня
        update_task = self.crm.update_task

        async def bot_first(task_id, **fields):
            self.crm.tasks_[task_id]["notified_at"] = datetime.now(UTC)
            return await update_task(task_id, **fields)

        self.crm.update_task = bot_first
        try:
            self.client.post(f"/tasks/{calls['id']}", data={
                "title": "Обзвонить должников", "assignee": str(anna),
                "start_on": today.isoformat()})
        finally:
            self.crm.update_task = update_task
        self.assertEqual(calls["start_on"], today)
        self.assertFalse([1 for chat, _ in self.bot.sent if chat == 7003],
                         "бот уже сказал - панель не повторяет")


if __name__ == "__main__":
    unittest.main()
