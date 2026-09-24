"""«Входящие»: запись обращений из ботов и отправка ответов из панели.

Боты (Telegram и MAX) пишут сюда вопросы в поддержку, анкеты и заявки -
best-effort: сбой CRM не должен стоить человеку ответа бота, поэтому
ошибка записи уходит в лог (канал и id, без текста) и дальше не идёт.

Ответ из панели - строка в очереди `crm.inbox_messages`. Панель в
интернет не ходит, отправляет процесс бота этим циклом: Telegram - своим
ботом, MAX - клиентом MAX, Авито - через API. Застрявшее «отправляется»
(перезапуск, не записанный итог) само не повторяется, а становится
«не ушло»: отправка не идемпотентна, и повтор после таймаута - второе
сообщение человеку. Повторяет человек кнопкой.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import faq_i18n, i18n, texts
from .. import keyboards as kb
from .. import logic as bot_logic
from ..services.avito import AvitoError
from . import company, logic, mailing, notices, service

log = logging.getLogger(__name__)

SEND_BATCH = 20
INTERVAL = 15
# Без подписки на API сообщений Авито отвечает 402: спрашивать его каждую
# минуту бессмысленно - час паузы, и отметка в панели.
AVITO_PAUSE_ON_402 = 3600
# Первый опрос Авито не тянет всю историю: обращения - это то, что пришло
# после подключения, а не архив переписки за годы.
AVITO_FIRST_LOOKBACK = timedelta(hours=24)
# Список чатов Авито - страницами по 100; дальше пяти страниц за круг не
# идём: столько чатов за минуту не меняется даже после простоя.
AVITO_PAGE = 100
AVITO_PAGES = 5
# Сколько чатов без обращения помнить (одни служебные сообщения).
AVITO_SEEN_KEEP = 500
# Итог отправки: попыток записи и пауза между ними, секунд.
FINISH_TRIES = 3
FINISH_PAUSE = 1.0
# «Отправляется» дольше этого - итог не записался, очередь обращения стоит.
STUCK_MINUTES = 10
# Сигналы о новых: сколько за круг, с какого числа - сводкой, сколько
# попыток при сбое отправки.
ANNOUNCE_LIMIT = 50
ANNOUNCE_BURST = 3
ANNOUNCE_TRIES = 3


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

async def _bot_user(db: Any, tg_id: int) -> dict | None:
    if db is None:
        return None
    try:
        row = await db.get_user(tg_id)
    except Exception:                                   # noqa: BLE001
        return None
    return dict(row) if row else None


async def _finish(crm: Any, message_id: int, **fields: Any) -> None:
    """Итог отправки - с повтором: не записанный итог оставил бы ответ
    «отправляется» и закрыл бы обращению очередь. Не вышло и так -
    строку через STUCK_MINUTES сметёт круг (fail_stuck_inbox_out)."""
    for attempt in range(FINISH_TRIES):
        try:
            await crm.finish_inbox_out(message_id, **fields)
            return
        except Exception:                               # noqa: BLE001
            if attempt == FINISH_TRIES - 1:
                log.error("входящие: итог ответа %s не записан - снимется через %s мин",
                          message_id, STUCK_MINUTES)
                return
            await asyncio.sleep(FINISH_PAUSE * (attempt + 1))


async def _after_tg_reply(bot: Any, db: Any, user: dict | None, lang: str) -> None:
    """Ответ из панели - не тупик. Клиента с договором бот переводит в режим
    вопроса, как кнопка частых вопросов: его следующее сообщение придёт
    во «Входящие», а не в ловушку меню."""
    if db is None or not user or user.get("state") != bot_logic.APPROVED:
        return
    tg_id = int(user["tg_id"])
    try:
        if not await db.patch(tg_id, expected_state=bot_logic.APPROVED,
                              state=bot_logic.WAIT_SUPPORT):
            return
        prompt = faq_i18n.T.get(lang, {}).get("handoff", texts.FAQ_HANDOFF)
        await bot.send_message(tg_id, company.with_contact(prompt),
                               reply_markup=kb.support_cancel(i18n.norm(lang)))
    except Exception:                                   # noqa: BLE001
        log.warning("входящие: режим вопроса для %s не включён", tg_id)


def _tg_body(text: str, user: dict | None, lang: str) -> str:
    """Текст ответа в Telegram. Человеку посреди анкеты или договора
    свободный текст бот читает как шаг сценария - поэтому к ответу
    добавлен прямой контакт: отвечать менеджеру - туда, а не в бота."""
    body = i18n.t(lang, "SUPPORT_REPLY_USER").format(answer=bot_logic.esc(text))
    state = (user or {}).get("state")
    if user and state not in (bot_logic.APPROVED, bot_logic.WAIT_SUPPORT):
        contact = faq_i18n.T.get(lang, {}).get("contact", texts.FAQ_GUEST_CONTACT)
        body += "\n\n" + company.with_contact(contact)
    return body


async def send_once(bot: Any, crm: Any, cfg: Any, *, db: Any = None,
                    max_client: Any = None, avito: Any = None) -> bool:
    """Отправить один ответ из очереди. False - очередь пуста."""
    message = await crm.claim_inbox_out()
    if message is None:
        return False
    vault = service.inbox_vault(getattr(cfg, "inbox_key", ""))
    text = service.inbox_open(vault, message.get("body_enc")) if vault else None
    if not text or text.startswith("[текст зашифрован") or text == "[не расшифровано]":
        await _finish(crm, message["id"], ok=False,
                      error="текст не расшифрован: проверьте INBOX_KEY")
        return True
    channel, origin = message.get("channel"), message.get("origin")
    ext = str(message.get("thread_ext_id") or "")
    status, error, sent_id = "failed", "", None
    tg_user, lang = None, "ru"
    if message.get("thread_status") == "spam":
        error = "обращение помечено спамом"
    elif channel == "tg" and origin == "bot" and ext.isdigit():
        tg_user = await _bot_user(db, int(ext))
        lang = i18n.user_lang(tg_user) if tg_user else "ru"
        status, error = await mailing.send_one(bot, None, {"channel": "tg", "tg_id": ext},
                                               _tg_body(text, tg_user, lang))
    elif channel == "max" and origin == "max_bot" and ext.isdigit():
        # Состояние человека в MAX живёт в базе MAX-бота: режим вопроса
        # отсюда не включить. Ответить он может кнопкой «Поддержка».
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
    await _finish(crm, message["id"], ok=status == "sent", error=error or None,
                  ext_id=sent_id)
    if status == "sent" and tg_user is not None:
        await _after_tg_reply(bot, db, tg_user, lang)
    return True


# Память неудачных сигналов: номер обращения (0 - сводка) -> попыток.
# Сбой отправки (лимит Telegram, сеть) повторяется со следующим кругом,
# но не вечно: бота могли выгнать из чата.
_announce_fails: dict[int, int] = {}


async def _announced(crm: Any, threads: list[dict]) -> None:
    now = datetime.now(UTC)
    for thread in threads:
        await crm.update_inbox_thread(thread["id"], announced_at=now)
        _announce_fails.pop(int(thread["id"]), None)


async def announce_once(bot: Any, crm: Any, cfg: Any, *, limit: int = ANNOUNCE_LIMIT) -> int:
    """Сигнал в служебный чат о новых обращениях.

    Выключенное уведомление или нет чата - отметка ставится сразу: иначе
    включение обрушило бы в чат всё накопленное. Не ушло - повтор
    следующим кругом, до ANNOUNCE_TRIES раз. Больше ANNOUNCE_BURST
    сразу (первый опрос Авито, сбой) - одна сводка вместо пачки: лимит
    Telegram на группу срезал бы большую часть сигналов.
    """
    threads = await crm.inbox_to_announce(limit)
    if not threads:
        return 0
    state = await notices.settings(crm)
    chat = notices.chat_for(state, "inbox_new", getattr(cfg, "admin_chat_id", None))
    if not state.get("inbox_new", {}).get("enabled", True) or bot is None or not chat:
        await _announced(crm, threads)
        return len(threads)
    batches = ([(0, threads, logic.inbox_team_summary(threads))]
               if len(threads) > ANNOUNCE_BURST else
               [(int(t["id"]), [t], logic.inbox_team_text(t)) for t in threads])
    done = 0
    for key, group, text in batches:
        try:
            ok = await notices.send_team(crm, bot, "inbox_new", text, chat)
        except Exception:                               # noqa: BLE001
            ok = False
        if not ok:
            tries = _announce_fails[key] = _announce_fails.get(key, 0) + 1
            if tries < ANNOUNCE_TRIES:
                continue
            log.warning("входящие: сигнал о %s не ушёл за %s попыток",
                        logic.inbox_no(key) if key else "пачке", tries)
        _announce_fails.pop(key, None)
        await _announced(crm, group)
        done += len(group)
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


def _load_seen(raw: Any) -> dict[str, str]:
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


async def avito_once(crm: Any, avito: Any, cfg: Any) -> dict:
    """Один круг опроса Авито: новые сообщения чатов - во «Входящие».

    Чат перечитывается, только если его последнее сообщение сменилось:
    у чата с обращением курсор - ext_cursor, у чата без обращения (одни
    служебные сообщения) - память inbox_avito_seen, иначе его качали бы
    каждый круг. Своё сообщение (из приложения Авито или наш ответ)
    пишется ответом и снимает ожидание; служебные и заглушки - мимо.

    Нижняя граница - позднее из двух: начало подключения (первый удачный
    круг минус сутки) и срок хранения. Переписку старше срока удалила
    дневная чистка, и опрос не должен возвращать её обратно.
    """
    now = datetime.now(UTC)
    settings = await crm.settings()
    since_raw = settings.get("inbox_avito_since")
    since = logic._moment(since_raw) if since_raw else None
    first = since is None
    if first:
        since = now - AVITO_FIRST_LOOKBACK
    cutoff = max(since, now - timedelta(days=logic.INBOX_KEEP_DAYS))
    seen = _load_seen(settings.get("inbox_avito_seen"))
    seen_before = dict(seen)
    counts = {"chats": 0, "messages": 0}
    try:
        own = await avito.self_id()
        threads = {t["ext_id"]: t for t in await crm.inbox_threads(channel="avito",
                                                                   limit=5000)}
        for page in range(AVITO_PAGES):
            chats = await avito.chats(limit=AVITO_PAGE, offset=page * AVITO_PAGE)
            changed = False
            for chat in chats:
                last = chat.get("last_id")
                known = threads.get(chat["id"])
                if not last or (known and known.get("ext_cursor") == last):
                    continue
                if not known and seen.get(chat["id"]) == last:
                    continue
                if chat.get("updated") and chat["updated"] < cutoff:
                    continue
                changed = True
                counts["chats"] += 1
                fetched = await avito.messages(chat["id"])
                # Чат изменился, а сообщений нет - ответ неполный: курсор
                # не двигаем, перечитаем следующим кругом.
                thread_id, lost = None, not fetched
                for message in sorted(fetched, key=lambda m: m.get("created") or cutoff):
                    if message.get("noise") or (message.get("created") or cutoff) < cutoff:
                        continue
                    mine = message.get("author_id") == own
                    got = await record(
                        crm, cfg, channel="avito", origin="avito_api", ext_id=chat["id"],
                        direction="out" if mine else "in",
                        kind=message.get("kind", "text"), text=message.get("text"),
                        msg_id=message.get("id"), name=chat.get("name"),
                        subject=chat.get("subject"), subject_url=chat.get("url"),
                        at=message.get("created"),
                        author="avito-app" if mine else None, announce=not mine)
                    if got:
                        thread_id = got["thread_id"]
                        if got.get("message_id") is not None:
                            counts["messages"] += 1
                    else:
                        lost = True
                if thread_id is None and known:
                    thread_id = known["id"]
                # Курсор не двигается за сообщение, которое не записалось:
                # иначе чат пропускался бы, пока клиент не напишет ещё раз.
                # Повторное чтение безопасно - дубли отсекает номер сообщения.
                if lost:
                    continue
                if thread_id is not None:
                    await crm.update_inbox_thread(thread_id, ext_cursor=last)
                else:
                    seen.pop(chat["id"], None)
                    seen[chat["id"]] = last
            # Список идёт от свежих к старым: страница без изменений или
            # последняя страница - дальше смотреть нечего.
            if not changed or len(chats) < AVITO_PAGE:
                break
    except AvitoError as exc:
        await _avito_state(crm, ok=False, error=str(exc), every=_poll_every(cfg))
        raise
    if seen != seen_before:
        keep = dict(list(seen.items())[-AVITO_SEEN_KEEP:])
        await crm.set_setting("inbox_avito_seen", json.dumps(keep), by="avito")
    if first:
        # Начало отсчёта - первый УДАЧНЫЙ круг: ключи, заведённые до покупки
        # тарифа с API, не должны тянуть переписку за недели ожидания.
        await crm.set_setting("inbox_avito_since", since.isoformat(), by="avito")
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
            # Итог отправки не записался даже с повтором - строка висит
            # «отправляется» и держит очередь обращения. Не ждём перезапуска.
            if await crm.fail_stuck_inbox_out(older_minutes=STUCK_MINUTES):
                log.warning("входящие: зависшие ответы помечены «не ушло»")
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
