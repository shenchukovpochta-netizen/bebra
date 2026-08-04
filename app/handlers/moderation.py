"""Утверждение и отклонение заявок из служебного чата.

Одобрение здесь не заканчивает историю, а запускает договор: бот собирает PDF
и отдаёт его пользователю на подпись. Отклонение обязано объяснить, что не так,
иначе человек присылает то же самое второй раз и заявка ходит по кругу.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from .. import logic, texts
from ..config import Config
from ..db import Database, utcnow
from ..filters import ServiceChatReply
from ..services.crypto import Vault
from . import contract

log = logging.getLogger(__name__)
router = Router(name="moderation")


def _is_admin(user_id: int, cfg: Config) -> bool:
    return user_id in cfg.admins


async def _decide(db: Database, callback: CallbackQuery, target: int, *,
                  approved: bool, reason: str = "", back_to: str = "") -> bool:
    """Перевод статуса заявки. False - решение уже принято кем-то другим.

    expected_status закрывает случай двух модераторов, нажавших одновременно:
    иначе оба довели бы дело до конца, и пользователь получил бы два разных
    решения подряд.
    """
    if not await db.patch(
        target,
        expected_status=logic.ST_PENDING,
        status=logic.ST_APPROVED if approved else logic.ST_REJECTED,
        # При одобрении состояние двигает уже выдача договора: между
        # «одобрено» и «подписано» человек не в меню, а на подписи.
        state=logic.PENDING if approved else (back_to or logic.WAIT_FIO),
        reject_reason=None if approved else reason,
        reviewed_by=callback.from_user.id,
        reviewed_at=utcnow(),
    ):
        await callback.answer(texts.MOD_ALREADY_HANDLED, show_alert=True)
        await _mark_card(callback, approved=None)
        return False
    await db.log_event(target, "moderation_approved" if approved else "moderation_rejected",
                       {"by": callback.from_user.id, "reason": reason})
    return True


@router.callback_query(F.data.regexp(r"^approve:-?\d+$"))
async def cb_approve(callback: CallbackQuery, bot: Bot, db: Database, cfg: Config,
                     vault: Vault) -> None:
    if not _is_admin(callback.from_user.id, cfg):
        await callback.answer(texts.MOD_NO_RIGHTS, show_alert=True)
        return
    parsed = logic.parse_moderation_callback(callback.data)
    if parsed is None or await db.get_user(parsed[1]) is None:
        await callback.answer(texts.MOD_BROKEN_BUTTON, show_alert=True)
        return
    target = parsed[1]

    if not await _decide(db, callback, target, approved=True):
        return
    await callback.answer(texts.CONTRACT_APPROVED_TOAST)
    await _mark_card(callback, True)

    # Договор собирается уже после того, как решение зафиксировано: сорванная
    # сборка не должна оставлять заявку в pending, иначе её будут утверждать
    # второй раз. Пользователю при этом обязательно сказать - он ждёт договор.
    try:
        await contract.issue(bot, db, cfg, vault, target)
    except (contract.ContractProblem, TelegramAPIError) as exc:
        log.exception("договор для %s не выдан", target)
        await db.log_event(target, "contract_failed", {"error": str(exc)})
        await _notify(bot, target, texts.CONTRACT_FAILED_USER)
        await _alert(bot, cfg, texts.CONTRACT_ALERT_FAILED.format(
            tg_id=target, reason=logic.esc(str(exc))))


@router.callback_query(F.data.regexp(r"^reject:-?\d+$"))
async def cb_reject_menu(callback: CallbackQuery, cfg: Config) -> None:
    """Первый экран отказа: за что именно.

    Отдельный шаг, а не мгновенный отказ: у каждой причины есть шаг, на который
    вернут человека, и «отклонено» без объяснения возвращает ту же заявку.
    """
    if not _is_admin(callback.from_user.id, cfg):
        await callback.answer(texts.MOD_NO_RIGHTS, show_alert=True)
        return
    parsed = logic.parse_moderation_callback(callback.data)
    if parsed is None:
        await callback.answer(texts.MOD_BROKEN_BUTTON, show_alert=True)
        return
    await callback.answer(texts.MOD_PICK_REASON)
    try:
        await callback.message.edit_reply_markup(
            reply_markup=kb.reject_reasons(parsed[1], logic.REJECT_REASONS))
    except TelegramAPIError:
        pass


@router.callback_query(F.data.regexp(r"^rjx:-?\d+$"))
async def cb_reject_cancel(callback: CallbackQuery, cfg: Config) -> None:
    """«Назад»: вернуть карточке обычные кнопки."""
    if not _is_admin(callback.from_user.id, cfg):
        await callback.answer(texts.MOD_NO_RIGHTS, show_alert=True)
        return
    parsed = logic.parse_moderation_callback(
        (callback.data or "").replace("rjx:", "reject:", 1))
    if parsed is None:
        await callback.answer(texts.MOD_BROKEN_BUTTON, show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=kb.moderation(parsed[1]))
    except TelegramAPIError:
        pass


@router.callback_query(F.data.regexp(r"^rjc:-?\d+$"))
async def cb_reject_comment(callback: CallbackQuery, cfg: Config) -> None:
    """«Свой текст»: дальше модератор отвечает на карточку сообщением."""
    if not _is_admin(callback.from_user.id, cfg):
        await callback.answer(texts.MOD_NO_RIGHTS, show_alert=True)
        return
    await callback.answer(texts.MOD_ASK_COMMENT, show_alert=True)


@router.callback_query(F.data.regexp(r"^rj:\d+:[a-z]+$"))
async def cb_reject_reason(callback: CallbackQuery, bot: Bot, db: Database,
                           cfg: Config, vault: Vault) -> None:
    if not _is_admin(callback.from_user.id, cfg):
        await callback.answer(texts.MOD_NO_RIGHTS, show_alert=True)
        return
    parsed = logic.parse_reject_callback(callback.data)
    if parsed is None:
        await callback.answer(texts.MOD_BROKEN_BUTTON, show_alert=True)
        return
    target, code = parsed
    row = await db.get_user(target)
    if row is None:
        await callback.answer(texts.MOD_BROKEN_BUTTON, show_alert=True)
        return

    reason = logic.REJECT_REASONS[code][0]
    # Шаг возврата зависит от возраста: «согласие родителя» у взрослого -
    # это промах модератора по кнопке, и отправлять взрослого за согласием
    # нельзя - его сценарий такого шага не содержит.
    back_to = logic.reject_back_to(code, vault.decrypt(dict(row).get("anketa_enc")))
    if not await _decide(db, callback, target, approved=False,
                         reason=reason, back_to=back_to):
        return
    await db.set_purge_after(target, cfg.purge_rejected_days)
    await callback.answer(texts.MOD_REJECTED)
    await _mark_card(callback, False)
    await _notify(bot, target, texts.REJECTED_WITH_REASON.format(reason=logic.esc(reason)))


@router.message(ServiceChatReply())
async def mod_reply(message: Message, bot: Bot, db: Database, cfg: Config) -> None:
    """Отказ свободным текстом: ответ на карточку модерации.

    Пользователь ищется по (chat_id, message_id) карточки, а не разбором её
    подписи: разбор ломался бы от любой правки формулировки в texts.py.
    """
    if not _is_admin(message.from_user.id, cfg):
        return
    replied = message.reply_to_message
    # Ответ не на сообщение бота - это переписка модераторов между собой.
    # Без этой проверки бот вклинивался в каждый их разговор с «это сообщение
    # не привязано к заявке», и служебный чат становился неюзабельным.
    if not (replied.from_user and replied.from_user.is_bot):
        return
    row = await db.user_by_mod_message(message.chat.id, replied.message_id)
    if row is None:
        await message.reply(texts.MOD_REPLY_NOT_A_CARD)
        return

    target = dict(row)
    if target["status"] != logic.ST_PENDING:
        await message.reply(texts.MOD_REPLY_NOT_PENDING)
        return

    comment = logic.reject_comment(message.text or message.caption)
    if not comment.ok:
        await message.reply(comment.error)
        return

    tg_id = target["tg_id"]
    if not await db.patch(
        tg_id, expected_status=logic.ST_PENDING,
        status=logic.ST_REJECTED, state=logic.WAIT_FIO,
        reject_reason=comment.value,
        reviewed_by=message.from_user.id, reviewed_at=utcnow(),
    ):
        await message.reply(texts.MOD_REPLY_NOT_PENDING)
        return

    await db.log_event(tg_id, "moderation_rejected",
                       {"by": message.from_user.id, "reason": comment.value})
    await db.set_purge_after(tg_id, cfg.purge_rejected_days)
    await message.reply(texts.MOD_COMMENT_SAVED)
    await _notify(bot, tg_id,
                  texts.REJECTED_WITH_REASON.format(reason=logic.esc(comment.value)))
    # Кнопки с карточки снимаем: заявка решена, второй модератор не должен
    # видеть живой «Одобрить».
    try:
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=replied.message_id,
            reply_markup=None,
        )
    except TelegramAPIError:
        pass


async def _notify(bot: Bot, tg_id: int, text: str) -> None:
    """Уведомление пользователя о решении.

    Пользователь мог заблокировать бота - решение модератора при этом уже
    сохранено, откатывать его не за чем.
    """
    try:
        await bot.send_message(tg_id, text, reply_markup=kb.remove())
    except TelegramAPIError as exc:
        log.warning("не удалось уведомить %s: %s", tg_id, exc)


async def _alert(bot: Bot, cfg: Config, text: str) -> None:
    try:
        await bot.send_message(cfg.contract_chat_id, text)
    except TelegramAPIError:
        log.exception("алерт не доставлен")


async def _mark_card(callback: CallbackQuery, approved: bool | None) -> None:
    """Снимаем кнопки, чтобы второй модератор не нажал по той же заявке."""
    if approved is None:
        verdict = "⏳ Уже обработана"
    else:
        verdict = "✅ Одобрено" if approved else "⛔ Отклонено"
    who = callback.from_user.username or callback.from_user.id
    try:
        await callback.message.edit_caption(
            caption=f"{callback.message.caption or ''}\n\n{verdict} — @{who}",
            reply_markup=None,
        )
    except TelegramAPIError:
        pass
