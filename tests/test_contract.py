"""Шифрование анкеты и сборка договора.

Шифрование требует cryptography, поэтому его часть пропускается там, где
библиотеки нет. Договор собирается голым stdlib: docx-шаблон заполняется
через zipfile и re.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import logic  # noqa: E402
from app.crm import company  # noqa: E402
from app.services import contract  # noqa: E402

try:
    from app.services.crypto import KeyProblem, Vault, generate_key, load_key
    HAVE_CRYPTO = True
except ImportError:                                    # pragma: no cover
    HAVE_CRYPTO = False

TEMPLATE = ROOT / "app" / "contract_template.docx"
TODAY = date(2026, 8, 2)

ANKETA = {
    "birth_date": "07.03.1990",
    "birth_place": "гор. Казань",
    "passport_number": "1234 567890",
    "passport_date": "01.02.2015",
    "passport_code": "160-002",
    "passport_issuer": "ОУФМС России по Респ. Татарстан",
    "reg_address": "г. Казань, ул. Баумана, д. 1, кв. 2",
    "live_address": "г. Казань, ул. Кремлёвская, д. 5, кв. 9",
    "phone2": "+79001112233",
    "phone3": "+79004445566",
}
USER = {"tg_id": 5001, "full_name": "Иванов Иван Иванович", "phone": "+79990000000"}
# Реквизиты вымышленные и узнаваемые: в документ они попадают только из
# «Реквизитов организации», в поставочных шаблонах их нет.
COMPANY = {"company_name": "Индивидуальный предприниматель Петров Пётр Петрович",
           "company_short": "ИП Петров П. П.", "company_inn": "000000000019",
           "company_ogrn": "300000000000027", "company_address": "г. Энск, ул. Тестовая, 1",
           "company_phone": "+7 (900) 000-00-11", "company_email": "rent@example.com",
           "company_bank": "АО Тест-Банк", "company_account": "40802810000000000035",
           "company_bik": "044500043", "company_corr": "30101810000000000051",
           "company_tax": "УСН", "company_director": "Петров П. П."}


def make_ctx(**extra) -> dict:
    ctx = logic.contract_context(USER, ANKETA, number="АВ-2026-000042", today=TODAY)
    # Данные выдачи: без ответа оператора - прочерки из issue_context.
    ctx.update(logic.issue_context({"vin_frame": "264022410703084",
                                    "vin_motor": "240W25021406",
                                    "rent_term": "03.08 - 10.08",
                                    "rent_price": "3000 qr"}))
    ctx.update(purge_days="90", signed_at="не подписан",
               act_date="02.08.2026", return_date="02.08.2026",
               return_notes="Без замечаний", buyout_total="15 000,00 ₽",
               bike_color="Чёрный")
    # Тот же слой, что в handlers/contract._context: реквизиты последними.
    ctx.update(company.context(COMPANY))
    ctx.update(extra)
    return ctx


@unittest.skipUnless(HAVE_CRYPTO, "cryptography не установлена")
class TestVault(unittest.TestCase):
    def setUp(self):
        self.vault = Vault.from_raw(generate_key())

    def test_roundtrip(self):
        self.assertEqual(self.vault.decrypt(self.vault.encrypt(ANKETA)), ANKETA)

    def test_ciphertext_hides_the_data(self):
        token = self.vault.encrypt(ANKETA)
        for secret in ("1234 567890", "Баумана", "160-002", "Иванов"):
            self.assertNotIn(secret, token, secret)

    def test_empty_anketa_is_null(self):
        """Пустая анкета должна выглядеть в базе как NULL, а не как валидный
        шифротекст - иначе «анкеты нет» и «анкета пустая» не различить."""
        self.assertIsNone(self.vault.encrypt({}))
        self.assertIsNone(self.vault.encrypt(None))
        self.assertEqual(self.vault.decrypt(None), {})

    def test_nonce_differs_between_calls(self):
        self.assertNotEqual(self.vault.encrypt(ANKETA), self.vault.encrypt(ANKETA))

    def test_wrong_key_does_not_raise(self):
        """Разные ключи на двух запусках не должны блокировать человеку
        вообще любое действие, включая /start."""
        token = self.vault.encrypt(ANKETA)
        other = Vault.from_raw(generate_key())
        self.assertEqual(other.decrypt(token), {})

    def test_tampered_ciphertext_detected(self):
        token = self.vault.encrypt(ANKETA)
        broken = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        self.assertEqual(self.vault.decrypt(broken), {})

    def test_unknown_format_ignored(self):
        self.assertEqual(self.vault.decrypt("что-то из прошлой версии"), {})

    def test_key_accepts_base64_and_hex(self):
        self.assertEqual(len(load_key(generate_key())), 32)
        self.assertEqual(len(load_key("ab" * 32)), 32)

    def test_missing_or_short_key_refused(self):
        for raw in ("", None, "короткий"):
            with self.assertRaises(KeyProblem):
                load_key(raw)


def document_text(docx: bytes) -> str:
    """Видимый текст документа: содержимое всех <w:t> из word/document.xml."""
    import io
    import re
    import zipfile
    xml = zipfile.ZipFile(io.BytesIO(docx)).read("word/document.xml").decode("utf-8")
    return "".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", xml, re.S))


class TestTemplate(unittest.TestCase):
    def setUp(self):
        self.template = contract.load_template(TEMPLATE)

    def test_real_contract_landed(self):
        """Шаблон - настоящий договор юриста, а не рыба; арендодатель в нём -
        реквизиты из настройки, в шапке и в блоке реквизитов обоих разделов."""
        docx, _ = contract.build(TEMPLATE, make_ctx())
        text = document_text(docx).replace("\xa0", " ")
        for marker in ("150 000 (сто пятьдесят тысяч)",
                       "ПРАВИЛА ЭКСПЛУАТАЦИИ ЭЛЕКТРОВЕЛОСИПЕДА",
                       "АКТ ВОЗВРАТА",
                       f"{COMPANY['company_name']} (ИНН: {COMPANY['company_inn']}; "
                       f"ОГРН/ОГРНИП: {COMPANY['company_ogrn']})",
                       f"Расчетный счет: {COMPANY['company_account']}",
                       f"Банк: {COMPANY['company_bank']}, БИК {COMPANY['company_bik']}",
                       f"Корр. счет: {COMPANY['company_corr']}",
                       f"Телефон: {COMPANY['company_phone']}",
                       f"Эл. почта: {COMPANY['company_email']}"):
            self.assertIn(marker, text, marker)
        # договор и встроенный акт возврата - оба с реквизитами из настройки
        self.assertEqual(text.count(COMPANY["company_short"]), 2)
        self.assertEqual(text.count(COMPANY["company_address"]), 2)

    def test_redaction_of_the_lawyer_is_in_place(self):
        """Разделы редакции юриста: передача по Акту приёма-передачи, признание
        ПЭП и блок подписи. Подмена шаблона на старую редакцию их потеряет,
        а бот продолжит слать «подписанный» документ без этих условий."""
        docx, _ = contract.build(TEMPLATE, make_ctx())
        # NBSP в тексте юриста нормализуем: он ставится Word'ом произвольно
        # и не должен ломать проверку формулировки.
        text = document_text(docx).replace("\xa0", " ")
        for marker in ("Порядок подписания Акта приема-передачи",
                       "Без нажатия кнопки «Подписываю» Имущество Арендатору "
                       "не передается",
                       "простая электронная подпись (ПЭП)",
                       "Акта возврата Имущества;",
                       "Срок проката начинается с момента подписания Акта "
                       "приема-передачи",
                       "Подписано в электронном виде:",
                       "Telegram ID Арендатора:",
                       "Отпечаток документа (SHA-256):"):
            self.assertIn(marker, text, marker)

    def test_signature_block_carries_data_not_blanks(self):
        """Блок ПЭП заполняется ботом: момент подписи, Telegram ID и отпечаток.
        Незаполненный блок - это документ без единого следа подписания."""
        docx, digest = contract.build(
            TEMPLATE, make_ctx(signed_at="02.08.2026 11:00 UTC"))
        text = document_text(docx)
        self.assertIn("Подписано в электронном виде: 02.08.2026 11:00 UTC", text)
        self.assertIn(f"Telegram ID Арендатора: {USER['tg_id']}", text)
        self.assertIn(f"Отпечаток документа (SHA-256): {digest}", text)

    def test_retention_term_follows_configuration(self):
        """Срок хранения изображений в договоре - из PURGE_APPROVED_DAYS.
        Цифра, зашитая в шаблон, разъехалась бы с реальным удалением."""
        docx, _ = contract.build(TEMPLATE, make_ctx(purge_days="120"))
        text = document_text(docx)
        self.assertIn("удаляются через 120 дней", text)
        self.assertNotIn("удаляются через 365 дней", text)

    def test_no_leftovers_from_the_filled_sample(self):
        """Бланк пришёл из заполненного образца: тестовые ФИО, адрес и дата
        не должны были уехать в шаблон вместе с вёрсткой."""
        text = document_text(contract.build(TEMPLATE, make_ctx())[0]).lower()
        for junk in ("зубенко", "анимешников", "5252 525252", "79030654411",
                     "ciri_love4ever"):
            self.assertNotIn(junk, text, junk)
        # дата договора берётся из контекста, а не из даты образца
        self.assertIn("05.08.2026", document_text(
            contract.build(TEMPLATE, make_ctx(contract_date="05.08.2026"))[0]))

    def test_every_placeholder_is_provided(self):
        """Опечатка в шаблоне не должна тихо выкидывать реквизит из договора."""
        used = contract.placeholders(self.template)
        provided = set(make_ctx()) | {contract.HASH_FIELD}
        self.assertEqual(used - provided, set())

    def test_annex_and_act_placeholders_are_provided(self):
        """Согласие-приложение и оба акта заполняются тем же контекстом,
        что договор: неизвестное поле осталось бы в документе пометкой."""
        provided = set(make_ctx()) | {contract.HASH_FIELD}
        for name in ("soglasie_template.docx", "act_priema_template.docx",
                     "act_vozvrata_template.docx", "act_vykup_template.docx"):
            tpl = contract.load_template(ROOT / "app" / name)
            self.assertEqual(contract.placeholders(tpl) - provided, set(), name)

    def test_soglasie_annex_builds_with_own_digest(self):
        """Приложение-согласие: собственный текст 152-ФЗ и свой отпечаток."""
        path = ROOT / "app" / "soglasie_template.docx"
        docx, digest = contract.build(path, make_ctx())
        text = document_text(docx).replace("\xa0", " ")
        for marker in ("СОГЛАСИЕ НА ОБРАБОТКУ ПЕРСОНАЛЬНЫХ ДАННЫХ",
                       "АВ-2026-000042", "152-ФЗ", "Иванов Иван Иванович",
                       "1234 567890", "5 (пяти) лет", digest):
            self.assertIn(marker, text, marker)
        _, contract_digest = contract.build(TEMPLATE, make_ctx())
        self.assertNotEqual(digest, contract_digest)

    def test_unknown_placeholder_is_visible(self):
        rendered = contract.substitute("а {{ нетполя }} б", {})
        self.assertIn("нет поля", rendered)

    def test_values_are_xml_escaped(self):
        """Амперсанд в «кем выдан» не должен ломать XML документа."""
        ctx = make_ctx(passport_issuer="ОУФМС & Ко <тест>")
        docx, _ = contract.build(TEMPLATE, ctx)
        text = document_text(docx)
        self.assertIn("ОУФМС &amp; Ко &lt;тест&gt;", text)

    def test_all_anketa_fields_reach_the_document(self):
        docx, _ = contract.build(TEMPLATE, make_ctx())
        text = document_text(docx)
        for value in ANKETA.values():
            self.assertIn(value, text, value)
        self.assertIn(USER["full_name"], text)

    def test_minor_clause_rendered_for_minor_only(self):
        minor_docx, _ = contract.build(TEMPLATE, make_ctx(minor_clause=logic.MINOR_CLAUSE))
        adult_docx, _ = contract.build(TEMPLATE, make_ctx())
        self.assertIn("не достигший 18 лет", document_text(minor_docx))
        adult_text = document_text(adult_docx)
        self.assertNotIn("не достигший 18 лет", adult_text)
        # абзац удаляется целиком, а не оставляет пустой оговорки
        self.assertNotIn("minor_clause", adult_text)

    def test_dropping_clause_spares_the_neighbours(self):
        """Регрессия: поиск начала абзаца находил открытие СОСЕДНЕГО абзаца,
        и у взрослых вместе с оговоркой вырезалась строка перед ней."""
        adult_docx, _ = contract.build(TEMPLATE, make_ctx())
        text = document_text(adult_docx)
        self.assertIn("(номер телефона свой и второй, третий)", text)
        self.assertIn("заключили настоящий договор о нижеследующем:", text)

    def test_drop_paragraph_is_well_formed(self):
        """После удаления абзаца XML обязан остаться корректным."""
        import io
        import xml.etree.ElementTree as ET
        import zipfile
        adult_docx, _ = contract.build(TEMPLATE, make_ctx())
        xml_text = zipfile.ZipFile(io.BytesIO(adult_docx)).read("word/document.xml")
        ET.fromstring(xml_text)      # бросит ParseError, если разметка порвана

    def test_hash_is_reproducible(self):
        _, digest = contract.build(TEMPLATE, make_ctx())
        _, digest2 = contract.build(TEMPLATE, make_ctx())
        self.assertEqual(digest, digest2)

    def test_hash_covers_the_data(self):
        _, digest = contract.build(TEMPLATE, make_ctx())
        _, changed = contract.build(TEMPLATE, make_ctx(passport_number="9999 999999"))
        self.assertNotEqual(digest, changed)

    def test_signed_copy_differs_from_issued(self):
        """Подписанный экземпляр несёт свой отпечаток: в нём проставлен
        момент подписания."""
        _, unsigned = contract.build(TEMPLATE, make_ctx())
        _, signed = contract.build(TEMPLATE, make_ctx(signed_at="02.08.2026 11:00 UTC"))
        self.assertNotEqual(unsigned, signed)

    def test_hash_verifiable_from_the_document(self):
        """Правило проверки задним числом: стереть напечатанный отпечаток
        из document.xml, посчитать SHA-256 заново - должно сойтись."""
        import hashlib
        import io
        import zipfile
        docx, digest = contract.build(TEMPLATE, make_ctx())
        xml = zipfile.ZipFile(io.BytesIO(docx)).read("word/document.xml").decode("utf-8")
        self.assertIn(digest, xml)
        recomputed = hashlib.sha256(xml.replace(digest, "").encode("utf-8")).hexdigest()
        self.assertEqual(digest, recomputed)


BUNDLED = sorted((ROOT / "app").glob("*.docx"))
# Реквизит любого вида, вписанный текстом: ИНН (10/12 цифр), ОГРН/ОГРНИП
# (13/15), счёт (20), БИК (9) - сплошные цифры от девяти; телефон с +7 или
# 8 в любой записи; адрес почты; «ИП Фамилия». Проверка по видам, а не по
# значениям: значения владельца в репозитории не должны появиться и здесь.
LONG_NUMBER = r"(?<!\d)\d{9,}(?!\d)"
PHONE = r"(?:\+7|(?<![\d.,])8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)"
EMAIL = r"[\w.+-]+@[\w-]+\.[\w.-]+"
PERSON = r"(?:\bИП|[Пп]редпринимател\w*)\s+[А-ЯЁ][а-яё]+"


def visible_parts(docx: bytes) -> dict[str, str]:
    """Текст, который увидит читатель, по частям архива: <w:t> частей
    word/ (колонтитулы и сноски тоже) и текст свойств файла (автор)."""
    import io
    import re
    import zipfile
    out = {}
    with zipfile.ZipFile(io.BytesIO(docx)) as zf:
        for name in zf.namelist():
            if not name.endswith(".xml"):
                continue
            raw = zf.read(name).decode("utf-8")
            if name.startswith("word/"):
                out[name] = "".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", raw, re.S))
            elif name.startswith("docProps/"):
                out[name] = " ".join(re.sub(r"<[^>]+>", " ", raw).split())
    return out


class TestBundledDocsCarryNoRequisites(unittest.TestCase):
    """Поставочные документы - для любой установки, включая демо и
    франчайзи: арендодатель в них только подстановками из «Реквизитов
    организации», ни одного реквизита текстом и ни одного автора."""

    def test_six_documents_are_checked(self):
        self.assertEqual({p.name for p in BUNDLED}, {
            "contract_template.docx", "act_priema_template.docx",
            "act_vozvrata_template.docx", "act_vykup_template.docx",
            "soglasie_template.docx", "pdn_policy.docx"})

    def test_no_requisites_as_text(self):
        import re
        for path in BUNDLED:
            for part, text in visible_parts(path.read_bytes()).items():
                bare = contract.PLACEHOLDER.sub(" ", text).replace("\xa0", " ")
                for kind in (LONG_NUMBER, PHONE, EMAIL, PERSON):
                    self.assertEqual(re.findall(kind, bare), [],
                                     f"{path.name}/{part}: {kind}")

    def test_no_authors_in_file_properties(self):
        """Имена тех, кто правил файл, - тоже чужие данные: Word пишет их
        в свойства, и файл уходит клиенту вместе с ними."""
        import re
        import zipfile
        for path in BUNDLED:
            with zipfile.ZipFile(path) as zf:
                if "docProps/core.xml" not in zf.namelist():
                    continue
                core = zf.read("docProps/core.xml").decode("utf-8")
            for tag in ("dc:creator", "cp:lastModifiedBy"):
                got = re.findall(rf"<{tag}>([^<]*)</{tag}>", core)
                self.assertEqual([v for v in got if v.strip()], [], f"{path.name}: {tag}")

    def rendered(self, path, ctx) -> str:
        if path.name == "pdn_policy.docx":
            return document_text(contract.fill(path.read_bytes(), ctx))
        return document_text(contract.build(path, ctx)[0])

    def test_requisites_come_from_the_setting(self):
        """Каждое поле реквизитов, которое стоит в шаблоне, доезжает до
        документа значением из настройки."""
        from xml.sax.saxutils import escape
        for path in BUNDLED:
            fields = {f for f in contract.placeholders(path.read_bytes())
                      if f.startswith("company_")}
            self.assertTrue({"company_name", "company_inn"} <= fields, path.name)
            text = self.rendered(path, make_ctx())
            for field in fields:
                self.assertIn(escape(COMPANY[field]), text, f"{path.name}: {field}")

    def test_empty_setting_gives_dashes_not_gaps(self):
        """Реквизиты не заполнены - прочерки, а не «нет поля» и не скобки:
        документ уйдёт, и пустоту в нём видно сразу."""
        empty = make_ctx(**company.context({}))
        for path in BUNDLED:
            text = self.rendered(path, empty)
            self.assertNotIn("нет поля", text, path.name)
            self.assertNotIn("{{", text, path.name)
            self.assertIn("ИНН —", text.replace(":", ""), path.name)

    def test_policy_fill_keeps_a_file_without_placeholders(self):
        """Политика, где реквизиты вписаны текстом (правленная владельцем),
        и нечитаемый файл уходят байт в байт."""
        import io
        import zipfile
        plain = io.BytesIO()
        with zipfile.ZipFile(plain, "w") as zf:
            zf.writestr("word/document.xml", "<w:t>ИП со своим текстом</w:t>")
        self.assertEqual(contract.fill(plain.getvalue(), make_ctx()), plain.getvalue())
        self.assertEqual(contract.fill(b"not a zip", make_ctx()), b"not a zip")
        policy = (ROOT / "app" / "pdn_policy.docx").read_bytes()
        filled = contract.fill(policy, make_ctx())
        with zipfile.ZipFile(io.BytesIO(policy)) as src, \
                zipfile.ZipFile(io.BytesIO(filled)) as dst:
            for name in src.namelist():
                if name != "word/document.xml":
                    self.assertEqual(src.read(name), dst.read(name), name)


class TestDocx(unittest.TestCase):
    def test_builds_a_docx(self):
        docx, digest = contract.build(TEMPLATE, make_ctx())
        self.assertTrue(docx.startswith(b"PK"))
        self.assertEqual(len(digest), 64)
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(docx)) as zf:
            self.assertIsNone(zf.testzip())
            self.assertIn("word/document.xml", zf.namelist())
            self.assertIn("[Content_Types].xml", zf.namelist())

    def test_only_document_xml_changes(self):
        """Стили, шрифты и колонтитулы юриста копируются байт в байт."""
        import io
        import zipfile
        template = TEMPLATE.read_bytes()
        docx, _ = contract.build(TEMPLATE, make_ctx())
        with zipfile.ZipFile(io.BytesIO(template)) as src, \
                zipfile.ZipFile(io.BytesIO(docx)) as dst:
            self.assertEqual(sorted(src.namelist()), sorted(dst.namelist()))
            for name in src.namelist():
                if name == "word/document.xml":
                    continue
                self.assertEqual(src.read(name), dst.read(name), name)

    def test_long_values_do_not_break_build(self):
        ctx = make_ctx()
        ctx["passport_issuer"] = "ОТДЕЛОМ " + "ОЧЕНЬ ДЛИННОЕ НАЗВАНИЕ " * 8
        ctx["reg_address"] = "г. Казань, " + "ул. Очень Длинная, " * 10 + "д. 1"
        docx, _ = contract.build(TEMPLATE, ctx)
        self.assertTrue(docx.startswith(b"PK"))

    def test_missing_template_reports_clearly(self):
        with self.assertRaises(contract.TemplateProblem):
            contract.load_template(TEMPLATE.parent / "нет-такого.docx")

    def test_non_docx_template_reports_clearly(self):
        with self.assertRaises(contract.TemplateProblem):
            contract.load_template(ROOT / "README.md")


if __name__ == "__main__":
    unittest.main(verbosity=2)
