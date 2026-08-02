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
        AnswerCallbackQuery,
        EditMessageCaption,
        EditMessageReplyMarkup,
        GetChatMember,
        GetMe,
        SendDocument,
        SendMessage,
        SendPhoto,
    )
    from aiogram.types import (
        CallbackQuery,
        Chat,
        ChatMemberLeft,
        ChatMemberMember,
        Contact,
        Document,
        Message,
        PhotoSize,
        Update,
        User,
    )

    # app.tasks тянет app.db, а тот - asyncpg, поэтому обе зависимости
    # проверяются одной попыткой: иначе набор падает на машине без asyncpg.
    from app import logic, tasks
    from app.config import Config
    from app.handlers import contract, menu, moderation, registration
    from app.middlewares import PipelineMiddleware
    from app.services import files
    from app.services.crypto import Vault, generate_key
    HAVE_AIOGRAM = True
except ImportError:                                    # pragma: no cover
    HAVE_AIOGRAM = False
    # Заглушка обязательна: FakeSession ниже наследуется от BaseSession
    # на уровне модуля, и без неё импорт падал с NameError вместо пропуска -
    # весь файл не собирался, хотя пропустить его тут и предполагается.
    BaseSession = object

USER_ID, CHAT_ID = 5001, 5001
ADMIN_ID, ADMIN_CHAT = 111, -1009876543210
CHANNEL_ID = -1001234567890
# Чат фиксации сдачи и номер темы в нём - туда уходит подписанный договор.
FIX_CHAT, FIX_TOPIC = -1005555555555, 42

# Ответы на шаги анкеты в порядке logic.ANKETA_STEPS.
ANKETA_ANSWERS = (
    "07.03.1990",
    "гор. Казань",
    "1234 567890",
    "01.02.2015",
    "160-002",
    "ОУФМС России по Респ. Татарстан",
    "г. Казань, ул. Баумана, д. 1, кв. 2",
    "г. Казань, ул. Кремлёвская, д. 5, кв. 9",
    "+7 900 111-22-33",
    "+7 900 444-55-66",
)


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
        if isinstance(method, (SendMessage, SendPhoto, SendDocument)):
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
            elif isinstance(m, (SendPhoto, SendDocument)):
                out.append(m.caption or "<файл>")
        return out

    def sent_to(self, chat_id: int) -> list:
        return [m for m in self.calls
                if isinstance(m, (SendMessage, SendPhoto, SendDocument))
                and m.chat_id == chat_id]

    def documents(self) -> list:
        return [m for m in self.calls if isinstance(m, SendDocument)]

    def last_markup(self):
        for m in reversed(self.calls):
            if isinstance(m, (SendMessage, SendPhoto, SendDocument)) and m.reply_markup:
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
            "doc_file_id": None, "doc_path": None, "doc_is_photo": True,
            "purge_after": None, "anketa_enc": None,
            "contract_no": None, "contract_path": None, "contract_sha256": None,
            "contract_status": logic.CT_NONE, "contract_issued_at": None,
            "contract_signed_at": None, "mod_chat_id": None, "mod_message_id": None,
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

    async def next_contract_seq(self):
        self.contract_seq = getattr(self, "contract_seq", 0) + 1
        return self.contract_seq

    async def user_by_mod_message(self, chat_id, message_id):
        for row in self.users.values():
            if row.get("mod_chat_id") == chat_id and row.get("mod_message_id") == message_id:
                return dict(row)
        return None

    async def clear_anketa(self, tg_id):
        self.users[tg_id]["anketa_enc"] = None


TEMPLATE = Path(__file__).resolve().parent.parent / "app" / "contract_template.md"


def make_config(**overrides) -> Config:
    base = dict(
        bot_token="123:abc", channel_id=CHANNEL_ID, admin_chat_id=ADMIN_CHAT,
        admins=(ADMIN_ID,), pg={}, storage_dir=Path("/tmp/kyc"),
        pdn_key=generate_key(), contract_chat_id=ADMIN_CHAT,
        fix_chat_id=FIX_CHAT, fix_topic_id=FIX_TOPIC, contract_template=TEMPLATE,
        channel_url="https://t.me/test", oferta_url="https://e.ru/o",
        oferta_version="2026-01-15", pdn_url="", pdn_version="2026-01-15",
        video_url="https://e.ru/v",
        purge_approved_days=90, purge_rejected_days=3, updates_log_days=7,
        # Рейт-лимит здесь снят намеренно: FakeDB не двигает окно, а один
        # сценарный тест прогоняет две полные регистрации подряд. Сами пороги
        # проверяются в test_logic, где для этого не нужен Dispatcher.
        rate_soft=10_000, rate_hard=10_000,
    )
    base.update(overrides)
    return Config(**base)


# ─────────────────────────── сборка ───────────────────────────

def build(cfg: Config | None = None):
    # Router - объект уровня модуля, и aiogram запрещает подключать его
    # ко второму Dispatcher. Перезагружаем модули, чтобы каждый тест получил
    # собственные роутеры с теми же обработчиками.
    for module in (contract, registration, moderation, menu):
        importlib.reload(module)

    cfg = cfg or make_config()
    db = FakeDB()
    session = FakeSession()
    bot = Bot("123:abc", session=session)
    vault = Vault.from_raw(cfg.pdn_key)
    dp = Dispatcher()
    dp.update.outer_middleware(PipelineMiddleware(db, cfg, vault))
    dp.include_router(moderation.router)
    dp.include_router(contract.router)
    dp.include_router(registration.router)
    dp.include_router(menu.router)
    return dp, bot, db, session, cfg, vault


_seq = [0]


def _next_id() -> int:
    _seq[0] += 1
    return _seq[0]


def msg(text=None, *, chat_id=CHAT_ID, user_id=USER_ID, chat_type="private",
        photo=False, document=False, contact_user_id=None, reply_to=None,
        reply_from_bot=True) -> Update:
    kwargs = {}
    if document:
        kwargs["document"] = Document(file_id="d1", file_unique_id="du1",
                                      file_name="passport.jpg",
                                      mime_type="image/jpeg", file_size=2000)
    if photo:
        kwargs["photo"] = [PhotoSize(file_id="f1", file_unique_id="u1",
                                     width=100, height=100, file_size=1000)]
    if contact_user_id is not None:
        kwargs["contact"] = Contact(phone_number="79990000000", first_name="U",
                                    user_id=contact_user_id)
    if reply_to is not None:
        # Автор отвечаемого сообщения важен: карточку присылает бот, а ответ
        # модератора коллеге бот обязан пропустить молча.
        author = (User(id=1, is_bot=True, first_name="bot") if reply_from_bot
                  else User(id=user_id + 1, is_bot=False, first_name="admin2"))
        kwargs["reply_to_message"] = Message(
            message_id=reply_to, date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type=chat_type), from_user=author,
        )
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
    """Дать фоновым задачам (скачивание, хэш) доработать."""
    for _ in range(5):
        await asyncio.sleep(0)
    await tasks.drain(timeout=2)


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestFlow(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.dp, self.bot, self.db, self.session, self.cfg, self.vault = build()
        # скачивание и укладка файлов - не предмет этого теста
        self._orig_download, self._orig_store = files.download, files.store
        files.download = lambda bot, file_id, max_bytes: _async(b"bytes")
        files.store = lambda d, tg, slot, data: (
            Path(f"/tmp/{tg}-{slot}.{files.SLOT_EXT[slot]}"), "hash")

    async def asyncTearDown(self):
        files.download, files.store = self._orig_download, self._orig_store
        await self.bot.session.close()

    async def feed(self, update: Update):
        await self.dp.feed_update(self.bot, update)
        await settle()

    def anketa(self, tg_id: int = USER_ID) -> dict:
        return self.vault.decrypt(self.db.users[tg_id]["anketa_enc"])

    async def fill_anketa(self):
        for answer in ANKETA_ANSWERS:
            await self.feed(msg(answer))

    async def register_up_to_confirm(self):
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("oferta_ok"))
        await self.feed(msg(contact_user_id=USER_ID))
        await self.fill_anketa()
        await self.feed(msg(photo=True))

    async def submit(self):
        await self.register_up_to_confirm()
        await self.feed(cb("confirm"))

    def approve(self):
        return self.feed(cb(f"approve:{USER_ID}", chat_id=ADMIN_CHAT,
                            user_id=ADMIN_ID, chat_type="supergroup"))

    # ─── сам сценарий ───

    async def test_full_registration_reaches_confirm(self):
        await self.register_up_to_confirm()
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.CONFIRM)
        self.assertEqual(row["full_name"], "Иванов Иван Иванович")
        self.assertEqual(row["phone"], "+79990000000")

    async def test_anketa_collected_and_normalized(self):
        await self.register_up_to_confirm()
        anketa = self.anketa()
        self.assertEqual(anketa["passport_number"], "1234 567890")
        self.assertEqual(anketa["passport_code"], "160-002")
        # Телефоны приводятся к одному виду: в договоре три номера
        # не должны выглядеть по-разному.
        self.assertEqual(anketa["phone2"], "+79001112233")
        self.assertEqual(anketa["phone3"], "+79004445566")
        self.assertTrue(logic.anketa_complete(anketa))

    async def test_anketa_is_encrypted_at_rest(self):
        """Паспортные данные не должны читаться из строки, лежащей в базе."""
        await self.register_up_to_confirm()
        stored = self.db.users[USER_ID]["anketa_enc"]
        self.assertIsInstance(stored, str)
        for secret in ("1234 567890", "160-002", "Баумана"):
            self.assertNotIn(secret, stored)

    async def test_bad_passport_does_not_advance(self):
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("oferta_ok"))
        await self.feed(msg(contact_user_id=USER_ID))
        await self.feed(msg("07.03.1990"))
        await self.feed(msg("гор. Казань"))
        await self.feed(msg("12345"))                    # не 10 цифр
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PASSPORT)
        self.assertIn("10 цифр", " ".join(self.session.sent()))

    async def test_underage_rejected_at_birth_date(self):
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("oferta_ok"))
        await self.feed(msg(contact_user_id=USER_ID))
        await self.feed(msg("01.01.2020"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_BIRTH)
        self.assertIn("18 лет", " ".join(self.session.sent()))

    async def test_same_address_button_copies_registration(self):
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("oferta_ok"))
        await self.feed(msg(contact_user_id=USER_ID))
        for answer in ANKETA_ANSWERS[:7]:
            await self.feed(msg(answer))
        await self.feed(msg("Совпадает с регистрацией"))
        anketa = self.anketa()
        self.assertEqual(anketa["live_address"], anketa["reg_address"])
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PHONE2)

    async def test_duplicate_phone_rejected(self):
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("oferta_ok"))
        await self.feed(msg(contact_user_id=USER_ID))
        for answer in ANKETA_ANSWERS[:8]:
            await self.feed(msg(answer))
        await self.feed(msg("+7 999 000-00-00"))         # это основной номер
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PHONE2)
        self.assertIn("уже указан", " ".join(self.session.sent()))

    async def test_consent_screen_names_the_data(self):
        """Экран согласия - единственное место, где человеку перечисляют,
        что именно у него берут. Он обязан называть весь состав: анкета
        собирает заметно больше, чем ФИО и телефон."""
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        text = " ".join(self.session.sent())
        for expected in ("ФИО", "дату и место рождения", "паспортные данные",
                         "адреса регистрации", "телефон", "документа"):
            self.assertIn(expected, text)

    async def test_consent_screen_does_not_promise_removed_processing(self):
        """Селфи и распознавание убраны. Обещать в согласии обработку,
        которой нет, - такое же расхождение, как и обратное."""
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        text = " ".join(self.session.sent()).lower()
        for gone in ("селфи", "фотографию с этим документом", "распознавани"):
            self.assertNotIn(gone, text)

    async def test_foreign_contact_rejected(self):
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("oferta_ok"))
        await self.feed(msg(contact_user_id=999999))     # чужой контакт
        self.assertIsNone(self.db.users[USER_ID]["phone"])
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_CONTACT)

    async def test_moderation_card_goes_to_admin_chat(self):
        await self.submit()
        self.assertEqual(self.db.users[USER_ID]["status"], logic.ST_PENDING)
        self.assertTrue(self.session.sent_to(ADMIN_CHAT),
                        "карточка модерации не ушла в служебный чат")

    async def test_moderation_card_lists_contract_fields(self):
        """Утверждающий сверяет карточку с документом глазами - в ней должны
        быть все реквизиты будущего договора."""
        await self.submit()
        card = " ".join(m.caption or "" for m in self.session.sent_to(ADMIN_CHAT))
        for expected in ("1234 567890", "160-002", "Баумана", "+79001112233"):
            self.assertIn(expected, card)

    async def test_approve_from_group_chat_works(self):
        """Главная регрессия: раньше middleware отбрасывал всё непубличное,
        и нажатие «Одобрить» в группе модерации не доходило до обработчика."""
        await self.submit()
        await self.approve()
        row = self.db.users[USER_ID]
        self.assertEqual(row["status"], logic.ST_APPROVED)
        # Одобрение не завершает историю: договор выдан, но ещё не подписан.
        self.assertEqual(row["state"], logic.WAIT_SIGN)
        self.assertEqual(row["contract_status"], logic.CT_ISSUED)

    async def test_approve_issues_contract_to_user(self):
        await self.submit()
        await self.approve()
        to_user = [m for m in self.session.documents() if m.chat_id == USER_ID]
        self.assertTrue(to_user, "договор не отправлен пользователю")
        number = self.db.users[USER_ID]["contract_no"]
        self.assertRegex(number, r"^АВ-\d{4}-\d{6}$")
        self.assertIn(number, to_user[0].caption)

    async def test_sign_fixes_contract_in_topic(self):
        await self.submit()
        await self.approve()
        await self.feed(cb("sign"))

        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.APPROVED)
        self.assertEqual(row["contract_status"], logic.CT_SIGNED)
        self.assertIsNotNone(row["contract_signed_at"])

        to_fix = [m for m in self.session.documents() if m.chat_id == FIX_CHAT]
        self.assertTrue(to_fix, "подписанный договор не ушёл в чат фиксации")
        self.assertEqual(to_fix[0].message_thread_id, FIX_TOPIC,
                         "договор должен попадать в подгруппу фиксации сдачи")
        self.assertIn(row["contract_sha256"], to_fix[0].caption)

    async def test_signing_wipes_passport_data(self):
        """После подписи паспортные данные боту не нужны и стираются."""
        await self.submit()
        await self.approve()
        self.assertIsNotNone(self.db.users[USER_ID]["anketa_enc"])
        await self.feed(cb("sign"))
        self.assertIsNone(self.db.users[USER_ID]["anketa_enc"])
        # Реквизиты договора остаются: без них нечем доказать, что подписано.
        self.assertIsNotNone(self.db.users[USER_ID]["contract_sha256"])

    async def test_contract_mistake_restarts_anketa(self):
        await self.submit()
        await self.approve()
        await self.feed(cb("contract_mistake"))
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.WAIT_FIO)
        self.assertIsNone(row["anketa_enc"])

    async def test_reject_asks_for_reason_first(self):
        """«Отклонить» само по себе решение не принимает: без указания ошибки
        человек присылает то же самое второй раз."""
        await self.submit()
        await self.feed(cb(f"reject:{USER_ID}", chat_id=ADMIN_CHAT,
                           user_id=ADMIN_ID, chat_type="supergroup"))
        self.assertEqual(self.db.users[USER_ID]["status"], logic.ST_PENDING)

    async def test_reject_with_reason_returns_to_that_step(self):
        await self.submit()
        await self.feed(cb(f"reject:{USER_ID}", chat_id=ADMIN_CHAT,
                           user_id=ADMIN_ID, chat_type="supergroup"))
        await self.feed(cb(f"rj:{USER_ID}:doc", chat_id=ADMIN_CHAT,
                           user_id=ADMIN_ID, chat_type="supergroup"))
        row = self.db.users[USER_ID]
        self.assertEqual(row["status"], logic.ST_REJECTED)
        # Возврат на шаг документа, а не в начало анкеты: переигрывать
        # паспортные данные из-за нечитаемого фото незачем.
        self.assertEqual(row["state"], logic.WAIT_DOC)
        self.assertIn("документа", " ".join(self.session.sent()).lower())

    async def test_document_upload_is_sent_back_as_document(self):
        """Паспорт, присланный файлом, нельзя показать через sendPhoto: file_id
        несёт тип, и Telegram отвечает 400. Пользователь оставался бы после
        загрузки вообще без сообщения, а карточка утверждения не уходила."""
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("oferta_ok"))
        await self.feed(msg(contact_user_id=USER_ID))
        await self.fill_anketa()
        await self.feed(msg(document=True))

        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.CONFIRM)
        self.assertFalse(row["doc_is_photo"])
        to_user = self.session.sent_to(USER_ID)
        self.assertTrue(any(isinstance(m, SendDocument) for m in to_user),
                        "экран подтверждения должен уйти документом, а не фото")

        await self.feed(cb("confirm"))
        card = self.session.sent_to(ADMIN_CHAT)
        self.assertTrue(any(isinstance(m, SendDocument) for m in card),
                        "карточка утверждения тоже должна уйти документом")

    async def test_photo_upload_still_goes_as_photo(self):
        await self.register_up_to_confirm()
        self.assertTrue(self.db.users[USER_ID]["doc_is_photo"])
        to_user = self.session.sent_to(USER_ID)
        self.assertTrue(any(isinstance(m, SendPhoto) for m in to_user))

    async def test_fixing_phones_does_not_ask_for_document_again(self):
        """Отказ по телефонам возвращает на шаг телефонов. Гонять человека
        переснимать паспорт незачем - документ уже загружен и не менялся."""
        await self.submit()
        await self.feed(cb(f"reject:{USER_ID}", chat_id=ADMIN_CHAT,
                           user_id=ADMIN_ID, chat_type="supergroup"))
        await self.feed(cb(f"rj:{USER_ID}:phones", chat_id=ADMIN_CHAT,
                           user_id=ADMIN_ID, chat_type="supergroup"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PHONE2)

        await self.feed(msg("+7 900 777-11-22"))
        await self.feed(msg("+7 900 777-33-44"))
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.CONFIRM,
                         "после последнего телефона должно быть подтверждение")
        # Отказ поставил дату удаления на 3 дня вперёд, а шаг документа
        # пропущен - снять её больше негде.
        self.assertIsNone(row["purge_after"])
        self.assertEqual(self.anketa()["phone2"], "+79007771122")

    async def test_contract_date_is_frozen_at_issue(self):
        """Договор пересобирается при переотправке и при подписании. Дата
        в шапке обязана быть датой выдачи, а не «сегодня»: иначе подписанный
        экземпляр отличается от прочитанного, а сохранённый при выдаче
        отпечаток перестаёт соответствовать чему бы то ни было."""
        await self.submit()
        await self.approve()
        issued_at = self.db.users[USER_ID]["contract_issued_at"]
        self.assertIsNotNone(issued_at)

        await self.feed(msg("покажи ещё раз"))
        self.assertEqual(self.db.users[USER_ID]["contract_issued_at"], issued_at,
                         "переотправка не должна двигать дату выдачи")

        # Текст, собранный «завтра», обязан нести дату выдачи, а не завтрашнюю.
        data = dict(self.db.users[USER_ID])
        anketa = self.anketa()
        ctx = contract._context(self.cfg, data, anketa, number=data["contract_no"],
                                signed_at=contract.UNSIGNED, issued_at=issued_at)
        self.assertEqual(ctx["contract_date"], issued_at.strftime("%d.%m.%Y"))

    async def test_lost_contract_is_resent_on_any_message(self):
        """Кнопки живут только на сообщении с PDF. Потерял его - подписать
        нечем, и /start упирается сюда же: выхода из состояния нет."""
        await self.submit()
        await self.approve()
        before = len(self.session.documents())
        await self.feed(msg("а где договор?"))
        self.assertGreater(len(self.session.documents()), before,
                           "договор должен прийти заново")
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_SIGN)

    async def test_reupload_after_reject_clears_purge_deadline(self):
        """Отказ ставит дату удаления на 3 дня вперёд. Без сброса при новой
        загрузке ретеншен снёс бы свежий скан вместе со старым, и заявка ушла
        бы на утверждение без документа."""
        await self.submit()
        await self.feed(cb(f"reject:{USER_ID}", chat_id=ADMIN_CHAT,
                           user_id=ADMIN_ID, chat_type="supergroup"))
        await self.feed(cb(f"rj:{USER_ID}:doc", chat_id=ADMIN_CHAT,
                           user_id=ADMIN_ID, chat_type="supergroup"))
        self.assertIsNotNone(self.db.users[USER_ID]["purge_after"])
        await self.feed(msg(photo=True))                 # новое фото документа
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.CONFIRM)
        self.assertIsNone(row["purge_after"],
                          "дата удаления должна сбрасываться при новой загрузке")

    async def test_reject_by_reply_sends_moderator_text(self):
        await self.submit()
        card = self.db.users[USER_ID]["mod_message_id"]
        self.assertIsNotNone(card, "карточка модерации не запомнена")
        await self.feed(msg("Паспорт засвечен, переснимите при дневном свете",
                            chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                            chat_type="supergroup", reply_to=card))
        row = self.db.users[USER_ID]
        self.assertEqual(row["status"], logic.ST_REJECTED)
        self.assertIn("засвечен", row["reject_reason"])
        self.assertIn("засвечен", " ".join(self.session.sent()))

    async def test_user_reply_in_private_chat_is_not_swallowed(self):
        """Роутер модерации подключается первым. С фильтром «любой реплай»
        он ловил бы и обычного пользователя, ответившего на сообщение бота:
        человек посреди анкеты не получал бы ничего в ответ."""
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("oferta_ok"))
        await self.feed(msg(contact_user_id=USER_ID))
        before = len(self.session.sent())
        await self.feed(msg("07.03.1990", reply_to=1))    # ответ на сообщение бота
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_BIRTH_PLACE,
                         "ответ реплаем должен обрабатываться как обычный шаг анкеты")
        self.assertGreater(len(self.session.sent()), before)

    async def test_reply_to_another_admin_is_ignored(self):
        """Модераторы переписываются в том же чате. Без проверки автора бот
        вклинивался в каждый их разговор с «это сообщение не привязано
        к заявке» - служебный чат становился неюзабельным."""
        await self.submit()
        before = len(self.session.calls)
        await self.feed(msg("да, согласен", chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
                            chat_type="supergroup", reply_to=999,
                            reply_from_bot=False))
        self.assertEqual(len(self.session.calls), before,
                         "на реплай коллеге бот отвечать не должен")
        self.assertEqual(self.db.users[USER_ID]["status"], logic.ST_PENDING)

    async def test_reply_from_non_admin_ignored(self):
        await self.submit()
        card = self.db.users[USER_ID]["mod_message_id"]
        await self.feed(msg("отклоняю", chat_id=ADMIN_CHAT, user_id=777777,
                            chat_type="supergroup", reply_to=card))
        self.assertEqual(self.db.users[USER_ID]["status"], logic.ST_PENDING)

    async def test_non_admin_cannot_approve(self):
        await self.submit()
        await self.feed(cb(f"approve:{USER_ID}", chat_id=ADMIN_CHAT,
                           user_id=777777, chat_type="supergroup"))
        self.assertEqual(self.db.users[USER_ID]["status"], logic.ST_PENDING)

    async def test_second_moderator_click_is_noop(self):
        await self.submit()
        await self.approve()
        notifications = len(self.session.sent_to(USER_ID))
        await self.feed(cb(f"rj:{USER_ID}:doc", chat_id=ADMIN_CHAT, user_id=ADMIN_ID,
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
        await self.fill_anketa()
        await self.feed(msg(photo=True))
        self.assertIsNone(self.db.users[USER_ID]["purge_after"],
                          "дата удаления должна сбрасываться при новой загрузке")

    async def test_restart_clears_anketa(self):
        """«Заполнить повторно» означает и новые паспортные данные тоже:
        оставленная анкета молча уехала бы в договор старой."""
        await self.register_up_to_confirm()
        self.assertIsNotNone(self.db.users[USER_ID]["anketa_enc"])
        await self.feed(cb("restart"))
        self.assertIsNone(self.db.users[USER_ID]["anketa_enc"])

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
        await self.fill_anketa()
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
