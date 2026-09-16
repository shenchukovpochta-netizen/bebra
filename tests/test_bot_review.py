"""Регрессии ревью логики бота: тупики и перезаписи в переходах состояний.

- форма выдачи при активной аренде не должна трогать её данные;
- /start у заявки на проверке не начинает регистрацию заново;
- /start выводит из «причины сдачи» так же, как из «вопроса в поддержку»;
- после отказа /start продолжает с шага, на который вернул модератор;
- «Есть ошибка» в акте выкупа чинится повторной формой выдачи;
- напоминание о сроке не уходит тому, кто уже подписывает акт возврата;
- контакт ответом в служебном чате не роняет обработчик кабинета.

Гоняется через тот же Dispatcher и заглушки, что tests/test_flow.py.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import logic  # noqa: E402  - чистая логика, без aiogram

try:
    import test_flow as tf
    from aiogram import Bot, Dispatcher
    from aiogram.methods import SendMessage

    from app import texts
    from app.handlers import cabinet, contract, fleet, menu, moderation, registration
    from app.handlers import faq as faq_handlers
    from app.middlewares import PipelineMiddleware
    from app.services.crypto import Vault
    HAVE_FLOW = True
except ImportError:                                    # pragma: no cover
    HAVE_FLOW = False

USER_ID, ADMIN_ID, ADMIN_CHAT = 5001, 111, -1009876543210


class TestReminderLogic(unittest.TestCase):
    def test_no_reminder_while_signing_the_return_act(self):
        """Оператор уже прислал данные возврата, клиент подписывает акт:
        «продлите или верните велосипед» в этот момент - неправда."""
        row = {"rent_until": date(2026, 8, 10), "state": logic.WAIT_RETURN_SIGN}
        self.assertIsNone(logic.reminder_due(row, before_days=2,
                                             today=date(2026, 8, 9)))
        self.assertIsNone(logic.reminder_due(row, before_days=2,
                                             today=date(2026, 8, 12)))
        # В меню - напоминание идёт как раньше.
        self.assertEqual(
            logic.reminder_due({**row, "state": logic.APPROVED}, before_days=2,
                               today=date(2026, 8, 9)),
            logic.REMIND_SOON)


@unittest.skipUnless(HAVE_FLOW, "aiogram не установлен")
class TestBotLogicReview(tf.TestFlow):

    def sent_texts(self, chat_id: int) -> list[str]:
        return [m.text or "" for m in self.session.sent_to(chat_id)
                if isinstance(m, SendMessage)]

    # ─── форма выдачи при активной аренде ───

    async def test_issue_form_during_active_rental_leaves_data_untouched(self):
        """Отказ «сначала закройте текущую» обязан быть отказом целиком:
        раньше данные, срок и отметки напоминаний уже были перезаписаны
        к моменту, когда оператору говорили «не применить»."""
        await self.register_fully()
        row = self.db.users[USER_ID]
        row["remind_soon_at"] = "sent"
        before_until = row["rent_until"]
        await self.provide_issue(self.REPEAT_FORM)
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.APPROVED)
        self.assertEqual(row["issue_data"]["bike_model"], "Truck+",
                         "модель активной аренды перезаписана")
        self.assertEqual(row["rent_until"], before_until, "срок аренды сдвинут")
        self.assertEqual(row["remind_soon_at"], "sent",
                         "отметки напоминаний сброшены")
        self.assertIn("активная аренда", self.sent_texts(ADMIN_CHAT)[-1])

    # ─── /start в разных состояниях ───

    async def test_start_while_pending_does_not_restart(self):
        """Заявка на проверке: /start - «что там с моей заявкой», а не
        «заполнить заново». Иначе человек проходит анкету второй раз,
        а у модератора появляется вторая карточка."""
        await self.submit()
        await self.feed(tf.msg("/start"))
        row = self.db.users[USER_ID]
        self.assertEqual((row["state"], row["status"]),
                         (logic.PENDING, logic.ST_PENDING))
        self.assertIn("на проверке", self.sent_texts(USER_ID)[-1])

    async def test_start_after_approval_waiting_for_issue_says_so(self):
        """Одобрено, оператор ещё не прислал данные выдачи: /start не должен
        показывать меню «вы уже зарегистрированы» - кнопки в нём в этом
        состоянии не работают."""
        await self.submit()
        await self.approve()
        await self.feed(tf.msg("/start"))
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.PENDING)
        last = self.sent_texts(USER_ID)[-1]
        self.assertIn("одобрена", last.lower())
        self.assertNotIn("уже зарегистрированы", last)

    async def test_start_escapes_close_reason_state(self):
        """/start посреди «назовите причину сдачи» - «передумал», как и
        в поддержке: иначе следующее «привет» уезжает оператору
        запросом на закрытие аренды с причиной «привет»."""
        await self.register_fully()
        await self.feed(tf.msg(texts.BTN_CLOSE_RENT))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_CLOSE_REASON)
        await self.feed(tf.msg("/start"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.APPROVED)
        await self.feed(tf.msg("привет"))
        self.assertIsNone(self.db.users[USER_ID].get("close_reason"))
        self.assertFalse([t for t in self.sent_texts(ADMIN_CHAT)
                          if "Запрос на закрытие" in t],
                         "«привет» ушло оператору как причина сдачи")

    async def test_start_after_rejection_continues_from_the_step(self):
        """Текст отказа обещает «нажмите /start … чтобы продолжить с этого
        места». Значит, /start у отклонённого - повтор вопроса текущего
        шага, а не десять полей анкеты заново."""
        await self.submit()
        await self.feed(tf.cb(f"rj:{USER_ID}:passport", chat_id=ADMIN_CHAT,
                              user_id=ADMIN_ID, chat_type="supergroup"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PASSPORT)
        await self.feed(tf.msg("/start"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PASSPORT)
        self.assertIn("паспорт", self.sent_texts(USER_ID)[-1].lower())
        await self.feed(tf.msg("4321 098765"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PASSPORT_DATE)
        self.assertEqual(self.anketa()["passport_number"], "4321 098765")

    # ─── ошибка в акте выкупа ───

    async def test_buyout_act_mistake_is_fixed_by_a_new_issue_form(self):
        """Клиент нажал «Есть ошибка» на акте выкупа. Раньше у оператора не
        было ни одного пути поправить данные: форма выдачи отвергалась
        из-за активной аренды, а клиент оставался в подписи акта навсегда."""
        await self.buyout_rental()
        row = self.db.users[USER_ID]
        row["rent_until"] = row["buyout_from"] + timedelta(days=200)
        until = row["rent_until"]
        await self.buyout_pass(row["buyout_from"] + timedelta(days=119))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_BUYOUT_SIGN)

        await self.feed(tf.cb("buyout_mistake"))
        n_docs = len(self.session.documents())
        await self.provide_issue(self.BUYOUT_FORM.replace("Truck+", "Truck+ Pro"))
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.WAIT_BUYOUT_SIGN)
        self.assertEqual(row["issue_data"]["bike_model"], "Truck+ Pro")
        self.assertEqual(row["rent_until"], until, "срок аренды трогать нельзя")
        self.assertIsNotNone(row["act_in_signed_at"])
        resent = [m for m in self.session.documents()[n_docs:] if m.chat_id == USER_ID]
        self.assertTrue(resent, "исправленный акт выкупа не пришёл клиенту")
        self.assertIn("Truck+ Pro", tf.docx_text(resent[-1].document.data))
        self.assertIn("акт выкупа", self.sent_texts(ADMIN_CHAT)[-1].lower())

    # ─── переход к акту приёма ───

    async def test_issue_form_after_failed_act_is_guarded_by_state(self):
        """Договор подписан, оплата подтверждена, акт не собрался - клиент
        в меню. Повторная форма выдачи переводит его на подпись акта,
        но только из меню или поддержки, а не поверх чужого шага."""
        await self.submit()
        await self.approve_fully()
        await self.feed(tf.cb("sign"))
        await self.confirm_pay()
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_ACT_SIGN)
        # Акт не собрался: контракт откатил клиента в меню.
        self.db.users[USER_ID]["state"] = logic.APPROVED
        await self.provide_issue()
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_ACT_SIGN)


@unittest.skipUnless(HAVE_FLOW, "aiogram не установлен")
class TestServiceChatContact(unittest.IsolatedAsyncioTestCase):
    """Роутеры в боевом порядке (кабинет первым), CRM отключена."""

    async def asyncSetUp(self):
        for module in (cabinet, fleet, contract, registration, moderation,
                       faq_handlers, menu):
            importlib.reload(module)
        self.cfg = tf.make_config()
        self.db = tf.FakeDB()
        self.session = tf.FakeSession()
        self.bot = Bot("123:abc", session=self.session)
        self.dp = Dispatcher()
        self.dp.update.outer_middleware(
            PipelineMiddleware(self.db, self.cfg, Vault.from_raw(self.cfg.pdn_key)))
        for router in (cabinet.router, fleet.router, moderation.router,
                       contract.router, registration.router, faq_handlers.router,
                       menu.router):
            self.dp.include_router(router)

    async def asyncTearDown(self):
        await self.bot.session.close()

    async def test_contact_reply_in_service_chat_does_not_crash(self):
        """Оператор ответил на карточку контактом клиента: апдейт идёт
        мимо пользовательского конвейера, `user` в данных нет - обработчик
        привязки кабинета не должен падать на обязательном аргументе."""
        update = tf.msg(chat_id=ADMIN_CHAT, user_id=ADMIN_ID, chat_type="supergroup",
                        contact_user_id=USER_ID, reply_to=77)
        await self.dp.feed_update(self.bot, update)
        self.assertIn(update.update_id, self.db.finished,
                      "апдейт не закрыт - обработчик упал")


# Наследование даёт обвязку (Dispatcher, заглушки бота и базы), но вместе
# с ней и все тесты test_flow - их тут повторять незачем.
if HAVE_FLOW:
    for _name in dir(tf.TestFlow):
        if _name.startswith("test_") and _name not in TestBotLogicReview.__dict__:
            setattr(TestBotLogicReview, _name, None)


if __name__ == "__main__":
    unittest.main()
