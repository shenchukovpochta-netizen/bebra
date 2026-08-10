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
