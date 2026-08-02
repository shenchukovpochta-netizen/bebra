"""Одобрение и отклонение заявок из служебного чата."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery

from .. import keyboards as kb
from .. import logic, texts
from ..config import Config
from ..db import Database, utcnow

log = logging.getLogger(__name__)
router = Router(name="moderation")


@router.callback_query(F.data.regexp(r"^(approve|reject):-?\d+$"))
async def cb_moderate(callback: CallbackQuery, bot: Bot, db: Database, cfg: Config) -> None:
    if callback.from_user.id not in cfg.admins:
        await callback.answer(texts.MOD_NO_RIGHTS, show_alert=True)
        return

    parsed = logic.parse_moderation_callback(callback.data)
    if parsed is None:
        await callback.answer(texts.MOD_BROKEN_BUTTON, show_alert=True)
        return
    action, target = parsed

    if await db.get_user(target) is None:
        await callback.answer(texts.MOD_BROKEN_BUTTON, show_alert=True)
        return

    approved = action == "approve"
    # expected_status: два модератора, нажавшие одновременно, иначе оба довели
    # бы дело до конца, и пользователь получил бы два письма подряд.
    if not await db.patch(
        target,
        expected_status=logic.ST_PENDING,
        status=logic.ST_APPROVED if approved else logic.ST_REJECTED,
        state=logic.APPROVED if approved else logic.WAIT_FIO,
        reviewed_by=callback.from_user.id,
        reviewed_at=utcnow(),
    ):
        await callback.answer(texts.MOD_ALREADY_HANDLED, show_alert=True)
        await _mark_card(callback, approved=None)
        return
    # У каждого скана появляется дата удаления: отказы чистятся быстро,
    # одобренные живут ровно столько, сколько заявлено в политике.
    await db.set_purge_after(
        target, cfg.purge_approved_days if approved else cfg.purge_rejected_days
    )
    await db.log_event(target, "moderation_approved" if approved else "moderation_rejected",
                       {"by": callback.from_user.id})

    await callback.answer(texts.MOD_APPROVED if approved else texts.MOD_REJECTED)
    await _mark_card(callback, approved)

    try:
        if approved:
            await bot.send_message(target, texts.REGISTERED.format(video_url=cfg.video_url),
                                   reply_markup=kb.main_menu())
        else:
            await bot.send_message(target, texts.REJECTED)
    except TelegramAPIError as exc:
        # Пользователь мог заблокировать бота - решение модератора при этом
        # уже сохранено, откатывать его не за чем.
        log.warning("не удалось уведомить %s: %s", target, exc)


async def _mark_card(callback: CallbackQuery, approved: bool | None) -> None:
    """Снимаем кнопки, чтобы второй модератор не нажал по той же заявке."""
    if approved is None:
        verdict = "⏳ Уже обработана"
    else:
        verdict = "✅ Одобрено" if approved else "⛔ Отклонено"
    try:
        await callback.message.edit_caption(
            caption=f"{callback.message.caption or ''}\n\n{verdict} — @{callback.from_user.username or callback.from_user.id}",
            reply_markup=None,
        )
    except TelegramAPIError:
        pass
