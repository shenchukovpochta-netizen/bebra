"""Рабочая группа точек: разбор форм, сверка с базой, путь через бота.

Раньше группу читал сценарий n8n и писал в Google-таблицу. Формы здесь
берутся из самого бота (`fixation_form`, `closure_report`): у формата
один владелец, и разбор обязан читать ровно то, что бот пишет.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import logic as bot_logic  # noqa: E402
from app.crm import logic, opsgroup, service  # noqa: E402
from tests.fake_crm import FakeCrm  # noqa: E402

D = Decimal
TODAY = date.today()
NOW = datetime.now(UTC)
USER = {"full_name": "Иванов Иван Иванович", "phone": "+79001234567", "username": "ivan"}
ANKETA = {"phone2": "89111234567", "reg_address": "Казань, Баумана 1, кв. 2",
          "live_address": "Казань, Кремлёвская 5, кв. 9"}
ISSUE = {"vin_frame": "LXR123456", "vin_motor": "60V240W2305001",
         "rent_term": "03.08 - 10.08", "rent_price": "3000 qr", "kit_akb": "2"}


def run(coro):
    return asyncio.run(coro)


def fix_text(user=USER, anketa=ANKETA, issue=ISSUE) -> str:
    return bot_logic.fixation_form(user, anketa, issue)


def return_text(close=None, user=USER, issue=ISSUE) -> str:
    close = close or {"closed_at": "12.08", "debt_paid": "1 500", "damage": "царапина",
                      "return_address": "Павлюхина", "accepted_by": "Петя",
                      "reason": "уезжает"}
    return bot_logic.closure_report(user, issue, close)


SWAP = """ЗАМЕНА
1. ФИО: Иванов Иван
откуда: Павлюхина
было:
2. Вин номер рамы: LXR123456
3. Вин номер мотор колеса: 60v240w 2305001
пробег: 1 200
не заряжается аккумулятор
стало:
2. Вин номер рамы: LXR999999
3. Вин номер мотор колеса: 60V240W2305999
пробег: 300"""

DAILY = """Павлюхина 26.09
1. Количество вело в ремонте на начало дня: 10
2. Отремонтировано со склада: 3
3. Отремонтировано у арендаторов: 2
4. Количество в ремонте на конец дня: 8
5. Список использованных деталей за день:
колодки - 1 упаковка
Камера - 1шт
6. Количество помытых вело: 4"""


class TestParsing(unittest.TestCase):
    def test_vin_key_ignores_case_spaces_and_cyrillic_twins(self):
        self.assertEqual(logic.vin_key("60v240w 230-5001"), "60V240W2305001")
        # «В» и «С» кириллицей - обычная опечатка с телефона; «60В» - это
        # «60 вольт», латинская V.
        self.assertEqual(logic.vin_key("60В240W23С"), "60V240W23C")
        self.assertEqual(logic.vin_key("lxb12"), logic.vin_key("LXV12"), "B и V - одно")
        self.assertEqual(logic.vin_key(None), "")

    def test_fixation_form_round_trip(self):
        text = fix_text()
        self.assertTrue(logic.is_ops_fix(text))
        data, err = logic.parse_ops_fix(text)
        self.assertEqual(err, "")
        self.assertEqual(data["fio"], "Иванов Иван Иванович")
        self.assertEqual(data["vin_motor"], "60V240W2305001")
        self.assertEqual(data["vin_frame"], "LXR123456")
        self.assertEqual(data["rent_term"], "03.08 - 10.08")
        self.assertEqual(data["payment"], "3000 qr")
        # Второй номер «7. Номер телефона 2» n8n не читал никогда.
        self.assertEqual(data["phones"], ["+79001234567", "+79111234567"])
        self.assertEqual(data["kit"]["Теплые перчатки на руль (муфты)"], "0")
        self.assertEqual(data["kit"]["Курьерская сумка"], "0")
        self.assertEqual(data["gps"], "", "прочерк - это пусто")

    def test_fixation_keeps_no_addresses(self):
        """Адреса лежат в анкете зашифрованными - в журнал группы они не идут."""
        data, _ = logic.parse_ops_fix(fix_text())
        dump = repr(data)
        self.assertNotIn("Баумана", dump)
        self.assertNotIn("Кремлёвская", dump)

    def test_fixation_without_numbers_is_refused(self):
        data, err = logic.parse_ops_fix("1. ФИО: Иванов\n5. Сроки аренды: 01.09 - 08.09")
        self.assertIsNone(data)
        self.assertIn("рамы или мотора", err)

    def test_frame_key_does_not_match_referral(self):
        """n8n искал по подстроке всей строки: «рама» нашлась бы в «программа»."""
        data, _ = logic.parse_ops_fix("1. ФИО: А Б\n17. Реф.программа: XYZ12345\n"
                                      "3. Вин номер мотор колеса: 60V1")
        self.assertEqual(data["vin_frame"], "")

    def test_swap_before_and_after(self):
        self.assertTrue(logic.is_ops_swap(SWAP))
        data, err = logic.parse_ops_swap(SWAP)
        self.assertEqual(err, "")
        self.assertEqual((data["old_frame"], data["new_frame"]), ("LXR123456", "LXR999999"))
        self.assertEqual(logic.vin_key(data["old_motor"]), "60V240W2305001")
        self.assertEqual(data["reason_text"], "не заряжается аккумулятор")
        self.assertEqual(data["reason"], "repair")
        self.assertEqual((data["mileage_old"], data["mileage_new"]), (1200, 300))
        self.assertEqual(data["from_location"], "Павлюхина")

    def test_swap_reason_line_wins_and_missing_after_is_refused(self):
        data, _ = logic.parse_ops_swap(SWAP.replace("не заряжается аккумулятор",
                                                    "причина: плановое ТО"))
        self.assertEqual(data["reason"], "maintenance")
        self.assertIn("стало", logic.parse_ops_swap("ЗАМЕНА\nрама: 1234")[1])
        self.assertIn("нового", logic.parse_ops_swap("ЗАМЕНА\nмотор: 60V1234\nстало:")[1])

    def test_swap_reason_words(self):
        self.assertEqual(logic.swap_reason_code("клиент сломал колесо"), "repair",
                         "поломка важнее того, кто сломал")
        self.assertEqual(logic.swap_reason_code("что-то стучит"), "repair")
        self.assertEqual(logic.swap_reason_code("Плановое ТО"), "maintenance")
        self.assertEqual(logic.swap_reason_code("по просьбе клиента"), "client")
        self.assertEqual(logic.swap_reason_code("так вышло"), "other")

    def test_return_report_round_trip_both_spellings(self):
        data, err = logic.parse_ops_return(return_text())
        self.assertEqual(err, "")
        self.assertEqual(data["accepted_by"], "Петя")
        self.assertEqual(data["return_address"], "Павлюхина")
        self.assertEqual(data["debt_paid_sum"], "1500.00")
        self.assertEqual(data["repair_paid_sum"], "0.00", "прочерк - ноль")
        self.assertEqual(logic.vin_key(data["vin_motor"]), "60V240W2305001")
        # В группе живёт и «Кто принял вело» - n8n терял одно из написаний.
        data, _ = logic.parse_ops_return(return_text().replace("принял велик", "принял вело"))
        self.assertEqual(data["accepted_by"], "Петя")

    def test_return_money_takes_the_first_number(self):
        """n8n склеивал все цифры: «1500 нал + 300 перевод» -> 1500300."""
        self.assertEqual(logic.ops_money("1500 нал + 300 перевод"), D("1500.00"))
        self.assertEqual(logic.ops_money("1 500,50 ₽"), D("1500.50"))
        self.assertEqual(logic.ops_money("нет"), D("0.00"))
        self.assertIsNone(logic.ops_money("потом отдаст"))

    def test_return_multiline_damage(self):
        text = return_text().replace("Какие повреждения есть: царапина",
                                     "Какие повреждения есть: царапина\nнет зеркала")
        data, _ = logic.parse_ops_return(text)
        self.assertEqual(data["damage"], "царапина нет зеркала")

    def test_daily_report(self):
        data, err = logic.parse_ops_daily(DAILY)
        self.assertEqual(err, "")
        self.assertEqual(data["location"], "Павлюхина 26.09")
        self.assertEqual((data["repair_start"], data["repaired_stock"],
                          data["repaired_clients"], data["repair_end"], data["washed"]),
                         (10, 3, 2, 8, 4))
        # Дата в шапке «26.09» не режет список деталей, как «6.» у n8n.
        self.assertEqual(data["parts"], ["колодки - 1 упаковка", "Камера - 1шт"])
        self.assertIsNone(logic.parse_ops_daily("ок, принял")[0])

    def test_daily_word_forms(self):
        data, _ = logic.parse_ops_daily("Адоратского\nВ ремонте на конец дня 5\nПомытых: 2")
        self.assertEqual((data["repair_end"], data["washed"]), (5, 2))

    def test_same_person(self):
        self.assertTrue(logic.same_person("Иван Иванов", "Иванов Иван Иванович"))
        self.assertTrue(logic.same_person("иванов  иван", "Иванов Иван"))
        self.assertTrue(logic.same_person("Семёнов Пётр", "Семенов Петр Ильич"))
        self.assertFalse(logic.same_person("Иванов Пётр", "Иванов Иван Иванович"))
        self.assertFalse(logic.same_person("", "Иванов Иван"))

    def test_blacklist_by_phone_and_name(self):
        clients = [
            {"id": 1, "full_name": "Петров Пётр", "phone": "+79000000001",
             "phone2": "8 911 123-45-67", "status": "blacklist", "note": "кража"},
            {"id": 2, "full_name": "Иванов Иван Иванович", "phone": "+79000000002",
             "status": "active"},
            {"id": 3, "full_name": "Сидоров Сидор", "phone": "+79000000003",
             "status": "blocked"},
        ]
        client, by = logic.blacklist_hit(["+79111234567"], "Кто-то", clients)
        self.assertEqual((client["id"], by), (1, "телефон"), "по второму номеру карточки")
        client, by = logic.blacklist_hit([], "сидоров  сидор", clients)
        self.assertEqual((client["id"], by), (3, "ФИО"))
        self.assertIsNone(logic.blacklist_hit(["+79000000002"], "Иванов Иван Иванович",
                                              clients), "активный клиент - не стоп-лист")

    def test_blacklist_text_escapes(self):
        text = logic.ops_blacklist_text({"full_name": "<b>X</b>", "status": "blacklist",
                                         "note": "a & b"}, "ФИО")
        self.assertIn("&lt;b&gt;", text)
        self.assertIn("a &amp; b", text)
        self.assertIn("Чёрный список", text)

    def test_query_ignores_chatter(self):
        self.assertEqual(logic.ops_query(" 60V240W2305001 "), "60V240W2305001")
        self.assertIsNone(logic.ops_query("спасибо, понял, сейчас позвоню ему"))
        self.assertIsNone(logic.ops_query("60V1\n60V2"))

    def test_debt_text_for_manual_rental(self):
        """Ручное начисление: долг по журналу 0, а к оплате уже есть."""
        rental = {"status": "active", "billed_until": TODAY - timedelta(days=2),
                  "price": D(3000), "period_days": 7, "search_at": None}
        text = logic.ops_debt_text({"code": "B-1", "status": "rented"}, rental,
                                   {"full_name": "<Иван>", "phone": "+7900"}, D(0),
                                   today=TODAY)
        self.assertIn("Долга нет", text)
        self.assertIn("К оплате сейчас: 3 000 ₽", text)
        self.assertIn("просрочка 2 дн.", text)
        self.assertIn("&lt;Иван&gt;", text)
        text = logic.ops_debt_text({"code": "B-1", "status": "available"}, None, None,
                                   0, today=TODAY)
        self.assertIn("Аренды нет", text)

    def test_gps_text(self):
        bike = {"code": "B-1", "motor_no": "60V1"}
        self.assertIn("не привязан", logic.ops_gps_text(bike, None, now=NOW))
        text = logic.ops_gps_text(bike, {"alias": "T1", "phone": "+79005554433",
                                         "last_seen": NOW - timedelta(minutes=5),
                                         "lat": 55.79, "lon": 49.12, "voltage": D("12.4")},
                                  now=NOW)
        self.assertIn("+79005554433", text)
        self.assertIn("5 мин назад", text)
        self.assertIn("yandex.ru/maps", text)
        self.assertNotIn("Сфотографируйте", text)


class OpsCase(unittest.TestCase):
    def setUp(self):
        self.crm = FakeCrm()
        self.client_id = run(self.crm.create_client(full_name="Иванов Иван Иванович",
                                                    phone="+79001234567"))
        self.bike_id = run(self.crm.create_bike(code="B-1", model="Truck+",
                                                frame_no="LXR123456",
                                                motor_no="60V240W2305001"))
        self.new_id = run(self.crm.create_bike(code="B-2", model="Truck+",
                                               frame_no="LXR999999",
                                               motor_no="60V240W2305999"))
        self.tariff_id = run(self.crm.create_tariff("Неделя", 7, D(3000), None))
        self.rental_id = run(service.open_rental(
            self.crm, client=run(self.crm.client(self.client_id)),
            bike=run(self.crm.bike(self.bike_id)),
            tariff=run(self.crm.tariff(self.tariff_id)), started_on=TODAY,
            contract_no="АВ-1", by="test"))
        self.seq = 100

    def meta(self):
        self.seq += 1
        return {"chat_id": -100777, "message_id": self.seq, "thread_id": 7,
                "author_tg": 55, "author": "@point"}

    def handle(self, topic, text, **kw):
        return run(opsgroup.handle(self.crm, topic, text, self.meta(), today=TODAY,
                                   now=NOW, **kw))

    def reports(self):
        return run(self.crm.ops_reports())


class TestFixation(OpsCase):
    def test_matching_form_gets_thumbs_up(self):
        out = self.handle("fix", fix_text())
        self.assertEqual((out.reaction, out.reply), ("👍", None))
        [row] = self.reports()
        self.assertTrue(row["ok"])
        self.assertEqual((row["rental_id"], row["client_id"], row["bike_id"]),
                         (self.rental_id, self.client_id, self.bike_id))

    def test_blacklisted_client_is_stopped(self):
        run(self.crm.create_client(full_name="Кто-то другой", phone="+79111234567"))
        other = next(c for c in self.crm.clients_.values() if c["phone"] == "+79111234567")
        other["status"] = "blacklist"
        out = self.handle("fix", fix_text())
        self.assertEqual(out.reaction, "👎")
        self.assertIn("стоп-листе", out.reply)
        self.assertFalse(self.reports()[0]["ok"])

    def test_unknown_bike(self):
        out = self.handle("fix", fix_text(issue={**ISSUE, "vin_motor": "60V0000000",
                                                 "vin_frame": "NOPE0000"}))
        self.assertEqual(out.reaction, "👎")
        self.assertIn("не найден в парке", out.reply)

    def test_frame_disagrees_with_card(self):
        out = self.handle("fix", fix_text(issue={**ISSUE, "vin_frame": "LXR999999"}))
        self.assertEqual(out.reaction, "👎")
        self.assertIn("рама", out.reply)

    def test_motor_disagrees_with_card(self):
        """Нашёлся по раме, а мотор другой - колесо меняли, номер в карточке старый."""
        out = self.handle("fix", fix_text(issue={**ISSUE, "vin_motor": "60V240W7777777"}))
        self.assertEqual(out.reaction, "👎")
        self.assertIn("обновите номер", out.reply)

    def test_no_rental_in_crm(self):
        out = self.handle("fix", fix_text(issue={**ISSUE, "vin_motor": "60V240W2305999",
                                                 "vin_frame": ""}))
        self.assertEqual(out.reaction, "👎")
        self.assertIn("нет идущей аренды", out.reply)

    def test_other_client_on_the_bike(self):
        out = self.handle("fix", fix_text(user={**USER, "full_name": "Петров Пётр",
                                                "phone": "+79005550000"},
                                          anketa={}))
        self.assertEqual(out.reaction, "👎")
        self.assertIn("другому клиенту", out.reply)

    def test_same_message_twice_is_one_report(self):
        meta = self.meta()
        for _ in range(2):
            run(opsgroup.handle(self.crm, "fix", fix_text(), meta, today=TODAY, now=NOW))
        self.assertEqual(len(self.reports()), 1)

    def test_chatter_in_topic_is_ignored(self):
        out = self.handle("fix", "ребята, кто на Павлюхина?")
        self.assertEqual((out.reaction, out.reply), (None, None))
        self.assertEqual(self.reports(), [])


class TestSwap(OpsCase):
    def test_needs_rights(self):
        out = self.handle("fix", SWAP, swap_allowed=False)
        self.assertEqual(out.reaction, "👎")
        self.assertEqual(run(self.crm.rental(self.rental_id))["bike_id"], self.bike_id)

    def test_swap_is_made_in_crm(self):
        before = run(self.crm.rental(self.rental_id))
        out = self.handle("fix", SWAP, swap_allowed=True, by="tg:55")
        self.assertEqual(out.reaction, "👍", out.reply)
        after = run(self.crm.rental(self.rental_id))
        self.assertEqual(after["bike_id"], self.new_id)
        self.assertEqual(after["billed_until"], before["billed_until"])
        self.assertEqual(run(self.crm.bike(self.bike_id))["status"], "repair",
                         "не заряжается - снятый уходит в ремонт")
        self.assertIn("B-1 → B-2", out.reply)
        # Повтор того же текста (перепост) - подтверждение, а не вторая замена.
        out = self.handle("fix", SWAP, swap_allowed=True, by="tg:55")
        self.assertEqual(out.reaction, "👍")
        self.assertIsNone(out.reply)

    def issued_at(self, point):
        """Аренда выдана с `point`, в справочнике две точки."""
        for name in ("Павлюхина", "Адоратского"):
            run(self.crm.create_location(name=name, city="Казань", address=None,
                                         note=None))
        self.crm.rentals_[self.rental_id]["location"] = point
        run(self.crm.update_bike(self.bike_id, location=point))

    def test_removed_bike_stays_where_the_form_says(self):
        """«откуда: Павлюхина» - это «где меняли»: снятый остаётся там, а
        не на точке аренды, как и при замене из панели."""
        self.issued_at("Адоратского")
        out = self.handle("fix", SWAP, swap_allowed=True, by="tg:55")
        self.assertEqual(out.reaction, "👍", out.reply)
        old = run(self.crm.bike(self.bike_id))
        self.assertEqual((old["status"], old["location"]), ("repair", "Павлюхина"))
        self.assertEqual(run(self.crm.bike(self.new_id))["location"], "Адоратского",
                         "новый - на точке аренды")
        self.assertIn("на точке Павлюхина", out.reply, "точку видно в чате")

    def test_unknown_swap_point_is_not_guessed(self):
        self.issued_at("Адоратского")
        out = self.handle("fix", SWAP.replace("откуда: Павлюхина", "откуда: у метро"),
                          swap_allowed=True, by="tg:55")
        self.assertEqual(out.reaction, "👍", out.reply)
        self.assertEqual(run(self.crm.bike(self.bike_id))["location"], "Адоратского",
                         "не сопоставилось - точка аренды")
        self.assertNotIn("на точке", out.reply)

    def test_directory_failure_does_not_stop_the_swap(self):
        self.issued_at("Адоратского")

        async def broken(**kwargs):
            raise RuntimeError("нет связи")

        self.crm.locations = broken
        with self.assertLogs("app.crm.opsgroup", level="ERROR"):
            out = self.handle("fix", SWAP, swap_allowed=True, by="tg:55")
        self.assertEqual(out.reaction, "👍", out.reply)
        self.assertEqual(run(self.crm.bike(self.bike_id))["location"], "Адоратского")

    def test_new_bike_must_be_free(self):
        run(self.crm.update_bike(self.new_id, status="repair"))
        out = self.handle("fix", SWAP, swap_allowed=True, by="tg:55")
        self.assertEqual(out.reaction, "👎")
        self.assertIn("Замена не проведена", out.reply)
        self.assertEqual(run(self.crm.rental(self.rental_id))["bike_id"], self.bike_id)

    def test_other_client_named(self):
        out = self.handle("fix", SWAP.replace("Иванов Иван", "Петров Пётр"),
                          swap_allowed=True, by="tg:55")
        self.assertEqual(out.reaction, "👎")
        self.assertIn("у другого клиента", out.reply)


class TestReturn(OpsCase):
    def close(self, days_ago=0):
        run(service.close_rental(self.crm, run(self.crm.rental(self.rental_id)),
                                 closed_on=TODAY - timedelta(days=days_ago), note=None))

    def test_rental_still_open(self):
        out = self.handle("return", return_text())
        self.assertEqual(out.reaction, "👎")
        self.assertIn("ещё открыта", out.reply)

    def test_closed_rental_with_debt(self):
        self.close()
        out = self.handle("return", return_text())
        self.assertEqual(out.reaction, "👍")
        self.assertIn("долг 3 000 ₽", out.reply)
        self.assertIn("1 500", out.reply)
        row = self.reports()[0]
        self.assertEqual(row["rental_id"], self.rental_id)
        # Денег из группы в журнал не пишется ничего.
        self.assertEqual(run(self.crm.client_balance(self.client_id)), D("-3000.00"))

    def test_closed_and_paid(self):
        run(self.crm.add_ledger(client_id=self.client_id, kind="payment", amount=D(3000),
                                method="cash", created_by="test"))
        self.close()
        out = self.handle("return", return_text())
        self.assertEqual((out.reaction, out.reply), ("👍", None))

    def test_old_closure_is_not_this_return(self):
        self.close(days_ago=30)
        out = self.handle("return", return_text())
        self.assertEqual(out.reaction, "👎")
        self.assertIn("не отражена", out.reply)

    def test_broken_form_is_answered_and_chatter_is_not(self):
        out = self.handle("return", "Когда сдал: сегодня\nКто принял велик: Петя")
        self.assertEqual(out.reaction, "👎")
        self.assertEqual(self.handle("return", "фото повреждений ниже").reaction, None)


class TestQueries(OpsCase):
    def test_daily_is_saved(self):
        out = self.handle("daily", DAILY)
        self.assertEqual(out.reaction, "👍")
        row = self.reports()[0]
        self.assertEqual(row["kind"], "daily")
        self.assertIn("Помыто велосипедов: 4", logic.ops_report_summary(row))

    def test_debt_by_motor_and_phone(self):
        out = self.handle("debt", "60v240w2305001")
        self.assertIn("Иванов Иван Иванович", out.reply)
        self.assertIn("Долг по журналу: 3 000 ₽", out.reply)
        out = self.handle("debt", "8 900 123-45-67")
        self.assertIn("B-1", out.reply)
        self.assertIn("Не нашёл", self.handle("debt", "60V0000000").reply)
        self.assertIsNone(self.handle("debt", "ок").reply)

    def test_gps_reply(self):
        tid = run(self.crm.create_tracker(device_id="861", alias="T-1",
                                          bike_id=self.bike_id, phone="+79005554433"))
        self.assertTrue(tid)
        out = self.handle("gps", "LXR123456")
        self.assertIn("T-1", out.reply)
        self.assertIn("+79005554433", out.reply)
        out = self.handle("gps", "60V240W2305999")
        self.assertIn("не привязан", out.reply)


try:
    import importlib

    from aiogram import Bot, Dispatcher
    from aiogram.methods import SendMessage, SetMessageReaction
    from aiogram.types import Chat, Message, Update, User

    from app.filters import ops_topic
    from app.handlers import menu, ops
    from app.middlewares import PipelineMiddleware
    from app.services.crypto import Vault
    from tests.test_flow import ADMIN_ID, FakeDB, FakeSession, make_config
    HAVE_AIOGRAM = True
except ImportError:                                    # pragma: no cover
    HAVE_AIOGRAM = False

OPS_CHAT = -1002631509993
_UPDATE_IDS = iter(range(9000, 10**6))


def ops_update(text, *, thread=7, chat_id=OPS_CHAT, user_id=777):
    uid = next(_UPDATE_IDS)
    return Update(update_id=uid, message=Message(
        message_id=uid, date=datetime.now(UTC), message_thread_id=thread,
        is_topic_message=thread is not None,
        chat=Chat(id=chat_id, type="supergroup", is_forum=True),
        from_user=User(id=user_id, is_bot=False, first_name="Точка", username="point"),
        text=text))


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestOpsThroughBot(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for module in (ops, menu):
            importlib.reload(module)
        self.cfg = make_config(ops_chat_id=OPS_CHAT, ops_topic_fix=7, ops_topic_return=2383,
                               ops_topic_debt=23488, ops_topic_gps=23745,
                               ops_topic_daily=604)
        self.db, self.crm, self.session = FakeDB(), FakeCrm(), FakeSession()
        self.bot = Bot("123:abc", session=self.session)
        self.dp = Dispatcher()
        self.dp.update.outer_middleware(PipelineMiddleware(
            self.db, self.cfg, Vault.from_raw(self.cfg.pdn_key), self.crm))
        self.dp.include_router(ops.router)
        self.dp.include_router(menu.router)
        client_id = await self.crm.create_client(full_name="Иванов Иван Иванович",
                                                 phone="+79001234567")
        bike_id = await self.crm.create_bike(code="B-1", model="Truck+",
                                             frame_no="LXR123456", motor_no="60V240W2305001")
        await self.crm.create_bike(code="B-2", model="Truck+", frame_no="LXR999999",
                                   motor_no="60V240W2305999")
        tariff_id = await self.crm.create_tariff("Неделя", 7, D(3000), None)
        self.rental_id = await service.open_rental(
            self.crm, client=await self.crm.client(client_id),
            bike=await self.crm.bike(bike_id), tariff=await self.crm.tariff(tariff_id),
            started_on=TODAY, contract_no="АВ-1", by="test")

    async def feed(self, update):
        await self.dp.feed_update(self.bot, update)

    def reactions(self):
        return [m.reaction[0].emoji for m in self.session.calls
                if isinstance(m, SetMessageReaction)]

    def replies(self):
        return [m for m in self.session.calls if isinstance(m, SendMessage)]

    async def test_topic_routing(self):
        self.assertEqual(ops_topic(self.cfg, ops_update("x").message), "fix")
        self.assertEqual(ops_topic(self.cfg, ops_update("x", thread=604).message), "daily")
        self.assertIsNone(ops_topic(self.cfg, ops_update("x", thread=None).message))
        self.assertIsNone(ops_topic(self.cfg, ops_update("x", chat_id=-100555).message))
        self.assertIsNone(ops_topic(make_config(), ops_update("x").message),
                          "без OPS_CHAT_ID группа не читается")

    async def test_fixation_gets_reaction_and_no_client_pipeline(self):
        await self.feed(ops_update(fix_text()))
        self.assertEqual(self.reactions(), ["👍"])
        self.assertEqual(self.replies(), [], "ни меню, ни «подпишитесь на канал»")
        self.assertEqual(self.db.users, {}, "сотрудник точки - не клиент, анкеты нет")

    async def test_mismatch_is_answered_in_the_same_topic(self):
        await self.feed(ops_update(fix_text(issue={**ISSUE, "vin_motor": "60V0000000",
                                                   "vin_frame": ""})))
        self.assertEqual(self.reactions(), ["👎"])
        [reply] = self.replies()
        self.assertEqual(reply.chat_id, OPS_CHAT)
        self.assertEqual(reply.message_thread_id, 7)
        self.assertIn("не найден в парке", reply.text)

    async def test_chatter_is_swallowed_silently(self):
        await self.feed(ops_update("всем привет"))
        await self.feed(ops_update("ок", thread=23488))
        self.assertEqual(self.session.calls, [])

    async def test_other_topics_of_the_group_are_dropped(self):
        await self.feed(ops_update(fix_text(), thread=99))
        self.assertEqual(self.session.calls, [])

    async def test_swap_needs_a_known_person(self):
        await self.feed(ops_update(SWAP))
        self.assertEqual(self.reactions(), ["👎"])
        self.assertIn("/staff", self.replies()[0].text)
        self.assertEqual((await self.crm.rental(self.rental_id))["bike_id"],
                         next(b["id"] for b in self.crm.bikes_.values() if b["code"] == "B-1"))
        # Оператор из ADMINS - может.
        await self.feed(ops_update(SWAP, user_id=ADMIN_ID))
        self.assertEqual(self.reactions(), ["👎", "👍"])
        rental = await self.crm.rental(self.rental_id)
        self.assertEqual(rental["bike_code"], "B-2")

    async def test_linked_staff_with_rental_rights_may_swap(self):
        profile = next(p for p in self.crm.profiles_.values() if p["code"] == "manager")
        sid = await self.crm.create_staff("m1", "x", "Менеджер", "manager",
                                          profile_id=profile["id"])
        await self.crm.link_staff_tg(sid, 777, "point")
        await self.feed(ops_update(SWAP))
        self.assertEqual(self.reactions(), ["👍"])

    async def test_debt_reply(self):
        await self.feed(ops_update("60V240W2305001", thread=23488))
        [reply] = self.replies()
        self.assertIn("Долг по журналу: 3 000 ₽", reply.text)
        self.assertEqual(reply.message_thread_id, 23488)


class TestShouldProcess(unittest.TestCase):
    def test_ops_messages_pass_the_group_gate(self):
        self.assertTrue(bot_logic.should_process("supergroup", from_admin_chat=False,
                                                 is_moderation_callback=False,
                                                 is_ops_message=True))
        self.assertFalse(bot_logic.should_process("supergroup", from_admin_chat=False,
                                                  is_moderation_callback=False))


try:
    from tests import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestOpsInPanel(tw.WebCase if HAVE_WEB else unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        tw.run(self.crm.update_bike(self.bike_id, motor_no="60V240W2305001"))
        self.rental_id = tw.run(service.open_rental(
            self.crm, client=tw.run(self.crm.client(self.client_id)),
            bike=tw.run(self.crm.bike(self.bike_id)),
            tariff=tw.run(self.crm.tariff(self.tariff_id)), started_on=TODAY,
            contract_no="АВ-1", by="test"))

    def test_log_and_rental_card(self):
        self.assertIn("Сообщений из группы ещё нет", self.get_ok("/ops"))
        meta = {"chat_id": -1, "message_id": 5, "thread_id": 7, "author_tg": 1,
                "author": "@point"}
        tw.run(opsgroup.handle(self.crm, "fix", fix_text(user={**USER,
               "full_name": "Иванов Иван", "phone": "+79990000000"}, anketa={},
               issue={**ISSUE, "vin_frame": ""}), meta, today=TODAY, now=NOW))
        tw.run(opsgroup.handle(self.crm, "daily", DAILY, {**meta, "message_id": 6},
                               today=TODAY, now=NOW))
        page = self.get_ok("/ops")
        self.assertIn("Фиксация выдачи", page)
        self.assertIn("@point", page)
        self.assertIn("Детали: колодки - 1 упаковка", page)
        self.assertNotIn("Павлюхина 26.09", self.get_ok("/ops?kind=fix"))
        self.assertNotIn("03.08 - 10.08", self.get_ok("/ops?bad=1"), "совпавшая - не 👎")
        self.assertIn("Группа точек", self.get_ok(f"/rentals/{self.rental_id}"))

    def test_tracker_sim_phone(self):
        tid = tw.run(self.crm.create_tracker(device_id="861", alias="T-1"))
        r = self.client.post(f"/trackers/{tid}/phone", data={"phone": "8 900 555-44-33"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(tw.run(self.crm.tracker(tid))["phone"], "+79005554433")
        self.client.post(f"/trackers/{tid}/phone", data={"phone": "звоните Пете"})
        self.assertEqual(tw.run(self.crm.tracker(tid))["phone"], "+79005554433",
                         "мусор не затирает номер")
        self.assertIn("+79005554433", self.get_ok(f"/trackers/{tid}"))
