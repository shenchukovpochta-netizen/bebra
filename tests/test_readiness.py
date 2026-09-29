"""Страница «Готовность»: что в установке не настроено и где это чинится.

Правила - чистые функции app/crm/readiness.py, факты собирает маршрут
панели. Свежая установка должна увидеть весь список дел сразу, а
настроенная - ни одной ложной тревоги.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.crm import company, logic, readiness  # noqa: E402
from app.services import contract  # noqa: E402
from tests import test_web as tw  # noqa: E402

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
FILLED = {**{code: f"значение {code}" for code in company.COMPANY_FIELDS},
          "company_inn": "000000000019"}
# Экран согласия бота, который называет того же оператора, что реквизиты.
CONSENT = "Для договора проката ИП Петров (ИНН 000000000019) обрабатывает данные."
POINT = {"name": "Центр", "active": True, "address": "ул. Ленина, 1",
         "hours": "10-19", "phone": "+7 900 000-00-00"}
ADMIN = {"login": "admin", "active": True}


def facts(**over):
    """Настроенная установка; каждый тест портит одно."""
    base = {"settings": dict(FILLED), "consent": CONSENT, "locations": [POINT],
            "models": [{"title": "Kugoo", "active": True}],
            "tariffs": [{"name": "Неделя", "model": "Kugoo", "kind": "bike", "active": True}],
            "staff": [ADMIN, {"login": "oper", "active": True}],
            "bot": {"ok": True, "name": "rent_bot", "error": ""},
            "acquiring": {"configured": True, "enabled": True},
            "bank_last": {"created_at": NOW - timedelta(hours=1)},
            "trackers": [{"last_seen": NOW - timedelta(minutes=5)}],
            "https": True, "now": NOW}
    base["settings"]["inbox_avito_state"] = {"ok": True, "at": NOW.isoformat()}
    base["settings"][logic.BACKUP_STATUS_KEY] = backup_status()
    base.update(over)
    return base


def backup_status(**parts):
    """Отчёт сервиса backup: дамп ночью, копия в облаке, проверка прошла."""
    night = (NOW - timedelta(hours=9)).isoformat()
    base = {"dump": {"at": night, "ok": True, "last_ok": night, "size": 1024},
            "offsite": {"enabled": True, "at": night, "ok": True, "last_ok": night,
                        "target": "s3/bk"},
            "restore": {"at": night, "ok": True, "last_ok": night, "source": "offsite"}}
    base.update(parts)
    return json.dumps(base)


def by_code(items):
    return {i["code"]: i for i in items}


class TestRules(unittest.TestCase):
    def test_configured_install_is_all_green(self):
        items = by_code(readiness.checks(**facts()))
        self.assertEqual({c for c, i in items.items() if i["state"] != readiness.OK}, set())
        summary = readiness.summary(items.values())
        self.assertTrue(summary["ready"])
        self.assertEqual((summary["required_ok"], summary["required"]), (6, 6))

    def test_fresh_install_shows_the_whole_todo_list(self):
        items = by_code(readiness.checks(**facts(
            settings={}, locations=[], models=[], tariffs=[], staff=[ADMIN],
            bot={"ok": None, "error": "бот к панели не подключён"},
            acquiring={"configured": False, "enabled": True}, bank_last=None,
            trackers=[], https=False)))
        for code in ("company", "consent", "points", "prices", "staff", "bot"):
            self.assertEqual(items[code]["state"], readiness.TODO, code)
            self.assertTrue(items[code]["required"], code)
        for code in ("acquiring", "bank", "trackers", "avito", "https"):
            self.assertEqual(items[code]["state"], readiness.OFF, code)
            self.assertFalse(items[code]["required"], code)
        self.assertFalse(readiness.summary(items.values())["ready"])
        # у каждой строки - куда идти: страница панели или шаг на сервере
        for code, i in items.items():
            self.assertTrue(i["href"] or i["how"], code)


    def test_backup_is_judged_by_the_service_report(self):
        """Каталог бэкапов панели не смонтирован: судим по отчёту сервиса
        backup в базе, тем же правилом, что карточка «Сервер»."""
        def backup(raw):
            settings = {**facts()["settings"], logic.BACKUP_STATUS_KEY: raw}
            return by_code(readiness.checks(**facts(settings=settings)))["backup"]

        silent = backup(None)
        self.assertEqual(silent["state"], readiness.WARN)
        self.assertIn("ни разу не отчитался", silent["text"])
        local = backup(backup_status(offsite={"enabled": False}))
        self.assertEqual(local["state"], readiness.OFF, "дамп только на этом сервере")
        self.assertIn("BACKUP_S3_", local["how"])
        failed = backup(backup_status(dump={"at": NOW.isoformat(), "ok": False,
                                            "error": "No space left on device"}))
        self.assertEqual(failed["state"], readiness.WARN)
        self.assertIn("No space left on device", failed["text"])
        self.assertFalse(failed["required"], "бэкап не держит обязательное")
    def test_missing_requisites_are_named(self):
        settings = {**FILLED, "company_inn": " ", "company_bik": ""}
        got = readiness.check_company(settings)
        self.assertEqual(got["state"], readiness.TODO)
        self.assertIn("ИНН", got["text"])
        self.assertIn("БИК", got["text"])
        self.assertEqual(got["href"], "/company")
        # почта необязательна: в документе будет честный прочерк
        self.assertEqual(readiness.check_company({**FILLED, "company_email": ""})["state"],
                         readiness.OK)

    def test_required_requisites_are_the_ones_in_our_documents(self):
        """Готовность проверяет ровно то, что стоит в поставочных шаблонах:
        новое поле в договоре без строки здесь ушло бы клиенту прочерком
        незамеченным."""
        used = set()
        for path in (ROOT / "app").glob("*.docx"):
            used |= {f for f in contract.placeholders(path.read_bytes())
                     if f.startswith("company_")}
        self.assertEqual(used - {"company_email"}, set(readiness.COMPANY_REQUIRED))

    def test_consent_screen_names_the_operator_from_the_requisites(self):
        """Экран согласия в боте - текст, а не подстановка: чужой ИНН на нём
        значит, что клиент соглашается не с тем оператором."""
        got = readiness.check_consent(FILLED, CONSENT)
        self.assertEqual(got["state"], readiness.OK)
        other = CONSENT.replace("000000000019", "000000000027")
        got = readiness.check_consent(FILLED, other)
        self.assertEqual(got["state"], readiness.TODO)
        self.assertIn("app/texts.py", got["how"])
        got = readiness.check_consent({}, CONSENT)
        self.assertEqual((got["state"], got["href"]), (readiness.TODO, "/company"))

    def test_points_without_address_hours_or_phone(self):
        closed = {**POINT, "name": "Старая", "active": False, "phone": ""}
        bad = {**POINT, "name": "Новая", "phone": "", "hours": None}
        got = readiness.check_points([POINT, closed, bad])
        self.assertEqual(got["state"], readiness.TODO)
        self.assertIn("Новая: нет режим, телефон", got["text"])
        self.assertNotIn("Старая", got["text"], "закрытая точка клиенту не нужна")
        self.assertEqual(readiness.check_points([POINT, closed])["state"], readiness.OK)

    def test_models_without_their_own_price(self):
        models = [{"title": "Kugoo", "active": True}, {"title": "Monster", "active": True},
                  {"title": "Снята", "active": False}]
        own = [{"model": "Kugoo", "kind": "bike", "active": True},
               {"model": "Monster", "kind": "battery", "active": True}]
        got = readiness.check_prices(models, own)
        self.assertEqual(got["state"], readiness.TODO, "тариф на батарею - не цена велосипеда")
        self.assertIn("Monster", got["text"])
        self.assertNotIn("Снята", got["text"])
        spare = own + [{"model": None, "kind": "bike", "active": True}]
        got = readiness.check_prices(models, spare)
        self.assertEqual(got["state"], readiness.WARN, "запасной тариф - цена есть")
        self.assertIn("запасному", got["text"])
        self.assertEqual(readiness.check_prices(models, [])["state"], readiness.TODO)
        off = [{"model": "Kugoo", "kind": "bike", "active": False}]
        self.assertEqual(readiness.check_prices(models, off)["state"], readiness.TODO)

    def test_bot_that_does_not_answer(self):
        got = readiness.check_bot({"ok": False, "error": "Unauthorized"})
        self.assertEqual(got["state"], readiness.WARN)
        self.assertIn("Unauthorized", got["text"])

    def test_acquiring_switched_off_in_the_panel(self):
        got = readiness.check_acquiring({"configured": True, "enabled": False})
        self.assertEqual((got["state"], got["href"]), (readiness.WARN, "/payments"))

    def test_quiet_bank_and_trackers(self):
        old = {"created_at": NOW - timedelta(days=8)}
        self.assertEqual(readiness.check_bank(old, NOW)["state"], readiness.WARN)
        quiet = [{"last_seen": NOW - timedelta(hours=13)}, {"last_seen": None}]
        got = readiness.check_trackers(quiet, NOW)
        self.assertEqual(got["state"], readiness.WARN)
        self.assertEqual(got["at"], NOW - timedelta(hours=13))
        self.assertIn("12 ч", got["text"])
        # заведён руками, но опрос его ни разу не видел
        self.assertEqual(readiness.check_trackers([{"last_seen": None}], NOW)["state"],
                         readiness.OFF)

    def test_parked_fleet_at_night_is_not_a_dead_poll(self):
        """last_seen - время самого трекера, а не круга опроса: ночью весь
        парк стоит, и спящий StarLine выходит на связь раз в несколько
        часов. Мёртвый опрос - когда весь парк «молчит» по порогу тревоги
        из настроек, а не после часа тишины."""
        night = [{"last_seen": NOW - timedelta(hours=5)}]
        self.assertEqual(readiness.check_trackers(night, NOW)["state"], readiness.OK)
        items = by_code(readiness.checks(**facts(trackers=night)))
        self.assertEqual(items["trackers"]["state"], readiness.OK)
        # порог правят в панели - «Готовность» читает тот же
        strict = {"tracker_offline_hours": "4"}
        self.assertEqual(readiness.check_trackers(night, NOW, strict)["state"], readiness.WARN)
        items = by_code(readiness.checks(**facts(
            trackers=night, settings={**FILLED, **strict})))
        self.assertEqual(items["trackers"]["state"], readiness.WARN)

    def test_avito_poll_that_stopped(self):
        state = {"ok": False, "at": (NOW - timedelta(hours=1)).isoformat(),
                 "error": "401 Unauthorized"}
        got = readiness.check_avito({"inbox_avito_state": state}, NOW)
        self.assertEqual(got["state"], readiness.WARN)
        self.assertIn("401", got["text"])

    def test_page_is_under_settings(self):
        self.assertEqual(logic.section_for("/readiness"), "settings")


@unittest.skipUnless(tw.HAVE_WEB, "fastapi не установлен")
class TestPage(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_fresh_install_page(self):
        text = self.get_ok("/readiness")
        for words in ("Готовность к работе", "Без этого прокат не работает",
                      "Можно подключить позже", "Реквизиты организации",
                      'href="/company"', 'href="/locations"', 'href="/staff"',
                      "Сервис backup ни разу не отчитался", "не всё настроено"):
            self.assertIn(words, text)
        self.assertNotIn("None", text)

    def test_requisites_turn_green_after_saving(self):
        self.client.post("/company", data={code: FILLED[code] for code in company.ALL_FIELDS
                                           if code in FILLED})
        row = self.get_ok("/readiness").split('id="company"')[1].split("</tr>")[0]
        self.assertIn("готово", row)

    def test_configured_facts_reach_the_page(self):
        tw.run(self.crm.create_tariff("Неделя", 7, Decimal(3000), None))
        tw.run(self.crm.create_staff("oper", logic.hash_password("password-1"), "Оператор",
                                     "manager"))
        text = self.get_ok("/readiness")
        staff = text.split('id="staff"')[1].split("</tr>")[0]
        self.assertIn("Активных входов: 2", staff)
        bot = text.split('id="bot"')[1].split("</tr>")[0]
        self.assertIn("@mybike_test_bot", bot, "живость бота - из get_me")

    def test_tabs_lead_here(self):
        for page in ("/company", "/documents", "/locations", "/models", "/notices",
                     "/intake"):
            self.assertIn('href="/readiness"', self.get_ok(page), page)

    def test_only_the_owner_sees_it(self):
        profile = tw.run(self.crm.access_profile_by_code("manager"))
        tw.run(self.crm.create_staff("ivan", logic.hash_password("password-1"),
                                     "Иван", "manager", profile["id"]))
        self.client.post("/logout")
        self.login("ivan", "password-1")
        self.assertEqual(self.client.get("/readiness").status_code, 403)


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
