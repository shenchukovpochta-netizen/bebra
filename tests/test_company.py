"""Реквизиты организации и шаблоны документов.

Реквизиты правит владелец в панели, а подставляет их в договор и акты
бот - другой процесс. Общая у них только база, поэтому проверяется и то,
что снимок доезжает, и то, что пустое поле становится прочерком, а не
строкой «None» посреди договора.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import faq, i18n, texts  # noqa: E402
from app.crm import (
    company,  # noqa: E402
    logic,  # noqa: E402
)

try:
    import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False


class TestCompanyLogic(unittest.TestCase):
    def setUp(self):
        company.reset()

    def test_context_has_every_field_and_dashes_the_empty(self):
        ctx = company.context({"company_name": "ИП Иванов", "company_inn": " 1660 "})
        self.assertEqual(set(ctx), set(company.COMPANY_FIELDS))
        self.assertEqual(ctx["company_name"], "ИП Иванов")
        self.assertEqual(ctx["company_inn"], "1660")
        self.assertEqual(ctx["company_bank"], "—", "пустое - прочерк, не None")

    def test_long_value_is_refused(self):
        self.assertEqual(company.check_value(" 1660 00 "), ("1660 00", ""))
        self.assertIn("не длиннее", company.check_value("x" * 500)[1])

    def test_snapshot_keeps_only_known_fields(self):
        company.set_snapshot({"company_inn": "1660", "ref_bonus": "500"})
        snapshot = company.snapshot()
        self.assertEqual(snapshot["company_inn"], "1660")
        self.assertNotIn("ref_bonus", snapshot, "чужие настройки в реквизиты не лезут")

    def test_refresh_reads_once_and_then_uses_the_snapshot(self):
        calls = []

        class FakeCrm:
            async def settings(self):
                calls.append(1)
                return {"company_inn": "1660"}

        crm = FakeCrm()
        tw.run(company.refresh(crm)) if HAVE_WEB else None
        if not HAVE_WEB:                                # pragma: no cover
            self.skipTest("нет тестовой обвязки")
        self.assertEqual(company.snapshot()["company_inn"], "1660")
        tw.run(company.refresh(crm))
        self.assertEqual(len(calls), 1, "снимок живёт TTL, а не читается каждый раз")
        tw.run(company.refresh(crm, force=True))
        self.assertEqual(len(calls), 2)

    def test_broken_database_keeps_the_old_snapshot(self):
        if not HAVE_WEB:                                # pragma: no cover
            self.skipTest("нет тестовой обвязки")
        company.set_snapshot({"company_inn": "1660"})

        class Broken:
            async def settings(self):
                raise RuntimeError("база недоступна")

        tw.run(company.refresh(Broken(), force=True))
        self.assertEqual(company.snapshot()["company_inn"], "1660",
                         "старые реквизиты лучше прочерков")

    def test_settings_section_is_owner_only(self):
        owner = dict(logic.BUILT_IN_PROFILES[0][2]["sections"])
        self.assertEqual(owner.get("settings"), "edit")
        for code, _name, perms, _built in logic.BUILT_IN_PROFILES[1:]:
            self.assertIsNone(perms["sections"].get("settings"),
                              f"{code}: реквизиты правит владелец")


class TestManagerContact(unittest.TestCase):
    """Контакт менеджера: одна настройка вместо ссылки в девяти файлах."""

    def setUp(self):
        company.reset()

    def tearDown(self):
        company.reset()

    def test_handle_becomes_a_link_and_junk_is_refused(self):
        self.assertEqual(company.check_contact(" @maybike_kazan "),
                         ("https://t.me/maybike_kazan", ""))
        self.assertEqual(company.check_contact("t.me/maybike"),
                         ("https://t.me/maybike", ""))
        self.assertEqual(company.check_contact("https://vk.me/maybike"),
                         ("https://vk.me/maybike", ""))
        self.assertIn("ссылка вида", company.check_contact("позвоните Ринату")[1])
        self.assertIn("латиница", company.check_contact("@Ринат")[1])

    def test_empty_means_the_built_in_contact(self):
        self.assertEqual(company.check_contact(""), ("", ""))
        self.assertEqual(company.support_url(), texts.SUPPORT_CONTACT_URL)
        company.set_snapshot({"support_contact": ""})
        self.assertEqual(company.support_url(), texts.SUPPORT_CONTACT_URL,
                         "стёртое поле возвращает зашитый контакт, а не пустоту")

    def test_setting_replaces_the_contact_in_every_text(self):
        company.set_snapshot({"support_contact": "https://t.me/novy_menedzher"})
        self.assertEqual(company.support_url(), "https://t.me/novy_menedzher")
        for key in ("SUPPORT_PROMPT", "SUPPORT_SENT", "FAQ_GUEST_CONTACT",
                    "CAB_UNAVAILABLE"):
            text = i18n.t("ru", key)
            self.assertIn("https://t.me/novy_menedzher", text, key)
            self.assertNotIn(texts.SUPPORT_CONTACT_URL, text, key)

    def test_translations_get_the_same_contact(self):
        company.set_snapshot({"support_contact": "https://t.me/novy_menedzher"})
        for lang in ("en", "uz", "tt", "ar"):
            text = i18n.t(lang, "SUPPORT_PROMPT")
            self.assertNotIn(texts.SUPPORT_CONTACT_URL, text, lang)

    def test_faq_answers_carry_the_contact_too(self):
        company.set_snapshot({"support_contact": "https://t.me/novy_menedzher"})
        menu = faq.menu_text("ru", registered=False)
        self.assertIn("https://t.me/novy_menedzher", menu)
        self.assertNotIn(texts.SUPPORT_CONTACT_URL, menu)

    def test_snapshot_keeps_the_contact_next_to_the_requisites(self):
        company.set_snapshot({"company_inn": "1660",
                              "support_contact": "https://t.me/x"})
        self.assertEqual(company.snapshot()["support_contact"], "https://t.me/x")
        self.assertNotIn("support_contact", company.context(company.snapshot()),
                         "в документы контакт не идёт: это не реквизит")


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestCompanyInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        company.reset()
        self.login()

    def tearDown(self):
        # Снимок общий на процесс: оставленный здесь контакт подменил бы
        # ссылку в тестах бота, которые идут следом.
        company.reset()

    def save(self, **over):
        data = {code: "" for code in company.ALL_FIELDS}
        data.update({"company_name": "ИП Иванов Иван Иванович",
                     "company_inn": "166000000000"})
        data.update(over)
        return self.client.post("/company", data=data)

    def test_requisites_are_saved_and_shown(self):
        r = self.save()
        self.assertEqual(r.status_code, 303)
        settings = tw.run(self.crm.settings())
        self.assertEqual(settings["company_inn"], "166000000000")
        page = self.get_ok("/company")
        self.assertIn("ИП Иванов Иван Иванович", page)
        self.assertIn("Реквизиты организации", page)

    def test_saving_refreshes_the_snapshot_in_this_process(self):
        self.save()
        self.assertEqual(company.snapshot()["company_inn"], "166000000000")
        self.assertEqual(company.context(company.snapshot())["company_name"],
                         "ИП Иванов Иван Иванович")

    def test_templates_are_checked_by_reading(self):
        page = self.get_ok("/company")
        self.assertIn("Шаблоны документов", page)
        self.assertIn("Договор аренды", page)

    def test_contact_is_saved_and_picked_up_by_this_process(self):
        r = self.save(support_contact="@novy_menedzher")
        self.assertEqual(r.status_code, 303)
        settings = tw.run(self.crm.settings())
        self.assertEqual(settings["support_contact"],
                         "https://t.me/novy_menedzher",
                         "@имя разворачивается в ссылку при сохранении")
        self.assertEqual(company.support_url(), "https://t.me/novy_menedzher")
        self.assertIn("https://t.me/novy_menedzher", self.get_ok("/company"))

    def test_junk_contact_is_refused_and_nothing_is_saved(self):
        self.save()
        self.client.post("/company", data={
            **{code: "" for code in company.ALL_FIELDS},
            "company_name": "ИП Иванов Иван Иванович",
            "support_contact": "позвоните Ринату"})
        settings = tw.run(self.crm.settings())
        self.assertEqual(settings.get("support_contact", ""), "")
        self.assertEqual(settings["company_name"], "ИП Иванов Иван Иванович",
                         "отказ на одном поле не должен стирать остальные")

    def test_only_the_owner_gets_the_section(self):
        profile = tw.run(self.crm.access_profile_by_code("manager"))
        tw.run(self.crm.create_staff("ivan", logic.hash_password("password-1"),
                                     "Иван", "manager", profile["id"]))
        self.client.post("/logout")
        self.login("ivan", "password-1")
        self.assertEqual(self.client.get("/company").status_code, 403)
        self.assertEqual(self.save().status_code, 403)


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()


class TestCompanyInDocuments(unittest.TestCase):
    """Реквизиты доезжают до договора и актов - через тот же контекст."""

    def setUp(self):
        company.reset()

    def test_context_is_added_to_every_document(self):
        from app.handlers import contract as contract_handlers
        company.set_snapshot({"company_name": "ИП Иванов Иван Иванович",
                              "company_inn": "166000000000"})
        ctx = contract_handlers._context(
            _cfg(), {"full_name": "Петров Пётр", "phone": "+79990000000"}, {},
            number="АВ-1", signed_at="—", issued_at=None)
        self.assertEqual(ctx["company_name"], "ИП Иванов Иван Иванович")
        self.assertEqual(ctx["company_inn"], "166000000000")
        self.assertEqual(ctx["company_bank"], "—", "пустой реквизит - прочерк")

    def test_empty_requisites_do_not_break_the_document(self):
        from app.handlers import contract as contract_handlers
        ctx = contract_handlers._context(
            _cfg(), {"full_name": "Петров Пётр"}, {}, number="АВ-1",
            signed_at="—", issued_at=None)
        self.assertEqual(set(company.COMPANY_FIELDS) - set(ctx), set(),
                         "поля есть всегда: иначе шаблон напечатал бы «нет поля»")


def _cfg():
    from tests.test_flow import make_config
    return make_config()
