"""Ветка частых вопросов: распознавание тем, приоритеты и содержание ответов.

Отдельный набор без aiogram и без базы: роутер интентов - чистая логика,
и ломаться он должен здесь, а не в чате с клиентом.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import faq  # noqa: E402
from app import faq_i18n as i18n  # noqa: E402
from app import texts  # noqa: E402

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
        need = set(i18n.REQUIRED_KEYS) | answer_keys | title_keys
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
