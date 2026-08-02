"""Договор: формирование, выдача на подпись, подпись и фиксация.

Порядок такой: утверждающий одобряет заявку -> бот собирает PDF и отдаёт его
пользователю -> пользователь нажимает «Подписываю» -> подписанный экземпляр
уходит в чат фиксации, а паспортные данные стираются из базы.

Состояние wait_sign отделено от approved намеренно: между «заявку одобрили»
и «договор подписан» велосипед выдавать нельзя, а по одному лишь approved
эти два случая не различить.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import keyboards as kb
from .. import logic, texts
from ..config import Config
from ..db import Database, utcnow
from ..filters import StateIs
from ..services import contract as contract_service
from ..services import files
from ..services.crypto import Vault

log = logging.getLogger(__name__)
router = Router(name="contract")

UNSIGNED = "не подписан"


class ContractProblem(Exception):
    """Договор собрать не удалось. Наружу пользователю не показывается."""


def _context(cfg: Config, data: dict, anketa: dict, *, number: str,
             signed_at: str) -> dict[str, Any]:
    ctx = logic.contract_context(data, anketa, number=number)
    # purge_days идёт из конфигурации, а не из шаблона: сроки в договоре
    # обязаны совпадать с тем, по которым ретеншен реально удаляет сканы.
    ctx["purge_days"] = str(cfg.purge_approved_days)
    ctx["signed_at"] = signed_at
    return ctx


def _filename(number: str) -> str:
    return f"dogovor-{number}.pdf"


async def _build(cfg: Config, data: dict, anketa: dict, *, number: str,
                 signed_at: str) -> tuple[bytes, str]:
    ctx = _context(cfg, data, anketa, number=number, signed_at=signed_at)
    try:
        return contract_service.build(cfg.contract_template, ctx)
    except (contract_service.TemplateProblem, OSError) as exc:
        raise ContractProblem(str(exc)) from exc


async def issue(bot: Bot, db: Database, cfg: Config, vault: Vault, tg_id: int) -> str:
    """Собрать договор и отдать пользователю на подпись. Возвращает номер.

    Номер берётся из последовательности один раз и потом переиспользуется:
    при повторной выдаче (например, после исправления ошибки) договор обязан
    остаться под тем же номером, иначе в журнале выдачи их окажется два.
    """
    row = await db.get_user(tg_id)
    if row is None:
        raise ContractProblem(f"нет записи о пользователе {tg_id}")
    data = dict(row)

    anketa = vault.decrypt(data.get("anketa_enc"))
    missing = logic.missing_anketa_fields(anketa)
    if missing:
        raise ContractProblem(f"не заполнено: {', '.join(missing)}")

    number = data.get("contract_no") or logic.contract_number(await db.next_contract_seq())
    pdf, digest = await _build(cfg, data, anketa, number=number, signed_at=UNSIGNED)
    path, _ = files.store(cfg.storage_dir, tg_id, "contract", pdf)

    if not await db.patch(
        tg_id, expected_status=logic.ST_APPROVED,
        state=logic.WAIT_SIGN,
        contract_no=number, contract_path=str(path), contract_sha256=digest,
        contract_status=logic.CT_ISSUED, contract_issued_at=utcnow(),
    ):
        # Статус успел уехать - договор уже неактуален, файл на диске не нужен.
        files.remove(path)
        raise ContractProblem(f"статус {tg_id} изменился, договор не выдан")

    await db.log_event(tg_id, "contract_issued", {"number": number})
    await bot.send_document(
        tg_id,
        BufferedInputFile(pdf, filename=_filename(number)),
        caption=texts.CONTRACT_READY_USER.format(number=logic.esc(number)),
        reply_markup=kb.sign_contract(),
    )
    return number


@router.callback_query(StateIs(logic.WAIT_SIGN), F.data == "sign")
async def cb_sign(callback: CallbackQuery, bot: Bot, db: Database, cfg: Config,
                  vault: Vault, user: dict) -> None:
    """Простая электронная подпись: факт нажатия, момент и отпечаток текста.

    Договор пересобирается с проставленной датой подписания, поэтому отпечаток
    у подписанного экземпляра свой - именно он и уходит в чат фиксации.
    """
    if user["state"] != logic.WAIT_SIGN:
        await callback.answer(texts.CONTRACT_PRESS_BUTTON, show_alert=True)
        return

    signed_at = utcnow()
    if not await db.patch(user["tg_id"], expected_state=logic.WAIT_SIGN,
                          state=logic.APPROVED, contract_status=logic.CT_SIGNED,
                          contract_signed_at=signed_at):
        await callback.answer()
        return
    await callback.answer(texts.CONTRACT_SIGN_TOAST)

    tg_id = user["tg_id"]
    row = await db.get_user(tg_id)
    data = dict(row) if row else dict(user)
    anketa = vault.decrypt(data.get("anketa_enc"))
    number = data.get("contract_no") or ""
    stamp = signed_at.strftime("%d.%m.%Y %H:%M UTC")

    try:
        pdf, digest = await _build(cfg, data, anketa, number=number, signed_at=stamp)
    except ContractProblem:
        # Подпись уже зафиксирована в базе, откатывать её нельзя. Человеку
        # отдаём меню, а разбираться с шаблоном будут по алерту.
        log.exception("подписанный экземпляр %s не собрался", tg_id)
        await bot.send_message(cfg.contract_chat_id, texts.CONTRACT_ALERT_FAILED.format(
            tg_id=tg_id, reason="не удалось пересобрать подписанный экземпляр"))
        await callback.message.answer(
            texts.REGISTERED.format(video_url=cfg.video_url), reply_markup=kb.main_menu())
        return

    old_path = data.get("contract_path")
    path, _ = files.store(cfg.storage_dir, tg_id, "contract", pdf)
    await db.patch(tg_id, contract_path=str(path), contract_sha256=digest)
    if old_path and old_path != str(path):
        files.remove(old_path)          # неподписанный экземпляр больше не нужен

    await db.log_event(tg_id, "contract_signed", {"number": number})
    await db.set_purge_after(tg_id, cfg.purge_approved_days)

    document = BufferedInputFile(pdf, filename=_filename(number))
    await callback.message.answer_document(
        document,
        caption=texts.CONTRACT_SIGNED_USER.format(
            number=logic.esc(number), signed_at=stamp, video_url=cfg.video_url),
        reply_markup=kb.main_menu(),
    )

    await _fix(bot, db, cfg, data, anketa, pdf=pdf, number=number,
               signed_at=stamp, digest=digest)
    # Паспортные данные дальше боту не нужны: договор сформирован, экземпляры
    # у сторон и в чате фиксации. Держать их «на всякий случай» - ровно то,
    # за что спрашивают при проверке.
    await db.clear_anketa(tg_id)


async def _fix(bot: Bot, db: Database, cfg: Config, data: dict, anketa: dict, *,
               pdf: bytes, number: str, signed_at: str, digest: str) -> None:
    """Подписанный договор в чат фиксации сдачи.

    Недоставка не роняет подпись: договор уже подписан и лежит у пользователя.
    Но молчать нельзя - без записи в чате фиксации выдачу нечем подтвердить,
    поэтому факт уходит в лог и алертом.
    """
    try:
        await bot.send_document(
            cfg.fix_chat_id,
            BufferedInputFile(pdf, filename=_filename(number)),
            caption=texts.CONTRACT_FIX_CARD.format(
                number=logic.esc(number),
                fields=_fix_fields(data, anketa),
                tg_id=data.get("tg_id"),
                signed_at=signed_at,
                sha256=digest,
            ),
            message_thread_id=cfg.fix_topic_id,
        )
    except TelegramAPIError:
        log.exception("договор %s не доставлен в чат фиксации %s (тема %s)",
                      number, cfg.fix_chat_id, cfg.fix_topic_id)
        await db.log_event(data.get("tg_id"), "contract_fix_failed", {"number": number})
        try:
            await bot.send_message(cfg.contract_chat_id,
                                   texts.CONTRACT_ALERT_NOT_DELIVERED.format(
                                       number=number, tg_id=data.get("tg_id")))
        except TelegramAPIError:
            log.exception("алерт о недоставке тоже не ушёл")


def _fix_fields(data: dict, anketa: dict) -> str:
    ctx = logic.contract_context(data, anketa, number="")
    return "\n".join(
        f"{label}: <b>{logic.esc(ctx.get(field, '—'))}</b>"
        for field, label in logic.CONTRACT_LABELS
    )


@router.callback_query(StateIs(logic.WAIT_SIGN), F.data == "contract_mistake")
async def cb_mistake(callback: CallbackQuery, db: Database, vault: Vault,
                     user: dict) -> None:
    """Пользователь нашёл ошибку в своём договоре.

    Анкета стирается целиком: искать, какое именно поле неверно, человек будет
    дольше, чем введёт заново, а частично исправленная анкета - источник
    договоров с перепутанными реквизитами.
    """
    if user["state"] != logic.WAIT_SIGN:
        await callback.answer(texts.CONTRACT_PRESS_BUTTON, show_alert=True)
        return
    if not await db.patch(user["tg_id"], expected_state=logic.WAIT_SIGN,
                          state=logic.WAIT_FIO, status=logic.ST_NEW,
                          anketa_enc=None, contract_status=logic.CT_NONE):
        await callback.answer()
        return
    await db.log_event(user["tg_id"], "contract_mistake")
    await callback.answer()
    await callback.message.answer(texts.CONTRACT_MISTAKE)
    await callback.message.answer(texts.WELCOME, reply_markup=kb.remove())


@router.message(StateIs(logic.WAIT_SIGN))
async def st_wait_sign(message: Message) -> None:
    await message.answer(texts.CONTRACT_PRESS_BUTTON)
