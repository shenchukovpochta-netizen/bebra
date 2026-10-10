"""Мастер первого запуска: «Готовность» по шагам для новой установки.

Правила - чистые функции app/crm/firstrun.py, шаги пишут через те же
помощники панели, что и страницы разделов. Здесь стерегут: кого и когда
мастер встречает (владельца свежей установки, пока не готово
обязательное - и больше никого и никогда), что каждый шаг пишет ровно то
же, что его раздел, и повтор ничего не удваивает, что прогресс живёт в
настройках, а скрытый мастер не возвращается сам.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.crm import company, firstrun, logic, readiness  # noqa: E402
from tests import test_web as tw  # noqa: E402
from tests.test_readiness import FILLED, facts  # noqa: E402

D = Decimal
TEMPLATE = ROOT / "app" / "web" / "templates" / "setup.html"
OWNER = {"login": "admin", "profile_code": "owner"}
POINT = {"action": "add", "name": "Центр", "city": "Самара", "address": "ул. Ленина, 1",
         "hours": "пн-вс: 10:00-19:00", "phone": "+7 900 000-00-00"}


def by_code(rows):
    return {r["code"]: r for r in rows}


class TestRules(unittest.TestCase):
    def fresh_items(self):
        return firstrun.essentials(settings={}, locations=[], models=[], tariffs=[],
                                   staff=[{"login": "admin", "active": True}])

    def test_steps_are_readiness_rows(self):
        """Своих проверок нет: состояние шага - строка «Готовности»."""
        items = readiness.checks(**facts())
        rows = by_code(firstrun.steps(items, {}))
        self.assertEqual(list(rows), list(firstrun.STEPS))
        for code in firstrun.ESSENTIAL:
            self.assertEqual(rows[code]["state"], by_code(items)[code]["state"], code)
        self.assertEqual(rows["connect"]["state"], by_code(items)["bot"]["state"])
        self.assertEqual(rows["check"]["state"], readiness.OK)

    def test_done_needs_the_pass_and_no_todo(self):
        """Готов - пройденный шаг, у которого «Готовность» не против. Одних
        фактов мало: цены и точки свежей базы поставочные, их подтверждают."""
        items = readiness.checks(**facts())
        rows = by_code(firstrun.steps(items, {}))
        self.assertFalse(any(r["done"] for r in rows.values()), "не пройден - не готов")
        settings = {firstrun.STEPS_KEY: "company,prices,connect"}
        rows = by_code(firstrun.steps(items, settings))
        self.assertEqual([c for c, r in rows.items() if r["done"]],
                         ["company", "prices", "connect"])
        # пройден, но «не настроено» - не готов
        rows = by_code(firstrun.steps(self.fresh_items(), settings))
        self.assertTrue(rows["company"]["passed"])
        self.assertFalse(rows["company"]["done"])
        self.assertTrue(rows["connect"]["done"], "подключения - инструкция к серверу")
        self.assertEqual(firstrun.progress(rows.values()),
                         {"done": 1, "total": 6, "next": "Организация"})

    def test_progress_value_is_clean_and_idempotent(self):
        settings = {firstrun.STEPS_KEY: "staff, чужое,company,company"}
        self.assertEqual(firstrun.passed(settings), ["company", "staff"])
        value = firstrun.with_step(settings, "points")
        self.assertEqual(value, "company,points,staff")
        self.assertEqual(firstrun.with_step({firstrun.STEPS_KEY: value}, "points"), value)
        self.assertEqual(firstrun.passed({}), [])

    def ok_items(self):
        return firstrun.essentials(**{k: v for k, v in facts().items()
                                      if k in ("settings", "locations", "models",
                                               "tariffs", "staff")})

    def test_who_and_when(self):
        """Владелец, установка, застанная свежей, не скрыт, не демо и не
        готов обязательный шаг - только тогда. Любое одно «нет» - мастера нет."""
        live = {firstrun.STATE_KEY: ""}
        passed = {**live, firstrun.STEPS_KEY: ",".join(firstrun.ESSENTIAL)}
        todo = firstrun.steps(self.fresh_items(), live)
        ok = firstrun.steps(self.ok_items(), passed)
        self.assertTrue(firstrun.needed(todo))
        self.assertFalse(firstrun.needed(ok))
        base = {"staff": OWNER, "settings": live, "demo": False, "rows": todo}
        self.assertTrue(firstrun.wanted(**base))
        for change in ({"staff": {"login": "ivan", "profile_code": "manager"}},
                       {"staff": {"login": "x", "profile_code": None}}, {"staff": None},
                       {"settings": {}}, {"demo": True}, {"rows": ok},
                       {"settings": {firstrun.STATE_KEY: "dismissed"}},
                       {"settings": {firstrun.STATE_KEY: "done"}}):
            with self.subTest(change):
                self.assertFalse(firstrun.wanted(**{**base, **change}))
        self.assertFalse(firstrun.started({}), "нет ключа - свежей не застали")
        self.assertTrue(firstrun.started(live))

    def test_readiness_alone_does_not_retire_it(self):
        """«Готовность» довольна, но точки и цены не пройдены: на свежей
        базе они поставочные (Казань), и мастер их ещё не показал."""
        items = self.ok_items()
        settings = {firstrun.STATE_KEY: "", firstrun.STEPS_KEY: "company,staff"}
        rows = firstrun.steps(items, settings)
        self.assertTrue(firstrun.needed(rows))
        self.assertTrue(firstrun.wanted(staff=OWNER, settings=settings, demo=False,
                                        rows=rows))
        settings[firstrun.STEPS_KEY] = "company,points,prices,staff"
        self.assertFalse(firstrun.needed(firstrun.steps(items, settings)),
                         "подключения и проверка не обязательны")

    def test_current_and_following(self):
        rows = firstrun.steps(self.fresh_items(), {firstrun.STEPS_KEY: "company"})
        self.assertEqual(firstrun.current(rows)["code"], "company", "пройден, но не готов")
        self.assertEqual(firstrun.current(rows, "staff")["code"], "staff")
        self.assertEqual(firstrun.current(rows, "<script>")["code"], "company")
        done = [{**r, "done": True} for r in rows]
        self.assertEqual(firstrun.current(done)["code"], "check")
        self.assertEqual(firstrun.following(rows, "company"), "points")
        self.assertEqual(firstrun.following(rows, "check"), "check")

    def test_price_rows(self):
        models = [{"id": 1, "title": "Kugoo", "active": True, "bikes": 3},
                  {"id": 2, "title": "Monster", "active": True},
                  {"id": 3, "title": "Старая", "active": False}]
        week = {"id": 10, "name": "Неделя", "model": "Kugoo", "period_days": 7,
                "price": D(3000), "kind": "bike", "active": True}
        month = {"id": 11, "name": "Месяц", "model": "Kugoo", "period_days": 30,
                 "price": D(11000), "kind": "bike", "active": True}
        battery = {"id": 12, "name": "АКБ", "model": "Monster", "period_days": 7,
                   "price": D(500), "kind": "battery", "active": True}
        off = {"id": 13, "name": "Неделя", "model": "Monster", "period_days": 7,
               "price": D(1), "kind": "bike", "active": False}
        spare = {"id": 14, "name": "Неделя", "model": None, "period_days": 7,
                 "price": D(2500), "kind": "bike", "active": True}
        rows = firstrun.price_rows(models, [month, week, battery, off, spare])
        self.assertEqual([r["key"] for r in rows], [1, 2, firstrun.ANY_MODEL])
        self.assertEqual((rows[0]["week"]["id"], [t["id"] for t in rows[0]["other"]]),
                         (10, [11]))
        self.assertEqual(rows[0]["bikes"], 3)
        self.assertIsNone(rows[1]["week"], "батарея и выключенный - не цена велосипеда")
        self.assertEqual((rows[2]["model"], rows[2]["week"]["id"]), (None, 14))

    def test_staff_roles(self):
        people = [{"login": "oper", "profile_code": "manager", "active": True},
                  {"login": "old", "profile_code": "tech", "active": False},
                  {"login": "admin", "profile_code": "owner", "active": True}]
        self.assertEqual(firstrun.staff_roles(people), {"operator": ["oper"], "mechanic": []})
        self.assertEqual({code for code, _ in firstrun.ROLES.values()},
                         {code for code, *_ in logic.BUILT_IN_PROFILES} - {"owner"})

    def test_connect_rows_exist_and_live_on_the_server(self):
        """Каждая строка шага есть в «Готовности», и каждая инструкция ведёт
        на сервер (.env, secrets/, bootstrap.sh), а не в форму панели."""
        codes = {i["code"] for i in readiness.checks(**facts())}
        self.assertEqual(set(firstrun.CONNECT) - codes, set())
        self.assertEqual(set(firstrun.CONNECT_HOW), set(firstrun.CONNECT))
        for code, lines in firstrun.CONNECT_HOW.items():
            text = " ".join(lines)
            self.assertRegex(text, r"\.env|secrets/", code)
            self.assertIn("bootstrap.sh", text, code)

    def test_page_is_under_settings(self):
        self.assertEqual(logic.section_for("/setup"), "settings")
        self.assertEqual(logic.section_for("/setup/staff"), "settings")
        only_settings = {"perms": {"sections": {"settings": "edit"}}}
        self.assertEqual(logic.home_for(only_settings), "/company",
                         "мастер не становится домашней страницей настроек")


@unittest.skipUnless(tw.HAVE_WEB, "fastapi не установлен")
class TestWizard(tw.WebCase):
    """Свежая заглушка - как новая установка: ни реквизитов, ни точек, ни
    цен, в панели один администратор, журналов нет."""

    def add(self, login, profile_code, perms=None):
        if perms is not None:
            profile_id = tw.run(self.crm.create_access_profile(f"Профиль {login}", perms))
        else:
            profile_id = tw.run(self.crm.access_profile_by_code(profile_code))["id"]
        tw.run(self.crm.create_staff(login, logic.hash_password("password-1"), login,
                                     "manager", profile_id))

    def as_(self, login, password="password-1", **extra):
        self.client.post("/logout")
        return self.client.post("/login", data={"login": login, "password": password,
                                                **extra})

    def settings(self):
        return tw.run(self.crm.settings())

    def configure(self):
        """Обязательное готово: реквизиты, точка, цена, второй вход."""
        for code in company.COMPANY_FIELDS:
            tw.run(self.crm.set_setting(code, FILLED[code], by="test"))
        tw.run(self.crm.create_location(name="Центр", city="Самара", address="Ленина, 1",
                                        note=None, hours="10-19", phone="+7 900"))
        tw.run(self.crm.create_tariff("Неделя", 7, D(3000), None))
        self.add("oper", "manager")

    # ─── кого встречает ───

    def test_owner_of_a_fresh_install_lands_in_the_wizard(self):
        r = self.login()
        self.assertEqual(r.headers["location"], "/setup")
        text = self.get_ok("/setup")
        for words in ("Первый запуск", "1 · Организация", "4 · Сотрудники",
                      "6 · Проверка", 'action="/setup/company"', "готово шагов: 0 из 6",
                      "Скрыть мастер"):
            self.assertIn(words, text)
        self.assertNotIn("None", text)
        # сводка не закрыта: плашка сверху, числа под ней
        page = self.get_ok("/")
        self.assertIn("Продолжить настройку", page)
        self.assertIn("Три числа", page)

    def test_the_link_the_owner_came_by_wins(self):
        r = self.client.post("/login", data={"login": "admin", "password": "admin-pass-123",
                                             "next": "/clients"})
        self.assertEqual(r.headers["location"], "/clients")
        # чужой адрес по-прежнему не берётся: вместо него - мастер, он свой
        r = self.as_("admin", "admin-pass-123", next="https://evil.example/")
        self.assertEqual(r.headers["location"], "/setup")

    def test_other_profiles_never_meet_it(self):
        self.add("ivan", "manager")
        self.add("petr", "tech")
        # свой профиль со всеми правами - всё равно не владелец
        self.add("boss", None, perms={"sections": dict.fromkeys(logic.SECTIONS, "edit"),
                                      "actions": {}})
        for login in ("ivan", "petr", "boss"):
            with self.subTest(login):
                r = self.as_(login)
                self.assertNotEqual(r.headers["location"], "/setup")
                self.assertNotIn("Продолжить настройку", self.get_ok("/"))
        self.as_("ivan")
        self.assertEqual(self.client.get("/setup").status_code, 403)
        self.assertEqual(self.client.post("/setup/dismiss").status_code, 403)

    def test_installation_with_history_never_sees_it(self):
        """Боевая база: история была до первой встречи - мастера нет, даже
        если реквизиты в ней так и не заполнили, и решение не пишется."""
        tw.run(self.crm.create_bike(code="B-1", model="Kugoo"))
        self.assertEqual(self.login().headers["location"], "/")
        self.assertNotIn("Продолжить настройку", self.get_ok("/"))
        self.assertNotIn(firstrun.STATE_KEY, self.settings())

    def test_the_park_filled_during_setup_keeps_it(self):
        """Первый велосипед после первой встречи - уже журнал статусов, но
        свежесть решена при встрече: мастер ведёт дальше, а не пропадает
        навсегда без «Скрыть»."""
        self.assertEqual(self.login().headers["location"], "/setup")
        self.assertEqual(self.settings()[firstrun.STATE_KEY], "", "решение записано")
        tw.run(self.crm.create_bike(code="B-1", model="Kugoo"))
        self.assertIsNotNone(tw.run(self.crm.history_start()))
        self.assertEqual(self.as_("admin", "admin-pass-123").headers["location"], "/setup")
        self.assertIn("Продолжить настройку", self.get_ok("/"))

    def test_configured_installation_is_led_through_the_rest(self):
        """Реквизиты и сотрудники заведены мимо мастера, точки и цены не
        пройдены: «Готовность» довольна, но мастер встречает - поставочное
        надо увидеть. Пройденные шаги его убирают."""
        self.configure()
        self.assertEqual(self.login().headers["location"], "/setup")
        text = self.get_ok("/setup")
        self.assertIn("готово шагов: 0 из 6", text)
        for code in firstrun.ESSENTIAL:
            self.client.post("/setup/pass", data={"step": code})
        self.assertEqual(self.as_("admin", "admin-pass-123").headers["location"], "/")
        self.assertNotIn("Продолжить настройку", self.get_ok("/"))

    def test_a_broken_check_does_not_block_the_login(self):
        async def broken():
            raise RuntimeError("база ушла")
        self.crm.history_start = broken
        with self.assertLogs("app.web.app", "ERROR"):
            self.assertEqual(self.login().headers["location"], "/")

    # ─── скрыть и вернуть ───

    def test_dismiss_and_resume(self):
        self.login()
        r = self.client.post("/setup/dismiss")
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/"))
        self.assertEqual(self.settings()[firstrun.STATE_KEY], "dismissed")
        page = self.get_ok("/")
        self.assertNotIn("Продолжить настройку", page)
        self.assertIn("Мастер скрыт", page)
        self.assertEqual(self.as_("admin", "admin-pass-123").headers["location"], "/")
        # страница по ссылке остаётся и умеет вернуть мастер
        self.assertIn("Показывать снова", self.get_ok("/setup"))
        self.assertIn('href="/setup"', self.get_ok("/readiness"))
        self.client.post("/setup/resume")
        self.assertEqual(self.settings()[firstrun.STATE_KEY], "")
        self.assertIn("Продолжить настройку", self.get_ok("/"))

    def test_finish_retires_it(self):
        self.login()
        r = self.client.post("/setup/finish")
        self.assertEqual(r.headers["location"], "/readiness")
        self.assertEqual(self.settings()[firstrun.STATE_KEY], "done")
        self.assertIn("check", firstrun.passed(self.settings()))
        self.assertEqual(self.as_("admin", "admin-pass-123").headers["location"], "/")
        self.assertIn("Мастер пройден", self.get_ok("/setup"))

    # ─── шаги пишут тем же путём, что и разделы ───

    def test_company_step_is_the_company_form(self):
        self.login()
        data = {code: FILLED.get(code, "") for code in company.ALL_FIELDS}
        r = self.client.post("/setup/company", data={**data, "company_inn": "1" * 201})
        self.assertEqual(r.headers["location"], "/setup?step=company")
        self.assertNotIn("company_name", self.settings(), "ошибка - ни одного поля")
        self.assertIn("не длиннее", self.get_ok("/setup?step=company"))
        r = self.client.post("/setup/company", data=data)
        self.assertEqual(r.headers["location"], "/setup")
        settings = self.settings()
        for code in company.COMPANY_FIELDS:
            self.assertEqual(settings[code], FILLED[code], code)
        self.assertEqual(company.snapshot()["company_inn"], FILLED["company_inn"],
                         "снимок панели - сразу, как у «Реквизитов»")
        self.assertEqual(firstrun.passed(settings), ["company"])
        row = self.get_ok("/readiness").split('id="company"')[1].split("</tr>")[0]
        self.assertIn("готово", row)
        # следующий открытый шаг - точки
        self.assertIn('class="now">2 · Точки', self.get_ok("/setup"))

    def test_points_step_adds_and_closes_like_the_points_page(self):
        self.login()
        r = self.client.post("/setup/points", data={**POINT, "phone": ""})
        self.assertEqual(r.headers["location"], "/setup?step=points")
        self.assertEqual(tw.run(self.crm.locations()), [])
        self.assertIn("Заполните телефон", self.get_ok("/setup?step=points"))
        r = self.client.post("/setup/points", data=POINT)
        self.assertEqual(r.headers["location"], "/setup?step=points",
                         "после добавления - та же страница: точек может быть две")
        [place] = tw.run(self.crm.locations())
        self.assertEqual((place["name"], place["city"], place["address"], place["hours"],
                          place["phone"]),
                         ("Центр", "Самара", "ул. Ленина, 1", "пн-вс: 10:00-19:00",
                          "+7 900 000-00-00"))
        self.assertEqual(firstrun.passed(self.settings()), ["points"])
        # повтор не заводит вторую
        self.client.post("/setup/points", data=POINT)
        self.assertEqual(len(tw.run(self.crm.locations())), 1)
        self.assertIn("уже есть", self.get_ok("/setup?step=points"))
        # поставочную точку - закрыть на месте
        other = tw.run(self.crm.create_location(name="Павлюхина", city="Казань",
                                                address=None, note=None))
        self.assertIn("нет адрес", self.get_ok("/setup?step=points"))
        self.client.post("/setup/points", data={"action": "close", "location_id": str(other)})
        self.assertFalse(next(p for p in tw.run(self.crm.locations())
                              if p["id"] == other)["active"])
        self.assertEqual(self.client.post("/setup/points", data={
            "action": "close", "location_id": "²"}).status_code, 404)
        text = self.get_ok("/setup?step=points")
        self.assertIn("Закрыты: Павлюхина", text)
        self.assertIn("Открыты: Центр", text)

    def test_prices_step_goes_through_the_tariff_code(self):
        tw.run(self.crm.create_bike_model(title="Kugoo", brand=None, factory_title=None,
                                          battery_slots=2, note=None))
        monster = tw.run(self.crm.create_bike_model(title="Monster", brand=None,
                                                    factory_title=None, battery_slots=2,
                                                    note=None))
        kugoo_id = next(m["id"] for m in tw.run(self.crm.bike_models())
                        if m["title"] == "Kugoo")
        week = tw.run(self.crm.create_tariff("Неделя", 7, D(3000), None, model="Monster"))
        tw.run(self.crm.create_tariff("Месяц", 30, D(11000), None, model="Monster"))
        self.login()
        text = self.get_ok("/setup?step=prices")
        self.assertIn('value="3000"', text)
        self.assertIn("Месяц — ", text)
        form = {f"use_{kugoo_id}": "1", f"week_{kugoo_id}": "2 500",
                f"use_{monster}": "1", f"week_{monster}": "3000", "week_any": ""}
        # неверная цена - ни одной записи
        r = self.client.post("/setup/prices", data={**form, f"week_{kugoo_id}": "дёшево"})
        self.assertEqual(r.headers["location"], "/setup?step=prices")
        self.assertEqual(len(tw.run(self.crm.tariffs())), 2)
        self.assertIn("Kugoo: Сумма", self.get_ok("/setup?step=prices"))

        r = self.client.post("/setup/prices", data=form)
        self.assertEqual(r.headers["location"], "/setup")
        tariffs = tw.run(self.crm.tariffs())
        kugoo = [t for t in tariffs if t["model"] == "Kugoo"]
        self.assertEqual([(t["period_days"], t["price"], t["kind"]) for t in kugoo],
                         [(7, D(2500), "bike")])
        self.assertEqual(len(tariffs), 3, "та же цена у Monster - без записи")
        self.assertIn("prices", firstrun.passed(self.settings()))
        # повтор - ничего нового
        self.client.post("/setup/prices", data=form)
        self.assertEqual(len(tw.run(self.crm.tariffs())), 3)
        # новая цена - правка того же тарифа, а не второй на тот же срок
        self.client.post("/setup/prices", data={**form, f"week_{monster}": "3200",
                                                "week_any": "2000"})
        tariffs = tw.run(self.crm.tariffs())
        self.assertEqual(tw.run(self.crm.tariff(week))["price"], D(3200))
        spare = [t for t in tariffs if not t["model"]]
        self.assertEqual([(t["period_days"], t["price"]) for t in spare], [(7, D(2000))])
        self.assertEqual(len(tariffs), 4)
        # снятая галочка - модель в архив каталога, её цены не тронуты
        self.client.post("/setup/prices", data={f"use_{monster}": "1",
                                                f"week_{monster}": "3200"})
        models = {m["title"]: m for m in tw.run(self.crm.bike_models())}
        self.assertFalse(models["Kugoo"]["active"])
        self.assertTrue(models["Monster"]["active"])
        self.assertEqual(len(tw.run(self.crm.tariffs())), 4)
        self.assertIn("В архиве: Kugoo", self.get_ok("/setup?step=prices"))

    def test_prices_step_needs_the_tariffs_right(self):
        self.add("setup", None, perms={"sections": {"settings": "edit", "tariffs": "view"},
                                       "actions": {}})
        self.as_("setup")
        self.assertEqual(self.client.post("/setup/prices", data={}).status_code, 403)
        self.assertEqual(self.client.post("/setup/staff", data={}).status_code, 403)

    def test_staff_step_creates_logins_and_shows_the_password_once(self):
        self.login()
        owner = tw.run(self.crm.access_profile_by_code("owner"))
        r = self.client.post("/setup/staff", data={
            "role": "operator", "login": "Olga", "name": "Ольга",
            # профиль и пароль из формы не берутся: их решает шаг
            "profile_id": str(owner["id"]), "password": "12345678"})
        self.assertEqual(r.headers["location"], "/setup?step=staff")
        olga = tw.run(self.crm.staff_by_login("olga"))
        self.assertEqual((olga["name"], olga["profile_code"], olga["role"]),
                         ("Ольга", "manager", "manager"))
        text = self.get_ok("/setup?step=staff")
        password = re.search(r"пароль <code>([^<]+)</code>", text).group(1)
        self.assertNotEqual(password, "12345678")
        self.assertTrue(logic.verify_password(password, olga["password_hash"]))
        self.assertIn("есть: olga", text)
        self.assertNotIn(password, self.get_ok("/setup?step=staff"), "один раз")
        self.assertEqual(firstrun.passed(self.settings()), ["staff"])
        # тот же логин - отказ, второго входа нет
        self.client.post("/setup/staff", data={"role": "mechanic", "login": "olga"})
        self.assertEqual(len(tw.run(self.crm.staff_all())), 2)
        self.assertIn("Логин olga уже занят", self.get_ok("/setup?step=staff"))
        self.client.post("/setup/staff", data={"role": "admin", "login": "root"})
        self.assertIsNone(tw.run(self.crm.staff_by_login("root")))
        self.client.post("/setup/staff", data={"role": "mechanic", "login": "petr"})
        self.assertEqual(tw.run(self.crm.staff_by_login("petr"))["profile_code"], "tech")
        # новый сотрудник входит показанным паролем
        self.assertEqual(self.as_("olga", password).status_code, 303)
        self.assertIn("Выдача", self.get_ok("/"))

    def test_double_click_keeps_the_password(self):
        """Двойной клик: оба запроса несут старую cookie, и второй ответ
        перезаписывает первый. Вход один, а пароль всё равно показан."""
        self.login()
        once = re.search(r'name="once" value="([^"]+)"',
                         self.get_ok("/setup?step=staff")).group(1)
        before = dict(self.client.cookies)
        data = {"role": "operator", "login": "olga", "once": once}
        self.client.post("/setup/staff", data=data)
        self.client.cookies.clear()
        self.client.cookies.update(before)
        r = self.client.post("/setup/staff", data=data)
        self.assertEqual(r.headers["location"], "/setup?step=staff")
        self.assertEqual(len(tw.run(self.crm.staff_all())), 2, "второго входа нет")
        text = self.get_ok("/setup?step=staff")
        self.assertIn("повторное нажатие пропущено", text)
        self.assertNotIn("уже занят", text)
        password = re.search(r"пароль <code>([^<]+)</code>", text).group(1)
        self.assertTrue(logic.verify_password(
            password, tw.run(self.crm.staff_by_login("olga"))["password_hash"]))
        # отказ ключ не тратит: исправленная форма с ним же проходит
        once = re.search(r'name="once" value="([^"]+)"', text).group(1)
        self.client.post("/setup/staff", data={"role": "mechanic", "login": "й",
                                               "once": once})
        self.client.post("/setup/staff", data={"role": "mechanic", "login": "petr",
                                               "once": once})
        self.assertIsNotNone(tw.run(self.crm.staff_by_login("petr")))

    def test_staff_step_hides_logins_without_the_staff_section(self):
        """/setup открыт по праву на настройки; логины и имена - раздел
        «Сотрудники», и мастер не открывает его в обход профиля."""
        tw.run(self.crm.create_staff("secretboss", logic.hash_password("password-1"),
                                     "Иван Секретов", "manager",
                                     tw.run(self.crm.access_profile_by_code("manager"))["id"]))
        self.add("setup", None, perms={"sections": {"settings": "view"}, "actions": {}})
        self.as_("setup")
        self.assertEqual(self.client.get("/staff").status_code, 403)
        text = self.get_ok("/setup?step=staff")
        for secret in ("secretboss", "Иван Секретов", "есть: "):
            self.assertNotIn(secret, text)
        self.assertIn("ваша роль его не открывает", text)
        self.assertIn("Активных входов: 3", text, "число - из «Готовности»")
        self.as_("admin", "admin-pass-123")
        text = self.get_ok("/setup?step=staff")
        self.assertIn("Иван Секретов", text)
        self.assertIn("есть: secretboss", text)
        self.assertNotIn("ваша роль его не открывает", text)

    def test_connect_step_takes_no_secrets(self):
        self.login()
        text = self.get_ok("/setup?step=connect")
        card = text.split("5. Подключения")[1]
        for words in ("secrets/bot_token", "TOCHKA_CUSTOMER_CODE", "TOCHKA_ACCOUNT_ID",
                      "STARLINE_APP_ID", "secrets/avito_client_secret", "CRM_DOMAIN",
                      "bootstrap.sh", "@mybike_test_bot"):
            self.assertIn(words, card)
        inputs = re.findall(r"<input\b[^>]*>", card)
        self.assertTrue(inputs)
        self.assertTrue(all('type="hidden"' in i for i in inputs), inputs)
        self.client.post("/setup/pass", data={"step": "connect"})
        self.client.post("/setup/pass", data={"step": "check"})
        self.client.post("/setup/pass", data={"step": "nope"})
        self.assertEqual(firstrun.passed(self.settings()), ["connect"],
                         "проверка - только своей кнопкой")
        self.assertEqual(self.settings()[firstrun.STATE_KEY], "", "не завершён и не скрыт")

    def test_progress_lives_in_settings_not_in_the_session(self):
        self.login()
        data = {code: FILLED.get(code, "") for code in company.ALL_FIELDS}
        self.client.post("/setup/company", data=data)
        self.client.post("/setup/pass", data={"step": "points"})
        # другой браузер - другая сессия
        other = tw.TestClient(self.app, follow_redirects=False)
        other.post("/login", data={"login": "admin", "password": "admin-pass-123"})
        text = other.get("/setup").text
        self.assertIn('class="done">1 · Организация', text)
        self.assertIn('class="now">2 · Точки', text, "точек нет - шаг не готов")
        self.assertIn("готово шагов: 1 из 6", text)

    def test_every_step_page_renders(self):
        self.login()
        for code in firstrun.STEPS:
            with self.subTest(code):
                text = self.get_ok(f"/setup?step={code}")
                self.assertIn(f'class="now">{list(firstrun.STEPS).index(code) + 1} · ', text)
                self.assertNotIn("None", text)


class TestTemplate(unittest.TestCase):
    """Списки шагов на телефоне - карточки: подпись у каждой ячейки."""

    def test_every_cell_is_labelled(self):
        from tests.test_phone import CELL, TABLE
        tables = TABLE.findall(TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(len(tables), 3)
        for body in tables:
            for cell in CELL.findall(body):
                self.assertRegex(cell, r'data-label="[^"]+"')

    def test_step_chips_in_the_dark(self):
        """Тёмная тема: «готово» - не светлая плашка с зелёным текстом,
        «сейчас» - не цвет страницы. По этим плашкам ходят по мастеру.
        Цвета - токенами темы (тинт «хорошо» и оранжевый акцент), а не
        светлыми hex: те же правила рисуют и тёмную."""
        css = (Path(__file__).resolve().parent.parent / "app/web/static/style.css").read_text()
        self.assertIn(".steps>.done{background:var(--okbg);color:var(--ok)}", css)
        self.assertIn(".steps>.now{background:var(--accent);color:#fff}", css)
        self.assertNotIn(".steps>.done{background:#", css)


@unittest.skipUnless(tw.HAVE_WEB, "fastapi не установлен")
class TestDemo(unittest.TestCase):
    """В демо мастер выключен: владелец демо попадает на сводку, страница -
    только посмотреть, а шаги закрыты тем же стражем, что /staff."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.crm = tw.FakeCrm()
        cfg = tw.WebConfig(pg={}, secret="test-secret", admin_login="admin",
                           admin_password="", bot_token="",
                           storage_dir=Path(tmp.name) / "kyc", port=8080,
                           remind_before_days=2, demo=True,
                           bike_photo_dir=Path(tmp.name) / "bikes",
                           doc_dir=Path(tmp.name) / "doctemplates")
        owner = tw.run(self.crm.access_profile_by_code("owner"))
        tw.run(self.crm.create_staff("demo", logic.hash_password("demo"), "Demo", "admin",
                                     owner["id"]))
        app = tw.create_app(crm=self.crm, db=tw.FakeBotDB(), cfg=cfg, bot=None)
        app.state.demo_limits = None
        self.client = tw.TestClient(app, follow_redirects=False)

    def test_wizard_is_off(self):
        r = self.client.post("/login", data={"login": "demo", "password": "demo"})
        self.assertEqual(r.headers["location"], "/")
        self.assertNotIn("Продолжить настройку", self.client.get("/").text)
        text = self.client.get("/setup").text
        self.assertIn("В демо мастер первого запуска выключен", text)
        self.assertNotIn('action="/setup/', text)
        r = self.client.post("/setup/staff", data={"role": "operator", "login": "oper"})
        self.assertEqual(r.status_code, 303)
        self.assertIsNone(tw.run(self.crm.staff_by_login("oper")))
        self.client.post("/setup/dismiss")
        self.assertNotIn(firstrun.STATE_KEY, tw.run(self.crm.settings()))


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
