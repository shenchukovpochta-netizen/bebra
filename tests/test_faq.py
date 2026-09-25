"""Ветка частых вопросов: распознавание тем, приоритеты и содержание ответов.

Отдельный набор без aiogram и без базы: роутер интентов - чистая логика,
и ломаться он должен здесь, а не в чате с клиентом.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import (
    faq,  # noqa: E402
    texts,  # noqa: E402
)
from app import faq_i18n as i18n  # noqa: E402
from app.crm import points  # noqa: E402

DAY = datetime(2026, 8, 10, 12, 0)      # рабочее время
NIGHT = datetime(2026, 8, 10, 23, 0)    # вне графика


class TestMatching(unittest.TestCase):
    """Фразы взяты из тем обращений заказчика: это то, что клиенты пишут."""

    CASES = {
        "ADDR": ("скиньте адрес", "где вы находитесь", "как доехать до вас",
                 "вы на павлюхина?", "скиньте геолокацию"),
        "HOURS": ("до скольки работаете", "работаете в воскресенье",
                  "какой график", "вы ещё открыты"),
        "PRICE": ("сколько стоит аренда", "какая цена на неделю",
                  "скиньте прайс", "почем в месяц"),
        "PAY": ("куда платить", "скиньте реквизиты", "как оплатить",
                "оплатил, вот чек", "перевёл 3000"),
        "BATT_Q": ("батарея быстро садится", "не держит заряд",
                   "какой запас хода", "сколько км проедет"),
        "BATT_SWAP": ("есть заряженные аккумуляторы", "хочу поменять акб",
                      "можно обмен акб"),
        # «третий акб» в исходном роутере не ловился: там стоял один символ
        # окончания. Расширенное правило обязано ловить оба написания.
        "BATT_3": ("хочу третий акб", "нужен доп акб", "третья акб есть?",
                   "нужен третий аккумулятор", "дополнительный аккумулятор есть"),
        "RETURN": ("хочу сдать велосипед", "верну завтра",
                   "сделайте перерасчет", "расторгаем договор"),
        "RENEW": ("хочу продлить", "продлеваю", "продление как оформить"),
        "LEAD": ("есть в наличии велики", "хочу арендовать",
                 "можно взять велик", "свободные есть?"),
        "DOCS": ("какие документы нужны", "есть ли залог",
                 "нужна ли прописка", "я не гражданин рф, миграционная карта"),
        "BRK_EL": ("велик не едет", "дисплей не включается", "заглох в дороге"),
        "BRK_WHEEL": ("пробил колесо", "прокол камеры", "спустило шину"),
        "BRK_MECH": ("тормоза скрипят", "слетела цепь", "сломал педаль",
                     "потерял ключ"),
        "PICKUP": ("вы можете приехать", "заберете велик?", "нужна доставка",
                   "далеко нести", "вы приедете сегодня"),
        "EXT_REP": ("у вас можно починить самокат", "ремонтируете скутеры",
                    "почините мой велик"),
        "REP_STATUS": ("когда будет готов велик", "какие новости по вело"),
        "BUYOUT": ("хочу выкупить велик", "есть рассрочка",
                   "продаете велосипеды?"),
        "CHP": ("у меня угнали велосипед", "попал в дтп",
                "написал заявление в полицию"),
        "DEBT": ("я просрочил", "оплачу завтра", "нет денег сейчас",
                 "какая у меня задолженность"),
        "CLAIM": ("подам в суд", "напишу отзыв", "верните деньги"),
        "DISC": ("будет скидка?", "есть промокод", "я постоянный клиент"),
        "MINOR": ("мне 16", "оформляем на сына", "нужно согласие родителей"),
    }

    def test_every_intent_recognises_real_phrases(self):
        for code, phrases in self.CASES.items():
            for phrase in phrases:
                hit = faq.match(phrase)
                self.assertIsNotNone(hit, phrase)
                self.assertEqual(hit.code, code, f"{phrase!r} -> {hit.code}")

    def test_every_intent_is_covered_by_cases(self):
        """Новый интент без примера фразы - интент, который никто не проверил."""
        self.assertEqual({i.code for i in faq.INTENTS}, set(self.CASES))

    def test_unknown_question_has_no_intent(self):
        for phrase in ("привет", "спасибо большое", "ок", ""):
            self.assertIsNone(faq.match(phrase), phrase)
        self.assertIsNone(faq.match(None))

    def test_red_lines_win_over_ordinary_topics(self):
        """Приоритет заказчика: в сообщении про долг бот обязан молчать,
        даже если там же спрашивают, куда платить."""
        self.assertEqual(faq.match("я просрочил, куда платить?").code, "DEBT")
        self.assertEqual(faq.match("велик угнали, вернете деньги?").code, "CHP")
        self.assertEqual(faq.match("мне 17, какие документы нужны").code, "MINOR")
        self.assertEqual(faq.match("дайте скидку на продление").code, "DISC")

    def test_case_is_ignored(self):
        self.assertEqual(faq.match("ГДЕ ВЫ НАХОДИТЕСЬ").code, "ADDR")

    def test_renewal_with_a_term_lands_in_price_but_answers_renewal(self):
        """В роутере заказчика «на неделю» - триггер цены, и он старше
        продления: «продлеваю на неделю» распознаётся темой цены. Для
        действующего арендатора это не беда - ответ всё равно про продление,
        а не прайс для новых. Проверяем именно исход, а не ярлык."""
        hit = faq.match("продлеваю на неделю")
        self.assertEqual(hit.code, "PRICE")
        text = faq.answer(hit, now=DAY, renter=True, plan="3000 qr")
        self.assertIn("qr.nspk.ru", text)
        self.assertNotIn("Truck+", text)

    def test_client_coming_himself_is_not_a_pickup_request(self):
        """Регрессия по триггерам заказчика: голое «приехать» ловило клиента,
        который сам собрался приехать, и он получал «выездного мастера нет»."""
        for phrase in ("могу приехать в 17", "я приехать сегодня не успею"):
            hit = faq.match(phrase)
            self.assertNotEqual(hit.code if hit else "", "PICKUP", phrase)


class TestAnswers(unittest.TestCase):
    def setUp(self):
        self.by_code = faq.BY_CODE

    def test_every_open_topic_has_an_answer(self):
        for intent in faq.INTENTS:
            if intent.red:
                continue
            text = faq.answer(intent, now=DAY)
            self.assertTrue(text.strip(), intent.code)
            self.assertNotIn("{", text, intent.code)

    def test_red_lines_say_nothing_but_the_neutral_line(self):
        """§5 инструкции: про долг, угон, суд и скидку бот не пишет ничего."""
        for intent in faq.INTENTS:
            if not intent.red:
                continue
            text = faq.answer(intent, now=DAY)
            self.assertEqual(text, faq.RED_LINE_REPLY, intent.code)
            for leak in ("qr.nspk", "₽", "полиц", "заявлен", "суд", "долг"):
                self.assertNotIn(leak, text.lower(), f"{intent.code}: {leak}")

    def test_bot_never_names_repair_price(self):
        """Стоимость и срок ремонта называет мастер после осмотра."""
        for code in ("BRK_EL", "BRK_WHEEL", "BRK_MECH", "REP_STATUS", "EXT_REP"):
            text = faq.answer(self.by_code[code], now=DAY)
            self.assertNotIn("₽", text, code)
            self.assertNotIn("рубл", text.lower(), code)

    def test_payment_link_is_escaped_for_html(self):
        """Неэкранированный & в ссылке - это отказ Telegram разобрать
        сообщение, то есть вопрос без ответа."""
        for code in ("PAY", "RENEW"):
            text = faq.answer(self.by_code[code], now=DAY)
            self.assertIn("qr.nspk.ru", text)
            self.assertIn("&amp;", text)
            self.assertNotIn("?type=01&bank", text)

    def test_payment_link_can_be_replaced_without_touching_the_texts(self):
        """Расчётный счёт меняется настройкой: ссылка подставляется
        в ответы, а не зашита в них."""
        for code in ("PAY", "RENEW"):
            text = faq.answer(self.by_code[code], now=DAY,
                              pay_url="https://qr.nspk.ru/NEW?a=1&b=2")
            self.assertIn("https://qr.nspk.ru/NEW?a=1&amp;b=2", text)
            self.assertNotIn("BS1A0050", text)
        renter = faq.answer(self.by_code["PRICE"], now=DAY, renter=True,
                            pay_url="https://qr.nspk.ru/NEW?a=1")
        self.assertIn("qr.nspk.ru/NEW", renter)

    def test_no_answer_leaks_an_unfilled_link_placeholder(self):
        for intent in faq.INTENTS:
            for renter in (False, True):
                text = faq.answer(intent, now=DAY, renter=renter, plan="3000")
                self.assertNotIn(faq.PAY_FIELD, text, intent.code)

    def test_price_answer_depends_on_who_asks(self):
        price = self.by_code["PRICE"]
        lead = faq.answer(price, now=DAY, renter=False)
        self.assertIn("Truck+", lead)
        self.assertEqual(lead, texts.TARIFFS, "тарифы должны быть одни на бота")

        renter = faq.answer(price, now=DAY, renter=True, plan="3000 qr")
        self.assertIn("3000 qr", renter)
        self.assertIn("qr.nspk.ru", renter)
        self.assertNotIn("Truck+", renter, "действующему нужен не прайс, а продление")

    def test_renter_without_known_plan_still_gets_the_link(self):
        renter = faq.answer(self.by_code["PRICE"], now=DAY, renter=True)
        self.assertIn("qr.nspk.ru", renter)

    def test_after_hours_note_only_when_closed(self):
        addr = self.by_code["ADDR"]
        self.assertNotIn(faq.AFTER_HOURS, faq.answer(addr, now=DAY))
        self.assertIn(faq.AFTER_HOURS, faq.answer(addr, now=NIGHT))
        # Продление оплачивается онлайн - звать приехать незачем.
        self.assertNotIn(faq.AFTER_HOURS, faq.answer(self.by_code["RENEW"], now=NIGHT))

    def test_working_hours_boundaries(self):
        self.assertFalse(faq.is_open(datetime(2026, 8, 10, 9, 59)))
        self.assertTrue(faq.is_open(datetime(2026, 8, 10, 10, 0)))
        self.assertTrue(faq.is_open(datetime(2026, 8, 10, 18, 59)))
        self.assertFalse(faq.is_open(datetime(2026, 8, 10, 19, 0)))

    def test_both_points_are_named_where_it_matters(self):
        for code in ("ADDR", "HOURS", "LEAD", "BRK_MECH", "EXT_REP"):
            text = faq.answer(self.by_code[code], now=DAY)
            self.assertIn("Адоратского", text, code)
            self.assertIn("Павлюхина", text, code)

    def test_promises_to_return_depend_on_the_client_answering(self):
        """Регрессия: тема из меню обещала «уточню и вернусь», но карточка
        менеджеру уходит только с ответным сообщением клиента — молча
        обещать перезвон нельзя, ждать он будет зря."""
        for intent in faq.MENU_TOPICS:
            if not intent.handoff:
                continue
            text = faq.answer(intent, now=DAY)
            self.assertTrue(
                any(w in text.lower() for w in
                    ("напишите", "подскажите", "скажите", "опишите",
                     "предупредите", "какая точка", "какой срок", "передам")),
                f"{intent.code}: ответ не просит клиента ответить")

    def test_unresolved_prices_are_not_invented(self):
        """Цены, которых нет в карточке фактов (третий АКБ, забор велика),
        бот называть не должен - только «уточню и вернусь»."""
        for code in ("BATT_3", "PICKUP"):
            text = faq.answer(self.by_code[code], now=DAY)
            self.assertNotIn("₽", text, code)


class TestMenuTopics(unittest.TestCase):
    def test_red_lines_are_not_offered_as_topics(self):
        for intent in faq.MENU_TOPICS:
            self.assertFalse(intent.red, intent.code)

    def test_breakdown_is_one_topic_not_three(self):
        """Три интента поломок отвечаются одним текстом: в меню хватит одной
        темы, иначе человек выбирает между «колесо» и «механика» вслепую."""
        broken = [i for i in faq.MENU_TOPICS if i.code.startswith("BRK_")]
        self.assertEqual(len(broken), 1)

    def test_topics_are_ordered_by_priority(self):
        codes = [i.priority for i in faq.MENU_TOPICS]
        self.assertEqual(codes, sorted(codes))

    def test_callback_data_fits_telegram_limit(self):
        for intent in faq.MENU_TOPICS:
            self.assertLessEqual(len(f"faq:{intent.code}".encode()), 64)


class TestI18n(unittest.TestCase):
    """Переводы ветки: полнота, ссылки и совпадение цен с русским прайсом."""

    FOREIGN = tuple(code for code in i18n.LANGS if code != "ru")

    def test_requested_languages_are_supported(self):
        # русский, английский, узбекский, туркменский, татарский,
        # египетский (арабский), иранский (фарси), хинди, чувашский -
        # список заказчика
        self.assertEqual(set(i18n.LANGS),
                         {"ru", "en", "uz", "tk", "tt", "ar", "fa", "hi", "cv"})
        for code in i18n.LANGS:
            self.assertIn(code, i18n.LANG_TITLES)

    def test_every_language_has_every_key(self):
        """Пропущенный ключ молча уронил бы клиента на русский - лучше
        узнать об этом здесь, чем от клиента."""
        answer_keys = {"a_" + i.code for i in faq.MENU_TOPICS
                       if i.code != "PRICE"}
        title_keys = {"t_" + i.code for i in faq.MENU_TOPICS}
        # Ответ по справочнику точек без перевода ушёл бы русским списком
        # посреди диалога на другом языке.
        point_keys = {"p_" + i.code for i in faq.MENU_TOPICS
                      if i.code in faq.POINT_ANSWERS}
        need = set(i18n.REQUIRED_KEYS) | answer_keys | title_keys | point_keys
        for code in self.FOREIGN:
            missing = need - set(i18n.T[code])
            self.assertFalse(missing, f"{code}: нет ключей {sorted(missing)}")

    def test_every_topic_answers_in_every_language(self):
        for code in i18n.LANGS:
            for intent in faq.MENU_TOPICS:
                text = faq.answer(intent, now=DAY, lang=code)
                self.assertTrue(text.strip(), f"{code}/{intent.code}")
                self.assertNotIn("{pay_url}", text, f"{code}/{intent.code}")
                self.assertNotIn("нет поля", text, f"{code}/{intent.code}")

    def test_payment_link_lands_in_every_language(self):
        for code in i18n.LANGS:
            for topic in ("PAY", "RENEW"):
                text = faq.answer(faq.BY_CODE[topic], now=DAY, lang=code,
                                  pay_url="https://qr.nspk.ru/X?a=1&b=2")
                self.assertIn("qr.nspk.ru/X", text, f"{code}/{topic}")
                self.assertIn("&amp;", text, f"{code}/{topic}")

    def test_prices_match_the_russian_price_list(self):
        """Числа в TARIFF_ROWS обязаны совпадать с texts.TARIFFS: два прайса
        в двух местах разъезжаются при первой же правке цен."""
        tariffs = texts.TARIFFS.replace("\xa0", " ")
        for name, w1, w2, mo in faq.TARIFF_ROWS:
            for price in (w1, w2, mo):
                self.assertIn(price, tariffs, f"{name}: {price}")

    def test_buyout_prices_are_the_same_in_every_language(self):
        for code in i18n.LANGS:
            text = faq.answer(faq.BY_CODE["BUYOUT"], now=DAY, lang=code)
            self.assertIn("35 000", text, code)
            self.assertIn("45 000", text, code)

    def test_foreign_price_answer_carries_all_rates(self):
        for code in self.FOREIGN:
            text = faq.answer(faq.BY_CODE["PRICE"], now=DAY, lang=code)
            for price in ("3 000", "3 400", "3 500", "5 400", "6 400",
                          "11 000", "12 000", "650"):
                self.assertIn(price, text, f"{code}: нет цены {price}")

    def test_unknown_language_falls_back_to_russian(self):
        text = faq.answer(faq.BY_CODE["ADDR"], now=DAY, lang="xx")
        self.assertIn("Адоратского", text)
        self.assertEqual(faq.topic_title(faq.BY_CODE["ADDR"], "xx"),
                         faq.BY_CODE["ADDR"].title)

    def test_after_hours_note_is_translated(self):
        for code in self.FOREIGN:
            night = faq.answer(faq.BY_CODE["ADDR"], now=NIGHT, lang=code)
            day = faq.answer(faq.BY_CODE["ADDR"], now=DAY, lang=code)
            self.assertNotEqual(night, day, code)
            self.assertNotIn(faq.AFTER_HOURS, night,
                             f"{code}: приписка осталась русской")

    def test_lang_callbacks_fit_telegram_limit(self):
        for code in i18n.LANGS:
            self.assertLessEqual(len(f"faqlang:{code}".encode()), 64)

    def test_addresses_survive_translation(self):
        """Адрес - то, что человек покажет таксисту: кириллический оригинал
        обязан присутствовать в каждом переводе ответа про адреса."""
        for code in i18n.LANGS:
            text = faq.answer(faq.BY_CODE["ADDR"], now=DAY, lang=code)
            self.assertIn("11А", text, code)
            self.assertIn("97А", text, code)

    def test_no_html_markup_hazards_in_translations(self):
        for code in self.FOREIGN:
            for key, value in i18n.T[code].items():
                self.assertNotIn("<", value, f"{code}/{key}")
                self.assertNotIn("&", value.replace("&amp;", ""),
                                 f"{code}/{key}")

    def test_working_hours_are_the_same_in_every_language(self):
        """10:00–19:00 - факт, а не формулировка: перевод не имеет права
        назвать другие часы или потерять их."""
        for code in i18n.LANGS:
            text = faq.answer(faq.BY_CODE["HOURS"], now=DAY, lang=code)
            self.assertIn("10:00", text, code)
            self.assertIn("19:00", text, code)

    def test_contact_key_carries_the_contact_url(self):
        for code in self.FOREIGN:
            self.assertIn("t.me/arenda_velo_kazan",
                          i18n.T[code]["contact"], code)

    def test_renter_price_in_foreign_language_talks_renewal(self):
        for code in self.FOREIGN:
            text = faq.answer(faq.BY_CODE["PRICE"], now=DAY, lang=code,
                              renter=True, plan="3000 qr")
            self.assertIn("3000 qr", text, code)
            self.assertIn("qr.nspk.ru", text, code)
            self.assertNotIn("Truck+", text, code)


# Три точки, как их отдаёт справочник панели: третья заведена владельцем,
# у второй не заполнены режим и телефон, у третьей нет названия для курьеров.
THREE_POINTS = (
    {"name": "Павлюхина", "public_title": "Май Байк — Павлюхина",
     "address": "г. Казань, ул. Павлюхина, 97А", "hours": "пн-вс: 10:00-19:00",
     "phone": "+7 (904) 676-49-26"},
    {"name": "Адоратского", "public_title": "Май Байк — Адоратского",
     "address": "г. Казань, ул. Адоратского, 11А", "hours": None, "phone": ""},
    {"name": "Восстания", "public_title": None,
     "address": "г. Казань, ул. Восстания, 100", "hours": "пн-пт: 09:00-21:00",
     "phone": "+7 (900) 000-00-03"},
)
ADDRESSES = tuple(p["address"] for p in THREE_POINTS)

# «Две», «обе», «на обеих» на каждом языке: с третьей точкой в справочнике
# любое из них - ложь. Фразы узкие намеренно: «два АКБ» и «2 batteries»
# правдивы и должны остаться.
NUMBER_CLAIMS = {
    "ru": ("две точки", "обе ", "обеих", "двух точ"),
    "en": ("two points", "both", "either"),
    "uz": ("ikkita punkt", "ikkala"),
    "tk": ("iki nokat", "ikisi"),
    "ar": ("نقطتان", "نقطتين"),
    "fa": ("دو شعبه", "هر دو"),
    "hi": ("दो पॉइंट", "दोनों"),
    "tt": ("ике пункт", "икесе"),
    "cv": ("икӗ пункт", "иккӗшӗ"),
}


class TestPointsFromDirectory(unittest.TestCase):
    """Ответы по справочнику точек: каждая открытая точка, на каждом языке,
    и прежний зашитый текст, пока справочника нет."""

    def ask(self, code, lang="ru", now=DAY, pts=THREE_POINTS):
        return faq.answer(faq.BY_CODE[code], now=now, lang=lang, points=pts)

    def test_three_points_are_listed_in_russian(self):
        for code in ("ADDR", "HOURS"):
            text = self.ask(code)
            for point in THREE_POINTS:
                self.assertIn(point["address"], text, code)
            self.assertIn("Май Байк — Павлюхина", text, "название для курьеров")
            self.assertIn("Восстания", text, "нет названия - служебное имя")
            self.assertIn("пн-пт: 09:00-21:00", text, "режим у точки свой")
            self.assertIn("+7 (904) 676-49-26", text)
            self.assertNotIn("ГСК «Сокол»", text, "зашитый текст не подмешан")
            self.assertNotIn(faq.POINTS_FIELD, text)

    def test_three_points_are_listed_in_every_language(self):
        """Адрес - данные: на любом языке он такой, как записан в панели."""
        for lang in i18n.LANGS:
            for code in ("ADDR", "HOURS"):
                text = self.ask(code, lang)
                for address in ADDRESSES:
                    self.assertIn(address, text, f"{lang}/{code}")
                self.assertNotIn(faq.POINTS_FIELD, text, f"{lang}/{code}")
                self.assertNotEqual(text, faq.answer(faq.BY_CODE[code], now=DAY,
                                                     lang=lang), f"{lang}/{code}")

    def test_headers_around_the_list_are_translated(self):
        ru = self.ask("ADDR").split("\n")[0]
        for lang in i18n.LANGS:
            if lang == "ru":
                continue
            for code in ("ADDR", "HOURS"):
                head = self.ask(code, lang).split("\n")[0]
                self.assertNotEqual(head, ru, f"{lang}/{code}: шапка русская")

    def test_no_answer_claims_a_number_of_points(self):
        for lang, claims in NUMBER_CLAIMS.items():
            for intent in faq.INTENTS:
                text = faq.answer(intent, now=NIGHT, lang=lang,
                                  points=THREE_POINTS).lower()
                for claim in claims:
                    self.assertNotIn(claim, text, f"{lang}/{intent.code}")

    def test_every_point_mentioning_answer_lists_all_points(self):
        """Лид, поломка, АКБ и чужая техника тоже зовут на точку - с третьей
        точкой в справочнике ни один из них не может назвать только две."""
        for lang in i18n.LANGS:
            for code in faq.POINT_ANSWERS:
                text = self.ask(code, lang)
                for address in ADDRESSES:
                    self.assertIn(address, text, f"{lang}/{code}")

    def test_answers_without_points_are_unchanged(self):
        """Бот без базы и пустой справочник - прежние зашитые ответы."""
        for lang in i18n.LANGS:
            for intent in faq.INTENTS:
                base = faq.answer(intent, now=DAY, lang=lang)
                for empty in (None, [], [{"name": "", "address": None}]):
                    self.assertEqual(
                        faq.answer(intent, now=DAY, lang=lang, points=empty),
                        base, f"{lang}/{intent.code}: {empty!r}")
        text = self.ask("ADDR", pts=None)
        self.assertIn("11А", text)
        self.assertIn("97А", text)

    def test_empty_fields_are_skipped(self):
        text = self.ask("HOURS")
        self.assertEqual(text.count("🕙"), 2, "у Адоратского режима нет")
        self.assertEqual(text.count("📞"), 2, "у Адоратского телефона нет")
        self.assertNotIn("None", text)
        block = text.split("📍 Май Байк — Адоратского\n", 1)[1].split("\n\n")[0]
        self.assertEqual(block, "г. Казань, ул. Адоратского, 11А")

    def test_short_list_is_one_line_per_point(self):
        """Адрес и режим - одной строкой: «привозите в часы работы» без
        часов - вопрос, с которым клиент вернётся."""
        text = self.ask("LEAD")
        lines = [line for line in text.split("\n") if line.startswith("📍")]
        self.assertEqual(lines, ["📍 г. Казань, ул. Павлюхина, 97А · 🕙 пн-вс: 10:00-19:00",
                                 "📍 г. Казань, ул. Адоратского, 11А",
                                 "📍 г. Казань, ул. Восстания, 100 · 🕙 пн-пт: 09:00-21:00"])

    def test_every_answer_listing_points_names_each_points_hours(self):
        """Каждый ответ со списком точек - с режимом каждой, на всех языках."""
        for lang in i18n.LANGS:
            for code in faq.POINT_ANSWERS:
                text = self.ask(code, lang)
                self.assertIn("пн-вс: 10:00-19:00", text, f"{lang}/{code}")
                self.assertIn("пн-пт: 09:00-21:00", text, f"{lang}/{code}")

    def test_directions_reach_every_list(self):
        """«Как найти» (ГСК, бокс) - в полном списке строкой под адресом, в
        коротком - под строкой адреса: без него курьер приедет к воротам
        кооператива, а не к боксу."""
        way = "Заезд в ГСК «Сокол», ищите 9-й бокс — если не найдёте, напишите, встретим"
        pts = [{**THREE_POINTS[0], "directions": way}, *THREE_POINTS[1:]]
        for lang in i18n.LANGS:
            for code in faq.POINT_ANSWERS:
                text = self.ask(code, lang, pts=pts)
                self.assertIn("🧭 " + way, text, f"{lang}/{code}")
                self.assertEqual(text.count("🧭"), 1, f"{lang}/{code}")
        full = self.ask("ADDR", pts=pts)
        self.assertIn("г. Казань, ул. Павлюхина, 97А\n🧭 " + way, full)

    def test_values_from_the_panel_are_escaped(self):
        """«&» или «<» в адресе - сообщение, которое Telegram не разберёт."""
        odd = [{"name": "Склад", "address": "ул. Правды, 1 <корп. 2> & двор"}]
        for code in faq.POINT_ANSWERS:
            text = self.ask(code, pts=odd)
            self.assertIn("ул. Правды, 1 &lt;корп. 2&gt; &amp; двор", text, code)
            self.assertNotIn("<корп", text, code)

    def test_point_data_is_not_substituted_again(self):
        """Список ставится последним: подстановка оплаты по адресу не ходит."""
        odd = [{"name": "Склад", "address": "двор {pay_url}"}]
        text = self.ask("ADDR", pts=odd)
        self.assertIn("двор {pay_url}", text)
        self.assertNotIn("qr.nspk.ru", text)

    def test_every_points_template_has_the_list_once(self):
        """Лид и поломка собраны заменой зашитой строки точек: правка текста,
        потерявшая эту строку, потеряла бы и список."""
        for code, text in faq.POINT_ANSWERS.items():
            self.assertEqual(text.count(faq.POINTS_FIELD), 1, code)
        for lang in i18n.LANGS[1:]:
            for key, value in i18n.T[lang].items():
                if key.startswith("p_"):
                    self.assertEqual(value.count(faq.POINTS_FIELD), 1,
                                     f"{lang}/{key}")

    def test_fixed_hours_never_meet_directory_points(self):
        """«Сейчас закрыто, работаем 10–19 без выходных» рядом с точкой
        «пн-пт: 09:00-21:00» - сообщение спорит само с собой и в 19:30
        отправляет клиента от открытой точки. Со справочником режим - у
        каждой точки в списке, общих часов нет ни в одном ответе, ни в
        приписке, ни на одном языке."""
        evening = DAY.replace(hour=19, minute=30)
        for lang in i18n.LANGS:
            t = i18n.T.get(lang, {})
            fixed = [faq.AFTER_HOURS, faq.WORKING_HOURS, "10:00–19:00",
                     t.get("after_hours") or faq.AFTER_HOURS]
            for intent in faq.INTENTS:
                for now in (DAY, evening, NIGHT):
                    text = faq.answer(intent, now=now, lang=lang, points=THREE_POINTS)
                    for phrase in fixed:
                        self.assertNotIn(phrase, text, f"{lang}/{intent.code}/{now:%H:%M}")
        # Без справочника - прежняя приписка вне графика.
        self.assertIn(faq.AFTER_HOURS, self.ask("ADDR", now=NIGHT, pts=None))
        self.assertNotIn(faq.AFTER_HOURS, self.ask("ADDR", pts=None))

    def test_handoff_answers_still_ask_the_client_to_reply(self):
        for intent in faq.MENU_TOPICS:
            if not intent.handoff or intent.code not in faq.POINT_ANSWERS:
                continue
            text = self.ask(intent.code).lower()
            self.assertTrue(
                any(w in text for w in ("напишите", "подскажите", "скажите",
                                        "опишите", "передам")), intent.code)

    def test_bot_never_names_repair_price_with_points_either(self):
        for code in ("BRK_EL", "BRK_WHEEL", "BRK_MECH", "EXT_REP"):
            text = self.ask(code)
            self.assertNotIn("₽", text, code)
            self.assertNotIn("рубл", text.lower(), code)


class _Directory:
    """Справочник точек как его видит бот: только чтение открытых."""

    def __init__(self, rows):
        self.rows, self.calls = list(rows), []

    async def locations(self, *, active_only=False):
        self.calls.append(active_only)
        return [dict(r) for r in self.rows
                if not active_only or r.get("active", True)]


class TestPointsSnapshot(unittest.TestCase):
    """Снимок справочника в процессе бота - по образцу реквизитов."""

    def setUp(self):
        points.reset()

    def tearDown(self):
        # Снимок общий на процесс: оставленный здесь список подменил бы
        # адреса в тестах бота, которые идут следом.
        points.reset()

    def test_snapshot_is_stale_right_after_boot(self):
        # time.monotonic() считает от загрузки системы: через минуту после
        # перезагрузки сервера он около 60. Ненаполненный снимок не должен
        # выглядеть свежим - иначе бот пять минут отвечал бы зашитыми адресами.
        with mock.patch.object(points.time, "monotonic", return_value=60.0):
            self.assertFalse(points.is_fresh())
            points.set_snapshot(THREE_POINTS)
            self.assertTrue(points.is_fresh())
        self.assertFalse(points.is_fresh(now=60.0 + points.TTL_SECONDS))

    def test_refresh_reads_once_and_then_uses_the_snapshot(self):
        crm = _Directory(THREE_POINTS)
        rows = asyncio.run(points.refresh(crm))
        self.assertEqual([r["address"] for r in rows], list(ADDRESSES))
        asyncio.run(points.refresh(crm))
        self.assertEqual(crm.calls, [True], "снимок живёт TTL, а не читается "
                                            "на каждый апдейт; только открытые")
        asyncio.run(points.refresh(crm, force=True))
        self.assertEqual(len(crm.calls), 2)

    def test_empty_directory_is_a_snapshot_too(self):
        crm = _Directory([])
        self.assertEqual(asyncio.run(points.refresh(crm)), [])
        asyncio.run(points.refresh(crm))
        self.assertEqual(len(crm.calls), 1)
        self.assertTrue(points.is_fresh())

    def test_closed_points_and_internal_fields_stay_out(self):
        points.set_snapshot([
            {**THREE_POINTS[0], "active": True, "note": "ключ у охраны",
             "lat": 55.7, "lon": 49.1, "id": 1},
            {**THREE_POINTS[1], "active": False},
        ])
        snap = points.snapshot()
        self.assertEqual(len(snap), 1, "на закрытую точку клиента не зовут")
        self.assertEqual(set(snap[0]), set(points.FIELDS))
        self.assertEqual(snap[0]["hours"], "пн-вс: 10:00-19:00")

    def test_empty_fields_become_empty_strings(self):
        points.set_snapshot([THREE_POINTS[1]])
        self.assertEqual(points.snapshot()[0]["hours"], "")
        self.assertEqual(points.snapshot()[0]["phone"], "")

    def test_snapshot_is_a_copy(self):
        points.set_snapshot(THREE_POINTS)
        points.snapshot()[0]["address"] = "испорчено"
        points.snapshot().clear()
        self.assertEqual(points.snapshot()[0]["address"], ADDRESSES[0])

    def test_broken_database_keeps_the_old_snapshot(self):
        points.set_snapshot(THREE_POINTS)

        class Broken:
            async def locations(self, *, active_only=False):
                raise RuntimeError("база недоступна")

        with self.assertLogs("app.crm.points", "ERROR"):
            rows = asyncio.run(points.refresh(Broken(), force=True))
        self.assertEqual(len(rows), 3, "вчерашний список лучше зашитого")

    def test_bot_without_crm_forgets_the_snapshot(self):
        """Без CRM справочника нет: ответ - зашитый, а не остаток снимка."""
        points.set_snapshot(THREE_POINTS)
        self.assertEqual(asyncio.run(points.refresh(None)), [])
        self.assertEqual(points.snapshot(), [])
        self.assertFalse(points.is_fresh())

    def test_snapshot_feeds_the_answer(self):
        asyncio.run(points.refresh(_Directory(THREE_POINTS)))
        text = faq.answer(faq.BY_CODE["ADDR"], now=DAY, points=points.snapshot())
        for address in ADDRESSES:
            self.assertIn(address, text)


class TestRenterDetection(unittest.TestCase):
    def test_signed_contract_makes_a_renter(self):
        self.assertTrue(faq.is_renter({"contract_signed_at": "2026-08-01"}))

    def test_returned_bike_is_no_longer_a_rental(self):
        self.assertFalse(faq.is_renter({"contract_signed_at": "2026-08-01",
                                        "act_out_signed_at": "2026-08-08"}))

    def test_new_user_is_a_lead(self):
        self.assertFalse(faq.is_renter({}))

    def test_plan_comes_from_issue_data(self):
        self.assertEqual(faq.plan_of({"issue_data": {"rent_price": "3000 qr"}}),
                         "3000 qr")
        self.assertEqual(faq.plan_of({}), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
