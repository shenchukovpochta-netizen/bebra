"""Сквозной прогон сценария через настоящий Dispatcher.

Отличие от остальных тестов: здесь работают реальные роутеры, фильтры
и middleware aiogram. Именно этот слой раньше пропускал дефекты, которые
проверки чистой логики поймать не могли, - например то, что нажатия
«Одобрить» в групповом чате не доходили до обработчика.

Требует установленного aiogram, поэтому пропускается, если его нет:
    py -3 -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from aiogram import Bot, Dispatcher
    from aiogram.client.session.base import BaseSession
    from aiogram.methods import (
        AnswerCallbackQuery, EditMessageCaption, EditMessageReplyMarkup,
        GetChatMember, GetMe, SendMessage, SendPhoto,
    )
    from aiogram.types import (
        CallbackQuery, Chat, ChatMemberLeft, ChatMemberMember, Contact,
        Message, PhotoSize, Update, User,
    )
    # app.tasks тянет app.db, а тот - asyncpg, поэтому обе зависимости
    # проверяются одной попыткой: иначе набор падает на машине без asyncpg.
    from app import logic, tasks
    from app.config import Config
    from app.handlers import menu, moderation, registration
    from app.middlewares import PipelineMiddleware
    from app.services import files
    HAVE_AIOGRAM = True
except ImportError:                                    # pragma: no cover
    HAVE_AIOGRAM = False

USER_ID, CHAT_ID = 5001, 5001
ADMIN_ID, ADMIN_CHAT = 111, -1009876543210
CHANNEL_ID = -1001234567890


# ─────────────────────────── заглушки ───────────────────────────

class FakeSession(BaseSession):
    """Ничего не отправляет наружу, только записывает вызовы."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list = []
        self.subscribed = True

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs):    # pragma: no cover
        yield b"fake-image-bytes"

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="bot", username="testbot")
        if isinstance(method, GetChatMember):
            user = User(id=method.user_id, is_bot=False, first_name="u")
            return (ChatMemberMember(user=user, status="member") if self.subscribed
                    else ChatMemberLeft(user=user, status="left"))
        if isinstance(method, (SendMessage, SendPhoto)):
            return Message(
                message_id=len(self.calls), date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
            )
        if isinstance(method, (AnswerCallbackQuery, EditMessageCaption,
                               EditMessageReplyMarkup)):
            return True
        return True

    # помощники для проверок
    def sent(self) -> list[str]:
        out = []
        for m in self.calls:
            if isinstance(m, SendMessage):
                out.append(m.text)
            elif isinstance(m, SendPhoto):
                out.append(m.caption or "<фото>")
        return out

    def sent_to(self, chat_id: int) -> list:
        return [m for m in self.calls
                if isinstance(m, (SendMessage, SendPhoto)) and m.chat_id == chat_id]

    def last_markup(self):
        for m in reversed(self.calls):
            if isinstance(m, (SendMessage, SendPhoto)) and m.reply_markup:
                return m.reply_markup
        return None


class FakeDB:
    """Поведение bot.users в памяти, включая оптимистичную блокировку."""

    def __init__(self) -> None:
        self.users: dict[int, dict] = {}
        self.events: list[tuple] = []
        self.claimed: set[int] = set()
        self.finished: set[int] = set()

    async def claim_update(self, update_id, tg_id, kind, payload) -> bool:
        if update_id in self.claimed:
            return False
        self.claimed.add(update_id)
        return True

    async def finish_update(self, update_id) -> None:
        self.finished.add(update_id)

    async def upsert_user(self, tg_id, username):
        row = self.users.setdefault(tg_id, {
            "tg_id": tg_id, "username": username, "state": logic.NEW,
            "status": logic.ST_NEW, "rl_count": 0, "full_name": None, "phone": None,
            "doc_file_id": None, "selfie_file_id": None, "doc_path": None,
            "selfie_path": None, "doc_ocr": None, "name_match": None, "ocr_at": None,
            "purge_after": None,
        })
        row["rl_count"] += 1
        return dict(row)

    async def get_user(self, tg_id):
        row = self.users.get(tg_id)
        return dict(row) if row else None

    async def patch(self, tg_id, *, expected_state=None, expected_status=None, **fields):
        row = self.users.get(tg_id)
        if row is None:
            return False
        if expected_state is not None and row["state"] != expected_state:
            return False
        if expected_status is not None and row["status"] != expected_status:
            return False
        row.update(fields)
        return True

    async def log_event(self, tg_id, type_, payload=None):
        self.events.append((tg_id, type_))

    async def set_purge_after(self, tg_id, days):
        self.users[tg_id]["purge_after"] = days

    async def count_duplicate_docs(self, tg_id, sha256):
        return 0


def make_config(**overrides) -> Config:
    base = dict(
        bot_token="123:abc", channel_id=CHANNEL_ID, admin_chat_id=ADMIN_CHAT,
        admins=(ADMIN_ID,), pg={}, storage_dir=Path("/tmp/kyc"),
        channel_url="https://t.me/test", oferta_url="https://e.ru/o",
        oferta_version="2026-01-15", pdn_url="", pdn_version="2026-01-15",
        video_url="https://e.ru/v", ocr_enabled=False, ocr_url="", ocr_model="",
        ocr_api_key="", ocr_folder_id="", ocr_processor="Обработчик",
        purge_approved_days=90, purge_rejected_days=3, updates_log_days=7,
        rate_soft=20, rate_hard=25,
    )
    base.update(overrides)
    return Config(**base)


# ─────────────────────────── сборка ───────────────────────────

def build(cfg: Config | None = None):
    # Router - объект уровня модуля, и aiogram запрещает подключать его
    # ко второму Dispatcher. Перезагружаем модули, чтобы каждый тест получил
    # собственные роутеры с теми же обработчиками.
    for module in (registration, moderation, menu):
        importlib.reload(module)

    cfg = cfg or make_config()
    db = FakeDB()
    session = FakeSession()
    bot = Bot("123:abc", session=session)
    dp = Dispatcher()
    dp.update.outer_middleware(PipelineMiddleware(db, cfg))
    dp.include_router(moderation.router)
    dp.include_router(registration.router)
    dp.include_router(menu.router)
    return dp, bot, db, session, cfg


_seq = [0]


def _next_id() -> int:
    _seq[0] += 1
    return _seq[0]


def msg(text=None, *, chat_id=CHAT_ID, user_id=USER_ID, chat_type="private",
        photo=False, contact_user_id=None) -> Update:
    kwargs = {}
    if photo:
        kwargs["photo"] = [PhotoSize(file_id="f1", file_unique_id="u1",
                                     width=100, height=100, file_size=1000)]
    if contact_user_id is not None:
        kwargs["contact"] = Contact(phone_number="79990000000", first_name="U",
                                    user_id=contact_user_id)
    return Update(update_id=_next_id(), message=Message(
        message_id=_next_id(), date=datetime.now(timezone.utc),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="U"),
        text=text, **kwargs,
    ))


def cb(data, *, chat_id=CHAT_ID, user_id=USER_ID, chat_type="private") -> Update:
    return Update(update_id=_next_id(), callback_query=CallbackQuery(
        id=str(_next_id()), from_user=User(id=user_id, is_bot=False, first_name="U"),
        chat_instance="ci", data=data,
        message=Message(message_id=_next_id(), date=datetime.now(timezone.utc),
                        chat=Chat(id=chat_id, type=chat_type)),
    ))


async def settle() -> None:
    """Дать фоновым задачам (скачивание, OCR) доработать."""
    for _ in range(5):
        await asyncio.sleep(0)
    await tasks.drain(timeout=2)


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestFlow(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.dp, self.bot, self.db, self.session, self.cfg = build()
        # скачивание и укладка файлов - не предмет этого теста
        self._orig_download, self._orig_store = files.download, files.store
        files.download = lambda bot, file_id, max_bytes: _async(b"bytes")
        files.store = lambda d, tg, slot, data: (Path(f"/tmp/{tg}-{slot}.jpg"), "hash")

    async def asyncTearDown(self):
        files.download, files.store = self._orig_download, self._orig_store
        await self.bot.session.close()

    async def feed(self, update: Update):
        await self.dp.feed_update(self.bot, update)
        await settle()

    async def register_up_to_confirm(self):
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("oferta_ok"))
        await self.feed(msg(contact_user_id=USER_ID))
        await self.feed(msg(photo=True))
        await self.feed(msg(photo=True))

    # ─── сам сценарий ───

    async def test_full_registration_reaches_confirm(self):
        await self.register_up_to_confirm()
        self.assertEqual(self.db.users[USER_ID]["state"], logic.CONFIRM)
        self.assertEqual(self.db.users[USER_ID]["full_name"], "Иванов Иван Иванович")
        self.assertEqual(self.db.users[USER_ID]["phone"], "79990000000")

    async def test_consent_screen_names_the_data(self):
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        text = " ".join(self.session.sent())
        for expected in ("ФИО", "номер телефона", "документа", "фотографию"):
            self.assertIn(expected, text)

    async def test_foreign_contact_rejected(self):
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("oferta_ok"))
        await self.feed(msg(contact_user_id=999999))     # чужой контакт
        self.assertIsNone(self.db.users[USER_ID]["phone"])
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_CONTACT)

    async def test_moderation_card_goes_to_admin_chat(self):
        await self.register_up_to_confirm()
        await self.feed(cb("confirm"))
        self.assertEqual(self.db.users[USER_ID]["status"], logic.ST_PENDING)
        self.assertTrue(self.session.sent_to(ADMIN_CHAT),
                        "карточка модерации не ушла в служебный чат")

    async def test_approve_from_group_chat_works(self):
        """Главная регрессия: раньше middleware отбрасывал всё непубличное,
        и нажатие «Одобрить» в группе модерации не доходило до обработчика."""
        await self.register_up_to_confirm()
        await self.feed(cb("confirm"))
        await self.feed(cb(f"approve:{USER_ID}", chat_id=ADMIN_CHAT,
                           user_id=ADMIN_ID, chat_type="supergroup"))
        self.assertEqual(self.db.users[USER_ID]["status"], logic.ST_APPROVED)
        self.assertEqual(self.db.users[USER_ID]["state"], logic.APPROVED)

    async def test_reject_from_group_chat_works(self):
        await self.register_up_to_confirm()
        await self.feed(cb("confirm"))
        await self.feed(cb(f"reject:{USER_ID}", chat_id=ADMIN_CHAT,
                           user_id=ADMIN_ID, chat_type="supergroup"))
        self.assertEqual(self.db.users[USER_ID]["status"], logic.ST_REJECTED)

    async def test_non_admin_cannot_approve(self):
        await self.register_up_to_confirm()
        await self.feed(cb("confirm"))
        await self.feed(cb(f"approve:{USER_ID}", chat_id=ADMIN_CHAT,
                           user_id=777777, chat_type="supergroup"))
        self.assertEqual(self.db.users[USER_ID]["status"], logic.ST_PENDING)

    async def test_second_moderator_click_is_noop(self):
        await self.register_up_to_confirm()
        await self.feed(cb("confirm"))
        first = cb(f"approve:{USER_ID}", chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                   chat_type="supergroup")
        await self.feed(first)
        notifications = len(self.session.sent_to(USER_ID))
        await self.feed(cb(f"reject:{USER_ID}", chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                           chat_type="supergroup"))
        self.assertEqual(self.db.users[USER_ID]["status"], logic.ST_APPROVED)
        self.assertEqual(len(self.session.sent_to(USER_ID)), notifications,
                         "второе решение не должно слать пользователю ещё одно письмо")

    async def test_group_chatter_ignored(self):
        await self.feed(msg("привет", chat_id=-100555, chat_type="supergroup"))
        self.assertEqual(self.session.sent(), [])
        self.assertEqual(self.db.users, {})

    async def test_moderation_callback_from_foreign_chat_ignored(self):
        await self.feed(cb(f"approve:{USER_ID}", chat_id=-100555,
                           user_id=ADMIN_ID, chat_type="supergroup"))
        self.assertEqual(self.session.sent(), [])

    async def test_unsubscribed_user_blocked_at_gate(self):
        self.session.subscribed = False
        await self.feed(msg("/start"))
        self.assertIn("не подписаны", " ".join(self.session.sent()))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.NEW)

    async def test_duplicate_update_processed_once(self):
        await self.feed(msg("/start"))
        update = msg("Иванов Иван Иванович")
        await self.feed(update)
        before = len(self.session.calls)
        await self.feed(update)                          # тот же update_id
        self.assertEqual(len(self.session.calls), before)

    async def test_restart_clears_scans(self):
        await self.register_up_to_confirm()
        await self.feed(cb("restart"))
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.WAIT_FIO)
        self.assertIsNone(row["doc_file_id"])
        self.assertIsNotNone(row["purge_after"])

    async def test_new_upload_clears_purge_deadline(self):
        """Регрессия: дата удаления от прошлой попытки оставалась на месте,
        и ретеншен сносил свежие сканы."""
        await self.register_up_to_confirm()
        await self.feed(cb("restart"))
        self.assertIsNotNone(self.db.users[USER_ID]["purge_after"])
        await self.feed(msg("Петров Пётр Петрович"))
        await self.feed(cb("oferta_ok"))
        await self.feed(msg(contact_user_id=USER_ID))
        await self.feed(msg(photo=True))
        self.assertIsNone(self.db.users[USER_ID]["purge_after"],
                          "дата удаления должна сбрасываться при новой загрузке")

    async def test_legacy_state_recovers(self):
        await self.feed(msg("/start"))
        self.db.users[USER_ID]["state"] = "wait_pdn"      # состояние старой версии
        await self.feed(msg("что-то"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_FIO)

    async def test_document_of_wrong_type_rejected(self):
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("oferta_ok"))
        await self.feed(msg(contact_user_id=USER_ID))
        await self.feed(msg("просто текст"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_DOC)

    async def test_every_update_is_finished(self):
        await self.register_up_to_confirm()
        self.assertEqual(self.db.claimed, self.db.finished,
                         "все обработанные апдейты должны закрываться")


def _async(value):
    async def _inner():
        return value
    return _inner()


if __name__ == "__main__":
    unittest.main()
