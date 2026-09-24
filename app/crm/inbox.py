"""«Входящие»: запись обращений из ботов и отправка ответов из панели.

Боты (Telegram и MAX) пишут сюда вопросы в поддержку, анкеты и заявки -
best-effort: сбой CRM не должен стоить человеку ответа бота, поэтому
ошибка записи уходит в лог (канал и id, без текста) и дальше не идёт.

Ответ из панели - строка в очереди `crm.inbox_messages`. Панель в
интернет не ходит, отправляет процесс бота этим циклом: Telegram - своим
ботом, MAX - клиентом MAX, Авито - через API. Застрявшее «отправляется»
после перезапуска не повторяется: отправка не идемпотентна, и повтор
после таймаута - второе сообщение человеку.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import i18n, texts
from .. import logic as bot_logic
from ..services.avito import AvitoError
from . import logic, mailing, notices, service

log = logging.getLogger(__name__)

SEND_BATCH = 20
INTERVAL = 15
# Без подписки на API сообщений Авито отвечает 402: спрашивать его каждую
# минуту бессмысленно - час паузы, и отметка в панели.
AVITO_PAUSE_ON_402 = 3600
# Первый опрос Авито не тянет всю историю: обращения - это то, что пришло
# после подключения, а не архив переписки за годы.
AVITO_FIRST_LOOKBACK = timedelta(hours=24)


async def record(crm: Any, cfg: Any, **fields: Any) -> dict | None:
    """Записать сообщение во «Входящие». Никогда не бросает исключений."""
    if crm is None:
        return None
    try:
        return await service.inbox_in(
            crm, service.inbox_vault(getattr(cfg, "inbox_key", "")), **fields)
    except Exception as exc:                            # noqa: BLE001
        # Без текста и без трассировки: в исключении базы бывают значения
        # строки, а у лога нет срока хранения, который есть у переписки.
        log.warning("входящие: %s/%s не записано (%s)", fields.get("channel"),
                    fields.get("ext_id"), type(exc).__name__)
        return None


# ─────────────────────── отправка ответов ───────────────────────

async def _lang(db: Any, tg_id: int) -> str:
    if db is None:
        return "ru"
    try:
        row = await db.get_user(tg_id)
    except Exception:                                   # noqa: BLE001
        return "ru"
    return i18n.user_lang(dict(row)) if row else "ru"


async def send_once(bot: Any, crm: Any, cfg: Any, *, db: Any = None,
                    max_client: Any = None, avito: Any = None) -> bool:
    """Отправить один ответ из очереди. False - очередь пуста."""
    message = await crm.claim_inbox_out()
    if message is None:
        return False
    vault = service.inbox_vault(getattr(cfg, "inbox_key", ""))
    text = service.inbox_open(vault, message.get("body_enc")) if vault else None
    if not text or text.startswith("[текст зашифрован") or text == "[не расшифровано]":
        await crm.finish_inbox_out(message["id"], ok=False,
                                   error="текст не расшифрован: проверьте INBOX_KEY")
        return True
    channel, origin = message.get("channel"), message.get("origin")
    ext = str(message.get("thread_ext_id") or "")
    status, error, sent_id = "failed", "", None
    if message.get("thread_status") == "spam":
        error = "обращение помечено спамом"
    elif channel == "tg" and origin == "bot" and ext.isdigit():
        lang = await _lang(db, int(ext))
        body = i18n.t(lang, "SUPPORT_REPLY_USER").format(answer=bot_logic.esc(text))
        status, error = await mailing.send_one(bot, None, {"channel": "tg", "tg_id": ext},
                                               body)
    elif channel == "max" and origin == "max_bot" and ext.isdigit():
        body = texts.SUPPORT_REPLY_USER.format(answer=bot_logic.esc(text))
        status, error = await mailing.send_one(bot, max_client,
                                               {"channel": "max", "max_id": ext}, body)
    elif channel == "avito" and origin == "avito_api":
        if avito is None or not getattr(avito, "ready", False):
            error = "Авито не подключён"
        else:
            try:
                got = await avito.send_text(ext, text)
                status, sent_id = "sent", str(got.get("id") or "") or None
            except Exception as exc:                    # noqa: BLE001
                error = f"Авито: {str(exc)[:180]}"
    else:
        error = "в этот канал бот не пишет"
    if status == "skipped":
        status = "failed"
    await crm.finish_inbox_out(message["id"], ok=status == "sent",
                               error=error or None, ext_id=sent_id)
    return True


async def announce_once(bot: Any, crm: Any, cfg: Any, *, limit: int = 20) -> int:
    """Сигнал в служебный чат о новых обращениях - один раз на обращение.

    Отметка ставится и при выключенном уведомлении: иначе включение
    обрушило бы в чат всё накопленное за время тишины.
    """
    done = 0
    for thread in await crm.inbox_to_announce(limit):
        try:
            await notices.send_team(crm, bot, "inbox_new", logic.inbox_team_text(thread),
                                    getattr(cfg, "admin_chat_id", None))
        except Exception:                               # noqa: BLE001
            log.warning("входящие: сигнал о %s не ушёл", logic.inbox_no(thread["id"]))
        await crm.update_inbox_thread(thread["id"], announced_at=datetime.now(UTC))
        done += 1
    return done


# ─────────────────────────── Авито ───────────────────────────

def _poll_every(cfg: Any) -> int:
    return max(int(getattr(cfg, "avito_poll_seconds", 60) or 60), 30)


async def _avito_state(crm: Any, *, ok: bool, error: str = "", every: int = 60) -> None:
    """Состояние опроса - в settings: панель видит его, не ходя в Авито.
    Пишется каждый круг: по свежести отметки панель понимает, живой ли опрос,
    а период круга (every) не даёт редкому опросу выглядеть мёртвым."""
    value = json.dumps({"ok": ok, "at": datetime.now(UTC).isoformat(),
                        "error": error[:300], "every": every}, ensure_ascii=False)
    try:
        await crm.set_setting("inbox_avito_state", value, by="avito")
    except Exception:                                   # noqa: BLE001
        log.warning("входящие: состояние Авито не записано")


async def avito_once(crm: Any, avito: Any, cfg: Any) -> dict:
    """Один круг опроса Авито: новые сообщения чатов - во «Входящие».

    Чат перечитывается, только если его последнее сообщение сменилось
    (ext_cursor). Своё сообщение (ответ из приложения Авито или наш)
    пишется ответом и снимает ожидание; служебные и заглушки - мимо.
    """
    settings = await crm.settings()
    since_raw = settings.get("inbox_avito_since")
    since = logic._moment(since_raw) if since_raw else None
    if since is None:
        since = datetime.now(UTC) - AVITO_FIRST_LOOKBACK
        await crm.set_setting("inbox_avito_since", since.isoformat(), by="avito")
    counts = {"chats": 0, "messages": 0}
    try:
        own = await avito.self_id()
        chats = await avito.chats()
        threads = {t["ext_id"]: t for t in await crm.inbox_threads(channel="avito",
                                                                   limit=5000)}
        for chat in chats:
            last = chat.get("last_id")
            known = threads.get(chat["id"])
            if not last or (known and known.get("ext_cursor") == last):
                continue
            if chat.get("updated") and chat["updated"] < since:
                continue
            counts["chats"] += 1
            thread_id, lost = None, False
            for message in sorted(await avito.messages(chat["id"]),
                                  key=lambda m: m.get("created") or since):
                if message.get("noise") or (message.get("created") or since) < since:
                    continue
                mine = message.get("author_id") == own
                got = await record(
                    crm, cfg, channel="avito", origin="avito_api", ext_id=chat["id"],
                    direction="out" if mine else "in", kind=message.get("kind", "text"),
                    text=message.get("text"), msg_id=message.get("id"),
                    name=chat.get("name"), subject=chat.get("subject"),
                    subject_url=chat.get("url"), at=message.get("created"),
                    author="avito-app" if mine else None, announce=not mine)
                if got:
                    thread_id = got["thread_id"]
                    if got.get("message_id") is not None:
                        counts["messages"] += 1
                else:
                    lost = True
            if thread_id is None and known:
                thread_id = known["id"]
            # Курсор не двигается за сообщение, которое не записалось: иначе
            # чат пропускался бы, пока клиент не напишет ещё раз. Повторное
            # чтение безопасно - дубли отсекает номер сообщения Авито.
            if thread_id is not None and not lost:
                await crm.update_inbox_thread(thread_id, ext_cursor=last)
    except AvitoError as exc:
        await _avito_state(crm, ok=False, error=str(exc), every=_poll_every(cfg))
        raise
    await _avito_state(crm, ok=True, every=_poll_every(cfg))
    return counts


async def inbox_loop(bot: Any, crm: Any, cfg: Any, *, db: Any = None,
                     max_client: Any = None, avito: Any = None,
                     interval: int = INTERVAL) -> None:
    """Фоновый круг «Входящих»: ответы из очереди, сигналы, опрос Авито."""
    try:
        stuck = await crm.fail_stuck_inbox_out()
        if stuck:
            log.warning("входящие: %s ответов «отправляется» после перезапуска - "
                        "помечены «не ушло»", stuck)
    except Exception:                                   # noqa: BLE001
        log.exception("входящие: очередь не проверена при старте")
    poll_every = _poll_every(cfg)
    if avito is None or not avito.ready:
        # Ключи убрали - прежняя отметка опроса не должна висеть в панели
        # плашкой «опрос не работает» вечно.
        try:
            await crm.set_setting("inbox_avito_state", "", by="avito")
        except Exception:                               # noqa: BLE001
            log.warning("входящие: отметка Авито не сброшена")
    next_avito = 0.0
    while True:
        try:
            for _ in range(SEND_BATCH):
                if not await send_once(bot, crm, cfg, db=db, max_client=max_client,
                                       avito=avito):
                    break
            await announce_once(bot, crm, cfg)
            if avito is not None and avito.ready and time.monotonic() >= next_avito:
                next_avito = time.monotonic() + poll_every
                try:
                    counts = await avito_once(crm, avito, cfg)
                    if counts["messages"]:
                        log.info("Авито: новых сообщений %s в %s чатах",
                                 counts["messages"], counts["chats"])
                except AvitoError as exc:
                    if exc.status == 402:
                        next_avito = time.monotonic() + AVITO_PAUSE_ON_402
                    log.warning("Авито: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.exception("входящие: круг не удался")
        await asyncio.sleep(interval)
