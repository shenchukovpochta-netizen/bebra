"""Цикл long polling и конвейер обработки апдейтов MAX.

Зеркало app/middlewares.py: клейм апдейта, загрузка пользователя, рейт-лимит,
гейт подписки - и только потом маршрутизация по состоянию. Отличие одно:
у MAX нет сквозного update_id, поэтому клейм идёт по устойчивому хэшу
содержимого (см. parse.dedup_id).
"""

from __future__ import annotations

import asyncio
import logging

from .. import logic, texts
from . import handlers, parse
from . import keyboards as kb
from .client import MaxAPIError
from .handlers import Ctx

log = logging.getLogger(__name__)


async def poll_forever(ctx: Ctx, stop: asyncio.Event) -> None:
    marker: int | None = None
    while not stop.is_set():
        try:
            batch = await ctx.cl.updates(marker)
        except MaxAPIError as exc:
            log.warning("getUpdates не удался: %s", exc)
            await asyncio.sleep(3)
            continue
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("опрос упал, продолжаю")
            await asyncio.sleep(3)
            continue
        marker = batch.get("marker", marker)
        for update in batch.get("updates", []):
            try:
                await process(ctx, update)
            except asyncio.CancelledError:
                raise
            except Exception:                           # noqa: BLE001
                # Ошибка одного апдейта не должна останавливать опрос;
                # клейм остаётся в processing, повтор переиграет его.
                log.exception("обработка апдейта MAX не удалась")


async def process(ctx: Ctx, update: dict) -> None:
    info = parse.describe(update)
    user_id, chat_id = info.get("user_id"), info.get("chat_id")
    if not user_id or chat_id is None:
        return
    if info.get("sender_is_bot"):
        return                     # свои и чужие боты конвейеру не интересны

    # Чат фиксации сюда не входит намеренно: туда пересылают формы и
    # обсуждают выдачу, и бот, влезающий с «не привязано к заявке»,
    # сделал бы чат неюзабельным. Входящих задач у бота там нет.
    service_chats = {ctx.cfg.admin_chat_id, ctx.cfg.contract_chat_id}
    is_service = chat_id in service_chats
    is_dialog = info["chat_type"] == "dialog"
    is_moderation_cb = (info["kind"] == "callback"
                        and logic.is_moderation_data(info.get("payload_cb")))
    is_service_reply = (is_service and info["kind"] == "message"
                        and info.get("reply_to_mid") is not None)
    if not is_dialog and not (is_service and (is_moderation_cb
                                              or is_service_reply)):
        return

    update_id = parse.dedup_id(update)
    if update_id is None:
        return
    if not await ctx.db.claim_update(update_id, user_id, info["kind"],
                                     {"kind": info["kind"],
                                      "chat_type": info["chat_type"]}):
        log.info("апдейт %s уже обработан, пропускаю", update_id)
        return

    if is_service and not is_dialog:
        await _dispatch_service(ctx, info)
    else:
        await _dispatch_dialog(ctx, update, info)
    await ctx.db.finish_update(update_id)


async def _dispatch_service(ctx: Ctx, info: dict) -> None:
    if info["kind"] == "callback":
        await handlers.cb_moderation(
            ctx, {"user_id": info["user_id"]}, info["callback_id"],
            info.get("payload_cb") or "", info["chat_id"])
        return
    await handlers.mod_reply(ctx, info["user_id"], info["chat_id"],
                             info["reply_to_mid"], info.get("text"))


async def _dispatch_dialog(ctx: Ctx, update: dict, info: dict) -> None:
    user_id = info["user_id"]
    row = await ctx.db.upsert_user(user_id, info.get("username"))
    user = dict(row)

    verdict = logic.rate_limit_verdict(user["rl_count"], ctx.cfg.rate_soft,
                                       ctx.cfg.rate_hard)
    if verdict == "drop":
        return
    if verdict == "warn":
        await handlers._say(ctx, user_id, texts.RATE_LIMITED)
        return

    if not await ctx.cl.is_member(ctx.cfg.channel_id, user_id):
        await handlers._say(
            ctx, user_id,
            texts.NOT_SUBSCRIBED.format(channel_url=logic.esc(ctx.cfg.channel_url)),
            kb.subscribe(ctx.cfg.channel_url))
        if info["kind"] == "callback":
            await ctx.cl.answer_callback(info["callback_id"], texts.SUB_NOT_FOUND)
        return

    if info["kind"] in ("start",):
        await handlers.start(ctx, user)
        return
    if info["kind"] == "callback":
        await _dispatch_callback(ctx, user, info)
        return
    await _dispatch_message(ctx, user, info)


async def _dispatch_callback(ctx: Ctx, user: dict, info: dict) -> None:
    payload = info.get("payload_cb") or ""
    cid = info["callback_id"]
    if payload == "check_sub":
        await ctx.cl.answer_callback(cid, "Подписка подтверждена")
        if user["state"] == logic.APPROVED:
            await handlers._say(ctx, user["tg_id"], texts.ALREADY_REGISTERED,
                                kb.main_menu())
        elif user["state"] in (logic.NEW, logic.WAIT_FIO):
            await ctx.db.patch(user["tg_id"], state=logic.WAIT_FIO)
            await handlers._say(ctx, user["tg_id"], texts.WELCOME)
        return
    if payload == "oferta_ok" and user["state"] == logic.WAIT_OFERTA:
        await handlers.cb_oferta(ctx, user, cid)
        return
    if payload == "same_addr":
        await handlers.cb_same_address(ctx, user, cid)
        return
    if payload == "confirm" and user["state"] == logic.CONFIRM:
        await handlers.cb_confirm(ctx, user, cid)
        return
    if payload == "restart" and user["state"] == logic.CONFIRM:
        await handlers.cb_restart(ctx, user, cid)
        return
    if payload == "sign" and user["state"] == logic.WAIT_SIGN:
        await handlers.cb_sign(ctx, user, cid)
        return
    if payload == "contract_mistake" and user["state"] == logic.WAIT_SIGN:
        await handlers.cb_mistake(ctx, user, cid)
        return
    if payload.startswith("menu:"):
        # Только из меню: старая кнопка, нажатая посреди анкеты, не должна
        # выдёргивать человека из шага приглашением «выберите действие».
        if user["state"] in (logic.APPROVED, logic.WAIT_SUPPORT):
            await handlers.cb_menu(ctx, user, cid, payload.split(":", 1)[1])
        else:
            await ctx.cl.answer_callback(cid, "Кнопка устарела, отправьте /start")
        return
    if payload == "support_cancel" and user["state"] == logic.WAIT_SUPPORT:
        await handlers.cb_support_cancel(ctx, user, cid)
        return
    await ctx.cl.answer_callback(cid, "Кнопка устарела, отправьте /start")


async def _dispatch_message(ctx: Ctx, user: dict, info: dict) -> None:
    text = info.get("text")
    attachments = info.get("attachments") or []
    state = user["state"]

    if (text or "").strip() == "/start":
        await handlers.start(ctx, user)
        return
    if not logic.is_known_state(state):
        log.warning("неизвестное состояние %r у %s - сбрасываю", state,
                    user["tg_id"])
        await ctx.db.patch(user["tg_id"], state=logic.WAIT_FIO)
        await handlers._say(ctx, user["tg_id"], texts.WELCOME)
        return

    if state == logic.NEW:
        await handlers.start(ctx, user)
    elif state == logic.WAIT_FIO:
        if text:
            await handlers.st_fio(ctx, user, text)
        else:
            await handlers._say(ctx, user["tg_id"], texts.FIO_AS_TEXT)
    elif state == logic.WAIT_OFERTA:
        await handlers._say(ctx, user["tg_id"], texts.OFERTA_PRESS_BUTTON)
    elif state == logic.WAIT_CONTACT:
        await handlers.st_contact(ctx, user, attachments)
    elif state in logic.ANKETA_BY_STATE:
        if text:
            await handlers.st_anketa(ctx, user, text)
        else:
            await handlers._say(ctx, user["tg_id"], texts.ANKETA_AS_TEXT)
    elif state in (logic.WAIT_DOC, logic.WAIT_PARENT_CONSENT):
        await handlers.st_upload(ctx, user, attachments)
    elif state == logic.CONFIRM:
        await handlers._say(ctx, user["tg_id"], texts.CONFIRM_PRESS_BUTTON)
    elif state == logic.PENDING:
        await handlers._say(ctx, user["tg_id"], texts.PENDING_WAIT)
    elif state == logic.WAIT_SIGN:
        await handlers.st_wait_sign(ctx, user)
    elif state == logic.WAIT_SUPPORT:
        if text:
            await handlers.st_support(ctx, user, text)
        else:
            await handlers._say(ctx, user["tg_id"], texts.SUPPORT_AS_TEXT)
    else:                                                # APPROVED
        await handlers.menu(ctx, user)
