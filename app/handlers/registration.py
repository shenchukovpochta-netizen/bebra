"""Шаги регистрации: ФИО → оферта → согласие на ПДн → контакт → документ →
селфи → подтверждение."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from .. import logic, tasks, texts
from ..config import Config
from ..db import Database, utcnow
from ..filters import StateIs
from ..services import files, ocr
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


# ─────────────────────────── /start ───────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, db: Database, cfg: Config, user: dict) -> None:
    if user["status"] == logic.ST_APPROVED:
        await message.answer(texts.ALREADY_REGISTERED, reply_markup=kb.main_menu())
        return
    await db.patch(user["tg_id"], state=logic.WAIT_FIO)
    await message.answer(texts.WELCOME, reply_markup=kb.remove())


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery, db: Database, user: dict) -> None:
    # Досюда доходят только подписанные: неподписанных разворачивает middleware.
    await callback.answer("Подписка подтверждена")
    if user["status"] == logic.ST_APPROVED:
        await callback.message.answer(texts.ALREADY_REGISTERED, reply_markup=kb.main_menu())
        return
    if user["state"] in (logic.NEW, logic.WAIT_FIO):
        await db.patch(user["tg_id"], state=logic.WAIT_FIO)
        await callback.message.answer(texts.WELCOME)


# ─────────────────────────── ФИО ───────────────────────────

@router.message(StateIs(logic.NEW))
async def st_new(message: Message, db: Database, user: dict) -> None:
    await db.patch(user["tg_id"], state=logic.WAIT_FIO)
    await message.answer(texts.WELCOME)


@router.message(StateIs(logic.WAIT_FIO), F.text)
async def st_fio(message: Message, db: Database, cfg: Config, user: dict) -> None:
    result = logic.validate_fio(message.text)
    if not result.ok:
        await message.answer(result.error)
        return
    if not await db.patch(user["tg_id"], expected_state=logic.WAIT_FIO,
                          full_name=result.value, state=logic.WAIT_OFERTA):
        return
    await db.log_event(user["tg_id"], "fio_set")
    await message.answer(_oferta_text(cfg, result.value),
                         reply_markup=kb.oferta(cfg.oferta_url, cfg.pdn_url))


def _oferta_text(cfg: Config, fio: str) -> str:
    processor_line = ""
    if cfg.ocr_enabled:
        processor_line = texts.OFERTA_PROCESSOR_LINE.format(
            processor=logic.esc(cfg.ocr_processor))
    return texts.OFERTA.format(
        fio=logic.esc(fio),
        processor_line=processor_line,
        purge_days=cfg.purge_approved_days,
    )


@router.message(StateIs(logic.WAIT_FIO))
async def st_fio_wrong(message: Message) -> None:
    await message.answer(texts.FIO_AS_TEXT)


# ─────────────────────────── оферта и ПДн ───────────────────────────

@router.callback_query(StateIs(logic.WAIT_OFERTA), F.data == "oferta_ok")
async def cb_oferta(callback: CallbackQuery, db: Database, cfg: Config, user: dict) -> None:
    now = utcnow()
    # Согласие на обработку ПДн живёт внутри оферты, но фиксируется отдельными
    # полями: если редакция оферты изменится, надо будет доказать, под какой
    # именно человек подписался. Одного «принял оферту» для этого мало.
    if not await db.patch(user["tg_id"], expected_state=logic.WAIT_OFERTA,
                          state=logic.WAIT_CONTACT,
                          oferta_version=cfg.oferta_version, oferta_accepted_at=now,
                          pdn_version=cfg.consent_version, pdn_consent_at=now):
        await callback.answer()
        return
    await db.log_event(user["tg_id"], "oferta_accepted",
                       {"version": cfg.oferta_version,
                        "consent_version": cfg.consent_version})
    await callback.answer(texts.OFERTA_ACCEPTED)
    await callback.message.answer(texts.ASK_CONTACT, reply_markup=kb.share_contact())


@router.message(StateIs(logic.WAIT_OFERTA))
async def st_oferta_wrong(message: Message) -> None:
    await message.answer(texts.OFERTA_PRESS_BUTTON)


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
}

SAME_ADDRESS_ANSWER = "совпадает с регистрацией"


def _markup_for(state: str) -> Any:
    """Клавиатура шага. У большинства её нет - только у тех, где кнопка
    экономит человеку ввод длинной строки."""
    if state == logic.WAIT_LIVE_ADDR:
        return kb.same_address()
    return kb.remove()


async def _advance(message: Message, db: Database, vault: Vault, user: dict,
                   step: logic.Step, value: str) -> None:
    """Записать ответ шага и задать следующий вопрос.

    Анкета читается и пишется целиком: полей десяток, они лежат в одном
    зашифрованном столбце, и частичное обновление тут невозможно в принципе.
    Гонку закрывает expected_state - параллельный апдейт получит False
    и молча выйдет, не затерев соседнее поле.
    """
    anketa = vault.decrypt(user.get("anketa_enc"))
    anketa[step.field] = value
    following = logic.next_state(step.state)
    if not await db.patch(user["tg_id"], expected_state=step.state,
                          anketa_enc=vault.encrypt(anketa), state=following):
        return
    await message.answer(PROMPTS[following], reply_markup=_markup_for(following))


@router.message(StateIs(*logic.ANKETA_BY_STATE), F.text)
async def st_anketa(message: Message, db: Database, vault: Vault, user: dict) -> None:
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
        await _advance(message, db, vault, user, step, anketa.get("reg_address", ""))
        return

    if step.state in (logic.WAIT_PHONE2, logic.WAIT_PHONE3):
        taken = [p for p in (logic.normalize_phone(user.get("phone")),
                             anketa.get("phone2")) if p]
        result = step.validate(message.text, taken=taken)
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

    await _advance(message, db, vault, user, step, result.value)


@router.message(StateIs(*logic.ANKETA_BY_STATE))
async def st_anketa_wrong(message: Message) -> None:
    await message.answer(texts.ANKETA_AS_TEXT)


# ─────────────────────────── документ ───────────────────────────

@router.message(StateIs(logic.WAIT_DOC), F.photo | F.document)
async def st_doc(message: Message, bot: Bot, db: Database, cfg: Config, user: dict) -> None:
    check = _check_upload(message)
    if not check.ok:
        await message.answer(check.error)
        return
    file_id = _file_id(message)
    # purge_after сбрасывается обязательно. «Заполнить повторно» и отказ
    # модератора ставят дату удаления в прошлое (или на 3 дня вперёд), и если
    # её не снять, ретеншен снесёт СВЕЖИЕ сканы вместе со старыми: пользователь
    # окажется в confirm без doc_file_id, а карточка модерации не отправится.
    if not await db.patch(user["tg_id"], expected_state=logic.WAIT_DOC,
                          doc_file_id=file_id, doc_path=None, doc_sha256=None,
                          doc_ocr=None, name_match=None, ocr_at=None,
                          purge_after=None, state=logic.WAIT_SELFIE):
        return
    await db.log_event(user["tg_id"], "doc_uploaded")
    await message.answer(texts.PROCESSING_AND_ASK_SELFIE)
    # Скачивание и OCR - в фоне: пользователь не должен ждать сеть.
    tasks.spawn(_process_doc(bot, db, cfg, user["tg_id"], file_id, user["full_name"]))


@router.message(StateIs(logic.WAIT_DOC))
async def st_doc_wrong(message: Message) -> None:
    await message.answer(texts.DOC_NEED_PHOTO)


# ─────────────────────────── селфи ───────────────────────────

@router.message(StateIs(logic.WAIT_SELFIE), F.photo | F.document)
async def st_selfie(message: Message, bot: Bot, db: Database, cfg: Config, user: dict) -> None:
    # При рассинхроне состояния sendPhoto с пустым file_id даёт 400,
    # и пользователь застрял бы в confirm вообще без сообщений.
    if not user["doc_file_id"]:
        await db.patch(user["tg_id"], state=logic.WAIT_DOC)
        await message.answer(texts.DOC_LOST)
        return

    check = _check_upload(message)
    if not check.ok:
        await message.answer(check.error)
        return

    file_id = _file_id(message)
    # purge_after сбрасывается по той же причине, что и в шаге документа:
    # отказ модератора ставит дату удаления на 3 дня вперёд, а отказ с причиной
    # «селфи не подходит» возвращает человека сюда, минуя шаг документа. Без
    # сброса ретеншен снёс бы фото паспорта прямо посреди исправления, и заявка
    # ушла бы на модерацию без документа.
    if not await db.patch(user["tg_id"], expected_state=logic.WAIT_SELFIE,
                          selfie_file_id=file_id, selfie_path=None,
                          selfie_sha256=None, purge_after=None,
                          state=logic.CONFIRM):
        return
    await db.log_event(user["tg_id"], "selfie_uploaded")
    await message.answer_photo(
        user["doc_file_id"],
        caption=texts.CONFIRM_CAPTION.format(
            fio=logic.esc(user["full_name"]),
            phone=logic.esc(str(user["phone"] or "").lstrip("+")),
        ),
        reply_markup=kb.confirm(),
    )
    tasks.spawn(_process_selfie(bot, db, cfg, user["tg_id"], file_id))


@router.message(StateIs(logic.WAIT_SELFIE))
async def st_selfie_wrong(message: Message) -> None:
    await message.answer(texts.SELFIE_NEED_PHOTO)


# ─────────────────────────── подтверждение ───────────────────────────

@router.callback_query(StateIs(logic.CONFIRM), F.data == "restart")
async def cb_restart(callback: CallbackQuery, db: Database, cfg: Config, user: dict) -> None:
    # Ссылки на старые сканы обнуляются, purge_after ставится в прошлое -
    # ретеншен подберёт файлы и удалит их с диска. Анкета стирается вместе
    # с ними: «Заполнить повторно» означает и новые паспортные данные тоже,
    # а оставленная анкета молча уехала бы в договор старой.
    if not await db.patch(user["tg_id"], expected_state=logic.CONFIRM,
                          state=logic.WAIT_FIO, doc_file_id=None, doc_sha256=None,
                          selfie_file_id=None, selfie_sha256=None,
                          doc_ocr=None, name_match=None, ocr_at=None,
                          anketa_enc=None, purge_after=utcnow()):
        await callback.answer()
        return
    await db.log_event(user["tg_id"], "restart")
    await callback.answer(texts.RESTART_TOAST)
    await callback.message.answer(texts.RESTART)


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
        await callback.message.answer(
            texts.REGISTERED.format(video_url=cfg.video_url), reply_markup=kb.main_menu())
        return

    if not await db.patch(user["tg_id"], expected_state=logic.CONFIRM,
                          state=logic.PENDING, status=logic.ST_PENDING):
        await callback.answer()
        return
    await db.log_event(user["tg_id"], "submitted")
    await callback.answer(texts.SUBMITTED_TOAST)
    await callback.message.answer(texts.SUBMITTED)
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
        await callback.message.answer(texts.SUBMIT_PROBLEM)


@router.message(StateIs(logic.CONFIRM))
async def st_confirm_wrong(message: Message) -> None:
    await message.answer(texts.CONFIRM_PRESS_BUTTON)


@router.message(StateIs(logic.PENDING))
async def st_pending(message: Message) -> None:
    await message.answer(texts.PENDING_WAIT)


# ─────────────────────────── фоновые задачи ───────────────────────────

def _ocr_summary(row: dict) -> str:
    if row.get("ocr_at") is None:
        return texts.OCR_PENDING
    score = row.get("name_match")
    if score is None:
        return texts.OCR_FAILED
    total = (row.get("doc_ocr") or {}).get("tokens_total", 0)
    matched = len((row.get("doc_ocr") or {}).get("matched", []))
    return texts.OCR_SCORE.format(percent=round(float(score) * 100), matched=matched, total=total)


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

    sent = await bot.send_photo(
        cfg.contract_chat_id,
        data["doc_file_id"],
        caption=texts.CONTRACT_CARD.format(
            number=logic.esc(data.get("contract_no") or "будет присвоен"),
            fields=anketa_lines(data, anketa),
            tg_id=tg_id,
            ocr=_ocr_summary(data),
        ),
        reply_markup=kb.moderation(tg_id),
    )
    # Запоминаем, где лежит карточка: отказ «с указанием ошибок» пишется
    # ответом на неё, и найти пользователя надо по message_id, а не разбором
    # текста подписи.
    await db.patch(tg_id, mod_chat_id=sent.chat.id, mod_message_id=sent.message_id)
    if data["selfie_file_id"]:
        await bot.send_photo(cfg.contract_chat_id, data["selfie_file_id"],
                             caption=texts.MOD_SELFIE)


async def _process_doc(bot: Bot, db: Database, cfg: Config, tg_id: int,
                       file_id: str | None, full_name: str | None) -> None:
    try:
        if not file_id:
            return
        data = await files.download(bot, file_id, logic.MAX_UPLOAD_BYTES)
        path, digest = files.store(cfg.storage_dir, tg_id, "doc", data)
        await db.patch(tg_id, doc_path=str(path), doc_sha256=digest)

        duplicates = await db.count_duplicate_docs(tg_id, digest)
        if duplicates:
            await db.log_event(tg_id, "duplicate_document", {"count": duplicates})
            await bot.send_message(cfg.admin_chat_id, texts.ALERT_DUPLICATE.format(
                tg_id=tg_id, count=duplicates))

        result = await ocr.recognize_and_match(cfg, data, full_name)
        if result is None:
            await db.patch(tg_id, ocr_at=utcnow(), name_match=None)
        else:
            await db.patch(
                tg_id, ocr_at=utcnow(), name_match=result.score,
                doc_ocr={"entities": result.entities, "matched": list(result.matched),
                         "tokens_total": result.tokens_total, "recognized": result.recognized},
            )
        await _alert_if_late(bot, db, cfg, tg_id, result)
    except Exception:                                   # noqa: BLE001
        log.exception("обработка документа %s не удалась", tg_id)


async def _alert_if_late(bot: Bot, db: Database, cfg: Config, tg_id: int,
                         result: logic.OcrResult | None) -> None:
    """Если OCR закончился уже после отправки карточки, модератор о результате
    сам не узнает - в карточке было «ещё не готов». Досылаем отдельно."""
    row = await db.get_user(tg_id)
    if row is None or row["status"] != logic.ST_PENDING:
        return
    if result is None:
        await bot.send_message(cfg.admin_chat_id, texts.ALERT_OCR_FAILED.format(tg_id=tg_id))
    elif result.mismatch:
        await bot.send_message(cfg.admin_chat_id, texts.ALERT_MISMATCH.format(
            tg_id=tg_id, matched=len(result.matched), total=result.tokens_total,
            percent=round(result.score * 100)))


async def _process_selfie(bot: Bot, db: Database, cfg: Config, tg_id: int,
                          file_id: str | None) -> None:
    try:
        if not file_id:
            return
        data = await files.download(bot, file_id, logic.MAX_UPLOAD_BYTES)
        path, digest = files.store(cfg.storage_dir, tg_id, "selfie", data)
        await db.patch(tg_id, selfie_path=str(path), selfie_sha256=digest)
    except Exception:                                   # noqa: BLE001
        log.exception("обработка селфи %s не удалась", tg_id)
