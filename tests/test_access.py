"""Профили доступа: матрица «раздел × уровень», страж маршрутов, отдельные
права на журнал и на паспортные документы. Чистая логика и панель через
TestClient (обвязка из tests/test_web.py).
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False


def staff(**sections) -> dict:
    actions = sections.pop("actions", {})
    return {"perms": {"sections": sections, "actions": actions}}


class TestAccessLogic(unittest.TestCase):
    def test_section_for_path(self):
        self.assertEqual(logic.section_for("/"), "dashboard")
        self.assertEqual(logic.section_for("/clients"), "clients")
        self.assertEqual(logic.section_for("/clients/7/ledger"), "clients")
        self.assertEqual(logic.section_for("/billing/run"), "finance")
        self.assertEqual(logic.section_for("/profiles/3"), "staff")
        # выгрузки: точка - тоже граница раздела, иначе CSV был бы открыт всем
        self.assertEqual(logic.section_for("/clients.csv"), "clients")
        self.assertEqual(logic.section_for("/finance.csv"), "finance")
        # общие страницы разделу не принадлежат
        for path in ("/login", "/logout", "/me", "/me/password", "/healthz"):
            self.assertIsNone(logic.section_for(path), path)
        # префикс не должен цеплять чужой адрес
        self.assertIsNone(logic.section_for("/importer"))

    def test_levels(self):
        s = staff(bikes="edit", reports="view")
        self.assertTrue(logic.can_view(s, "bikes"))
        self.assertTrue(logic.can_edit(s, "bikes"))
        self.assertTrue(logic.can_view(s, "reports"))
        self.assertFalse(logic.can_edit(s, "reports"), "смотреть - не менять")
        self.assertFalse(logic.can_view(s, "finance"))
        self.assertFalse(logic.can_view(None, "bikes"), "без сотрудника прав нет")

    def test_unknown_sections_and_levels_are_dropped(self):
        perms = logic.normalize_perms({"sections": {"bikes": "edit", "ядерка": "edit",
                                                    "finance": "всё"},
                                       "actions": {"money_edit": True, "выдумка": True}})
        self.assertEqual(perms["sections"], {"bikes": "edit"})
        self.assertEqual(perms["actions"], {"money_edit": True})
        self.assertEqual(logic.normalize_perms(None),
                         {"sections": {}, "actions": {}})
        self.assertEqual(logic.normalize_perms('{"sections":{"bikes":"view"}}')["sections"],
                         {"bikes": "view"}, "jsonb без кодека приходит строкой")
        self.assertEqual(logic.normalize_perms("не json"), {"sections": {}, "actions": {}})

    def test_actions_are_separate_from_sections(self):
        s = staff(clients="edit")
        self.assertFalse(logic.can_act(s, "money_edit"))
        self.assertFalse(logic.can_act(s, "client_docs"))
        self.assertTrue(logic.can_act(staff(clients="edit", actions={"money_edit": True}),
                                      "money_edit"))

    def test_visible_sections_keep_menu_order(self):
        s = staff(reports="view", dashboard="view", bikes="edit")
        self.assertEqual(logic.visible_sections(s), ["dashboard", "bikes", "reports"])

    def test_built_in_profiles(self):
        codes = [p[0] for p in logic.BUILT_IN_PROFILES]
        self.assertEqual(codes, ["owner", "manager", "tech"])
        owner = dict(zip(("code", "name", "perms", "built_in"),
                         logic.BUILT_IN_PROFILES[0], strict=True))
        self.assertTrue(owner["built_in"])
        self.assertEqual(logic.visible_sections({"perms": owner["perms"]}),
                         list(logic.SECTIONS))
        for action in logic.ACTIONS:
            self.assertTrue(logic.can_act({"perms": owner["perms"]}, action), action)
        tech = {"perms": logic.BUILT_IN_PROFILES[2][2]}
        self.assertTrue(logic.can_edit(tech, "bikes"))
        self.assertFalse(logic.can_view(tech, "finance"), "механик денег не видит")
        self.assertFalse(logic.can_act(tech, "money_edit"))

    def test_inbox_section_is_found_by_path(self):
        """«Входящие» - свой раздел: страж берёт его по адресу, как любой
        другой, и формы карточки обращения закрыты тем же правом."""
        self.assertIn("inbox", logic.SECTIONS)
        self.assertIn(("/inbox", "inbox"), logic.SECTION_PATHS)
        for path in ("/inbox", "/inbox/7", "/inbox/7/reply", "/inbox/7/status",
                     "/inbox/7/client", "/inbox/7/answered", "/inbox/7/out/3/again"):
            self.assertEqual(logic.section_for(path), "inbox", path)
        self.assertIsNone(logic.section_for("/inboxes"), "префикс не цепляет чужой адрес")
        # Хук - не раздел панели: сотрудника у шлюза нет, защита у него своя.
        self.assertIsNone(logic.section_for("/hook/inbox"))
        self.assertEqual(logic.home_for(staff(inbox="view")), "/inbox")

    def test_inbox_is_owner_only_by_default(self):
        """Переписка с клиентами - ПДн: из встроенных профилей её видит
        только «Владелец», остальным раздел открывают руками."""
        profiles = {code: {"perms": perms}
                    for code, _name, perms, _built in logic.BUILT_IN_PROFILES}
        self.assertTrue(logic.can_edit(profiles["owner"], "inbox"))
        for code in ("manager", "tech"):
            self.assertNotIn("inbox", profiles[code]["perms"]["sections"], code)
            self.assertFalse(logic.can_view(profiles[code], "inbox"), code)
            self.assertNotIn("inbox", logic.visible_sections(profiles[code]), code)

    def test_schema_profiles_agree_on_inbox(self):
        """В базу встроенные профили кладёт литерал schema.sql, а не код:
        раздел, забытый в литерале, владелец не увидел бы вовсе."""
        schema = (Path(__file__).resolve().parent.parent / "schema.sql").read_text("utf-8")
        found = dict(re.findall(
            r"\('(owner|manager|tech)',\s*'[^']*',\s*'(\{.*?\})'::jsonb", schema))
        self.assertEqual(set(found), {"owner", "manager", "tech"})
        owner = json.loads(found["owner"])["sections"]
        self.assertEqual(owner.get("inbox"), "edit")
        self.assertEqual(set(owner), set(logic.SECTIONS),
                         "у владельца в базе должны быть все разделы кода")
        for code in ("manager", "tech"):
            self.assertNotIn("inbox", json.loads(found[code])["sections"], code)

    def test_home_for_a_narrow_profile(self):
        self.assertEqual(logic.home_for(staff(dashboard="view", bikes="edit")), "/")
        self.assertEqual(logic.home_for(staff(bikes="edit")), "/bikes")
        self.assertEqual(logic.home_for(staff(finance="view")), "/finance")
        self.assertEqual(logic.home_for(staff()), "/me", "профиль пуст - свой кабинет")

    def test_profile_name_is_checked(self):
        self.assertEqual(logic.check_profile_name("  Механик  ").value, "Механик")
        bad = logic.check_profile_name("")
        self.assertFalse(bad.ok)
        self.assertIn("Название профиля", bad.error)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestAccessInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def profile(self, code):
        return tw.run(self.crm.access_profile_by_code(code))

    def add(self, login, code, password="password-1"):
        """Сотрудник на встроенном профиле; возвращает его строку."""
        profile = self.profile(code)
        tw.run(self.crm.create_staff(login, logic.hash_password(password), login,
                                     "manager", profile["id"]))
        return tw.run(self.crm.staff_by_login(login))

    def as_(self, login, password="password-1"):
        self.client.post("/logout")
        r = self.login(login, password)
        self.assertEqual(r.status_code, 303, f"{login} не вошёл")

    # ─── страж разделов ───

    def test_owner_sees_everything(self):
        page = self.get_ok("/")
        for label in ("Клиенты", "Финансы", "Импорт", "Сотрудники"):
            self.assertIn(f">{label}</a>", page)
        self.get_ok("/staff")
        self.get_ok("/profiles")
        self.get_ok("/import")

    def test_tech_sees_only_his_sections(self):
        self.add("petr", "tech")
        self.as_("petr")
        page = self.get_ok("/")
        self.assertIn(">Парк</a>", page)
        for label in ("Клиенты", "Финансы", "Импорт", "Сотрудники", "Выдача"):
            self.assertNotIn(f">{label}</a>", page, label)
        self.get_ok("/bikes")
        for path in ("/clients", "/clients.csv", "/finance", "/finance.csv",
                     "/import", "/staff", "/profiles", "/issue"):
            self.assertEqual(self.client.get(path).status_code, 403, path)

    def test_denied_page_names_the_section(self):
        self.add("petr", "tech")
        self.as_("petr")
        r = self.client.get("/finance")
        self.assertEqual(r.status_code, 403)
        self.assertIn("Финансы", r.text)
        self.assertIn("Механик", r.text)

    def test_view_only_section_refuses_post(self):
        self.add("ivan", "manager")
        self.as_("ivan")
        self.get_ok("/tariffs")                          # менеджер тарифы видит
        r = self.client.post("/tariffs", data={"name": "День", "period_days": "1",
                                               "price": "600"})
        self.assertEqual(r.status_code, 403, "смотреть можно, менять нельзя")
        self.assertEqual(len(tw.run(self.crm.tariffs())), 1)

    def test_own_password_and_logout_are_always_open(self):
        petr = self.add("petr", "tech")
        self.as_("petr")
        page = self.get_ok("/me")
        self.assertIn("Механик", page)
        self.assertIn("Сменить свой пароль", page)
        self.assertIn("нет доступа", page, "закрытые разделы тоже видны списком")
        r = self.client.post("/me/password", data={"old": "password-1",
                                                   "new": "password-2"})
        self.assertEqual(r.status_code, 303)
        self.assertTrue(logic.verify_password(
            "password-2", tw.run(self.crm.staff_by_id(petr["id"]))["password_hash"]))
        self.assertEqual(self.client.post("/logout").status_code, 303)

    def test_money_is_hidden_without_the_finance_section(self):
        """«Смотреть сводку» не должно обходить право на деньги."""
        self.add("petr", "tech")
        self.as_("petr")
        page = self.get_ok("/")
        self.assertIn("простой парка", page)
        for money in ("средний чек", "потеряно на простое", "поступило за месяц",
                      "Должники", "Начислить сейчас", "Баланс"):
            self.assertNotIn(money, page, money)

    def test_rental_list_without_finance_shows_no_rubles(self):
        tw.run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=tw.D(3000), billing="auto",
            started_on=tw.date.today(), contract_no=None, created_by="t"))
        self.add("petr", "tech")
        self.as_("petr")
        page = self.get_ok("/rentals")
        self.assertIn("Неделя", page)
        self.assertNotIn("<th>Баланс</th>", page)
        self.assertNotIn("3 000", page)
        self.assertIn("платёж сегодня", page, "срок возврата механику нужен")
        self.assertNotIn("Быстрая выдача", page)
        self.assertNotIn(">Форма<", page)

    def test_rental_card_without_finance_shows_no_rubles(self):
        rental_id = tw.run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=tw.D(3000), billing="auto",
            started_on=tw.date.today(), contract_no=None, created_by="t"))
        self.add("petr", "tech")
        self.as_("petr")
        page = self.get_ok(f"/rentals/{rental_id}")
        self.assertIn("Неделя", page)
        self.assertNotIn("3 000", page)
        self.assertNotIn("Баланс клиента", page)
        self.assertNotIn("Начисления по аренде", page)
        self.assertNotIn("Закрыть аренду", page, "аренды механик только смотрит")

    def test_view_only_pages_show_no_edit_controls(self):
        """Менеджер парк только смотрит: формы гасятся, но страница
        остаётся читаемой — без неё он не найдёт свободный велосипед."""
        self.add("ivan", "manager")
        self.as_("ivan")
        page = self.get_ok(f"/bikes/{self.bike_id}")
        self.assertIn("Kugoo V3", page)
        self.assertIn('<fieldset disabled class="plain">', page)
        self.assertNotIn("Записать ремонт", page)
        self.assertNotIn("+ Велосипед", self.get_ok("/bikes"))
        r = self.client.post(f"/bikes/{self.bike_id}/repair",
                             data={"node": "brakes", "parts_cost": "500"})
        self.assertEqual(r.status_code, 403)
        # «завести новое» - правка, хотя это и GET
        self.assertEqual(self.client.get("/bikes/new").status_code, 403)
        self.get_ok("/rentals/new")                      # аренды менеджер правит
        page = self.get_ok("/tariffs")
        self.assertIn("Неделя", page)
        self.assertIn("только на просмотр", page)
        self.assertNotIn("Новый тариф", page)

    def test_reports_keep_repairs_but_hide_money(self):
        self.add("petr", "tech")
        self.as_("petr")
        page = self.get_ok("/reports")
        self.assertIn("Что ломается", page, "отчёт по узлам - работа механика")
        self.assertIn("Какая модель дороже", page)
        for money in ("Деньги по месяцам", "Должники", "Чек/день", "<th>Потери</th>",
                      "амортизация в месяц"):
            self.assertNotIn(money, page, money)

    def test_login_lands_on_the_first_allowed_section(self):
        self.add("petr", "tech")
        self.client.post("/logout")
        r = self.login("petr", "password-1")
        self.assertEqual(r.headers["location"], "/")
        # профиль без сводки: вход ведёт в первый открытый раздел
        pid = tw.run(self.crm.create_access_profile(
            "Только парк", {"sections": {"bikes": "edit"}, "actions": {}}))
        petr = tw.run(self.crm.staff_by_login("petr"))
        tw.run(self.crm.set_staff_profile(petr["id"], pid))
        self.client.post("/logout")
        r = self.login("petr", "password-1")
        self.assertEqual(r.headers["location"], "/bikes")
        self.assertEqual(self.client.get("/").status_code, 403)

    # ─── отдельные права ───

    def test_money_edit_hides_and_blocks_the_ledger_form(self):
        self.add("ivan", "manager")
        self.as_("ivan")
        page = self.get_ok(f"/clients/{self.client_id}")
        self.assertNotIn("Записать в журнал", page)
        self.assertIn("Журнал операций", page, "смотреть журнал менеджер может")
        r = self.client.post(f"/clients/{self.client_id}/ledger",
                             data={"kind": "payment", "amount": "1000"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(tw.run(self.crm.client_balance(self.client_id)), 0)

    def test_client_csv_needs_finance(self):
        """В выгрузке колонка «Баланс» — без финансов её не отдавать."""
        self.assertIn("CSV", self.get_ok("/clients"))          # владельцу можно
        self.add("petr", "tech")
        self.as_("petr")
        self.assertEqual(self.client.get("/clients.csv").status_code, 403)

    def test_money_edit_when_granted(self):
        profile = self.profile("manager")
        perms = logic.normalize_perms(profile["perms"])
        perms["actions"]["money_edit"] = True
        tw.run(self.crm.update_access_profile(profile["id"], name=profile["name"],
                                              perms=perms))
        self.add("ivan", "manager")
        self.as_("ivan")
        self.assertIn("Записать в журнал", self.get_ok(f"/clients/{self.client_id}"))
        r = self.client.post(f"/clients/{self.client_id}/ledger",
                             data={"kind": "payment", "amount": "1000"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(tw.run(self.crm.client_balance(self.client_id)), 1000)

    def test_client_docs_guards_the_contract(self):
        self.db.users[5001] = {"tg_id": 5001, "contract_status": "signed",
                               "contract_path": "/tmp/kyc/нет.pdf", "phone": "+79990000000"}
        self.add("ivan", "manager")
        self.as_("ivan")
        page = self.get_ok(f"/clients/{self.client_id}")
        self.assertNotIn("скачать подписанный", page)
        r = self.client.get(f"/clients/{self.client_id}/contract")
        self.assertEqual(r.status_code, 403)
        self.assertIn("Паспортные документы", r.text)

    def test_client_docs_guards_the_same_file_in_the_signing_packet(self):
        """Тот же договор с паспортными данными открывался со страницы
        заявки на подпись по одному лишь праву на раздел «Клиенты» -
        и отдельное право `client_docs` не защищало ничего."""
        self.db.users[5001] = {"tg_id": 5001, "contract_status": "signed",
                               "contract_path": "/tmp/kyc/нет.pdf",
                               "phone": "+79990000000"}
        req = tw.run(self.crm.create_sign_request(
            client_id=self.client_id, rental_id=None, token="tok-1",
            docs=[{"title": "Договор", "path": "/tmp/kyc/нет.pdf",
                   "sha256": "x"}],
            agreement="соглашение",
            expires_at=datetime.now(UTC) + timedelta(days=1), by="admin"))
        self.add("ivan", "manager")
        self.as_("ivan")
        page = self.get_ok(f"/signings/{req['id']}")
        self.assertNotIn(f"/signings/{req['id']}/doc/0", page)
        r = self.client.get(f"/signings/{req['id']}/doc/0")
        self.assertEqual(r.status_code, 403)
        self.assertIn("Паспортные документы", r.text)

    # ─── управление профилями ───

    def test_owner_edits_a_profile_and_rights_apply_at_once(self):
        self.add("ivan", "manager")
        profile = self.profile("manager")
        data = {"name": "Менеджер", "s_dashboard": "view", "s_clients": "edit",
                "s_finance": "edit", "a_money_edit": "1"}
        r = self.client.post(f"/profiles/{profile['id']}", data=data)
        self.assertEqual(r.status_code, 303)
        fresh = self.profile("manager")
        self.assertEqual(logic.normalize_perms(fresh["perms"])["sections"],
                         {"dashboard": "view", "clients": "edit", "finance": "edit"})
        self.as_("ivan")
        self.assertIn("Записать в журнал", self.get_ok(f"/clients/{self.client_id}"))
        self.assertEqual(self.client.get("/bikes").status_code, 403,
                         "снятое право действует сразу, без перезахода")

    def test_built_in_owner_profile_is_locked(self):
        owner = self.profile("owner")
        r = self.client.post(f"/profiles/{owner['id']}", data={"name": "Хозяин"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("не меняется", self.get_ok(f"/profiles/{owner['id']}"))
        self.assertEqual(self.profile("owner")["name"], "Владелец")
        self.client.post(f"/profiles/{owner['id']}/delete")
        self.assertIsNotNone(self.profile("owner"))

    def test_cannot_lock_yourself_out_of_the_staff_section(self):
        # владелец на своём же не встроенном профиле
        pid = tw.run(self.crm.create_access_profile(
            "Совладелец", {"sections": dict.fromkeys(logic.SECTIONS, "edit"),
                           "actions": {}}))
        me = tw.run(self.crm.staff_by_login("admin"))
        tw.run(self.crm.set_staff_profile(me["id"], pid))
        r = self.client.post(f"/profiles/{pid}", data={"name": "Совладелец",
                                                       "s_dashboard": "view"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("некому будет его вернуть", self.get_ok(f"/profiles/{pid}"))
        self.assertTrue(logic.can_edit(tw.run(self.crm.staff_by_id(me["id"])), "staff"))

    def test_profile_create_and_delete(self):
        r = self.client.post("/profiles", data={"name": "Точка Адоратского"})
        self.assertEqual(r.status_code, 303)
        pid = int(r.headers["location"].rsplit("/", 1)[1])
        self.assertIn("Точка Адоратского", self.get_ok("/profiles"))
        # занятое название - и при создании, и при переименовании
        self.client.post("/profiles", data={"name": "Точка Адоратского"})
        self.assertIn("уже есть", self.get_ok("/profiles"))
        other = tw.run(self.crm.create_access_profile("Точка Павлюхина", {}))
        self.client.post(f"/profiles/{other}", data={"name": "Точка Адоратского"})
        self.assertIn("уже есть", self.get_ok(f"/profiles/{other}"))
        self.assertEqual(tw.run(self.crm.access_profile(other))["name"], "Точка Павлюхина")
        # с сотрудником на профиле удаление не проходит
        tw.run(self.crm.create_staff("anna", logic.hash_password("password-1"),
                                     "Анна", "manager", pid))
        self.client.post(f"/profiles/{pid}/delete")
        self.assertIn("ещё есть сотрудники", self.get_ok(f"/profiles/{pid}"))
        anna = tw.run(self.crm.staff_by_login("anna"))
        tw.run(self.crm.set_staff_profile(anna["id"], self.profile("tech")["id"]))
        r = self.client.post(f"/profiles/{pid}/delete")
        self.assertEqual(r.headers["location"], "/profiles")
        self.assertEqual(self.client.get(f"/profiles/{pid}").status_code, 404)

    def test_new_staff_needs_a_profile(self):
        r = self.client.post("/staff", data={"login": "anna", "password": "password-1",
                                             "name": "Анна"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("Выберите профиль доступа", self.get_ok("/staff"))
        self.assertIsNone(tw.run(self.crm.staff_by_login("anna")))
        r = self.client.post("/staff", data={"login": "anna", "password": "password-1",
                                             "name": "Анна",
                                             "profile_id": self.profile("tech")["id"]})
        anna = tw.run(self.crm.staff_by_login("anna"))
        self.assertEqual(anna["profile_code"], "tech")
        self.assertEqual(anna["role"], "manager")

    def test_owner_profile_sets_the_admin_role(self):
        self.client.post("/staff", data={"login": "boss2", "password": "password-1",
                                         "name": "Второй",
                                         "profile_id": self.profile("owner")["id"]})
        self.assertEqual(tw.run(self.crm.staff_by_login("boss2"))["role"], "admin")

    def test_profile_is_switched_from_the_staff_list(self):
        petr = self.add("petr", "tech")
        r = self.client.post(f"/staff/{petr['id']}/profile",
                             data={"profile_id": self.profile("manager")["id"]})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(tw.run(self.crm.staff_by_id(petr["id"]))["profile_code"],
                         "manager")
        # свой профиль трогать нельзя
        me = tw.run(self.crm.staff_by_login("admin"))
        self.client.post(f"/staff/{me['id']}/profile",
                         data={"profile_id": self.profile("tech")["id"]})
        self.assertIn("Свой профиль менять нельзя", self.get_ok("/staff"))
        self.assertEqual(tw.run(self.crm.staff_by_id(me["id"]))["profile_code"], "owner")

    def test_first_admin_gets_the_owner_profile(self):
        admin = tw.run(self.crm.staff_by_login("admin"))
        self.assertEqual(admin["profile_code"], "owner")
        self.assertTrue(admin["profile_built_in"])
        self.assertEqual(logic.visible_sections(admin), list(logic.SECTIONS))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestInboxHookIsPublic(tw.WebCase):
    """Хук «Входящих» открыт без входа в панель: у шлюза WhatsApp и n8n
    сотрудника нет. Страж входа его не заворачивает на /login - отвечает
    сам хук своим токеном. Сама лента при этом закрыта, как любой раздел."""

    TOKEN = "hook-token-access"

    def build(self, **over):
        self.cfg = dataclasses.replace(self.cfg, **over)
        self.app = tw.create_app(crm=self.crm, db=self.db, cfg=self.cfg, bot=self.bot)
        self.client = tw.TestClient(self.app, follow_redirects=False)

    def test_hook_prefix_is_in_public(self):
        from app.web import app as web_app
        self.assertTrue("/hook/inbox".startswith(web_app.PUBLIC))
        for path in ("/inbox", "/inbox/1", "/inbox/1/reply"):
            self.assertFalse(path.startswith(web_app.PUBLIC), path)

    def test_anonymous_hook_is_answered_by_the_hook_not_the_login(self):
        # токена нет - хука нет, но и не редирект на вход
        r = self.client.post("/hook/inbox", json={})
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("location", r.headers)
        self.build(inbox_hook_token=self.TOKEN)
        r = self.client.post("/hook/inbox", json={})
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("location", r.headers)
        r = self.client.post("/hook/inbox", json={},
                             headers={"Authorization": f"Bearer {self.TOKEN}"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertNotIn("set-cookie", r.headers, "хук сессию не заводит")

    def test_inbox_pages_still_need_login(self):
        self.build(inbox_hook_token=self.TOKEN)
        for path in ("/inbox", "/inbox/1"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 303, path)
            self.assertTrue(r.headers["location"].startswith("/login?next="), path)
        r = self.client.post("/inbox/1/reply", data={"text": "x"},
                             headers={"Authorization": f"Bearer {self.TOKEN}"})
        self.assertEqual(r.status_code, 303, "токен хука - не вход в панель")
        self.assertTrue(r.headers["location"].startswith("/login"))


if __name__ == "__main__":
    unittest.main()


class TestSessionHardening(tw.WebCase if tw.HAVE_WEB else unittest.TestCase):
    """Сессия привязана к паролю, адрес возврата - только свой, и
    страницы нельзя встроить в чужой сайт."""

    def test_backslash_next_does_not_leave_the_panel(self):
        for bad in ("/\\evil.example", "//evil.example", "https://evil.example",
                    "/ok\\@evil.example"):
            r = self.client.post("/login", data={"login": "admin",
                                                 "password": "admin-pass-123",
                                                 "next": bad})
            self.assertEqual(r.headers["location"], "/", bad)
            self.client.post("/logout")
        r = self.client.post("/login", data={"login": "admin",
                                             "password": "admin-pass-123",
                                             "next": "/clients?q=1"})
        self.assertEqual(r.headers["location"], "/clients?q=1")

    def test_security_headers(self):
        self.login()
        r = self.client.get("/")
        self.assertEqual(r.headers["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", r.headers["content-security-policy"])
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        self.assertEqual(r.headers["referrer-policy"], "same-origin")
        self.assertNotIn("strict-transport-security", r.headers, "без домена https нет")
        self.assertEqual(self.client.get("/login").headers["x-frame-options"], "DENY")

    def test_password_change_ends_other_sessions(self):
        from fastapi.testclient import TestClient
        self.login()
        other = TestClient(self.app, follow_redirects=False)
        other.post("/login", data={"login": "admin", "password": "admin-pass-123"})
        self.assertEqual(other.get("/").status_code, 200)
        r = self.client.post("/me/password", data={"old": "admin-pass-123",
                                                   "new": "new-pass-456789"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.client.get("/").status_code, 200, "своя сессия жива")
        self.assertEqual(other.get("/").status_code, 303, "чужая выбита")
        self.assertTrue(other.get("/").headers["location"].startswith("/login"))
