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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from aiogram import Bot, Dispatcher
    from aiogram.client.session.base import BaseSession
    from aiogram.exceptions import TelegramBadRequest
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
    from app import faq, logic, tasks, texts
    from app.config import Config
    from app.handlers import contract, menu, moderation, registration
    from app.handlers import faq as faq_handlers
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
# 17 с половиной лет на момент прогона: гарантированно несовершеннолетний,
# но уже старше 16. Абсолютной датой это не записать - тест состарился бы.
MINOR_BIRTH = (date.today() - timedelta(days=int(17.5 * 365))).strftime("%d.%m.%Y")
# Паспорт выдан год назад, то есть после его 14-летия: дата из взрослого
# набора ответов (2015) не прошла бы сверку дат и вернула бы на дату рождения.
MINOR_PASSPORT_DATE = (date.today() - timedelta(days=365)).strftime("%d.%m.%Y")
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
        # Чат, отправка фото в который должна падать: так проверяется
        # поведение бота, когда чек до оператора не дошёл.
        self.fail_photo_to: int | None = None

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs):    # pragma: no cover
        yield b"fake-image-bytes"

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if (isinstance(method, SendPhoto)
                and method.chat_id == self.fail_photo_to):
            raise TelegramBadRequest(method=method, message="chat not found")
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
            "parent_file_id": None, "parent_path": None, "parent_is_photo": True,
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
        self.events.append((tg_id, type_, payload or {}))

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

    async def user_by_support_message(self, chat_id, message_id):
        for row in self.users.values():
            if row.get("support_chat_id") == chat_id \
                    and row.get("support_message_id") == message_id:
                return dict(row)
        return None

    async def user_by_issue_message(self, chat_id, message_id):
        for row in self.users.values():
            if row.get("issue_chat_id") == chat_id \
                    and row.get("issue_message_id") == message_id:
                return dict(row)
        return None

    async def user_by_return_message(self, chat_id, message_id):
        for row in self.users.values():
            if row.get("return_chat_id") == chat_id \
                    and row.get("return_message_id") == message_id:
                return dict(row)
        return None

    async def rentals_of(self, tg_id, limit=10):
        """История аренд: закрытые аренды живут событиями, новые сверху."""
        return [{"payload": payload, "created_at": None}
                for who, type_, payload in reversed(self.events)
                if who == tg_id and type_ == "rental_closed"][:limit]

    async def clear_anketa(self, tg_id):
        self.users[tg_id]["anketa_enc"] = None


APP_DIR = Path(__file__).resolve().parent.parent / "app"
TEMPLATE = APP_DIR / "contract_template.docx"


def make_config(**overrides) -> Config:
    base = dict(
        bot_token="123:abc", channel_id=CHANNEL_ID, admin_chat_id=ADMIN_CHAT,
        admins=(ADMIN_ID,), pg={}, storage_dir=Path("/tmp/kyc"),
        pdn_key=generate_key(), contract_chat_id=ADMIN_CHAT,
        fix_chat_id=FIX_CHAT, fix_topic_id=FIX_TOPIC, contract_template=TEMPLATE,
        act_in_template=APP_DIR / "act_priema_template.docx",
        act_out_template=APP_DIR / "act_vozvrata_template.docx",
        soglasie_template=APP_DIR / "soglasie_template.docx",
        pdn_policy_file=APP_DIR / "pdn_policy.docx",
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
    # menu перезагружается последним: он берёт кнопку и ответы из ветки
    # частых вопросов, и ссылки должны указывать на свежий модуль.
    for module in (contract, registration, moderation, faq_handlers, menu):
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
    dp.include_router(faq_handlers.router)
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
        # remove тоже подменяется: настоящий unlink в тесте не нужен, а факт
        # удаления проверяется - без него утечка файлов при переигранной
        # выдаче прошла бы мимо тестов.
        self._orig_remove = files.remove
        files.download = lambda bot, file_id, max_bytes: _async(b"bytes")
        files.store = lambda d, tg, slot, data: (
            Path(f"/tmp/{tg}-{slot}.{files.SLOT_EXT[slot]}"), "hash")
        files.remove = lambda path: True

    async def asyncTearDown(self):
        files.download, files.store = self._orig_download, self._orig_store
        files.remove = self._orig_remove
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
        await self.feed(cb("pdn_ok"))
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

    ISSUE_FORM = ("рама: 264022410703084\n"
                  "мотор: 240W25021406\n"
                  "модель: Truck+\n"
                  "срок: 03.08 - 10.08\n"
                  "оплата: 3000 qr")

    async def provide_issue(self, form: str | None = None):
        """Ответ оператора на приглашение выдачи - после него уходит договор."""
        prompt_id = self.db.users[USER_ID]["issue_message_id"]
        self.assertIsNotNone(prompt_id, "приглашение выдачи не отправлено")
        await self.feed(msg(form or self.ISSUE_FORM, chat_id=ADMIN_CHAT,
                            user_id=ADMIN_ID, chat_type="supergroup",
                            reply_to=prompt_id))

    async def approve_fully(self):
        await self.approve()
        await self.provide_issue()

    def confirm_pay(self):
        """Оператор нажимает «Оплата получена» на карточке оплаты."""
        return self.feed(cb(f"pay:{USER_ID}", chat_id=ADMIN_CHAT,
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
        await self.feed(cb("pdn_ok"))
        await self.feed(cb("oferta_ok"))
        await self.feed(msg(contact_user_id=USER_ID))
        await self.feed(msg("07.03.1990"))
        await self.feed(msg("гор. Казань"))
        await self.feed(msg("12345"))                    # не 10 цифр
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PASSPORT)
        self.assertIn("10 цифр", " ".join(self.session.sent()))

    async def test_under_sixteen_rejected_at_birth_date(self):
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("pdn_ok"))
        await self.feed(cb("oferta_ok"))
        await self.feed(msg(contact_user_id=USER_ID))
        await self.feed(msg("01.01.2020"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_BIRTH)
        self.assertIn("16 лет", " ".join(self.session.sent()))

    async def register_minor_up_to_doc(self):
        """Регистрация 17-летнего до шага документа включительно."""
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("pdn_ok"))
        await self.feed(cb("oferta_ok"))
        await self.feed(msg(contact_user_id=USER_ID))
        answers = (MINOR_BIRTH,) + ANKETA_ANSWERS[1:3] \
            + (MINOR_PASSPORT_DATE,) + ANKETA_ANSWERS[4:]
        for answer in answers:
            await self.feed(msg(answer))
        await self.feed(msg(photo=True))

    async def test_minor_asked_for_parent_consent_after_doc(self):
        await self.register_minor_up_to_doc()
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PARENT_CONSENT)
        self.assertIn("согласие", " ".join(self.session.sent()).lower())

    async def test_minor_reaches_confirm_after_parent_photo(self):
        await self.register_minor_up_to_doc()
        await self.feed(msg("а можно без этого?"))       # текст вместо фото
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PARENT_CONSENT)
        await self.feed(msg(photo=True))
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.CONFIRM)
        self.assertTrue(row["parent_file_id"])

    async def test_minor_card_carries_consent_and_warning(self):
        """Утверждающий обязан увидеть возраст и согласие ДО кнопки
        «Одобрить»: договор с 16-17-летним без согласия оспаривается целиком."""
        await self.register_minor_up_to_doc()
        await self.feed(msg(photo=True))
        await self.feed(cb("confirm"))
        captions = [m.caption or "" for m in self.session.sent_to(ADMIN_CHAT)
                    if not isinstance(m, SendMessage)]
        self.assertEqual(len(captions), 2, "ожидали фото согласия и карточку")
        self.assertIn("законного представителя", captions[0])
        self.assertIn("16–17", captions[1])

    async def test_adult_card_has_no_minor_warning(self):
        await self.submit()
        captions = [m.caption or "" for m in self.session.sent_to(ADMIN_CHAT)
                    if not isinstance(m, SendMessage)]
        self.assertEqual(len(captions), 1, "у взрослого - только карточка")
        self.assertNotIn("16–17", captions[0])

    async def test_minor_contract_contains_parent_clause(self):
        await self.register_minor_up_to_doc()
        await self.feed(msg(photo=True))
        await self.feed(cb("confirm"))
        await self.approve_fully()
        # Договор уходит пользователю docx-документом; сам факт выдачи
        # проверяется в тестах взрослого сценария, здесь - оговорка.
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.WAIT_SIGN)
        ctx = logic.contract_context(row, self.anketa(), number=row["contract_no"])
        self.assertEqual(ctx["minor_clause"], logic.MINOR_CLAUSE)

    async def test_same_address_button_copies_registration(self):
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("pdn_ok"))
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
        await self.feed(cb("pdn_ok"))
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
        await self.feed(cb("pdn_ok"))
        text = " ".join(self.session.sent())
        for expected in ("ФИО", "дату и место рождения", "паспортные данные",
                         "адреса регистрации", "телефон", "документа"):
            self.assertIn(expected, text)

    async def test_consent_screen_does_not_promise_removed_processing(self):
        """Селфи и распознавание убраны. Обещать в согласии обработку,
        которой нет, - такое же расхождение, как и обратное."""
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("pdn_ok"))
        text = " ".join(self.session.sent()).lower()
        for gone in ("селфи", "фотографию с этим документом", "распознавани"):
            self.assertNotIn(gone, text)

    async def test_foreign_contact_rejected(self):
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("pdn_ok"))
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
        await self.approve_fully()
        row = self.db.users[USER_ID]
        self.assertEqual(row["status"], logic.ST_APPROVED)
        # Одобрение не завершает историю: договор выдан, но ещё не подписан.
        self.assertEqual(row["state"], logic.WAIT_SIGN)
        self.assertEqual(row["contract_status"], logic.CT_ISSUED)

    async def test_approve_issues_contract_to_user(self):
        await self.submit()
        await self.approve_fully()
        to_user = [m for m in self.session.documents() if m.chat_id == USER_ID]
        self.assertTrue(to_user, "договор не отправлен пользователю")
        number = self.db.users[USER_ID]["contract_no"]
        self.assertRegex(number, r"^АВ-\d{4}-\d{6}$")
        # среди документов пользователю (политика, приложение, договор)
        # договор с номером обязан быть
        self.assertTrue(any(number in (m.caption or "") for m in to_user))

    async def test_sign_fixes_contract_in_topic(self):
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))

        row = self.db.users[USER_ID]
        # после договора человек не в меню, а на оплате: порядок
        # «подписание - оплата - получение»
        self.assertEqual(row["state"], logic.WAIT_PAYMENT)
        self.assertEqual(row["contract_status"], logic.CT_SIGNED)
        self.assertIsNotNone(row["contract_signed_at"])

        to_fix = [m for m in self.session.documents() if m.chat_id == FIX_CHAT]
        self.assertTrue(to_fix, "подписанный договор не ушёл в чат фиксации")
        self.assertEqual(to_fix[0].message_thread_id, FIX_TOPIC,
                         "договор должен попадать в подгруппу фиксации сдачи")
        self.assertIn(row["contract_sha256"], to_fix[0].caption)

    async def register_fully(self):
        """До состояния approved: заявка, одобрение, данные выдачи,
        подпись договора, подтверждение оплаты и Акт приёма-передачи."""
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        await self.confirm_pay()
        await self.feed(cb("act_sign"))

    async def test_fixation_form_sent_after_signing(self):
        """После подписи утверждающий получает форму фиксации: бот вписал
        свои данные, прочерки дозаполняются руками и пересылаются в тему."""
        await self.register_fully()
        forms = [m.text for m in self.session.sent_to(ADMIN_CHAT)
                 if isinstance(m, SendMessage) and (m.text or "").startswith("1. ФИО:")]
        self.assertEqual(len(forms), 1, "форма должна уйти ровно один раз")
        form = forms[0]
        self.assertIn("1. ФИО: Иванов Иван Иванович", form)
        self.assertIn("6. Номер телефона (основной): 89990000000", form)
        self.assertIn("7. Номер телефона 2: 89001112233", form)
        self.assertIn("11. Адрес прописки с квартирой в Казани: "
                      "г. Казань, ул. Баумана, д. 1, кв. 2", form)
        # данные выдачи оператора уже в форме - прочерков не осталось
        self.assertIn("2. Вин номер рамы: 264022410703084", form)
        self.assertIn("5. Сроки аренды: 03.08 - 10.08", form)
        self.assertIn("10. Сумма и способ оплаты: 3000 qr", form)
        self.assertIn("16. Подписка на тг: да", form)

    async def test_approve_sends_issue_prompt_not_contract(self):
        """«Одобрить» не выдаёт договор сразу: сначала оператор отвечает
        данными выдачи - без них в документах прочерки под ручку."""
        await self.submit()
        await self.approve()
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.PENDING)
        self.assertIsNotNone(row["issue_message_id"])
        # файл политики на шаге ознакомления - не договор, его не считаем
        contracts = [m for m in self.session.documents()
                     if "Политика" not in (m.caption or "")]
        self.assertEqual(len(contracts), 0,
                         "договор не должен уйти до данных выдачи")
        prompts = [m.text for m in self.session.sent_to(ADMIN_CHAT)
                   if isinstance(m, SendMessage) and "данными выдачи" in (m.text or "")]
        self.assertTrue(prompts, "приглашение выдачи не отправлено")
        self.assertIn("рама:", prompts[-1])

    async def test_issue_reply_delivers_contract_with_data(self):
        await self.submit()
        await self.approve_fully()
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.WAIT_SIGN)
        self.assertEqual(row["issue_data"]["vin_frame"], "264022410703084")
        self.assertEqual(row["issue_data"]["kit_akb"], "2")
        docs = self.session.documents()
        self.assertTrue(docs, "договор не ушёл клиенту")

    async def test_issue_delivers_soglasie_annex_too(self):
        """Вместе с договором уходит приложение - Согласие на обработку ПДн."""
        await self.submit()
        await self.approve_fully()
        row = self.db.users[USER_ID]
        self.assertIsNotNone(row["soglasie_sha256"])
        annexes = [m for m in self.session.documents()
                   if m.chat_id == USER_ID
                   and "Согласие на обработку" in (m.caption or "")]
        self.assertTrue(annexes, "приложение-согласие не ушло клиенту")
        # отпечатки у договора и приложения свои
        self.assertNotEqual(row["soglasie_sha256"], row["contract_sha256"])

    async def test_signed_soglasie_reaches_fix_chat(self):
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        to_fix = [m for m in self.session.documents()
                  if m.chat_id == FIX_CHAT
                  and "Согласие на обработку" in (m.caption or "")]
        self.assertTrue(to_fix, "подписанное согласие не ушло в чат фиксации")

    async def test_bad_issue_form_is_rejected_with_reason(self):
        await self.submit()
        await self.approve()
        await self.provide_issue("рама: 264\nмотор: 240\nсрок: 03.08 - 10.08")
        self.assertEqual(self.db.users[USER_ID]["state"], logic.PENDING,
                         "без оплаты договор выдаваться не должен")
        replies = " ".join(m.text or "" for m in self.session.sent_to(ADMIN_CHAT)
                           if isinstance(m, SendMessage))
        self.assertIn("Не хватает: оплата", replies)

    async def test_unknown_issue_key_is_named(self):
        await self.submit()
        await self.approve()
        await self.provide_issue(self.ISSUE_FORM + "\nколесо: 2")
        replies = " ".join(m.text or "" for m in self.session.sent_to(ADMIN_CHAT)
                           if isinstance(m, SendMessage))
        self.assertIn("Не понял строки: колесо", replies)

    async def test_contract_sign_leads_to_payment_then_act(self):
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.WAIT_PAYMENT)
        acts = [m for m in self.session.documents()
                if "Акт приёма-передачи" in (m.caption or "")]
        self.assertFalse(acts, "акт не должен уходить до подтверждения оплаты")
        # клиенту - сумма из данных выдачи, оператору - карточка с кнопкой
        to_user = [m.text for m in self.session.sent_to(USER_ID)
                   if isinstance(m, SendMessage)]
        self.assertTrue(any("3000 qr" in (t or "") for t in to_user),
                        "клиент не увидел сумму оплаты")
        self.assertIsNotNone(row["pay_message_id"], "карточка оплаты не ушла")

        await self.confirm_pay()
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.WAIT_ACT_SIGN)
        self.assertIsNotNone(row["pay_confirmed_at"])
        acts = [m for m in self.session.documents()
                if "Акт приёма-передачи" in (m.caption or "")]
        self.assertTrue(acts, "после оплаты акт приёма не ушёл клиенту")

    async def test_payment_stage_gives_the_link_and_a_button(self):
        """Оплата по расчётному счёту: клиент получает ссылку текстом
        (её видно на десктопе) и кнопкой (с телефона открывает банк)."""
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        prompt = [m for m in self.session.sent_to(USER_ID)
                  if isinstance(m, SendMessage) and "оплата аренды" in (m.text or "")][-1]
        self.assertIn("qr.nspk.ru", prompt.text)
        # «&» в параметрах ссылки обязан быть экранирован, иначе Telegram
        # отвергает сообщение целиком и клиент остаётся без реквизитов.
        self.assertIn("&amp;bank=", prompt.text)
        self.assertNotIn("?type=01&bank", prompt.text)
        buttons = [b for row in prompt.reply_markup.inline_keyboard for b in row]
        pay = [b for b in buttons if b.url]
        self.assertEqual(len(pay), 1, "нет кнопки оплаты")
        self.assertEqual(pay[0].url, self.cfg.pay_url)
        self.assertIn("paid", [b.callback_data for b in buttons if b.callback_data])

    async def test_lost_payment_message_is_resent_with_the_link(self):
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        await self.feed(msg("а куда платить-то?"))
        last = self.session.calls[-1]
        self.assertIn("qr.nspk.ru", last.text)
        self.assertTrue(any(b.url for row in last.reply_markup.inline_keyboard
                            for b in row))

    async def test_payment_link_comes_from_configuration(self):
        """Счёт сменился - меняется одна переменная окружения, а не код."""
        self.dp, self.bot, self.db, self.session, self.cfg, self.vault = build(
            make_config(pay_url="https://qr.nspk.ru/NEW?type=01&bank=1"))
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        prompt = [m for m in self.session.sent_to(USER_ID)
                  if isinstance(m, SendMessage) and "оплата аренды" in (m.text or "")][-1]
        self.assertIn("qr.nspk.ru/NEW", prompt.text)
        self.assertNotIn("BS1A0050", prompt.text)

    async def test_reissue_does_not_leave_files_behind(self):
        """Регрессия: оператор поправил вин-номер и ответил ещё раз - старый
        договор оставался на диске, а в базе его пути уже не было. Ретеншен
        такой файл не находит никогда: документ с паспортом лежал бы вечно."""
        stored: list[Path] = []
        removed: list[str] = []
        counter = [0]

        def fake_store(d, tg, slot, data):
            counter[0] += 1
            path = Path(f"/tmp/{tg}-{slot}-{counter[0]}.{files.SLOT_EXT[slot]}")
            stored.append(path)
            return path, "hash"

        files.store = fake_store
        files.remove = lambda p: (removed.append(str(p)), True)[1]
        try:
            await self.submit()
            await self.approve_fully()
            first = self.db.users[USER_ID]["contract_path"]
            first_sog = self.db.users[USER_ID]["soglasie_path"]
            await self.provide_issue(self.ISSUE_FORM.replace("Truck+", "Kugoo"))
        finally:
            files.store = lambda d, tg, slot, data: (
                Path(f"/tmp/{tg}-{slot}.{files.SLOT_EXT[slot]}"), "hash")
            files.remove = self._orig_remove

        row = self.db.users[USER_ID]
        self.assertNotEqual(row["contract_path"], first, "выдача не переигралась")
        self.assertIn(first, removed, "старый договор остался на диске")
        self.assertIn(first_sog, removed, "старое согласие осталось на диске")
        self.assertNotIn(row["contract_path"], removed, "удалили актуальный файл")

    async def test_receipt_photo_reaches_the_operator(self):
        """Регрессия: бот просил прислать чек «сюда в чат», а чек оседал
        в переписке с ботом, куда оператор не смотрит."""
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        card_id = self.db.users[USER_ID]["pay_message_id"]
        await self.feed(msg(photo=True))

        receipts = [m for m in self.session.sent_to(ADMIN_CHAT)
                    if isinstance(m, SendPhoto) and "Чек" in (m.caption or "")]
        self.assertEqual(len(receipts), 1, "чек не ушёл оператору")
        self.assertIn("3000 qr", receipts[0].caption, "не видно ожидаемой суммы")
        self.assertEqual(receipts[0].reply_to_message_id, card_id,
                         "чек должен лежать под карточкой оплаты")
        to_user = [m.text for m in self.session.sent_to(USER_ID)
                   if isinstance(m, SendMessage)]
        self.assertIn("Чек передали оператору", to_user[-1])
        # состояние не меняется: поступление подтверждает оператор
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PAYMENT)

    async def test_receipt_document_is_sent_as_a_document(self):
        """Чек из банка приходит файлом: sendPhoto с file_id документа
        Telegram отвергает, и чек не дошёл бы вовсе."""
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        await self.feed(msg(document=True))
        receipts = [m for m in self.session.sent_to(ADMIN_CHAT)
                    if isinstance(m, SendDocument) and "Чек" in (m.caption or "")]
        self.assertEqual(len(receipts), 1)

    async def test_undelivered_receipt_is_admitted_to_the_client(self):
        """Если чек не ушёл, человек обязан узнать: иначе он уверен,
        что оплату уже проверяют."""
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        self.session.fail_photo_to = ADMIN_CHAT
        await self.feed(msg(photo=True))
        self.session.fail_photo_to = None
        last = [m.text for m in self.session.sent_to(USER_ID)
                if isinstance(m, SendMessage)][-1]
        self.assertIn("Не получилось передать чек", last)

    async def test_broken_pay_url_does_not_kill_the_whole_message(self):
        """Кнопка с битым url - это отказ Telegram принять СООБЩЕНИЕ целиком,
        то есть клиент без реквизитов. Опечатка в PAY_URL стоит кнопки,
        но не сообщения."""
        self.dp, self.bot, self.db, self.session, self.cfg, self.vault = build(
            make_config(pay_url="qr.nspk.ru/без-схемы"))
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        prompt = [m for m in self.session.sent_to(USER_ID)
                  if isinstance(m, SendMessage) and "оплата аренды" in (m.text or "")][-1]
        buttons = [b for row in prompt.reply_markup.inline_keyboard for b in row]
        self.assertFalse([b for b in buttons if b.url], "кнопки с битым url быть не должно")
        self.assertIn("paid", [b.callback_data for b in buttons if b.callback_data])

    async def test_pay_confirm_from_non_admin_refused(self):
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        await self.feed(cb(f"pay:{USER_ID}", chat_id=ADMIN_CHAT,
                           user_id=ADMIN_ID + 1, chat_type="supergroup"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PAYMENT,
                         "оплату подтверждает только админ")

    async def test_paid_button_pings_operator_without_state_change(self):
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        await self.feed(cb("paid"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PAYMENT)
        nudges = [m.text for m in self.session.sent_to(ADMIN_CHAT)
                  if isinstance(m, SendMessage) and "сообщает об оплате" in (m.text or "")]
        self.assertTrue(nudges, "сигнал клиента об оплате не дошёл до оператора")

    async def test_issue_update_during_payment_keeps_payment_stage(self):
        """Новые данные выдачи, пока клиент на оплате, не должны
        перескакивать оплату и слать акт."""
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        await self.provide_issue(self.ISSUE_FORM.replace("3000 qr", "3400 нал"))
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.WAIT_PAYMENT)
        self.assertEqual(row["issue_data"]["rent_price"], "3400 нал")
        acts = [m for m in self.session.documents()
                if "Акт приёма-передачи" in (m.caption or "")]
        self.assertFalse(acts, "акт не должен уходить до подтверждения оплаты")

    async def test_changed_price_reaches_the_client(self):
        """Регрессия: сумму поменяли, пока клиент на оплате, — у него на экране
        осталась прежняя, и он заплатил бы её."""
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        await self.provide_issue(self.ISSUE_FORM.replace("3000 qr", "3400 нал"))
        to_user = [m.text for m in self.session.sent_to(USER_ID)
                   if isinstance(m, SendMessage)]
        self.assertTrue(any("3400 нал" in (t or "") for t in to_user),
                        "клиенту не сообщили новую сумму")

    async def test_same_price_does_not_spam_the_client(self):
        """Повтор тех же данных выдачи — не повод писать клиенту второй раз."""
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        before = len([m for m in self.session.sent_to(USER_ID)
                      if isinstance(m, SendMessage) and "3000 qr" in (m.text or "")])
        await self.provide_issue()
        after = len([m for m in self.session.sent_to(USER_ID)
                     if isinstance(m, SendMessage) and "3000 qr" in (m.text or "")])
        self.assertEqual(before, after)

    async def test_act_sign_fixes_and_invites_return(self):
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        await self.confirm_pay()
        await self.feed(cb("act_sign"))
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.APPROVED)
        self.assertIsNotNone(row["act_in_signed_at"])
        self.assertIsNotNone(row["act_in_sha256"])
        self.assertIsNotNone(row["return_message_id"],
                             "приглашение возврата не отправлено")
        to_fix = [m for m in self.session.documents()
                  if m.chat_id == FIX_CHAT and "Акт приёма" in (m.caption or "")]
        self.assertTrue(to_fix, "акт приёма не в чате фиксации")
        self.assertEqual(to_fix[0].message_thread_id, FIX_TOPIC)

    CLOSE_FORM = ("когда: 07.08\n"
                  "адрес: адоратского\n"
                  "принял: ирик\n"
                  "отзыв: оставил\n"
                  "рекомендации: все ок")

    async def provide_return(self, text=None):
        """Оператор отвечает формой закрытия - из неё акт и отчёт."""
        prompt_id = self.db.users[USER_ID]["return_message_id"]
        self.assertIsNotNone(prompt_id)
        await self.feed(msg(text or self.CLOSE_FORM, chat_id=ADMIN_CHAT,
                            user_id=ADMIN_ID, chat_type="supergroup",
                            reply_to=prompt_id))

    async def test_return_flow_closes_rental(self):
        await self.register_fully()
        await self.provide_return(self.CLOSE_FORM + "\nповреждения: царапина на крыле"
                                                    "\nремонт: 500")
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.WAIT_RETURN_SIGN)
        # замечания акта собираются из формы, а не из причины и отзывов
        self.assertIn("царапина на крыле", row["return_data"]["return_notes"])
        self.assertIn("500", row["return_data"]["return_notes"])
        await self.feed(cb("return_sign"))
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.APPROVED)
        self.assertIsNotNone(row["act_out_signed_at"])
        to_fix = [m for m in self.session.documents()
                  if m.chat_id == FIX_CHAT and "Акт возврата" in (m.caption or "")]
        self.assertTrue(to_fix, "акт возврата не в чате фиксации")

    # ─── закрытие аренды по запросу клиента и история ───

    async def request_close(self, reason="выхожу на основную работу"):
        await self.feed(msg(texts.BTN_CLOSE_RENT))
        await self.feed(msg(reason))

    async def test_client_can_request_closure(self):
        await self.register_fully()
        await self.feed(msg(texts.BTN_CLOSE_RENT))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_CLOSE_REASON)
        await self.feed(msg("выхожу на основную работу"))

        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.APPROVED, "после запроса - обратно в меню")
        self.assertEqual(row["close_reason"], "выхожу на основную работу")
        cards = [m.text for m in self.session.sent_to(ADMIN_CHAT)
                 if isinstance(m, SendMessage) and "Запрос на закрытие" in (m.text or "")]
        self.assertTrue(cards, "запрос не дошёл до оператора")
        self.assertIn("выхожу на основную работу", cards[-1])
        self.assertIn("принял:", cards[-1], "в запросе нет формы закрытия")
        # запрос перевязывает ответ оператора на себя: форму он пришлёт
        # этой карточке, а не приглашению возврата месячной давности
        self.assertIsNotNone(row["return_message_id"])
        await self.provide_return()
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_RETURN_SIGN)

    async def test_close_button_in_question_mode_starts_the_closure(self):
        """Кнопка, набранная посреди вопроса, обязана начинать закрытие,
        а не выкидывать в меню с просьбой нажать ещё раз."""
        await self.register_fully()
        await self.feed(msg("🆘 Поддержка"))
        await self.feed(msg(texts.BTN_CLOSE_RENT))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_CLOSE_REASON)
        await self.feed(msg("продал машину, велик не нужен"))
        self.assertEqual(self.db.users[USER_ID]["close_reason"],
                         "продал машину, велик не нужен")
        self.assertIsNone(self.db.users[USER_ID].get("support_message_id"),
                          "причина не должна уехать вопросом в поддержку")

    async def test_menu_button_while_asked_for_the_reason_is_not_a_reason(self):
        """Регрессия того же класса, что в поддержке: кнопка меню, набранная
        в ответ на «почему сдаёте», уехала бы причиной в отчёт."""
        await self.register_fully()
        await self.feed(msg(texts.BTN_CLOSE_RENT))
        await self.feed(msg("💰 Тарифы"))
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.APPROVED)
        self.assertIsNone(row.get("close_reason"), "тариф стал причиной сдачи")
        joined = " ".join(self.session.sent()).replace("\xa0", " ")
        self.assertIn("11 000", joined)

    async def test_support_button_while_asked_for_the_reason_opens_support(self):
        await self.register_fully()
        await self.feed(msg(texts.BTN_CLOSE_RENT))
        await self.feed(msg("🆘 Поддержка"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_SUPPORT)
        self.assertIsNone(self.db.users[USER_ID].get("close_reason"))

    async def test_closure_without_active_rental_is_refused(self):
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        await self.confirm_pay()
        await self.feed(cb("act_mistake"))          # акт ещё не подписан
        self.db.users[USER_ID]["state"] = logic.APPROVED
        await self.feed(msg(texts.BTN_CLOSE_RENT))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.APPROVED)
        last = [m.text for m in self.session.sent_to(USER_ID)
                if isinstance(m, SendMessage)][-1]
        self.assertIn("Активной аренды", last)

    async def test_closure_report_matches_the_agreed_format(self):
        """Отчёт пересылают в таблицу: формат строк проверяется дословно."""
        await self.register_fully()
        await self.request_close("на осн работу выходит")
        await self.provide_return()

        reports = [m.text for m in self.session.sent_to(ADMIN_CHAT)
                   if isinstance(m, SendMessage) and (m.text or "").startswith("Когда сдал:")]
        self.assertEqual(len(reports), 1, "отчёт должен уйти ровно один раз")
        self.assertEqual(reports[0].splitlines(), [
            "Когда сдал: 07.08",
            "Сколько оплатил долгов: 0",
            "Какие повреждения есть: 0",
            "Сколько оплатил за ремонт: 0",
            "Оплатил мойку велосипеда: 0",
            "Причина сдачи: на осн работу выходит",
            "Адрес сдачи: адоратского",
            "Кто принял велик: ирик",
            "Оставил отзыв: оставил",
            "Какие рекомендации по улучшению сервиса/вело дали: все ок",
            "1. ФИО: Иванов Иван Иванович",
            "2. Вин номер рамы: 264022410703084",
            "3. Вин номер мотор колеса: 240W25021406",
        ])

    async def test_closed_rental_appears_in_history(self):
        await self.register_fully()
        # пока аренда идёт - она в списке как активная
        await self.feed(msg("📋 Мои аренды"))
        active = [m.text for m in self.session.sent_to(USER_ID)
                  if isinstance(m, SendMessage)][-1]
        self.assertIn("сейчас в аренде", active)
        self.assertIn(self.db.users[USER_ID]["contract_no"], active)

        await self.request_close()
        await self.provide_return()
        await self.feed(cb("return_sign"))
        await self.feed(msg("📋 Мои аренды"))
        closed = [m.text for m in self.session.sent_to(USER_ID)
                  if isinstance(m, SendMessage)][-1]
        self.assertIn("закрыта 07.08", closed)
        self.assertNotIn("сейчас в аренде", closed)

    async def test_history_is_empty_for_a_fresh_client(self):
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        await self.confirm_pay()
        await self.feed(cb("act_sign"))
        self.db.events.clear()
        self.db.users[USER_ID]["act_in_signed_at"] = None
        await self.feed(msg("📋 Мои аренды"))
        last = [m.text for m in self.session.sent_to(USER_ID)
                if isinstance(m, SendMessage)][-1]
        self.assertIn("Аренд пока не было", last)

    async def test_bad_closure_form_is_rejected_with_reason(self):
        await self.register_fully()
        await self.request_close()
        await self.provide_return("когда: 07.08\nпринял: ирик")   # без адреса
        self.assertEqual(self.db.users[USER_ID]["state"], logic.APPROVED,
                         "без адреса сдачи акт выпускать нельзя")
        replies = " ".join(m.text or "" for m in self.session.sent_to(ADMIN_CHAT)
                           if isinstance(m, SendMessage))
        self.assertIn("Не хватает: адрес", replies)

    async def test_return_before_act_signed_is_refused(self):
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        await self.confirm_pay()
        # приглашения возврата ещё нет - но проверим и явный путь: подделаем
        self.db.users[USER_ID]["return_chat_id"] = ADMIN_CHAT
        self.db.users[USER_ID]["return_message_id"] = 424242
        await self.feed(msg("Без замечаний", chat_id=ADMIN_CHAT,
                            user_id=ADMIN_ID, chat_type="supergroup",
                            reply_to=424242))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_ACT_SIGN,
                         "возврат до подписи акта приёма не должен проходить")

    async def test_consent_screen_has_no_oferta(self):
        """Оферты больше нет: экран - чистое согласие на обработку ПДн."""
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("pdn_ok"))
        joined = " ".join(self.session.sent())
        self.assertIn("Согласие на обработку персональных данных", joined)
        self.assertNotIn("принимаете оферту", joined)

    async def test_policy_step_precedes_consent(self):
        """Отдельная галочка ознакомления: после ФИО - файл политики,
        согласие появляется только после «Ознакомлен(а)»."""
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.WAIT_PDN)
        policy_docs = [m for m in self.session.documents()
                       if "Политика" in (m.caption or "")]
        self.assertTrue(policy_docs, "файл политики не отправлен")
        joined = " ".join(self.session.sent())
        self.assertNotIn("Нажимая «Даю согласие»", joined,
                         "согласие не должно показываться до ознакомления")

        await self.feed(cb("pdn_ok"))
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.WAIT_OFERTA)
        self.assertIsNotNone(row["policy_ack_at"], "момент ознакомления не записан")
        self.assertEqual(row["policy_version"], "2026-01-15")

    async def test_policy_step_ignores_text_and_resends(self):
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(msg("ок, читать не буду"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PDN)
        # Считаем по имени файла, а не по подписи: подпись у повторной
        # отправки короткая, и по слову «Политика» она бы не нашлась.
        policy_docs = [m for m in self.session.documents()
                       if "politika" in (m.document.filename or "")]
        self.assertEqual(len(policy_docs), 2, "политика должна переотправиться")
        self.assertIn("нажмите", policy_docs[-1].caption,
                      "повтор должен звать нажать кнопку, а не пересказывать документ")
        self.assertLess(len(policy_docs[-1].caption), len(policy_docs[0].caption))

    async def test_tariffs_button_shows_prices(self):
        await self.register_fully()
        await self.feed(msg("💰 Тарифы"))
        joined = " ".join(self.session.sent()).replace("\xa0", " ")
        self.assertIn("11 000", joined)
        self.assertIn("Kugoo V3 Pro", joined)

    async def test_support_question_reaches_admin_chat(self):
        await self.register_fully()
        await self.feed(msg("🆘 Поддержка"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_SUPPORT)
        await self.feed(msg("Когда можно забрать велосипед?"))
        row = self.db.users[USER_ID]
        self.assertEqual(row["state"], logic.APPROVED, "после вопроса - обратно в меню")
        self.assertIsNotNone(row["support_message_id"])
        cards = [m.text for m in self.session.sent_to(ADMIN_CHAT)
                 if isinstance(m, SendMessage) and "поддержку" in (m.text or "")]
        self.assertTrue(cards, "карточка вопроса не дошла до чата модерации")
        self.assertIn("Когда можно забрать велосипед?", cards[-1])
        self.assertIn(str(USER_ID), cards[-1])

    async def test_support_reply_goes_back_to_user(self):
        await self.register_fully()
        await self.feed(msg("🆘 Поддержка"))
        await self.feed(msg("Когда можно забрать велосипед?"))
        card_id = self.db.users[USER_ID]["support_message_id"]
        await self.feed(msg("Завтра с 10 до 19", chat_id=ADMIN_CHAT,
                            user_id=ADMIN_ID, chat_type="supergroup",
                            reply_to=card_id))
        to_user = [m.text for m in self.session.sent_to(USER_ID)
                   if isinstance(m, SendMessage)]
        self.assertTrue(any("Завтра с 10 до 19" in (t or "") for t in to_user),
                        "ответ поддержки не дошёл до пользователя")

    async def test_support_reply_does_not_touch_application(self):
        """Ответ на карточку ВОПРОСА не должен превращаться в отказ по заявке."""
        await self.register_fully()
        await self.feed(msg("🆘 Поддержка"))
        await self.feed(msg("Вопрос про зарядку"))
        card_id = self.db.users[USER_ID]["support_message_id"]
        await self.feed(msg("Заряжайте дома", chat_id=ADMIN_CHAT,
                            user_id=ADMIN_ID, chat_type="supergroup",
                            reply_to=card_id))
        row = self.db.users[USER_ID]
        self.assertEqual(row["status"], logic.ST_APPROVED, "статус заявки не трогаем")
        self.assertEqual(row["state"], logic.APPROVED)

    async def test_support_cancel_returns_to_menu(self):
        await self.register_fully()
        await self.feed(msg("🆘 Поддержка"))
        await self.feed(msg("Отмена"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.APPROVED)
        self.assertIsNone(self.db.users[USER_ID].get("support_message_id"))

    async def test_start_escapes_support_state(self):
        """/start посреди вопроса - «передумал», а не вопрос «/start»."""
        await self.register_fully()
        await self.feed(msg("🆘 Поддержка"))
        await self.feed(msg("/start"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.APPROVED)

    async def test_menu_button_escapes_support_state(self):
        """Кнопка меню, набранная в режиме вопроса, - «передумал», а не вопрос:
        иначе человек молча оставался в режиме, и следующее сообщение
        неожиданно уезжало карточкой в чат модерации."""
        await self.register_fully()
        await self.feed(msg("🆘 Поддержка"))
        await self.feed(msg("💰 Тарифы"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.APPROVED)
        joined = " ".join(self.session.sent()).replace("\xa0", " ")
        self.assertIn("11 000", joined)
        self.assertIsNone(self.db.users[USER_ID].get("support_message_id"),
                          "кнопка не должна превращаться в вопрос")

    # ─── ветка частых вопросов ───

    async def test_faq_button_lists_topics(self):
        await self.register_fully()
        await self.feed(msg(faq.MENU_BUTTON))
        last = self.session.calls[-1]
        self.assertIn("Выберите тему", last.text)
        codes = [b[0].callback_data for b in last.reply_markup.inline_keyboard]
        self.assertIn("faq:ADDR", codes)
        # красные линии темой не предлагаются: по ним бот молчит
        self.assertNotIn("faq:DEBT", codes)

    async def test_faq_topic_answers_without_touching_operator(self):
        await self.register_fully()
        before = len(self.session.sent_to(ADMIN_CHAT))
        await self.feed(cb("faq:ADDR"))
        answer = self.session.sent_to(USER_ID)[-1].text
        self.assertIn("Адоратского", answer)
        self.assertIn("Павлюхина", answer)
        self.assertEqual(len(self.session.sent_to(ADMIN_CHAT)), before,
                         "простой вопрос не должен дёргать менеджера")
        self.assertEqual(self.db.users[USER_ID]["state"], logic.APPROVED)

    async def test_faq_topic_needing_human_opens_the_question_mode(self):
        """Возврат, забор, выкуп: после ответа нужны подробности, и человек
        должен оказаться в режиме вопроса, а не в ловушке меню."""
        await self.register_fully()
        await self.feed(cb("faq:RETURN"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_SUPPORT)
        texts_sent = [m.text for m in self.session.sent_to(USER_ID)
                      if isinstance(m, SendMessage)]
        self.assertIn("перерасчёт", texts_sent[-2])
        self.assertIn("передам менеджеру", texts_sent[-1])
        # и следующее сообщение уходит карточкой человеку
        await self.feed(msg("Сдам завтра в 12, велик целый"))
        cards = [m.text for m in self.session.sent_to(ADMIN_CHAT)
                 if isinstance(m, SendMessage) and "Вопрос в поддержку" in (m.text or "")]
        self.assertTrue(cards)
        self.assertIn("Сдам завтра", cards[-1])

    async def test_support_question_gets_instant_answer_and_topic_label(self):
        await self.register_fully()
        await self.feed(msg("🆘 Поддержка"))
        await self.feed(msg("а где вы находитесь?"))
        to_user = " ".join(m.text or "" for m in self.session.sent_to(USER_ID)
                           if isinstance(m, SendMessage))
        self.assertIn("Адоратского", to_user, "бот не ответил сам")
        card = [m.text for m in self.session.sent_to(ADMIN_CHAT)
                if isinstance(m, SendMessage) and "Вопрос в поддержку" in (m.text or "")][-1]
        self.assertIn("Адреса точек", card, "в карточке нет темы")
        self.assertIn("Бот уже ответил", card)

    async def test_red_line_question_gets_only_the_neutral_reply(self):
        """Долг, угон, суд: бот не пишет по теме ничего, а карточка уходит
        человеку с меткой - разбирать это должен он."""
        await self.register_fully()
        await self.feed(msg("🆘 Поддержка"))
        # Считаем только то, что ушло В ОТВЕТ на сам вопрос: раньше в чате
        # уже были и ссылка на оплату, и суммы - законно.
        mark = len(self.session.calls)
        await self.feed(msg("я просрочил оплату, нет денег"))
        to_user = " ".join(
            m.text or "" for m in self.session.calls[mark:]
            if isinstance(m, SendMessage) and m.chat_id == USER_ID)
        self.assertIn("Передал ваш вопрос менеджеру", to_user)
        for leak in ("qr.nspk", "3 000", "штраф", "оплат"):
            self.assertNotIn(leak, to_user, leak)
        card = [m.text for m in self.session.sent_to(ADMIN_CHAT)
                if isinstance(m, SendMessage) and "Вопрос в поддержку" in (m.text or "")][-1]
        self.assertIn("ДОЛГ", card)
        self.assertIn("Бот по теме не отвечал", card)

    async def test_renter_asking_the_price_gets_renewal_not_the_tariff_list(self):
        await self.register_fully()
        await self.feed(msg("🆘 Поддержка"))
        await self.feed(msg("сколько стоит?"))
        to_user = " ".join(m.text or "" for m in self.session.sent_to(USER_ID)
                           if isinstance(m, SendMessage))
        self.assertIn("qr.nspk.ru", to_user)
        self.assertIn("3000 qr", to_user, "тариф берётся из данных выдачи")

    async def test_faq_button_in_question_mode_cancels_it(self):
        """Кнопка, набранная посреди вопроса, - «передумал», как и остальные:
        иначе человек молча остаётся в режиме и его следующее сообщение
        уезжает карточкой в чат модерации."""
        await self.register_fully()
        await self.feed(msg("🆘 Поддержка"))
        await self.feed(msg(faq.MENU_BUTTON))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.APPROVED)
        self.assertIn("Выберите тему", self.session.calls[-1].text)

    async def test_support_button_twice_stays_in_support(self):
        await self.register_fully()
        await self.feed(msg("🆘 Поддержка"))
        await self.feed(msg("🆘 Поддержка"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_SUPPORT)
        await self.feed(msg("Вопрос после двойного нажатия"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.APPROVED)
        self.assertIsNotNone(self.db.users[USER_ID]["support_message_id"])

    async def test_signing_wipes_passport_data(self):
        """Анкета стирается после подписи АКТА, а не договора: паспортные
        данные печатаются ещё и в Акте приёма-передачи."""
        await self.submit()
        await self.approve_fully()
        self.assertIsNotNone(self.db.users[USER_ID]["anketa_enc"])
        await self.feed(cb("sign"))
        self.assertIsNotNone(self.db.users[USER_ID]["anketa_enc"],
                             "до подписи акта анкета ещё нужна")
        await self.confirm_pay()
        await self.feed(cb("act_sign"))
        self.assertIsNone(self.db.users[USER_ID]["anketa_enc"])
        # Реквизиты договора остаются: без них нечем доказать, что подписано.
        self.assertIsNotNone(self.db.users[USER_ID]["contract_sha256"])

    async def test_contract_mistake_restarts_anketa(self):
        await self.submit()
        await self.approve_fully()
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
        await self.feed(cb("pdn_ok"))
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

    async def test_config_urls_are_escaped_in_messages(self):
        """Все сообщения уходят с parse_mode=HTML. Ссылка из конфигурации
        с & (обычное дело для youtube) делает разметку невалидной, и Telegram
        отвергает сообщение целиком - человек не получает подписанный договор
        вообще, хотя подпись уже зафиксирована."""
        self.dp, self.bot, self.db, self.session, self.cfg, self.vault = build(
            make_config(video_url="https://youtu.be/x?si=a&t=10",
                        channel_url="https://t.me/c?a=1&b=2"))
        self._orig_download, self._orig_store = files.download, files.store
        files.download = lambda bot, file_id, max_bytes: _async(b"bytes")
        files.store = lambda d, tg, slot, data: (
            Path(f"/tmp/{tg}-{slot}.{files.SLOT_EXT[slot]}"), "hash")

        self.session.subscribed = False
        await self.feed(msg("/start"))
        gate = " ".join(self.session.sent())
        self.assertIn("&amp;", gate)
        self.assertNotIn("?a=1&b=2", gate)

        self.session.subscribed = True
        await self.submit()
        await self.approve_fully()
        await self.feed(cb("sign"))
        signed = " ".join(self.session.sent())
        self.assertIn("si=a&amp;t=10", signed)

    async def test_contract_date_is_frozen_at_issue(self):
        """Договор пересобирается при переотправке и при подписании. Дата
        в шапке обязана быть датой выдачи, а не «сегодня»: иначе подписанный
        экземпляр отличается от прочитанного, а сохранённый при выдаче
        отпечаток перестаёт соответствовать чему бы то ни было."""
        await self.submit()
        await self.approve_fully()
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

    async def test_reentering_the_same_phone_after_reject_is_allowed(self):
        """Возврат на шаг телефонов после отказа. Прошлый ответ хранится
        в анкете, и без исключения собственного поля бот отвергал его как
        дубликат самого себя - то есть верный ответ."""
        await self.submit()
        await self.feed(cb(f"reject:{USER_ID}", chat_id=ADMIN_CHAT,
                           user_id=ADMIN_ID, chat_type="supergroup"))
        await self.feed(cb(f"rj:{USER_ID}:phones", chat_id=ADMIN_CHAT,
                           user_id=ADMIN_ID, chat_type="supergroup"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PHONE2)

        # Тот же номер, что и был — человек уверен, что он правильный.
        await self.feed(msg("+7 900 111-22-33"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PHONE3,
                         "повторный ввод своего же номера должен приниматься")
        # А вот совпадение с основным номером по-прежнему отсекается.
        await self.feed(msg("+7 999 000-00-00"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_PHONE3)
        self.assertIn("уже указан", " ".join(self.session.sent()))

    async def test_lost_contract_is_resent_on_any_message(self):
        """Кнопки живут только на сообщении с договором. Потерял его - подписать
        нечем, и /start упирается сюда же: выхода из состояния нет."""
        await self.submit()
        await self.approve_fully()
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
        await self.feed(cb("pdn_ok"))
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
        await self.approve_fully()
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
        await self.feed(cb("pdn_ok"))
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
        self.db.users[USER_ID]["state"] = "wait_selfie"   # состояние старой версии
        await self.feed(msg("что-то"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_FIO)

    async def test_document_of_wrong_type_rejected(self):
        await self.feed(msg("/start"))
        await self.feed(msg("Иванов Иван Иванович"))
        await self.feed(cb("pdn_ok"))
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
