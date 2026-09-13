"""Регрессии ревью бота: старая карточка модерации (InaccessibleMessage),
возврат после отказа, когда ретеншен уже стёр анкету, отмена продления
ответом оператора и продление срока хранения документов при продлении.
Гоняется через тот же Dispatcher и заглушки, что tests/test_flow.py.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import test_flow as tf
    from aiogram.types import CallbackQuery, Chat, InaccessibleMessage, Update, User

    from app import logic, texts
    HAVE_FLOW = True
except ImportError:                                    # pragma: no cover
    HAVE_FLOW = False

USER_ID, ADMIN_ID, ADMIN_CHAT = 5001, 111, -1009876543210


def cb_old(data: str, *, chat_id: int, user_id: int, chat_type: str) -> Update:
    """Нажатие на карточке старше двух суток: Telegram отдаёт сообщение
    без текста и без методов правки."""
    return Update(update_id=tf._next_id(), callback_query=CallbackQuery(
        id=str(tf._next_id()), from_user=User(id=user_id, is_bot=False, first_name="A"),
        chat_instance="ci", data=data,
        message=InaccessibleMessage(chat=Chat(id=chat_id, type=chat_type),
                                    message_id=tf._next_id())))


@unittest.skipUnless(HAVE_FLOW, "aiogram не установлен")
class TestReviewFixes(tf.TestFlow):
    async def test_approve_on_old_card_still_notifies_client(self):
        await self.submit()
        n = len(self.session.calls)
        await self.feed(cb_old(f"approve:{USER_ID}", chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                               chat_type="supergroup"))
        row = self.db.users[USER_ID]
        self.assertEqual((row["status"], row["state"]), (logic.ST_APPROVED, logic.PENDING))
        self.assertIsNotNone(row.get("issue_message_id"))
        to_client = [m for m in self.session.calls[n:]
                     if getattr(m, "chat_id", None) == USER_ID]
        self.assertTrue(to_client, "клиент не узнал об одобрении")

    async def test_reject_on_old_card_still_tells_reason(self):
        await self.submit()
        n = len(self.session.calls)
        await self.feed(cb_old(f"rj:{USER_ID}:doc", chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                               chat_type="supergroup"))
        row = self.db.users[USER_ID]
        self.assertEqual(row["status"], logic.ST_REJECTED)
        to_client = [m for m in self.session.calls[n:]
                     if getattr(m, "chat_id", None) == USER_ID]
        self.assertTrue(to_client, "клиент не узнал причину отказа")

    async def test_return_after_purge_restarts_registration(self):
        await self.submit()
        await self.feed(tf.cb(f"rj:{USER_ID}:phones", chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                              chat_type="supergroup"))
        # ретеншен стёр анкету и сканы, как это делает Database.clear_files
        self.db.users[USER_ID].update(
            doc_file_id=None, doc_path=None, doc2_file_id=None, doc2_path=None,
            parent_file_id=None, parent_path=None, anketa_enc=None, purge_after=None)
        await self.feed(tf.msg("+7 900 777-88-99"))
        await self.feed(tf.msg("+7 900 666-55-44"))
        await self.feed(tf.msg(photo=True, file_id="n1"))
        await self.feed(tf.cb("doc_enough"))
        n = len(self.session.calls)
        await self.feed(tf.cb("confirm"))
        row = self.db.users[USER_ID]
        self.assertEqual((row["state"], row["status"]), (logic.WAIT_FIO, logic.ST_NEW))
        self.assertFalse(any(getattr(m, "chat_id", None) == ADMIN_CHAT
                             for m in self.session.calls[n:]), "карточка без анкеты")
        self.assertIn("resubmit_after_purge", [e[1] for e in self.db.events])
        # человек может пройти заново, а не упираться в «ждите»
        await self.feed(tf.msg("Иванов Иван Иванович"))
        self.assertNotEqual(self.db.users[USER_ID]["state"], logic.PENDING)

    async def _rent_and_ask_extension(self) -> int:
        await self.submit()
        await self.approve_fully()
        await self.feed(tf.cb("sign"))
        await self.confirm_pay()
        await self.feed(tf.cb("act_sign"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.APPROVED)
        await self.feed(tf.cb("extend"))
        ext_id = self.db.users[USER_ID]["extend_message_id"]
        await self.feed(tf.msg("до: 31.12.2099\nоплата: 3000 qr", chat_id=ADMIN_CHAT,
                               user_id=ADMIN_ID, chat_type="supergroup", reply_to=ext_id))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PAYMENT)
        self.assertIsNotNone(self.db.users[USER_ID]["extend_until"])
        return ext_id

    async def test_operator_can_cancel_extension(self):
        ext_id = await self._rent_and_ask_extension()
        await self.feed(tf.msg("Отмена", chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                               chat_type="supergroup", reply_to=ext_id))
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.APPROVED)
        self.assertIsNone(row["extend_until"])
        self.assertIsNone(row.get("pay_message_id"))
        self.assertIn(texts.EXTEND_CANCELLED.split("\n")[0],
                      self.session.sent_to(USER_ID)[-1].text)
        self.assertIn("Продление отменено", self.session.sent_to(ADMIN_CHAT)[-1].text)
        self.assertIn("extend_cancelled", [e[1] for e in self.db.events])
        # клиент снова в меню: кнопки не отвечают «ждём оплату»
        await self.feed(tf.msg("🆘 Поддержка"))
        self.assertNotIn("ждём оплату".lower(), self.session.sent()[-1].lower())
        # новая заявка ещё не принята оператором: «отмена» на её карточке -
        # оператору честно говорят, что отменять нечего
        await self.feed(tf.cb("extend"))
        ext_id2 = self.db.users[USER_ID]["extend_message_id"]
        self.assertNotEqual(ext_id2, ext_id)
        await self.feed(tf.msg("отмена", chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                               chat_type="supergroup", reply_to=ext_id2))
        self.assertIn("Отменять нечего", self.session.sent_to(ADMIN_CHAT)[-1].text)
        self.assertNotEqual(self.db.users[USER_ID]["state"], logic.WAIT_PAYMENT)

    async def test_paid_extension_refreshes_purge_after(self):
        await self._rent_and_ask_extension()
        self.db.users[USER_ID]["purge_after"] = None
        await self.confirm_pay()
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.APPROVED)
        self.assertIsNone(row["extend_until"])
        self.assertIsNotNone(row["purge_after"], "срок хранения документов не сдвинут")


# Наследование даёт обвязку (Dispatcher, заглушки бота и базы), но вместе
# с ней и все тесты test_flow - их тут повторять незачем.
if HAVE_FLOW:
    for _name in dir(tf.TestFlow):
        if _name.startswith("test_") and _name not in TestReviewFixes.__dict__:
            setattr(TestReviewFixes, _name, None)


if __name__ == "__main__":
    unittest.main()
