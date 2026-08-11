"""Шаги регистрации: ФИО → ознакомление с Политикой ПДн → согласие →
контакт → анкета для договора → фото документа → подтверждение."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import keyboards as kb
from .. import logic, tasks, texts
from ..config import Config
from ..db import Database, utcnow
from ..filters import StateIs
from ..services import files
from ..services.crypto import Vault

log = logging.getLogger(__name__)
router = Router(name="registration")


def _file_id(message: Message) -> str | None:
    if message.photo:
        return message.photo[-1].file_id      # последний размер - максимальный
    if message.document:
        return message.document.file_id
    return None


def _check_upload(message: Message) -> logic.Validation:
    if message.photo:
        return logic.validate_upload(True, None, message.photo[-1].file_size)
    doc = message.document
    return logic.validate_upload(False, doc.mime_type if doc else None,
                                 doc.file_size if doc else None)


async def send_file(bot: Bot, chat_id: int, file_id: str, is_photo: bool, *,
                    caption: str, reply_markup: Any = None) -> Any:
    """Переслать файл тем же способом, каким пользователь его прислал.

    file_id несёт в себе тип файла, и sendPhoto с file_id документа Telegram
    отвергает с 400. Пользователь, отправивший паспорт файлом (а так делают,
    чтобы не терять качество), получал бы после загрузки пустоту: состояние
    уже переехало на подтверждение, а сообщение с кнопками не ушло. Карточка
    утверждения не отправлялась по той же причине.
    """
    send = bot.send_photo if is_photo else bot.send_document
    return await send(chat_id, file_id, caption=caption, reply_markup=reply_markup)


async def send_doc(bot: Bot, chat_id: int, data: dict, *, caption: str,
                   reply_markup: Any = None) -> Any:
    return await send_file(bot, chat_id, data["doc_file_id"],
                           data.get("doc_is_photo", True),
                           caption=caption, reply_markup=reply_markup)


# ─────────────────────────── /start ───────────────────────────

async def send_welcome(answer) -> None:
    """Приветствие + вход в частые вопросы.

    Кнопка вопросов - отдельным сообщением: на приветствии стоит
    ReplyKeyboardRemove, а две разметки в одно сообщение Telegram
    не принимает. Ответы доступны ДО регистрации: ночному лиду нужны
    адрес и тарифы сейчас, а не после анкеты.
    """
    await answer(texts.WELCOME, reply_markup=kb.remove())
    await answer(texts.FAQ_ENTRY_HINT, reply_markup=kb.faq_entry())


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database, cfg: Config, user: dict) -> None:
    if user["status"] == logic.ST_APPROVED:
        # /start посреди вопроса в поддержку - это «передумал»: не вернуть
        # состояние - и следующее сообщение молча уедет карточкой в чат
        # модерации, хотя человек уже смотрит на меню.
        if user["state"] == logic.WAIT_SUPPORT:
            await db.patch(user["tg_id"], expected_state=logic.WAIT_SUPPORT,
                           state=logic.APPROVED)
        await message.answer(texts.ALREADY_REGISTERED, reply_markup=kb.main_menu())
        return
    await db.patch(user["tg_id"], state=logic.WAIT_FIO)
    await send_welcome(message.answer)


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery, bot: Bot, db: Database,
                       user: dict) -> None:
    # Досюда доходят только подписанные: неподписанных разворачивает middleware.
    # Ответы идут через bot по tg_id, а не через callback.message: у старого
    # сообщения Telegram отдаёт недоступный объект без метода answer.
    await callback.answer("Подписка подтверждена")
    # Проверяется состояние, а не статус: между «заявку одобрили» и «договор
    # подписан» статус уже approved, и по нему человек с неподписанным
    # договором получал бы «вы уже зарегистрированы» вместе с меню.
    if user["state"] == logic.APPROVED:
        await bot.send_message(user["tg_id"], texts.ALREADY_REGISTERED,
                               reply_markup=kb.main_menu())
        return
    if user["state"] in (logic.NEW, logic.WAIT_FIO):
        await db.patch(user["tg_id"], state=logic.WAIT_FIO)
        # То же приветствие, что и на /start, - с кнопкой частых вопросов:
        # человек, прошедший проверку подписки, не должен получать урезанный
        # вариант старта.
        await send_welcome(lambda text, **kw: bot.send_message(
            user["tg_id"], text, **kw))


# ─────────────────────────── ФИО ───────────────────────────

@router.message(StateIs(logic.NEW))
async def st_new(message: Message, db: Database, user: dict) -> None:
    await db.patch(user["tg_id"], state=logic.WAIT_FIO)
    await send_welcome(message.answer)


@router.message(StateIs(logic.WAIT_FIO), F.text)
async def st_fio(message: Message, bot: Bot, db: Database, cfg: Config,
                 user: dict) -> None:
    result = logic.validate_fio(message.text)
    if not result.ok:
        await message.answer(result.error)
        return
    if not await db.patch(user["tg_id"], expected_state=logic.WAIT_FIO,
                          full_name=result.value, state=logic.WAIT_PDN):
        return
    await db.log_event(user["tg_id"], "fio_set")
    await send_policy(bot, cfg, user["tg_id"])


def _consent_text(cfg: Config, fio: str) -> str:
    return texts.CONSENT.format(
        fio=logic.esc(fio),
        purge_days=cfg.purge_approved_days,
    )


@router.message(StateIs(logic.WAIT_FIO))
async def st_fio_wrong(message: Message) -> None:
    await message.answer(texts.FIO_AS_TEXT)


# ─────────────── ознакомление с Политикой обработки ПДн ───────────────

async def send_policy(bot: Bot, cfg: Config, tg_id: int,
                      caption: str | None = None) -> None:
    """Экран ознакомления: файл политики с кнопкой «Ознакомлен(а)».

    Политика уходит документом как есть, без подстановок - это готовый
    файл оператора, и предоставлять его через бота требует её же п. 3.2.
    Файл читается с диска на каждый показ: он маленький, а кэш file_id
    пережил бы замену файла и продолжил слать старую редакцию.
    """
    try:
        data = cfg.pdn_policy_file.read_bytes()
    except OSError:
        log.warning("файл политики ПДн %s не читается - шаг работает "
                    "текстом без вложения", cfg.pdn_policy_file)
        await bot.send_message(tg_id, texts.POLICY_NO_FILE,
                               reply_markup=kb.policy_ack(cfg.pdn_url))
        return
    await bot.send_document(
        tg_id,
        BufferedInputFile(data, filename="politika-obrabotki-pdn.docx"),
        caption=caption or texts.POLICY_CAPTION,
        reply_markup=kb.policy_ack(cfg.pdn_url),
    )


@router.callback_query(StateIs(logic.WAIT_PDN), F.data == "pdn_ok")
async def cb_policy(callback: CallbackQuery, bot: Bot, db: Database, cfg: Config,
                    user: dict) -> None:
    if not await db.patch(user["tg_id"], expected_state=logic.WAIT_PDN,
                          state=logic.WAIT_OFERTA,
                          policy_version=cfg.pdn_version,
                          policy_ack_at=utcnow()):
        await callback.answer()
        return
    await db.log_event(user["tg_id"], "policy_acknowledged",
                       {"version": cfg.pdn_version})
    await callback.answer(texts.POLICY_ACK_TOAST)
    await bot.send_message(user["tg_id"],
                           _consent_text(cfg, user.get("full_name") or ""),
                           reply_markup=kb.consent(cfg.oferta_url, cfg.pdn_url))


@router.message(StateIs(logic.WAIT_PDN))
async def st_policy_wrong(message: Message, bot: Bot, cfg: Config,
                          user: dict) -> None:
    """Любое сообщение на шаге ознакомления возвращает политику с кнопкой:
    кнопка живёт на сообщении с файлом, и потерянное в ленте сообщение
    без переотправки становится тупиком. Подпись короткая: длинное описание
    человек уже видел, а повторять его на каждое «ок» незачем."""
    await send_policy(bot, cfg, user["tg_id"], texts.POLICY_PRESS_BUTTON)


# ──────────────────── согласие на обработку ПДн ────────────────────

@router.callback_query(StateIs(logic.WAIT_OFERTA), F.data == "oferta_ok")
async def cb_oferta(callback: CallbackQuery, bot: Bot, db: Database, cfg: Config,
                    user: dict) -> None:
    now = utcnow()
    # Колонки называются oferta_* исторически: сейчас это момент и редакция
    # согласия на обработку ПДн. Переименование колонок на живой базе
    # не окупает косметики. Одного «принял оферту» для этого мало.
    if not await db.patch(user["tg_id"], expected_state=logic.WAIT_OFERTA,
                          state=logic.WAIT_CONTACT,
                          oferta_version=cfg.oferta_version, oferta_accepted_at=now,
                          pdn_version=cfg.consent_version, pdn_consent_at=now):
        await callback.answer()
        return
    await db.log_event(user["tg_id"], "oferta_accepted",
                       {"version": cfg.oferta_version,
                        "consent_version": cfg.consent_version})
    await callback.answer(texts.CONSENT_GIVEN)
    await bot.send_message(user["tg_id"], texts.ASK_CONTACT,
                           reply_markup=kb.share_contact())


@router.message(StateIs(logic.WAIT_OFERTA))
async def st_oferta_wrong(message: Message) -> None:
    await message.answer(texts.CONSENT_PRESS_BUTTON)


# ─────────────────────────── контакт ───────────────────────────

@router.message(StateIs(logic.WAIT_CONTACT), F.contact)
async def st_contact(message: Message, db: Database, user: dict) -> None:
    contact = message.contact
    if not logic.contact_belongs_to_sender(contact.user_id, message.from_user.id):
        await message.answer(texts.CONTACT_FOREIGN)
        return
    phone = logic.normalize_phone(contact.phone_number) or contact.phone_number
    following = logic.next_state(logic.WAIT_CONTACT)
    if not await db.patch(user["tg_id"], expected_state=logic.WAIT_CONTACT,
                          phone=phone, state=following):
        return
    await db.log_event(user["tg_id"], "contact_set")
    await message.answer(texts.ANKETA_INTRO, reply_markup=kb.remove())
    await message.answer(PROMPTS[following])


@router.message(StateIs(logic.WAIT_CONTACT))
async def st_contact_wrong(message: Message) -> None:
    await message.answer(texts.CONTACT_USE_BUTTON)


# ─────────────────────────── анкета для договора ───────────────────────────

# Вопрос к каждому шагу. Словарь, а не поле в logic.Step: logic.py намеренно
# не знает про тексты, туда смотрят тесты без установленного окружения.
PROMPTS: dict[str, str] = {
    logic.WAIT_BIRTH: texts.ASK_BIRTH,
    logic.WAIT_BIRTH_PLACE: texts.ASK_BIRTH_PLACE,
    logic.WAIT_PASSPORT: texts.ASK_PASSPORT,
    logic.WAIT_PASSPORT_DATE: texts.ASK_PASSPORT_DATE,
    logic.WAIT_PASSPORT_CODE: texts.ASK_PASSPORT_CODE,
    logic.WAIT_PASSPORT_ISSUER: texts.ASK_PASSPORT_ISSUER,
    logic.WAIT_REG_ADDR: texts.ASK_REG_ADDR,
    logic.WAIT_LIVE_ADDR: texts.ASK_LIVE_ADDR,
    logic.WAIT_PHONE2: texts.ASK_PHONE2,
    logic.WAIT_PHONE3: texts.ASK_PHONE3,
    logic.WAIT_DOC: texts.ASK_DOC,
    logic.WAIT_PARENT_CONSENT: texts.ASK_PARENT_CONSENT,
}

SAME_ADDRESS_ANSWER = "совпадает с регистрацией"


def _markup_for(state: str) -> Any:
    """Клавиатура шага. У большинства её нет - только у тех, где кнопка
    экономит человеку ввод длинной строки."""
    if state == logic.WAIT_LIVE_ADDR:
        return kb.same_address()
    return kb.remove()


async def _advance(message: Message, bot: Bot, db: Database, vault: Vault,
                   user: dict, step: logic.Step, value: str) -> None:
    """Записать ответ шага и задать следующий вопрос.

    Анкета читается и пишется целиком: полей десяток, они лежат в одном
    зашифрованном столбце, и частичное обновление тут невозможно в принципе.
    Гонку закрывает expected_state - параллельный апдейт получит False
    и молча выйдет, не затерев соседнее поле.
    """
    anketa = vault.decrypt(user.get("anketa_enc"))
    anketa[step.field] = value
    following = logic.next_state(step.state)

    # Документ уже загружен - значит человек вернулся сюда после отказа
    # с причиной вроде «телефоны не подходят». Гонять его переснимать паспорт
    # незачем: шаг документа пропускается. Куда именно дальше, решает
    # state_after_doc: 16-17-летнему без фото согласия родителя - за ним,
    # остальным - сразу на подтверждение. purge_after при выходе на
    # подтверждение обязателен к сбросу - отказ поставил дату удаления
    # на три дня вперёд, и мимо st_doc её снять больше негде.
    if following == logic.WAIT_DOC and user.get("doc_file_id"):
        following = logic.state_after_doc(
            anketa, has_parent_consent=bool(user.get("parent_file_id")))
    to_confirm = following == logic.CONFIRM

    if not await db.patch(user["tg_id"], expected_state=step.state,
                          anketa_enc=vault.encrypt(anketa), state=following,
                          **({"purge_after": None} if to_confirm else {})):
        return

    if to_confirm:
        await send_confirm(bot, user)
        return
    await message.answer(PROMPTS[following], reply_markup=_markup_for(following))


@router.message(StateIs(*logic.ANKETA_BY_STATE), F.text)
async def st_anketa(message: Message, bot: Bot, db: Database, vault: Vault,
                    user: dict) -> None:
    """Один обработчик на все шаги анкеты.

    Десять почти одинаковых функций разъезжаются при первой же вставке поля
    в середину: какой-нибудь переход неизбежно остаётся указывать на старого
    соседа. Порядок и проверки лежат в таблице logic.ANKETA_STEPS.
    """
    step = logic.ANKETA_BY_STATE[user["state"]]
    anketa = vault.decrypt(user.get("anketa_enc"))

    if step.state == logic.WAIT_LIVE_ADDR and \
            message.text.strip().lower() == SAME_ADDRESS_ANSWER:
        await message.answer(texts.SAME_AS_REG)
        await _advance(message, bot, db, vault, user, step,
                       anketa.get("reg_address", ""))
        return

    if step.state in (logic.WAIT_PHONE2, logic.WAIT_PHONE3):
        # Занятыми считаются все номера, кроме того, который сейчас и вводится.
        # Без этого человек, вернувшийся на шаг телефонов после отказа, получал
        # «этот номер уже указан» на свой же прошлый ответ - то есть на верный.
        taken = [p for p in (logic.normalize_phone(user.get("phone")),
                             anketa.get("phone2"), anketa.get("phone3")) if p]
        own = anketa.get(step.field)
        result = step.validate(message.text, taken=[p for p in taken if p != own])
    else:
        result = step.validate(message.text)

    if not result.ok:
        await message.answer(result.error)
        return

    # Сверка двух дат возможна только когда известны обе, поэтому она живёт
    # здесь, а не в валидаторе одного поля.
    if step.state == logic.WAIT_PASSPORT_DATE and not logic.passport_date_consistent(
            {**anketa, "passport_date": result.value}):
        await message.answer(texts.PASSPORT_DATE_BEFORE_BIRTH)
        await db.patch(user["tg_id"], expected_state=step.state, state=logic.WAIT_BIRTH)
        await message.answer(PROMPTS[logic.WAIT_BIRTH])
        return

    await _advance(message, bot, db, vault, user, step, result.value)


@router.message(StateIs(*logic.ANKETA_BY_STATE))
async def st_anketa_wrong(message: Message) -> None:
    await message.answer(texts.ANKETA_AS_TEXT)


# ─────────────────────────── документ ───────────────────────────

@router.message(StateIs(logic.WAIT_DOC), F.photo | F.document)
async def st_doc(message: Message, bot: Bot, db: Database, cfg: Config,
                 vault: Vault, user: dict) -> None:
    check = _check_upload(message)
    if not check.ok:
        await message.answer(check.error)
        return
    file_id = _file_id(message)
    is_photo = bool(message.photo)
    # 16-17-летнего после документа ждёт ещё фото согласия родителя,
    # взрослого - сразу подтверждение.
    following = logic.state_after_doc(
        vault.decrypt(user.get("anketa_enc")),
        has_parent_consent=bool(user.get("parent_file_id")))
    # purge_after сбрасывается обязательно. «Заполнить повторно» и отказ
    # модератора ставят дату удаления в прошлое (или на 3 дня вперёд), и если
    # её не снять, ретеншен снесёт СВЕЖИЕ сканы вместе со старыми: пользователь
    # окажется в confirm без doc_file_id, а карточка модерации не отправится.
    if not await db.patch(user["tg_id"], expected_state=logic.WAIT_DOC,
                          doc_file_id=file_id, doc_is_photo=is_photo,
                          doc_path=None, doc_sha256=None,
                          purge_after=None, state=following):
        return
    await db.log_event(user["tg_id"], "doc_uploaded")
    if following == logic.CONFIRM:
        await send_confirm(bot, {**user, "doc_file_id": file_id,
                                 "doc_is_photo": is_photo})
    else:
        await message.answer(PROMPTS[following], reply_markup=kb.remove())
    # Скачивание и хэш - в фоне: пользователь не должен ждать сеть.
    tasks.spawn(_process_upload(bot, db, cfg, user["tg_id"], file_id, "doc"))


async def send_confirm(bot: Bot, data: dict) -> None:
    """Экран подтверждения: документ, введённые данные и кнопки."""
    await send_doc(
        bot, data["tg_id"], data,
        caption=texts.CONFIRM_CAPTION.format(
            fio=logic.esc(data["full_name"]),
            phone=logic.esc(str(data["phone"] or "").lstrip("+")),
        ),
        reply_markup=kb.confirm(),
    )


@router.message(StateIs(logic.WAIT_DOC))
async def st_doc_wrong(message: Message) -> None:
    await message.answer(texts.DOC_NEED_PHOTO)


# ─────────────────── согласие родителя (16-17 лет) ───────────────────

@router.message(StateIs(logic.WAIT_PARENT_CONSENT), F.photo | F.document)
async def st_parent(message: Message, bot: Bot, db: Database, cfg: Config,
                    user: dict) -> None:
    check = _check_upload(message)
    if not check.ok:
        await message.answer(check.error)
        return
    # Скан паспорта мог уйти под ретеншен, пока человек ходил за согласием:
    # отказ ставит purge_after на три дня, а согласие родителя бывает
    # и дольше. Подтверждение без документа отправить нельзя - сначала
    # возвращаем на шаг документа, согласие примем следом.
    if not user.get("doc_file_id"):
        await db.patch(user["tg_id"], expected_state=logic.WAIT_PARENT_CONSENT,
                       state=logic.WAIT_DOC)
        await message.answer(PROMPTS[logic.WAIT_DOC])
        return
    file_id = _file_id(message)
    is_photo = bool(message.photo)
    if not await db.patch(user["tg_id"], expected_state=logic.WAIT_PARENT_CONSENT,
                          parent_file_id=file_id, parent_is_photo=is_photo,
                          parent_path=None, parent_sha256=None,
                          purge_after=None, state=logic.CONFIRM):
        return
    await db.log_event(user["tg_id"], "parent_consent_uploaded")
    await send_confirm(bot, user)
    tasks.spawn(_process_upload(bot, db, cfg, user["tg_id"], file_id, "parent"))


@router.message(StateIs(logic.WAIT_PARENT_CONSENT))
async def st_parent_wrong(message: Message) -> None:
    await message.answer(texts.PARENT_NEED_PHOTO)


# ─────────────────────────── подтверждение ───────────────────────────

@router.callback_query(StateIs(logic.CONFIRM), F.data == "restart")
async def cb_restart(callback: CallbackQuery, bot: Bot, db: Database, cfg: Config,
                     user: dict) -> None:
    # Ссылки на старые сканы обнуляются, purge_after ставится в прошлое -
    # ретеншен подберёт файлы и удалит их с диска. Анкета стирается вместе
    # с ними: «Заполнить повторно» означает и новые паспортные данные тоже,
    # а оставленная анкета молча уехала бы в договор старой.
    if not await db.patch(user["tg_id"], expected_state=logic.CONFIRM,
                          state=logic.WAIT_FIO, doc_file_id=None, doc_sha256=None,
                          parent_file_id=None, parent_sha256=None,
                          anketa_enc=None, purge_after=utcnow()):
        await callback.answer()
        return
    await db.log_event(user["tg_id"], "restart")
    await callback.answer(texts.RESTART_TOAST)
    await bot.send_message(user["tg_id"], texts.RESTART, reply_markup=kb.remove())


@router.callback_query(StateIs(logic.CONFIRM), F.data == "confirm")
async def cb_confirm(callback: CallbackQuery, bot: Bot, db: Database,
                     cfg: Config, vault: Vault, user: dict) -> None:
    if cfg.auto_approve:
        if not await db.patch(user["tg_id"], expected_state=logic.CONFIRM,
                              state=logic.APPROVED, status=logic.ST_APPROVED):
            await callback.answer()
            return
        await db.set_purge_after(user["tg_id"], cfg.purge_approved_days)
        await callback.answer("Готово")
        await bot.send_message(user["tg_id"],
                               texts.REGISTERED.format(video_url=logic.esc(cfg.video_url)),
                               reply_markup=kb.main_menu())
        return

    if not await db.patch(user["tg_id"], expected_state=logic.CONFIRM,
                          state=logic.PENDING, status=logic.ST_PENDING):
        await callback.answer()
        return
    await db.log_event(user["tg_id"], "submitted")
    await callback.answer(texts.SUBMITTED_TOAST)
    await bot.send_message(user["tg_id"], texts.SUBMITTED)
    # Если карточка не ушла (бот не в чате модерации, неверный ADMIN_CHAT_ID),
    # заявка становится невидимой: пользователь ждёт, модератор не знает.
    # Исключение наружу выпускать нельзя - пользователю уже сказано «отправлено».
    try:
        await send_moderation_card(bot, db, cfg, vault, user["tg_id"])
    except (TelegramAPIError, CardNotReady):
        log.exception("КАРТОЧКА МОДЕРАЦИИ НЕ ОТПРАВЛЕНА для %s - заявка невидима "
                      "для модераторов. Проверьте CONTRACT_CHAT_ID и то, что "
                      "владелец аккаунта нажал /start у бота: написать первым "
                      "в личку бот не может", user["tg_id"])
        await db.log_event(user["tg_id"], "moderation_card_failed")
        await bot.send_message(user["tg_id"], texts.SUBMIT_PROBLEM)


@router.message(StateIs(logic.CONFIRM))
async def st_confirm_wrong(message: Message) -> None:
    await message.answer(texts.CONFIRM_PRESS_BUTTON)


@router.message(StateIs(logic.PENDING))
async def st_pending(message: Message) -> None:
    await message.answer(texts.PENDING_WAIT)


# ─────────────────────────── фоновые задачи ───────────────────────────

class CardNotReady(Exception):
    """Нечего показывать модератору - отправлять карточку без документа нельзя."""


def anketa_lines(data: dict, anketa: dict) -> str:
    """Реквизиты будущего договора построчно, в том же порядке, что в договоре.

    Утверждающий сверяет карточку с фотографией документа глазами, и порядок
    полей обязан совпадать с порядком в договоре - иначе сверка превращается
    в поиск по списку.
    """
    ctx = logic.contract_context(data, anketa, number="")
    return "\n".join(
        f"{label}: <b>{logic.esc(ctx.get(field, '—'))}</b>"
        for field, label in logic.CONTRACT_LABELS
    )


async def send_moderation_card(bot: Bot, db: Database, cfg: Config, vault: Vault,
                               tg_id: int) -> None:
    """Карточка на утверждение договора.

    Уходит в cfg.contract_chat_id - личку того, кто утверждает договоры.
    Telegram не позволяет боту написать первым, поэтому владелец аккаунта
    обязан один раз нажать /start; пока этого не произошло, отправка падает
    с 403, и вызывающий обязан это обработать.
    """
    row = await db.get_user(tg_id)
    if row is None:
        raise CardNotReady(f"нет записи о пользователе {tg_id}")
    data = dict(row)
    # send_photo с None даёт ошибку валидации, а не TelegramAPIError, и она
    # пролетела бы мимо обработки в cb_confirm - уже после того, как
    # пользователю сказано «отправлено».
    if not data.get("doc_file_id"):
        raise CardNotReady(f"у {tg_id} нет doc_file_id")

    anketa = vault.decrypt(data.get("anketa_enc"))
    missing = logic.missing_anketa_fields(anketa)
    if missing:
        # Договор из неполной анкеты собрать нельзя, а карточка без части
        # реквизитов выглядит как полная - утвердят не глядя.
        raise CardNotReady(f"у {tg_id} не заполнено: {', '.join(missing)}")

    minor = logic.is_minor(anketa)
    if minor and not data.get("parent_file_id"):
        # Одобрить 16-17-летнего без согласия родителя нельзя, а карточка
        # без него выглядит как обычная взрослая заявка.
        raise CardNotReady(f"у {tg_id} (16-17 лет) нет фото согласия родителя")
    if minor:
        # Согласие уходит ПЕРЕД карточкой: утверждающий читает чат снизу
        # вверх от карточки с кнопками, и фото согласия оказывается прямо
        # над ней.
        await send_file(bot, cfg.contract_chat_id, data["parent_file_id"],
                        data.get("parent_is_photo", True),
                        caption=texts.PARENT_CARD_CAPTION.format(tg_id=tg_id))

    caption = texts.CONTRACT_CARD.format(
        number=logic.esc(data.get("contract_no") or "будет присвоен"),
        fields=anketa_lines(data, anketa),
        tg_id=tg_id,
    )
    if minor:
        caption += texts.CARD_MINOR_LINE
    sent = await send_doc(
        bot, cfg.contract_chat_id, data,
        caption=caption,
        reply_markup=kb.moderation(tg_id),
    )
    # Запоминаем, где лежит карточка: отказ «с указанием ошибок» пишется
    # ответом на неё, и найти пользователя надо по message_id, а не разбором
    # текста подписи.
    await db.patch(tg_id, mod_chat_id=sent.chat.id, mod_message_id=sent.message_id)


async def _process_upload(bot: Bot, db: Database, cfg: Config, tg_id: int,
                          file_id: str | None, slot: str) -> None:
    """Скачать присланный файл, положить на диск и посчитать хэш.

    Слот «doc» - скан документа, «parent» - согласие родителя; лежат
    и удаляются одинаково. Хэш у документа нужен для антифрода: один и тот же
    паспорт не должен проходить регистрацию с разных аккаунтов. Согласие
    на дубли не проверяется: одно и то же согласие у двух братьев - норма,
    а не фрод. Всё это в фоне - пользователь не должен ждать сеть,
    стоя на экране подтверждения.
    """
    try:
        if not file_id:
            return
        data = await files.download(bot, file_id, logic.MAX_UPLOAD_BYTES)
        path, digest = files.store(cfg.storage_dir, tg_id, slot, data)
        if slot == "parent":
            await db.patch(tg_id, parent_path=str(path), parent_sha256=digest)
            return
        await db.patch(tg_id, doc_path=str(path), doc_sha256=digest)

        duplicates = await db.count_duplicate_docs(tg_id, digest)
        if duplicates:
            await db.log_event(tg_id, "duplicate_document", {"count": duplicates})
            await bot.send_message(cfg.contract_chat_id, texts.ALERT_DUPLICATE.format(
                tg_id=tg_id, count=duplicates))
    except Exception:                                   # noqa: BLE001
        log.exception("обработка файла %s (%s) не удалась", tg_id, slot)
