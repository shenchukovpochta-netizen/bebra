"""Переводы клиентского диалога: полнота, плейсхолдеры, кнопки, ошибки.

Смысловую верность машинного перевода тест проверить не может - но ловит
всё, что ломает бота технически: пропущенный ключ (клиент молча получит
русский), потерянный или лишний {placeholder} (KeyError при .format),
разъехавшиеся подписи кнопок (нажатие перестанет распознаваться) и перевод
ошибки, которой валидатор больше не выдаёт (мертвый перевод при живой
русской ошибке).
"""

from __future__ import annotations

import string
import unittest

from app import faq, i18n, logic, texts
from app.i18n import en

REQUIRED_KEYS = frozenset(en.T)
FORMATTER = string.Formatter()


def fields(template: str) -> set[str]:
    return {name for _, name, _, _ in FORMATTER.parse(template) if name}


def russian_source(key: str) -> str:
    if key in i18n.BUTTONS_RU:
        return i18n.BUTTONS_RU[key]
    return getattr(texts, key)


class TestPacks(unittest.TestCase):
    def test_every_language_has_every_key(self):
        for code, pack in i18n.PACKS.items():
            missing = REQUIRED_KEYS - set(pack)
            extra = set(pack) - REQUIRED_KEYS
            self.assertFalse(missing, f"{code}: нет ключей {sorted(missing)}")
            self.assertFalse(extra, f"{code}: лишние ключи {sorted(extra)}")

    def test_placeholders_match_the_russian_source(self):
        """Потерянный {number} уронит .format у обработчика, лишний - даст
        KeyError. Набор плейсхолдеров обязан совпадать с русским texts.py."""
        for code, pack in i18n.PACKS.items():
            for key, value in pack.items():
                self.assertEqual(
                    fields(russian_source(key)), fields(value),
                    f"{code}/{key}: плейсхолдеры разошлись с русским")

    def test_no_empty_translations(self):
        for code, pack in i18n.PACKS.items():
            for key, value in pack.items():
                self.assertTrue(value.strip(), f"{code}/{key} пуст")

    def test_fallback_serves_russian(self):
        self.assertEqual(i18n.t("ru", "WELCOME"), texts.WELCOME)
        self.assertEqual(i18n.t(None, "WELCOME"), texts.WELCOME)
        self.assertEqual(i18n.t("xx", "WELCOME"), texts.WELCOME)
        self.assertEqual(i18n.t("en", "WELCOME"), en.T["WELCOME"])

    def test_support_contact_survives_in_every_language(self):
        """Прямой контакт проката обязан остаться ссылкой в переводах
        текстов, где он есть по-русски."""
        for key in ("SUPPORT_PROMPT", "SUPPORT_FAILED", "TARIFFS",
                    "RENT_REQUEST_FAILED", "CLOSE_REQUEST_FAILED",
                    "FAQ_GUEST_CONTACT"):
            for code, pack in i18n.PACKS.items():
                self.assertIn(texts.SUPPORT_CONTACT_URL, pack[key],
                              f"{code}/{key}: потерян контакт проката")

    def test_prices_survive_in_tariffs(self):
        """Цены в переведённых тарифах - те же, что в русском тексте."""
        for price in ("3 000", "5 400", "11 000", "3 400", "3 500",
                      "6 400", "12 000", "650"):
            digits = price.replace(" ", "")
            for code, pack in i18n.PACKS.items():
                flat = pack["TARIFFS"].replace(" ", "").replace(",", "") \
                                      .replace(" ", "")
                self.assertIn(digits, flat,
                              f"{code}: в тарифах нет цены {price}")


class TestButtons(unittest.TestCase):
    def test_canonical_labels_match_the_code(self):
        """Русские подписи в i18n - источник; они обязаны совпадать с тем,
        что реально используют faq.py и texts.py."""
        self.assertEqual(i18n.BUTTONS_RU["BTN_FAQ"], faq.MENU_BUTTON)
        self.assertEqual(i18n.BUTTONS_RU["BTN_CLOSE_RENT"], texts.BTN_CLOSE_RENT)

    def test_reverse_lookup_is_unambiguous(self):
        """Одна подпись - одна кнопка: коллизия делает нажатие лотереей."""
        seen: dict[str, str] = {}
        for key in i18n._TEXT_BUTTONS:
            for label in i18n.variants(key):
                self.assertNotIn(label, seen,
                                 f"подпись «{label}» и у {seen.get(label)}, "
                                 f"и у {key}")
                seen[label] = key

    def test_button_key_finds_any_language(self):
        for code, pack in i18n.PACKS.items():
            self.assertEqual(i18n.button_key(pack["BTN_RENT"]), "BTN_RENT",
                             f"{code}: кнопка аренды не распознаётся")
            self.assertEqual(i18n.button_key(pack["BTN_CANCEL"]), "BTN_CANCEL")
        self.assertEqual(i18n.button_key("🚲 Арендовать"), "BTN_RENT")
        self.assertIsNone(i18n.button_key("просто текст"))


class TestErrors(unittest.TestCase):
    def battery(self) -> set[str]:
        """Все ошибки, которые валидаторы реально выдают на плохой ввод."""
        errors = set()

        def bad(validation):
            self.assertFalse(validation.ok)
            errors.add(validation.error)

        bad(logic.validate_fio("ы"))
        bad(logic.validate_fio("Иванов Иван 2-й"))
        bad(logic.validate_fio("Иванов <Иван> Иванович"))
        bad(logic.validate_birth_date("07-03"))
        bad(logic.validate_birth_date("31.02.1990"))
        bad(logic.validate_birth_date("07.03.2999"))
        bad(logic.validate_birth_date("07.03.1890"))   # раньше 1900
        bad(logic.validate_birth_date("07.03.1920"))   # опечатка в годе
        bad(logic.validate_birth_date("07.03.2015"))   # моложе 16
        bad(logic.validate_passport_number("123"))
        bad(logic.validate_passport_code("12"))
        bad(logic.validate_passport_issuer("х"))
        bad(logic.validate_passport_issuer("ОУФМС <России>" + "х" * 10))
        bad(logic.validate_birth_place(""))
        bad(logic.validate_birth_place("гор. <Казань>"))
        bad(logic.validate_address("Казань"))
        bad(logic.validate_address("г. Казань, ул. <Баумана>, д. 1, кв. 2"))
        bad(logic.validate_address("г. Казань, улица Баумана, квартира два"))
        bad(logic.validate_phone("абвгд"))
        bad(logic.validate_phone("+79990000000", taken=["+79990000000"]))
        bad(logic.validate_upload(True, None, 100 * 1024 * 1024))
        bad(logic.validate_upload(False, "application/pdf", 100))
        bad(logic.support_question("хм"))
        bad(logic.support_question("о" * 1600))
        bad(logic.close_reason("ху"))
        return errors

    def test_translated_errors_are_real_validator_errors(self):
        """Ключ словаря ошибок - живая русская строка из logic.py: если
        формулировку в валидаторе поправили, перевод молча отвалится -
        этот тест не даст."""
        produced = self.battery()
        for code, errs in i18n.ERRORS.items():
            for ru_error in errs:
                self.assertIn(ru_error, produced,
                              f"{code}: перевод ошибки «{ru_error[:40]}…» "
                              "не совпадает ни с одной живой ошибкой")

    def test_common_errors_are_translated_everywhere(self):
        """Ошибки анкеты клиент видит чаще всего - они обязаны быть
        переведены на каждый язык полностью."""
        must = {
            "Похоже на опечатку. Введите ФИО полностью.",
            "Дата нужна в виде ДД.ММ.ГГГГ, например 07.03.1990.",
            "Не похоже на номер телефона. Пример: +7 900 123-45-67.",
        }
        for code, errs in i18n.ERRORS.items():
            for ru_error in must:
                self.assertIn(ru_error, errs, f"{code}: нет перевода «{ru_error}»")
        self.assertEqual(i18n.err("en", "Дата не может быть в будущем."),
                         "The date cannot be in the future.")
        self.assertEqual(i18n.err("ru", "Дата не может быть в будущем."),
                         "Дата не может быть в будущем.")
        # непереведённая ошибка уходит как есть, а не падает
        self.assertEqual(i18n.err("en", "какая-то новая ошибка"),
                         "какая-то новая ошибка")


if __name__ == "__main__":
    unittest.main()
