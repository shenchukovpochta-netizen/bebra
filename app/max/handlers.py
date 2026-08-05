"""Сценарий бота в MAX: те же шаги, что в Telegram-версии, поверх MaxClient.

Здесь нет своей логики решений - валидация, порядок шагов, причины отказов
и сборка договора берутся из общих модулей. Файл сознательно повторяет
структуру app/handlers/*: столкнувшись с расхождением поведения двух ботов,
искать его нужно в одинаково названных функциях.

Отличия транспорта, о которых стоит помнить:
- нет reply-клавиатур: меню и «Отмена» - inline-кнопки;
- нет тем в группах: подписанный договор уходит в чат фиксации общим потоком;
- id сообщений (mid) - строки, а не числа: в базе для них текстовые колонки;
- фото приходит вложением image с url для скачивания и token для пересылки.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import logic, tasks, texts
from ..config import Config
from ..db import Database, utcnow
from ..services import contract as contract_service
from ..services import files
from ..services.crypto import Vault
from . import keyboards as kb
from . import parse
from .client import MaxAPIError, MaxClient

log = logging.getLogger(__name__)

UNSIGNED = "не подписан"

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


class Ctx:
    """Всё, что нужно обработчику: клиент, база, конфиг, шифрование."""

    def __init__(self, cl: MaxClient, db: Database, cfg: Config,
                 vault: Vault) -> None:
        self.cl = cl
        self.db = db
        self.cfg = cfg
        self.vault = vault


async def _say(ctx: Ctx, user_id: int, text: str,
               keyboard: list | None = None) -> None:
    await ctx.cl.send(user_id=user_id, text=text, keyboard=keyboard)


def _prompt_keyboard(state: str) -> list | None:
    if state == logic.WAIT_LIVE_ADDR:
        return kb.same_address()
    if state == logic.WAIT_CONTACT:
        return kb.share_contact()
    return None


async def _ask(ctx: Ctx, user_id: int, state: str) -> None:
    await _say(ctx, user_id, PROMPTS[state], _prompt_keyboard(state))


# ─────────────────────────── старт и оферта ───────────────────────────

async def start(ctx: Ctx, user: dict) -> None:
    if user["status"] == logic.ST_APPROVED:
        if user["state"] == logic.WAIT_SUPPORT:
            await ctx.db.patch(user["tg_id"], expected_state=logic.WAIT_SUPPORT,
                               state=logic.APPROVED)
        await _say(ctx, user["tg_id"], texts.ALREADY_REGISTERED, kb.main_menu())
        return
    await ctx.db.patch(user["tg_id"], state=logic.WAIT_FIO)
    await _say(ctx, user["tg_id"], texts.WELCOME)


async def st_fio(ctx: Ctx, user: dict, text: str | None) -> None:
    result = logic.validate_fio(text)
    if not result.ok:
        await _say(ctx, user["tg_id"], result.error)
        return
    if not await ctx.db.patch(user["tg_id"], expected_state=logic.WAIT_FIO,
                              full_name=result.value, state=logic.WAIT_OFERTA):
        return
    await ctx.db.log_event(user["tg_id"], "fio_set")
    await _say(ctx, user["tg_id"],
               texts.OFERTA.format(fio=logic.esc(result.value),
                                   purge_days=ctx.cfg.purge_approved_days),
               kb.oferta(ctx.cfg.oferta_url, ctx.cfg.pdn_url))


async def cb_oferta(ctx: Ctx, user: dict, callback_id: str) -> None:
    now = utcnow()
    if not await ctx.db.patch(user["tg_id"], expected_state=logic.WAIT_OFERTA,
                              state=logic.WAIT_CONTACT,
                              oferta_version=ctx.cfg.oferta_version,
                              oferta_accepted_at=now,
                              pdn_version=ctx.cfg.consent_version,
                              pdn_consent_at=now):
        await ctx.cl.answer_callback(callback_id)
        return
    await ctx.db.log_event(user["tg_id"], "oferta_accepted",
                           {"version": ctx.cfg.oferta_version,
                            "consent_version": ctx.cfg.consent_version})
    await ctx.cl.answer_callback(callback_id, texts.OFERTA_ACCEPTED)
    await _say(ctx, user["tg_id"], texts.ASK_CONTACT, kb.share_contact())


async def st_contact(ctx: Ctx, user: dict, attachments: list) -> None:
    phone, owner_id = parse.contact_from(attachments)
    if phone is None:
        await _say(ctx, user["tg_id"], texts.CONTACT_USE_BUTTON,
                   kb.share_contact())
        return
    if not logic.contact_belongs_to_sender(owner_id, user["tg_id"]):
        await _say(ctx, user["tg_id"], texts.CONTACT_FOREIGN, kb.share_contact())
        return
    normalized = logic.normalize_phone(phone) or phone
    following = logic.next_state(logic.WAIT_CONTACT)
    if not await ctx.db.patch(user["tg_id"], expected_state=logic.WAIT_CONTACT,
                              phone=normalized, state=following):
        return
    await ctx.db.log_event(user["tg_id"], "contact_set")
    await _say(ctx, user["tg_id"], texts.ANKETA_INTRO)
    await _ask(ctx, user["tg_id"], following)


# ─────────────────────────── анкета ───────────────────────────

async def _advance(ctx: Ctx, user: dict, step: logic.Step, value: str) -> None:
    anketa = ctx.vault.decrypt(user.get("anketa_enc"))
    anketa[step.field] = value
    following = logic.next_state(step.state)
    if following == logic.WAIT_DOC and user.get("doc_file_id"):
        following = logic.state_after_doc(
            anketa, has_parent_consent=bool(user.get("parent_file_id")))
    to_confirm = following == logic.CONFIRM

    if not await ctx.db.patch(user["tg_id"], expected_state=step.state,
                              anketa_enc=ctx.vault.encrypt(anketa),
                              state=following,
                              **({"purge_after": None} if to_confirm else {})):
        return
    if to_confirm:
        await send_confirm(ctx, user)
        return
    await _ask(ctx, user["tg_id"], following)


async def st_anketa(ctx: Ctx, user: dict, text: str | None) -> None:
    step = logic.ANKETA_BY_STATE[user["state"]]
    anketa = ctx.vault.decrypt(user.get("anketa_enc"))

    if step.state in (logic.WAIT_PHONE2, logic.WAIT_PHONE3):
        taken = [p for p in (logic.normalize_phone(user.get("phone")),
                             anketa.get("phone2"), anketa.get("phone3")) if p]
        own = anketa.get(step.field)
        result = step.validate(text, taken=[p for p in taken if p != own])
    else:
        result = step.validate(text)

    if not result.ok:
        await _say(ctx, user["tg_id"], result.error)
        return

    if step.state == logic.WAIT_PASSPORT_DATE and not logic.passport_date_consistent(
            {**anketa, "passport_date": result.value}):
        await _say(ctx, user["tg_id"], texts.PASSPORT_DATE_BEFORE_BIRTH)
        await ctx.db.patch(user["tg_id"], expected_state=step.state,
                           state=logic.WAIT_BIRTH)
        await _ask(ctx, user["tg_id"], logic.WAIT_BIRTH)
        return

    await _advance(ctx, user, step, result.value)


async def cb_same_address(ctx: Ctx, user: dict, callback_id: str) -> None:
    """Кнопка «Совпадает с регистрацией» - в MAX она inline."""
    if user["state"] != logic.WAIT_LIVE_ADDR:
        await ctx.cl.answer_callback(callback_id)
        return
    anketa = ctx.vault.decrypt(user.get("anketa_enc"))
    await ctx.cl.answer_callback(callback_id, texts.SAME_AS_REG)
    await _advance(ctx, user, logic.ANKETA_BY_STATE[logic.WAIT_LIVE_ADDR],
                   anketa.get("reg_address", ""))


# ─────────────────────────── документы ───────────────────────────

async def st_upload(ctx: Ctx, user: dict, attachments: list) -> None:
    """Фото паспорта или согласия родителя - по текущему состоянию."""
    state = user["state"]
    image = parse.first_attachment(attachments, "image")
    if image is None or not image.get("token"):
        await _say(ctx, user["tg_id"],
                   texts.DOC_NEED_PHOTO if state == logic.WAIT_DOC
                   else texts.PARENT_NEED_PHOTO)
        return
    token, url = image["token"], image.get("url")

    if state == logic.WAIT_DOC:
        following = logic.state_after_doc(
            ctx.vault.decrypt(user.get("anketa_enc")),
            has_parent_consent=bool(user.get("parent_file_id")))
        if not await ctx.db.patch(user["tg_id"], expected_state=logic.WAIT_DOC,
                                  doc_file_id=token, doc_is_photo=True,
                                  doc_path=None, doc_sha256=None,
                                  purge_after=None, state=following):
            return
        await ctx.db.log_event(user["tg_id"], "doc_uploaded")
        if following == logic.CONFIRM:
            await send_confirm(ctx, {**user, "doc_file_id": token})
        else:
            await _ask(ctx, user["tg_id"], following)
        tasks.spawn(_process_upload(ctx, user["tg_id"], url, "doc"))
        return

    # согласие родителя
    if not user.get("doc_file_id"):
        await ctx.db.patch(user["tg_id"],
                           expected_state=logic.WAIT_PARENT_CONSENT,
                           state=logic.WAIT_DOC)
        await _ask(ctx, user["tg_id"], logic.WAIT_DOC)
        return
    if not await ctx.db.patch(user["tg_id"],
                              expected_state=logic.WAIT_PARENT_CONSENT,
                              parent_file_id=token, parent_is_photo=True,
                              parent_path=None, parent_sha256=None,
                              purge_after=None, state=logic.CONFIRM):
        return
    await ctx.db.log_event(user["tg_id"], "parent_consent_uploaded")
    await send_confirm(ctx, user)
    tasks.spawn(_process_upload(ctx, user["tg_id"], url, "parent"))


async def _process_upload(ctx: Ctx, tg_id: int, url: str | None,
                          slot: str) -> None:
    try:
        if not url:
            return
        data = await ctx.cl.download(url, logic.MAX_UPLOAD_BYTES)
        path, digest = files.store(ctx.cfg.storage_dir, tg_id, slot, data)
        if slot == "parent":
            await ctx.db.patch(tg_id, parent_path=str(path), parent_sha256=digest)
            return
        await ctx.db.patch(tg_id, doc_path=str(path), doc_sha256=digest)
        duplicates = await ctx.db.count_duplicate_docs(tg_id, digest)
        if duplicates:
            await ctx.db.log_event(tg_id, "duplicate_document",
                                   {"count": duplicates})
            await ctx.cl.send(chat_id=ctx.cfg.contract_chat_id,
                              text=texts.ALERT_DUPLICATE.format(
                                  tg_id=tg_id, count=duplicates))
    except Exception:                                   # noqa: BLE001
        log.exception("обработка файла %s (%s) не удалась", tg_id, slot)


# ─────────────────────────── подтверждение ───────────────────────────

def _image_attachment(token: str) -> dict:
    return {"type": "image", "payload": {"token": token}}


async def send_confirm(ctx: Ctx, data: dict) -> None:
    await ctx.cl.send(
        user_id=data["tg_id"],
        text=texts.CONFIRM_CAPTION.format(
            fio=logic.esc(data["full_name"]),
            phone=logic.esc(str(data["phone"] or "").lstrip("+"))),
        attachments=[_image_attachment(data["doc_file_id"])],
        keyboard=kb.confirm(),
    )


async def cb_restart(ctx: Ctx, user: dict, callback_id: str) -> None:
    if not await ctx.db.patch(user["tg_id"], expected_state=logic.CONFIRM,
                              state=logic.WAIT_FIO, doc_file_id=None,
                              doc_sha256=None, parent_file_id=None,
                              parent_sha256=None, anketa_enc=None,
                              purge_after=utcnow()):
        await ctx.cl.answer_callback(callback_id)
        return
    await ctx.db.log_event(user["tg_id"], "restart")
    await ctx.cl.answer_callback(callback_id, texts.RESTART_TOAST)
    await _say(ctx, user["tg_id"], texts.RESTART)


async def cb_confirm(ctx: Ctx, user: dict, callback_id: str) -> None:
    if not await ctx.db.patch(user["tg_id"], expected_state=logic.CONFIRM,
                              state=logic.PENDING, status=logic.ST_PENDING):
        await ctx.cl.answer_callback(callback_id)
        return
    await ctx.db.log_event(user["tg_id"], "submitted")
    await ctx.cl.answer_callback(callback_id, texts.SUBMITTED_TOAST)
    await _say(ctx, user["tg_id"], texts.SUBMITTED)
    try:
        await send_moderation_card(ctx, user["tg_id"])
    except (MaxAPIError, CardNotReady):
        log.exception("КАРТОЧКА МОДЕРАЦИИ НЕ ОТПРАВЛЕНА для %s", user["tg_id"])
        await ctx.db.log_event(user["tg_id"], "moderation_card_failed")
        await _say(ctx, user["tg_id"], texts.SUBMIT_PROBLEM)


class CardNotReady(Exception):
    """Нечего показывать модератору."""


def _anketa_lines(data: dict, anketa: dict) -> str:
    ctx_map = logic.contract_context(data, anketa, number="")
    return "\n".join(
        f"{label}: <b>{logic.esc(ctx_map.get(field, '—'))}</b>"
        for field, label in logic.CONTRACT_LABELS
    )


async def send_moderation_card(ctx: Ctx, tg_id: int) -> None:
    row = await ctx.db.get_user(tg_id)
    if row is None:
        raise CardNotReady(f"нет записи о пользователе {tg_id}")
    data = dict(row)
    if not data.get("doc_file_id"):
        raise CardNotReady(f"у {tg_id} нет doc_file_id")
    anketa = ctx.vault.decrypt(data.get("anketa_enc"))
    missing = logic.missing_anketa_fields(anketa)
    if missing:
        raise CardNotReady(f"у {tg_id} не заполнено: {', '.join(missing)}")

    minor = logic.is_minor(anketa)
    if minor and not data.get("parent_file_id"):
        raise CardNotReady(f"у {tg_id} (16-17 лет) нет фото согласия родителя")
    if minor:
        await ctx.cl.send(
            chat_id=ctx.cfg.contract_chat_id,
            text=texts.PARENT_CARD_CAPTION.format(tg_id=tg_id),
            attachments=[_image_attachment(data["parent_file_id"])])

    caption = texts.CONTRACT_CARD.format(
        number=logic.esc(data.get("contract_no") or "будет присвоен"),
        fields=_anketa_lines(data, anketa), tg_id=tg_id)
    if minor:
        caption += texts.CARD_MINOR_LINE
    sent = await ctx.cl.send(
        chat_id=ctx.cfg.contract_chat_id, text=caption,
        attachments=[_image_attachment(data["doc_file_id"])],
        keyboard=kb.moderation(tg_id))
    mid = (sent.get("message") or {}).get("body", {}).get("mid")
    await ctx.db.patch(tg_id, mod_chat_id=ctx.cfg.contract_chat_id,
                       mod_message_id=mid)


# ─────────────────────────── договор ───────────────────────────

class ContractProblem(Exception):
    pass


def _contract_ctx(ctx: Ctx, data: dict, anketa: dict, *, number: str,
                  signed_at: str, issued_at: Any) -> dict:
    built = logic.contract_context(
        data, anketa, number=number,
        today=issued_at.date() if issued_at else None)
    built["purge_days"] = str(ctx.cfg.purge_approved_days)
    built["signed_at"] = signed_at
    return built


def _build_docx(ctx: Ctx, data: dict, anketa: dict, *, number: str,
                signed_at: str, issued_at: Any) -> tuple[bytes, str]:
    try:
        return contract_service.build(
            ctx.cfg.contract_template,
            _contract_ctx(ctx, data, anketa, number=number,
                          signed_at=signed_at, issued_at=issued_at))
    except (contract_service.TemplateProblem, OSError) as exc:
        raise ContractProblem(str(exc)) from exc


async def _send_contract(ctx: Ctx, user_id: int, docx: bytes, number: str,
                         caption: str, keyboard: list | None) -> None:
    attachment = await ctx.cl.upload_file(f"dogovor-{number}.docx", docx)
    await ctx.cl.send(user_id=user_id, text=caption,
                      attachments=[attachment], keyboard=keyboard)


async def issue(ctx: Ctx, tg_id: int) -> str:
    row = await ctx.db.get_user(tg_id)
    if row is None:
        raise ContractProblem(f"нет записи о пользователе {tg_id}")
    data = dict(row)
    anketa = ctx.vault.decrypt(data.get("anketa_enc"))
    missing = logic.missing_anketa_fields(anketa)
    if missing:
        raise ContractProblem(f"не заполнено: {', '.join(missing)}")

    number = data.get("contract_no") or logic.contract_number(
        await ctx.db.next_contract_seq(), prefix=ctx.cfg.contract_prefix)
    issued_at = data.get("contract_issued_at") or utcnow()
    docx, digest = _build_docx(ctx, data, anketa, number=number,
                               signed_at=UNSIGNED, issued_at=issued_at)
    path, _ = files.store(ctx.cfg.storage_dir, tg_id, "contract", docx)
    if not await ctx.db.patch(
            tg_id, expected_status=logic.ST_APPROVED, state=logic.WAIT_SIGN,
            contract_no=number, contract_path=str(path), contract_sha256=digest,
            contract_status=logic.CT_ISSUED, contract_issued_at=issued_at):
        files.remove(path)
        raise ContractProblem(f"статус {tg_id} изменился, договор не выдан")
    await ctx.db.log_event(tg_id, "contract_issued", {"number": number})
    await _send_contract(ctx, tg_id, docx, number,
                         texts.CONTRACT_READY_USER.format(number=logic.esc(number)),
                         kb.sign_contract())
    return number


async def cb_sign(ctx: Ctx, user: dict, callback_id: str) -> None:
    signed_at = utcnow()
    if not await ctx.db.patch(user["tg_id"], expected_state=logic.WAIT_SIGN,
                              state=logic.APPROVED,
                              contract_status=logic.CT_SIGNED,
                              contract_signed_at=signed_at):
        await ctx.cl.answer_callback(callback_id)
        return
    await ctx.cl.answer_callback(callback_id, texts.CONTRACT_SIGN_TOAST)

    tg_id = user["tg_id"]
    row = await ctx.db.get_user(tg_id)
    data = dict(row) if row else dict(user)
    anketa = ctx.vault.decrypt(data.get("anketa_enc"))
    number = data.get("contract_no") or ""
    stamp = signed_at.strftime("%d.%m.%Y %H:%M UTC")

    try:
        docx, digest = _build_docx(ctx, data, anketa, number=number,
                                   signed_at=stamp,
                                   issued_at=data.get("contract_issued_at"))
    except ContractProblem:
        log.exception("подписанный экземпляр %s не собрался", tg_id)
        await ctx.cl.send(chat_id=ctx.cfg.contract_chat_id,
                          text=texts.CONTRACT_ALERT_FAILED.format(
                              tg_id=tg_id,
                              reason="не удалось пересобрать подписанный экземпляр"))
        await _say(ctx, tg_id,
                   texts.REGISTERED.format(video_url=logic.esc(ctx.cfg.video_url)),
                   kb.main_menu())
        return

    old_path = data.get("contract_path")
    path, _ = files.store(ctx.cfg.storage_dir, tg_id, "contract", docx)
    await ctx.db.patch(tg_id, contract_path=str(path), contract_sha256=digest)
    if old_path and old_path != str(path):
        files.remove(old_path)
    await ctx.db.log_event(tg_id, "contract_signed", {"number": number})
    await ctx.db.set_purge_after(tg_id, ctx.cfg.purge_approved_days)

    await _send_contract(ctx, tg_id, docx, number,
                         texts.CONTRACT_SIGNED_USER.format(
                             number=logic.esc(number), signed_at=stamp,
                             video_url=logic.esc(ctx.cfg.video_url)),
                         kb.main_menu())

    # фиксация сдачи: без тем, общим потоком чата фиксации
    try:
        attachment = await ctx.cl.upload_file(f"dogovor-{number}.docx", docx)
        await ctx.cl.send(chat_id=ctx.cfg.fix_chat_id,
                          text=texts.CONTRACT_FIX_CARD.format(
                              number=logic.esc(number),
                              fields=_anketa_lines(data, anketa),
                              tg_id=tg_id, signed_at=stamp, sha256=digest),
                          attachments=[attachment])
    except MaxAPIError:
        log.exception("договор %s не доставлен в чат фиксации", number)
        await ctx.db.log_event(tg_id, "contract_fix_failed", {"number": number})

    try:
        await ctx.cl.send(chat_id=ctx.cfg.contract_chat_id,
                          text=texts.FIXATION_FORM_INTRO.format(
                              number=logic.esc(number)))
        await ctx.cl.send(chat_id=ctx.cfg.contract_chat_id,
                          text=logic.fixation_form(data, anketa))
    except MaxAPIError:
        log.exception("форма фиксации по договору %s не доставлена", number)
        await ctx.db.log_event(tg_id, "fixation_form_failed", {"number": number})

    await ctx.db.clear_anketa(tg_id)


async def cb_mistake(ctx: Ctx, user: dict, callback_id: str) -> None:
    if not await ctx.db.patch(user["tg_id"], expected_state=logic.WAIT_SIGN,
                              state=logic.WAIT_FIO, status=logic.ST_NEW,
                              anketa_enc=None, contract_status=logic.CT_NONE):
        await ctx.cl.answer_callback(callback_id)
        return
    await ctx.db.log_event(user["tg_id"], "contract_mistake")
    await ctx.cl.answer_callback(callback_id)
    await _say(ctx, user["tg_id"], texts.CONTRACT_MISTAKE)
    await _say(ctx, user["tg_id"], texts.WELCOME)


async def st_wait_sign(ctx: Ctx, user: dict) -> None:
    """Любое сообщение в ожидании подписи возвращает договор с кнопками."""
    row = await ctx.db.get_user(user["tg_id"])
    data = dict(row) if row else dict(user)
    anketa = ctx.vault.decrypt(data.get("anketa_enc"))
    number = data.get("contract_no") or ""
    try:
        docx, _ = _build_docx(ctx, data, anketa, number=number,
                              signed_at=UNSIGNED,
                              issued_at=data.get("contract_issued_at"))
    except ContractProblem:
        log.exception("не удалось переотправить договор %s", user["tg_id"])
        await _say(ctx, user["tg_id"], texts.CONTRACT_PRESS_BUTTON)
        return
    await _send_contract(ctx, user["tg_id"], docx, number,
                         texts.CONTRACT_RESEND.format(number=logic.esc(number)),
                         kb.sign_contract())


# ─────────────────────────── меню и поддержка ───────────────────────────

async def menu(ctx: Ctx, user: dict) -> None:
    await _say(ctx, user["tg_id"], texts.MENU_PROMPT, kb.main_menu())


async def cb_menu(ctx: Ctx, user: dict, callback_id: str, item: str) -> None:
    await ctx.cl.answer_callback(callback_id)
    if user["state"] == logic.WAIT_SUPPORT and item != "support":
        # кнопка меню посреди вопроса - «передумал», как в Telegram-версии
        await ctx.db.patch(user["tg_id"], expected_state=logic.WAIT_SUPPORT,
                           state=logic.APPROVED)
    if item == "tariffs":
        await _say(ctx, user["tg_id"], texts.TARIFFS, kb.main_menu())
    elif item == "trips":
        await _say(ctx, user["tg_id"], "Поездок пока нет.", kb.main_menu())
    elif item == "support":
        if user["state"] != logic.WAIT_SUPPORT and not await ctx.db.patch(
                user["tg_id"], expected_state=logic.APPROVED,
                state=logic.WAIT_SUPPORT):
            await _say(ctx, user["tg_id"], texts.MENU_PROMPT, kb.main_menu())
            return
        await _say(ctx, user["tg_id"], texts.SUPPORT_PROMPT, kb.support_cancel())


async def cb_support_cancel(ctx: Ctx, user: dict, callback_id: str) -> None:
    await ctx.db.patch(user["tg_id"], expected_state=logic.WAIT_SUPPORT,
                       state=logic.APPROVED)
    await ctx.cl.answer_callback(callback_id)
    await _say(ctx, user["tg_id"], texts.SUPPORT_CANCELLED, kb.main_menu())


async def st_support(ctx: Ctx, user: dict, text: str | None) -> None:
    if (text or "").strip().lower() == "отмена":
        await ctx.db.patch(user["tg_id"], expected_state=logic.WAIT_SUPPORT,
                           state=logic.APPROVED)
        await _say(ctx, user["tg_id"], texts.SUPPORT_CANCELLED, kb.main_menu())
        return
    question = logic.support_question(text)
    if not question.ok:
        await _say(ctx, user["tg_id"], question.error)
        return
    try:
        sent = await ctx.cl.send(
            chat_id=ctx.cfg.admin_chat_id,
            text=texts.SUPPORT_CARD.format(
                fio=logic.esc(user.get("full_name") or "без имени"),
                handle=("@" + logic.esc(user["username"])
                        if user.get("username") else "без username"),
                tg_id=user["tg_id"],
                question=logic.esc(question.value)))
    except MaxAPIError:
        log.exception("вопрос в поддержку от %s не доставлен", user["tg_id"])
        await ctx.db.patch(user["tg_id"], expected_state=logic.WAIT_SUPPORT,
                           state=logic.APPROVED)
        await _say(ctx, user["tg_id"], texts.SUPPORT_FAILED, kb.main_menu())
        return
    mid = (sent.get("message") or {}).get("body", {}).get("mid")
    await ctx.db.patch(user["tg_id"], expected_state=logic.WAIT_SUPPORT,
                       state=logic.APPROVED,
                       support_chat_id=ctx.cfg.admin_chat_id,
                       support_message_id=mid)
    await ctx.db.log_event(user["tg_id"], "support_question")
    await _say(ctx, user["tg_id"], texts.SUPPORT_SENT, kb.main_menu())


# ─────────────────────────── модерация ───────────────────────────

def _is_admin(user_id: int, cfg: Config) -> bool:
    return user_id in cfg.admins


async def _decide(ctx: Ctx, moderator_id: int, target: int, *, approved: bool,
                  reason: str = "", back_to: str = "") -> bool:
    if not await ctx.db.patch(
            target, expected_status=logic.ST_PENDING,
            status=logic.ST_APPROVED if approved else logic.ST_REJECTED,
            state=logic.PENDING if approved else (back_to or logic.WAIT_FIO),
            reject_reason=None if approved else reason,
            reviewed_by=moderator_id, reviewed_at=utcnow()):
        return False
    await ctx.db.log_event(
        target, "moderation_approved" if approved else "moderation_rejected",
        {"by": moderator_id, "reason": reason})
    return True


async def _notify(ctx: Ctx, tg_id: int, text: str) -> None:
    try:
        await ctx.cl.send(user_id=tg_id, text=text)
    except MaxAPIError as exc:
        log.warning("не удалось уведомить %s: %s", tg_id, exc)


async def cb_moderation(ctx: Ctx, moderator: dict, callback_id: str,
                        payload: str, card_chat_id: int) -> None:
    """Все кнопки карточек модерации: approve / reject / rj / rjc / rjx."""
    if not _is_admin(moderator["user_id"], ctx.cfg):
        await ctx.cl.answer_callback(callback_id, texts.MOD_NO_RIGHTS)
        return

    if payload.startswith("approve:"):
        parsed = logic.parse_moderation_callback(payload)
        if parsed is None or await ctx.db.get_user(parsed[1]) is None:
            await ctx.cl.answer_callback(callback_id, texts.MOD_BROKEN_BUTTON)
            return
        target = parsed[1]
        if not await _decide(ctx, moderator["user_id"], target, approved=True):
            await ctx.cl.answer_callback(callback_id, texts.MOD_ALREADY_HANDLED)
            return
        await ctx.cl.answer_callback(callback_id, texts.CONTRACT_APPROVED_TOAST)
        try:
            await issue(ctx, target)
        except (ContractProblem, MaxAPIError) as exc:
            log.exception("договор для %s не выдан", target)
            await ctx.db.log_event(target, "contract_failed", {"error": str(exc)})
            await _notify(ctx, target, texts.CONTRACT_FAILED_USER)
            await ctx.cl.send(chat_id=ctx.cfg.contract_chat_id,
                              text=texts.CONTRACT_ALERT_FAILED.format(
                                  tg_id=target, reason=logic.esc(str(exc))))
        return

    if payload.startswith("reject:"):
        parsed = logic.parse_moderation_callback(payload)
        if parsed is None:
            await ctx.cl.answer_callback(callback_id, texts.MOD_BROKEN_BUTTON)
            return
        # В MAX кнопки старого сообщения не редактируются так свободно,
        # как в Telegram, поэтому меню причин уходит отдельным сообщением.
        await ctx.cl.answer_callback(callback_id, texts.MOD_PICK_REASON)
        await ctx.cl.send(chat_id=card_chat_id, text=texts.MOD_PICK_REASON,
                          keyboard=kb.reject_reasons(parsed[1],
                                                     logic.REJECT_REASONS))
        return

    if payload.startswith("rjx:"):
        await ctx.cl.answer_callback(callback_id)
        return

    if payload.startswith("rjc:"):
        await ctx.cl.answer_callback(callback_id, texts.MOD_ASK_COMMENT)
        return

    if payload.startswith("rj:"):
        parsed = logic.parse_reject_callback(payload)
        if parsed is None:
            await ctx.cl.answer_callback(callback_id, texts.MOD_BROKEN_BUTTON)
            return
        target, code = parsed
        row = await ctx.db.get_user(target)
        if row is None:
            await ctx.cl.answer_callback(callback_id, texts.MOD_BROKEN_BUTTON)
            return
        reason = logic.REJECT_REASONS[code][0]
        back_to = logic.reject_back_to(
            code, ctx.vault.decrypt(dict(row).get("anketa_enc")))
        if not await _decide(ctx, moderator["user_id"], target, approved=False,
                             reason=reason, back_to=back_to):
            await ctx.cl.answer_callback(callback_id, texts.MOD_ALREADY_HANDLED)
            return
        await ctx.db.set_purge_after(target, ctx.cfg.purge_rejected_days)
        await ctx.cl.answer_callback(callback_id, texts.MOD_REJECTED)
        await _notify(ctx, target,
                      texts.REJECTED_WITH_REASON.format(reason=logic.esc(reason)))
        return

    await ctx.cl.answer_callback(callback_id)


async def mod_reply(ctx: Ctx, moderator_id: int, chat_id: int,
                    reply_to_mid: str, text: str | None) -> None:
    """Ответ на карточку в служебном чате: вопрос поддержки или отказ."""
    if not _is_admin(moderator_id, ctx.cfg):
        return

    asked = await ctx.db.user_by_support_message(chat_id, reply_to_mid)
    if asked is not None:
        answer = logic.support_answer(text)
        if not answer.ok:
            await ctx.cl.send(chat_id=chat_id, text=answer.error)
            return
        try:
            await ctx.cl.send(user_id=dict(asked)["tg_id"],
                              text=texts.SUPPORT_REPLY_USER.format(
                                  answer=logic.esc(answer.value)))
        except MaxAPIError:
            await ctx.cl.send(chat_id=chat_id,
                              text=texts.SUPPORT_REPLY_NOT_DELIVERED)
            return
        await ctx.db.log_event(dict(asked)["tg_id"], "support_answered",
                               {"by": moderator_id})
        await ctx.cl.send(chat_id=chat_id, text=texts.SUPPORT_REPLIED)
        return

    row = await ctx.db.user_by_mod_message(chat_id, reply_to_mid)
    if row is None:
        # Молча: в отличие от Telegram, MAX не отдаёт автора отвечаемого
        # сообщения, и отличить ответ на карточку с опечаткой от ответа
        # коллеге нечем. Вклиниваться в переписку модераторов хуже,
        # чем не отреагировать на битую карточку.
        log.debug("реплай на неизвестный mid %s в чате %s", reply_to_mid, chat_id)
        return
    target = dict(row)
    if target["status"] != logic.ST_PENDING:
        await ctx.cl.send(chat_id=chat_id, text=texts.MOD_REPLY_NOT_PENDING)
        return
    comment = logic.reject_comment(text)
    if not comment.ok:
        await ctx.cl.send(chat_id=chat_id, text=comment.error)
        return
    if not await ctx.db.patch(target["tg_id"], expected_status=logic.ST_PENDING,
                              status=logic.ST_REJECTED, state=logic.WAIT_FIO,
                              reject_reason=comment.value,
                              reviewed_by=moderator_id, reviewed_at=utcnow()):
        await ctx.cl.send(chat_id=chat_id, text=texts.MOD_REPLY_NOT_PENDING)
        return
    await ctx.db.log_event(target["tg_id"], "moderation_rejected",
                           {"by": moderator_id, "reason": comment.value})
    await ctx.db.set_purge_after(target["tg_id"], ctx.cfg.purge_rejected_days)
    await ctx.cl.send(chat_id=chat_id, text=texts.MOD_COMMENT_SAVED)
    await _notify(ctx, target["tg_id"],
                  texts.REJECTED_WITH_REASON.format(
                      reason=logic.esc(comment.value)))
