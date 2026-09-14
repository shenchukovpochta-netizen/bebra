"""Парк из служебного чата сквозь настоящий Dispatcher: /bike, кнопки
статуса, ремонт ответом на карточку, права и блокировка в аренде."""

from __future__ import annotations

import importlib
import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from aiogram import Bot, Dispatcher
    from aiogram.methods import EditMessageText, SendMessage

    from app import logic, texts
    from app.crm import logic as crm_logic
    from app.handlers import cabinet, contract, fleet, menu, moderation, registration
    from app.handlers import faq as faq_handlers
    from app.middlewares import PipelineMiddleware
    from app.services.crypto import Vault
    from tests.fake_crm import FakeCrm
    from tests.test_flow import (
        ADMIN_CHAT,
        ADMIN_ID,
        USER_ID,
        FakeDB,
        FakeSession,
        cb,
        make_config,
        msg,
        settle,
    )
    HAVE_AIOGRAM = True
except ImportError:                                    # pragma: no cover
    HAVE_AIOGRAM = False

D = Decimal


def build():
    for module in (fleet, cabinet, contract, registration, moderation, faq_handlers, menu):
        importlib.reload(module)
    cfg = make_config()
    db, crm, session = FakeDB(), FakeCrm(), FakeSession()
    bot = Bot("123:abc", session=session)
    dp = Dispatcher()
    dp.update.outer_middleware(PipelineMiddleware(db, cfg, Vault.from_raw(cfg.pdn_key), crm))
    dp.include_router(cabinet.router)
    dp.include_router(fleet.router)
    dp.include_router(moderation.router)
    dp.include_router(contract.router)
    dp.include_router(registration.router)
    dp.include_router(faq_handlers.router)
    dp.include_router(menu.router)
    return dp, bot, db, crm, session, cfg


class TestRepairForm(unittest.TestCase):
    NODES = {"controller": "Контроллер", "brake_pads": "Тормоза: колодки",
             "brake_disc": "Тормоза: диск", "motor_wheel": "Мотор-колесо"}

    def test_parse(self):
        parsed, err = logic.parse_repair_form(
            "узел: контроллер\nзапчасти: 2 500\nработа: 500\nчто: заменил", self.NODES)
        self.assertEqual(err, "")
        self.assertEqual(parsed, {"node": "controller", "note": "заменил",
                                  "parts_cost": D("2500.00"), "labor_cost": D("500.00")})
        parsed, _ = logic.parse_repair_form("Узел: колодки", self.NODES)
        self.assertEqual((parsed["node"], parsed["parts_cost"], parsed["labor_cost"]),
                         ("brake_pads", D(0), D(0)))
        parsed, err = logic.parse_repair_form("узел: тормоза", self.NODES)
        self.assertIsNone(parsed)
        self.assertIn("Уточните узел", err)
        self.assertIn("колодки", err)
        self.assertNotIn("Мотор", err)
        self.assertIsNone(logic.parse_repair_form("узел: мотор\nработа: nan", self.NODES)[0])
        self.assertIsNone(logic.parse_repair_form("узел: мотор\nработа: 1e9", self.NODES)[0])
        parsed, _ = logic.parse_repair_form("узел: мотор\nзапчасти: 2 500 руб", self.NODES)
        self.assertEqual(parsed["parts_cost"], D("2500.00"))
        self.assertIn("нет в справочнике", logic.parse_repair_form("узел: варп", self.NODES)[1])
        self.assertIn("узел: …", logic.parse_repair_form("запчасти: 100", self.NODES)[1])
        self.assertIn("Не понял сумму", logic.parse_repair_form("узел: мотор\nработа: много",
                                                                 self.NODES)[1])
        self.assertIsNone(logic.parse_repair_form("узел: мотор\nработа: -5", self.NODES)[0])

    def test_helpers(self):
        self.assertTrue(logic.is_fleet_command("/bike B-03"))
        self.assertTrue(logic.is_fleet_command("/bike@mybike_bot 264022501706153"))
        self.assertTrue(logic.is_fleet_command("/велик"))
        self.assertFalse(logic.is_fleet_command("/start"))
        self.assertFalse(logic.is_fleet_command("bike B-03"))
        self.assertEqual(logic.fleet_command_arg("/bike  B-03 "), "B-03")
        self.assertEqual(logic.fleet_command_arg("/bike"), "")
        self.assertEqual(logic.bike_code_from_card("🚲 Велосипед B-03 · Truck+\nСтатус: …"), "B-03")
        self.assertIsNone(logic.bike_code_from_card("Договор на утверждение"))
        self.assertTrue(logic.is_moderation_data("bk:12:repair"))
        self.assertFalse(logic.is_moderation_data("bk:x:repair"))


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestFleetChat(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dp, self.bot, self.db, self.crm, self.session, self.cfg = build()
        self.bike_id = await self.crm.create_bike(code="B-03", model="Truck+",
                                                  frame_no="264022501706153",
                                                  location="Павлюхина")

    async def asyncTearDown(self):
        await self.bot.session.close()

    async def feed(self, update):
        await self.dp.feed_update(self.bot, update)
        await settle()

    def sent(self):
        return [m for m in self.session.sent_to(ADMIN_CHAT) if isinstance(m, SendMessage)]

    def admin_msg(self, text, reply_to=None, reply_text=None):
        return msg(text, chat_id=ADMIN_CHAT, user_id=ADMIN_ID, chat_type="supergroup",
                   reply_to=reply_to, reply_text=reply_text)

    async def test_card_and_status_button(self):
        await self.feed(self.admin_msg("/bike B-03"))
        card = self.sent()[-1]
        self.assertTrue(card.text.startswith("🚲 Велосипед B-03 · Truck+"))
        self.assertIn("<b>Свободен</b> · Павлюхина", card.text)
        self.assertIn("Не в аренде", card.text)
        buttons = [b.callback_data for row in card.reply_markup.inline_keyboard for b in row]
        self.assertEqual(buttons, [f"bk:{self.bike_id}:repair", f"bk:{self.bike_id}:maintenance",
                                   f"bk:{self.bike_id}:written_off"])
        await self.feed(cb(f"bk:{self.bike_id}:repair", chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                           chat_type="supergroup"))
        bike = await self.crm.bike(self.bike_id)
        self.assertEqual(bike["status"], "repair")
        log = await self.crm.bike_status_log(self.bike_id)
        self.assertEqual((log[0]["to_status"], log[0]["changed_by"]),
                         ("repair", f"tg:{ADMIN_ID}"))
        self.assertIn("В ремонте (из чата)", (await self.crm.bike_log(self.bike_id))[0]["note"])
        self.assertTrue(any(isinstance(m, EditMessageText) for m in self.session.calls))
        self.assertIn("B-03: В ремонте", self.sent()[-1].text)
        # тот же статус второй раз - ничего не пишется
        await self.feed(cb(f"bk:{self.bike_id}:repair", chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                           chat_type="supergroup"))
        self.assertEqual(len(await self.crm.bike_status_log(self.bike_id)), 2)

    async def test_lookup_by_frame_and_not_found(self):
        await self.feed(self.admin_msg("/bike 264022501706153"))
        self.assertTrue(self.sent()[-1].text.startswith("🚲 Велосипед B-03"))
        await self.feed(self.admin_msg("/bike b-03"))
        self.assertTrue(self.sent()[-1].text.startswith("🚲 Велосипед B-03"))
        await self.feed(self.admin_msg("/bike X-99"))
        self.assertIn("не найден", self.sent()[-1].text)
        await self.feed(self.admin_msg("/bike"))
        self.assertIn("/bike B-03", self.sent()[-1].text)

    async def test_repair_reply(self):
        await self.feed(self.admin_msg("/bike B-03"))
        # Telegram отдаёт в reply_to_message текст карточки - по нему
        # обработчик и находит велосипед
        card_text = self.sent()[-1].text
        card_index = len(self.session.calls)
        await self.feed(self.admin_msg("узел: контроллер\nзапчасти: 2500\nработа: 500\nчто: прошил",
                                       reply_to=card_index, reply_text=card_text))
        reply = self.sent()[-1].text
        self.assertIn("B-03: Контроллер - 3 000 ₽", reply)
        entries = await self.crm.bike_log(self.bike_id)
        self.assertEqual((entries[0]["kind"], entries[0]["cost"], entries[0]["created_by"]),
                         ("repair", D("3000.00"), f"tg:{ADMIN_ID}"))
        self.assertEqual(self.crm.repair_items_[0]["node"], "controller")
        # непонятный узел - подсказка, ничего не записано
        await self.feed(self.admin_msg("узел: варп-двигатель", reply_to=card_index,
                                       reply_text=card_text))
        self.assertIn("нет в справочнике", self.sent()[-1].text)
        self.assertEqual(len(await self.crm.bike_log(self.bike_id)), 1)
        # ответ на чужое сообщение (не карточку) уходит в разбор модерации
        await self.feed(self.admin_msg("узел: контроллер", reply_to=card_index,
                                       reply_text="Договор на утверждение"))
        self.assertNotIn("Контроллер", self.sent()[-1].text)
        # текст карточки, написанный человеком, а не ботом - не карточка
        await self.feed(msg("узел: контроллер", chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                            chat_type="supergroup", reply_to=card_index,
                            reply_from_bot=False, reply_text=card_text))
        self.assertEqual(len(await self.crm.bike_log(self.bike_id)), 1)
        # ремонты на карточке видны и после десятков других записей журнала
        for _ in range(25):
            await self.crm.add_bike_log(self.bike_id, "note", "заметка", None, "t")
        await self.feed(self.admin_msg("/bike B-03"))
        self.assertIn("Ремонты: ", self.sent()[-1].text)
        self.assertIn("Контроллер", self.sent()[-1].text)

    async def test_rented_bike_is_locked(self):
        cid = await self.crm.create_client(full_name="Иванов", phone="+79990000000")
        await self.crm.create_rental(client_id=cid, bike_id=self.bike_id, tariff_id=None,
                                     tariff_name="t", period_days=7, price=D(3000),
                                     billing="manual", started_on=date.today(),
                                     contract_no=None, created_by="t")
        await self.feed(self.admin_msg("/bike B-03"))
        card = self.sent()[-1]
        self.assertIn("У клиента: Иванов", card.text)
        self.assertEqual(card.reply_markup.inline_keyboard, [])
        await self.feed(cb(f"bk:{self.bike_id}:repair", chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                           chat_type="supergroup"))
        self.assertEqual((await self.crm.bike(self.bike_id))["status"], "rented")

    async def test_not_admin_and_not_service_chat(self):
        await self.feed(msg("/bike B-03", chat_id=ADMIN_CHAT, user_id=USER_ID,
                            chat_type="supergroup"))
        self.assertEqual(self.sent(), [])
        # в личке клиента команда идёт по обычному конвейеру и не даёт карточку
        await self.feed(msg("/bike B-03"))
        self.assertFalse(any(m.text.startswith("🚲 Велосипед")
                             for m in self.session.sent_to(USER_ID)
                             if isinstance(m, SendMessage)))

    async def test_status_labels_from_crm(self):
        label = crm_logic.BIKE_STATUSES["maintenance"]
        self.assertEqual(texts.FLEET_STATUS_SET.format(code="B-03", status=label, who="@x"),
                         "B-03: На ТО - @x")
        from types import SimpleNamespace
        self.assertEqual(fleet._who(SimpleNamespace(username="irik", first_name="И", id=1)),
                         "@irik")
        self.assertEqual(fleet._who(SimpleNamespace(username=None, first_name="Ирик", id=1)),
                         "Ирик")


if __name__ == "__main__":
    unittest.main()
