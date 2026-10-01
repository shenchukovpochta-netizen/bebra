"""Быстрые формы сотрудника в боте: сторонний ремонт (мастер) и выдача
(администратор).

Форма текстом -> предпросмотр -> кнопка -> запись в CRM теми же функциями,
что и панель. Проверяются разбор формы ровно в том виде, в каком её пишут
на точке (сумма строкой ниже ключа, «вовуча» вместо имени), запись с
правами роли и путь через бота целиком, с двойным нажатием.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_cabinet as tc
    import test_flow as tf
    from aiogram.methods import EditMessageText, SendMessage
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    from app.crm import quickforms
    from tests.fake_crm import FakeCrm
    HAVE_AIOGRAM = tc.HAVE_AIOGRAM
except ImportError:                                    # pragma: no cover
    HAVE_AIOGRAM = False

D = Decimal
TODAY = date(2026, 10, 1)
MASTER_TG, ADMIN_TG, STRANGER_TG = 9100, 9200, 9300

# Форма ровно в том виде, в каком её прислали с точки.
REPAIR = """Дата обращения: 23.09
Имя клиента: Артем
Номер телефона клиента: 89996557593 @Fhnvjk21
Проблема/заказ: мотор троит
Дата окончания (фактического, либо оговорено с клиентом): -
Кто выполняет (выполнил) работу: вовуча
Итоговая сумма (за работу):
0
Итоговая сумма (за запчасти):
0
Формат оплаты (нал/оплата по карте): 0"""


def repair(**over: str) -> str:
    """REPAIR с заменой строк по ключу: done="25.09" -> «Дата окончания…: 25.09»."""
    keys = {"opened": "Дата обращения", "name": "Имя клиента",
            "phone": "Номер телефона клиента", "problem": "Проблема/заказ",
            "done": "Дата окончания (фактического, либо оговорено с клиентом)",
            "tech": "Кто выполняет (выполнил) работу",
            "work": "Итоговая сумма (за работу)", "parts": "Итоговая сумма (за запчасти)",
            "pay": "Формат оплаты (нал/оплата по карте)"}
    lines = REPAIR.splitlines()
    out: list[str] = []
    skip = False
    for line in lines:
        if skip:
            skip = False
            continue
        key = line.partition(":")[0]
        name = next((k for k, v in keys.items() if v == key), None)
        if name in over:
            out.append(f"{key}: {over[name]}")
            # Сумма в образце - строкой ниже ключа: её заменяем целиком.
            skip = line.rstrip().endswith(":")
        else:
            out.append(line)
    return "\n".join(out)


def issue(**over: str) -> str:
    fields = {"Телефон клиента": "+7 900 111-22-33", "Велосипед №": "15",
              "Срок, дней": "7", "Пробег, км": "4300", "Аккумулятор №": "-",
              "Сумма оплаты": "3000", "Формат оплаты (нал/карта/перевод)": "нал",
              "Договор №": "-", "Дата начала": "01.10"}
    names = {"phone": "Телефон клиента", "bike": "Велосипед №", "term": "Срок, дней",
             "mileage": "Пробег, км", "battery": "Аккумулятор №", "pay": "Сумма оплаты",
             "method": "Формат оплаты (нал/карта/перевод)", "contract": "Договор №",
             "start": "Дата начала"}
    for name, value in over.items():
        fields[names[name]] = value
    return "Выдача\n" + "\n".join(f"{k}: {v}" for k, v in fields.items())


class TestParsing(unittest.TestCase):
    def test_form_as_sent_from_the_point(self):
        self.assertEqual(logic.quick_form_kind(REPAIR), "repair")
        data, errors = logic.parse_quick_repair(REPAIR, today=TODAY)
        self.assertEqual(errors, [])
        self.assertEqual(data["opened_on"], date(2026, 9, 23))
        self.assertEqual((data["name"], data["phone"], data["username"]),
                         ("Артем", "+79996557593", "Fhnvjk21"))
        self.assertEqual(data["problem"], "мотор троит")
        self.assertIsNone(data["finish_on"], "«-» - ещё в работе")
        self.assertEqual(data["tech"], "вовуча")
        self.assertEqual((data["work"], data["parts"], data["method"]),
                         (D(0), D(0), None), "сумма строкой ниже ключа и оплата «0»")

    def test_sums_and_payment_words(self):
        data, _ = logic.parse_quick_repair(
            repair(work="1 500 р", parts="2500", pay="оплата по карте"), today=TODAY)
        self.assertEqual((data["work"], data["parts"], data["method"]),
                         (D(1500), D(2500), "card"))
        for word, code in (("нал", "cash"), ("наличные", "cash"), ("безнал", "transfer"),
                           ("перевод", "transfer"), ("СБП", "sbp")):
            self.assertEqual(logic.form_method(word)[0], code, word)

    def test_leftover_zero_below_is_not_glued_to_the_sum(self):
        """«2500» в строке ключа, а под ней остался «0» из старой формы:
        склейка дала бы 25 000 ₽."""
        text = REPAIR.replace("Итоговая сумма (за запчасти):",
                              "Итоговая сумма (за запчасти): 2500")
        data, errors = logic.parse_quick_repair(text, today=TODAY)
        self.assertEqual(errors, [])
        self.assertEqual(data["parts"], D(2500))

    def test_complaint_may_take_several_lines(self):
        text = REPAIR.replace("Проблема/заказ: мотор троит",
                              "Проблема/заказ: мотор троит\nи не заряжается")
        data, _ = logic.parse_quick_repair(text, today=TODAY)
        self.assertEqual(data["problem"], "мотор троит и не заряжается")

    def test_what_stops_the_form(self):
        _, errors = logic.parse_quick_repair(repair(phone="звонить Пете"), today=TODAY)
        self.assertTrue(any("телефон" in e.lower() for e in errors))
        _, errors = logic.parse_quick_repair(repair(work="много"), today=TODAY)
        self.assertTrue(any("сумму" in e for e in errors))
        _, errors = logic.parse_quick_repair(repair(pay="нал"), today=TODAY)
        self.assertTrue(any("сумма ноль" in e for e in errors),
                        "оплата без суммы - не провести")
        _, errors = logic.parse_quick_repair(repair(opened="05.10.2026"), today=TODAY)
        self.assertTrue(any("будущем" in e for e in errors))

    def test_date_without_a_year_is_the_nearest(self):
        self.assertEqual(logic.form_date("28.12", today=date(2027, 1, 2))[0],
                         date(2026, 12, 28))
        self.assertEqual(logic.form_date("23.09.26", today=TODAY)[0], date(2026, 9, 23))
        self.assertEqual(logic.form_date("вчера", today=TODAY)[0], date(2026, 9, 30))
        self.assertEqual(logic.form_date("-", today=TODAY), (None, ""))
        self.assertTrue(logic.form_date("31.02", today=TODAY)[1])

    def test_issue_form(self):
        self.assertEqual(logic.quick_form_kind(issue()), "issue")
        data, errors = logic.parse_quick_issue(issue(mileage="4 300 км"), today=TODAY)
        self.assertEqual(errors, [])
        self.assertEqual((data["phone"], data["bike_code"], data["term"], data["mileage"],
                          data["pay"], data["method"], data["battery_code"]),
                         ("+79001112233", "15", 7, 4300, D(3000), "cash", None))
        self.assertEqual(logic.parse_quick_issue(issue(term="2 недели"), today=TODAY)[0]
                         ["term"], 14)
        _, errors = logic.parse_quick_issue(issue(method="0"), today=TODAY)
        self.assertTrue(any("формат оплаты" in e.lower() for e in errors))
        _, errors = logic.parse_quick_issue(issue(mileage=""), today=TODAY)
        self.assertTrue(any("Пробег" in e for e in errors))

    def test_ordinary_text_is_not_a_form(self):
        for text in ("Привет", "Выдача\nкогда?", "Проблема: не едет",
                     "1. ФИО: Иванов\n2. Вин номер рамы: X\n3. Срок: 7"):
            self.assertIsNone(logic.quick_form_kind(text), text)

    def test_master_from_a_nickname(self):
        people = [{"id": 1, "name": "Вова", "login": "vova", "active": True},
                  {"id": 2, "name": "Пётр Иванов", "login": "petr", "active": True},
                  {"id": 3, "name": "Вова Старый", "login": "old", "active": False}]
        self.assertEqual([p["id"] for p in logic.match_staff(people, "вовуча")], [1],
                         "отключённый не подходит")
        self.assertEqual([p["id"] for p in logic.match_staff(people, "Петр")], [2])
        self.assertEqual([p["id"] for p in logic.match_staff(people, "@petr")], [2])
        self.assertEqual(logic.match_staff(people, "Саша"), [])

    def test_order_number_in_the_card(self):
        self.assertEqual(logic.order_no_in("✅ Наряд РЕМ-000012 · сторонний"), "РЕМ-000012")
        self.assertIsNone(logic.order_no_in("Выдача оформлена"))


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class QuickCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.crm = FakeCrm()
        tech = await self.crm.access_profile_by_code("tech")
        manager = await self.crm.access_profile_by_code("manager")
        self.master_id = await self.crm.create_staff("vova", "hash", "Вова", "manager",
                                                     tech["id"])
        await self.crm.link_staff_tg(self.master_id, MASTER_TG, "vova")
        self.admin_id = await self.crm.create_staff("anna", "hash", "Анна", "manager",
                                                    manager["id"])
        await self.crm.link_staff_tg(self.admin_id, ADMIN_TG, "anna")
        self.master = await quickforms.staff_for(self.crm, MASTER_TG)
        self.admin = await quickforms.staff_for(self.crm, ADMIN_TG)

    async def orders(self):
        return await self.crm.work_orders()


class TestRepair(QuickCase):
    async def test_rights_are_the_panel_role(self):
        self.assertTrue(quickforms.may(self.master, "repair"))
        self.assertFalse(quickforms.may(self.master, "issue"), "мастер не выдаёт")
        self.assertTrue(quickforms.may(self.admin, "issue"))

    async def test_intake_opens_an_order_with_a_new_client(self):
        preview = await quickforms.preview_repair(self.crm, self.master, REPAIR, today=TODAY)
        self.assertTrue(preview.ok)
        self.assertIn("новая карточка", preview.text)
        self.assertIn("Мастер: Вова", preview.text, "«вовуча» - это Вова")
        card = await quickforms.apply_repair(self.crm, self.master, REPAIR, today=TODAY)
        self.assertIn("РЕМ-000001", card)
        order = (await self.orders())[0]
        self.assertEqual((order["payer"], order["bike_id"], order["complaint"]),
                         ("client", None, "мотор троит"))
        self.assertEqual(order["tech_id"], self.master_id)
        self.assertTrue(logic.order_is_open(order))
        self.assertIsNone(order.get("paid_at"))
        self.assertEqual(order["opened_at"].astimezone().date(), date(2026, 9, 23),
                         "наряд - с даты обращения, а не с сегодня")
        client = await self.crm.client_by_phone("+79996557593")
        self.assertEqual((client["full_name"], client["username"]), ("Артем", "Fhnvjk21"))

    async def test_known_phone_is_the_same_client(self):
        cid = await self.crm.create_client(full_name="Артём Петров", phone="+79990000001")
        await self.crm.update_client(cid, phone2="+79996557593")
        preview = await quickforms.preview_repair(self.crm, self.master, REPAIR, today=TODAY)
        self.assertIn("есть в CRM", preview.text)
        await quickforms.apply_repair(self.crm, self.master, REPAIR, today=TODAY)
        self.assertEqual((await self.orders())[0]["client_id"], cid)
        self.assertIsNone(await self.crm.client_by_phone("+79996557593"),
                          "запасной номер - не повод заводить вторую карточку")

    async def test_finished_paid_job_closes_and_lands_in_the_drawer(self):
        shift = await self.crm.create_shift(location=None, opening=D(0), note=None,
                                            by="staff:vova")
        text = repair(done="25.09", work="1500", parts="2500", pay="нал")
        await quickforms.apply_repair(self.crm, self.master, text, today=TODAY)
        order = (await self.orders())[0]
        self.assertFalse(logic.order_is_open(order))
        self.assertEqual(order["closed_at"].astimezone().date(), date(2026, 9, 25))
        self.assertEqual(order["total"], D(4000))
        self.assertIsNotNone(order["paid_at"])
        moves = await self.crm.cash_moves(shift)
        self.assertEqual([m["amount"] for m in moves], [D(4000)],
                         "наличные за ремонт - в ящик смены принявшего")
        self.assertEqual(await self.crm.client_balance(order["client_id"]), D(0),
                         "красная линия: ремонт в журнал аренды не идёт")

    async def test_promised_date_keeps_the_order_open(self):
        await quickforms.apply_repair(self.crm, self.master, repair(done="05.10"),
                                      today=TODAY)
        order = (await self.orders())[0]
        self.assertTrue(logic.order_is_open(order))
        self.assertIn("05.10.2026", order["note"])

    async def test_reply_to_the_card_completes_the_same_order(self):
        await quickforms.apply_repair(self.crm, self.master, REPAIR, today=TODAY)
        order = (await self.orders())[0]
        done = repair(done="01.10", work="2000", pay="карта")
        preview = await quickforms.preview_repair(self.crm, self.master, done,
                                                  today=TODAY, order=order)
        self.assertEqual(preview.target, order["id"])
        self.assertIn("дополнить", preview.text)
        await quickforms.apply_repair(self.crm, self.master, done, today=TODAY,
                                      order_id=order["id"])
        self.assertEqual(len(await self.orders()), 1, "дополнение, а не второй наряд")
        order = await self.crm.work_order(order["id"])
        self.assertFalse(logic.order_is_open(order))
        self.assertIsNotNone(order["paid_at"])
        again = await quickforms.apply_repair(self.crm, self.master, done, today=TODAY,
                                              order_id=order["id"])
        self.assertIn("уже", again, "вторая оплата не отмечается")
        items = await self.crm.order_items(order["id"])
        self.assertEqual(logic.order_totals_client(items), D(2000),
                         "строки не задвоились")

    async def test_unknown_master_is_said_not_guessed(self):
        preview = await quickforms.preview_repair(
            self.crm, self.master, repair(tech="Саша"), today=TODAY)
        self.assertIn("не нашёл", preview.text)
        await quickforms.apply_repair(self.crm, self.master, repair(tech="Саша"),
                                      today=TODAY)
        self.assertIsNone((await self.orders())[0]["tech_id"])

    async def test_own_bike_order_is_not_touched_by_the_form(self):
        bike = await self.crm.create_bike(code="B-1", model="Kugoo V3")
        order_id = await self.crm.create_work_order(
            bike_id=bike, payer="own", client_id=None, complaint="тормоза",
            object_note=None, tech_id=None, estimate=D(0), created_by="t")
        with self.assertRaises(quickforms.FormError):
            await quickforms.apply_repair(self.crm, self.master, REPAIR, today=TODAY,
                                          order_id=order_id)


class TestIssue(QuickCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client_id = await self.crm.create_client(full_name="Иванов Иван",
                                                      phone="+79001112233", tg_id=5001)
        self.bike_id = await self.crm.create_bike(code="15", model="Kugoo V3",
                                                  mileage_km=4266)
        self.tariff_id = await self.crm.create_tariff("Неделя", 7, D(3000), None)
        self.shift = await self.crm.create_shift(location=None, opening=D(0), note=None,
                                                 by="staff:anna")

    async def test_issue_with_cash(self):
        preview = await quickforms.preview_issue(self.crm, self.admin, issue(), today=TODAY)
        self.assertTrue(preview.ok, preview.text)
        self.assertIn("Иванов Иван", preview.text)
        self.assertIn("в кассу смены", preview.text)
        result = await quickforms.apply_issue(self.crm, self.admin, issue(), today=TODAY,
                                              panel_url="https://crm.example.ru/")
        self.assertIn("Выдача оформлена", result)
        rental = await self.crm.active_rental_of(self.client_id)
        self.assertEqual((rental["bike_id"], rental["period_days"]), (self.bike_id, 7))
        self.assertIn(f"issue/docs?rental={rental['id']}", result)
        bike = await self.crm.bike(self.bike_id)
        self.assertEqual((bike["status"], bike["mileage_km"]), ("rented", 4300))
        pay = [x for x in await self.crm.ledger_of(self.client_id) if x["kind"] == "payment"]
        self.assertEqual([(x["amount"], x["method"], x["shift_id"]) for x in pay],
                         [(D(3000), "cash", self.shift)])
        self.assertEqual(await self.crm.client_balance(self.client_id), D(0))

    async def test_battery_goes_with_the_bike(self):
        battery = await self.crm.create_battery(code="A-1")
        await quickforms.apply_issue(self.crm, self.admin, issue(battery="A-1"), today=TODAY)
        self.assertEqual((await self.crm.battery(battery))["status"], "rented")

    async def test_what_stops_an_issue(self):
        cases = [
            (issue(phone="+7 900 000-00-00"), "в CRM нет"),
            (issue(bike="99"), "в парке нет"),
            (issue(term="5"), "нет тарифа на 5 дн. Есть: 7"),
            (issue(mileage="100"), "Пробег"),
            (issue(battery="Z-9"), "Аккумулятора"),
        ]
        for text, said in cases:
            preview = await quickforms.preview_issue(self.crm, self.admin, text, today=TODAY)
            self.assertFalse(preview.ok, said)
            self.assertIn(said, preview.text)
        await self.crm.update_client(self.client_id, status="blacklist")
        preview = await quickforms.preview_issue(self.crm, self.admin, issue(), today=TODAY)
        self.assertIn("чёрном списке", preview.text)

    async def test_busy_bike_and_running_rental(self):
        await quickforms.apply_issue(self.crm, self.admin, issue(), today=TODAY)
        with self.assertRaises(quickforms.FormError) as err:
            await quickforms.apply_issue(self.crm, self.admin, issue(), today=TODAY)
        self.assertIn("уже идёт аренда", str(err.exception))
        other = await self.crm.create_client(full_name="Петров", phone="+79002223344")
        del other
        preview = await quickforms.preview_issue(
            self.crm, self.admin, issue(phone="+79002223344"), today=TODAY)
        self.assertIn("только свободный", preview.text)

    async def test_without_payment_period_is_a_debt(self):
        await quickforms.apply_issue(self.crm, self.admin, issue(pay="0", method="0"),
                                     today=TODAY)
        self.assertEqual(await self.crm.client_balance(self.client_id), D(-3000))


def _form_reply(text: str, *, user_id: int, preview_text: str, data: str) -> Update:
    """Нажатие кнопки под предпросмотром: предпросмотр бота - ответ на форму."""
    chat = Chat(id=user_id, type="private")
    form = Message(message_id=tf._next_id(), date=datetime.now(UTC), chat=chat,
                   from_user=User(id=user_id, is_bot=False, first_name="U"), text=text)
    shown = Message(message_id=tf._next_id(), date=datetime.now(UTC), chat=chat,
                    from_user=User(id=123, is_bot=True, first_name="bot"),
                    text=preview_text, reply_to_message=form)
    return Update(update_id=tf._next_id(), callback_query=CallbackQuery(
        id=str(tf._next_id()), from_user=User(id=user_id, is_bot=False, first_name="U"),
        chat_instance="ci", data=data, message=shown))


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestInTheBot(tc.CabinetCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        tech = await self.crm.access_profile_by_code("tech")
        manager = await self.crm.access_profile_by_code("manager")
        self.master_id = await self.crm.create_staff("vova", "hash", "Вова", "manager",
                                                     tech["id"])
        await self.crm.link_staff_tg(self.master_id, MASTER_TG, "vova")
        admin_id = await self.crm.create_staff("anna", "hash", "Анна", "manager",
                                               manager["id"])
        await self.crm.link_staff_tg(admin_id, ADMIN_TG, "anna")

    def replies(self, chat_id):
        return [m for m in self.session.sent_to(chat_id) if isinstance(m, SendMessage)]

    def edits(self):
        return [m for m in self.session.calls if isinstance(m, EditMessageText)]

    async def test_template_for_the_master(self):
        await self.feed(tc.msg("/remont", user_id=MASTER_TG, chat_id=MASTER_TG))
        text = self.last_text(MASTER_TG)
        self.assertIn("Дата обращения:", text)
        self.assertIn("Кто выполняет (выполнил) работу: Вова", text)
        self.assertIn("<pre>", self.replies(MASTER_TG)[-1].text)

    async def test_strangers_and_roles(self):
        await self.feed(tc.msg("/remont", user_id=STRANGER_TG, chat_id=STRANGER_TG))
        self.assertIn("для сотрудников", self.last_text(STRANGER_TG))
        await self.feed(tc.msg("/vydacha", user_id=MASTER_TG, chat_id=MASTER_TG))
        self.assertIn("не разрешено", self.last_text(MASTER_TG))
        await self.feed(tc.msg(REPAIR, user_id=STRANGER_TG, chat_id=STRANGER_TG))
        self.assertIn("для сотрудников", self.last_text(STRANGER_TG))
        self.assertEqual(await self.crm.work_orders(), [])

    async def test_form_preview_then_confirm_once(self):
        self.session.subscribed = False
        await self.feed(tc.msg(REPAIR, user_id=MASTER_TG, chat_id=MASTER_TG))
        preview = self.replies(MASTER_TG)[-1]
        self.assertIn("новый наряд", preview.text)
        buttons = [b.callback_data for row in preview.reply_markup.inline_keyboard
                   for b in row]
        self.assertEqual(buttons, ["qf:r:0", "qf:x"])
        self.assertEqual(await self.crm.work_orders(), [], "до кнопки - ничего")
        press = _form_reply(REPAIR, user_id=MASTER_TG, preview_text=preview.text,
                            data="qf:r:0")
        await self.feed(press)
        again = Update(update_id=tf._next_id(), callback_query=press.callback_query.model_copy(
            update={"id": "second"}))
        await self.feed(again)
        self.assertEqual(len(await self.crm.work_orders()), 1, "двойное нажатие - один наряд")
        self.assertIn("РЕМ-000001", self.edits()[-1].text)

    async def test_reply_to_the_card_completes_it(self):
        await self.feed(_form_reply(REPAIR, user_id=MASTER_TG, preview_text="…",
                                    data="qf:r:0"))
        card = self.edits()[-1].text
        done = repair(done="01.10", work="1500", pay="нал")
        await self.feed(tc.msg(done, user_id=MASTER_TG, chat_id=MASTER_TG,
                               reply_to=1, reply_text=tf.plain(card)))
        preview = self.replies(MASTER_TG)[-1]
        self.assertIn("РЕМ-000001 — дополнить", preview.text)
        order = (await self.crm.work_orders())[0]
        data = preview.reply_markup.inline_keyboard[0][0].callback_data
        self.assertEqual(data, f"qf:r:{order['id']}")
        await self.feed(_form_reply(done, user_id=MASTER_TG, preview_text=preview.text,
                                    data=data))
        order = await self.crm.work_order(order["id"])
        self.assertFalse(logic.order_is_open(order))
        self.assertEqual(len(await self.crm.work_orders()), 1)

    async def test_cancel_writes_nothing(self):
        await self.feed(_form_reply(REPAIR, user_id=MASTER_TG, preview_text="…",
                                    data="qf:x"))
        self.assertEqual(await self.crm.work_orders(), [])
        self.assertIn("Отменено", self.edits()[-1].text)

    async def test_someone_elses_form_is_not_confirmed(self):
        press = _form_reply(REPAIR, user_id=MASTER_TG, preview_text="…", data="qf:r:0")
        stolen = press.callback_query.model_copy(update={
            "from_user": User(id=ADMIN_TG, is_bot=False, first_name="A")})
        await self.feed(Update(update_id=tf._next_id(), callback_query=stolen))
        self.assertEqual(await self.crm.work_orders(), [])

    async def test_issue_end_to_end_tells_the_client(self):
        client_id = await self.crm.create_client(full_name="Иванов Иван",
                                                 phone="+79001112233", tg_id=5001)
        await self.crm.create_bike(code="15", model="Kugoo V3", mileage_km=4266)
        await self.crm.create_tariff("Неделя", 7, D(3000), None)
        form = issue(start=f"{date.today():%d.%m}")
        await self.feed(tc.msg(form, user_id=ADMIN_TG, chat_id=ADMIN_TG))
        preview = self.replies(ADMIN_TG)[-1]
        self.assertIn("Выдача — проверьте", preview.text)
        await self.feed(_form_reply(form, user_id=ADMIN_TG, preview_text=preview.text,
                                    data="qf:i"))
        self.assertIsNotNone(await self.crm.active_rental_of(client_id))
        self.assertIn("Выдача оформлена", self.edits()[-1].text)
        self.assertTrue(any("Аренда оформлена" in t for t in self.texts_to(5001)),
                        "клиенту - как из панели")

    async def test_form_does_not_end_up_in_the_anketa(self):
        """Сотрудник на шаге анкеты: форма - форма, а не его ФИО."""
        await self.feed(tc.msg("/start", user_id=MASTER_TG, chat_id=MASTER_TG))
        before = dict(self.db.users[MASTER_TG])
        await self.feed(tc.msg(REPAIR, user_id=MASTER_TG, chat_id=MASTER_TG))
        self.assertEqual(self.db.users[MASTER_TG].get("full_name"), before.get("full_name"))
        self.assertIn("новый наряд", self.replies(MASTER_TG)[-1].text)


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
