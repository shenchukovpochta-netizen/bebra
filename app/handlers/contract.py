"""Договор: формирование, выдача на подпись, подпись и фиксация.

Порядок такой: утверждающий одобряет заявку -> бот заполняет docx и отдаёт его
пользователю -> пользователь нажимает «Подписываю» -> подписанный экземпляр
уходит в чат фиксации, а паспортные данные стираются из базы.

Состояние wait_sign отделено от approved намеренно: между «заявку одобрили»
и «договор подписан» велосипед выдавать нельзя, а по одному лишь approved
эти два случая не различить.
"""

from __future__ import annotations

import logging
from datetime import datetime
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
             signed_at: str, issued_at: datetime | None) -> dict[str, Any]:
    # Дата договора берётся из момента выдачи, а не из «сегодня». Договор
    # пересобирается ещё дважды - при переотправке и при подписании, - и без
    # фиксации даты подписанный экземпляр отличался бы от прочитанного:
    # другая дата в шапке, другой отпечаток, и сохранённый при выдаче хэш
    # переставал бы соответствовать чему бы то ни было.
    ctx = logic.contract_context(data, anketa, number=number,
                                 today=issued_at.date() if issued_at else None)
    # Данные выдачи (вин-номера, комплектация, срок, оплата) - из ответа
    # оператора; до него в документ ушли бы прочерки, но issue() зовётся
    # только после сохранения issue_data.
    ctx.update(logic.issue_context(data.get("issue_data")))
    # purge_days идёт из конфигурации, а не из шаблона: сроки в договоре
    # обязаны совпадать с тем, по которым ретеншен реально удаляет сканы.
    ctx["purge_days"] = str(cfg.purge_approved_days)
    ctx["signed_at"] = signed_at
    return ctx


def _filename(number: str) -> str:
    return f"dogovor-{number}.docx"


def _soglasie_filename(number: str) -> str:
    return f"soglasie-pdn-{number}.docx"


def _build_soglasie(cfg: Config, data: dict, anketa: dict, *, number: str,
                    signed_at: str, issued_at: datetime | None) -> tuple[bytes, str]:
    """Согласие на обработку ПДн - приложение к договору.

    Собирается из того же контекста, что договор: реквизиты арендатора,
    номер и дата договора, момент подписи. Отпечаток у приложения свой -
    build() вписывает в плашку хэш самого приложения, а не договора.
    """
    ctx = _context(cfg, data, anketa, number=number, signed_at=signed_at,
                   issued_at=issued_at)
    try:
        return contract_service.build(cfg.soglasie_template, ctx)
    except (contract_service.TemplateProblem, OSError) as exc:
        raise ContractProblem(str(exc)) from exc


async def _build(cfg: Config, data: dict, anketa: dict, *, number: str,
                 signed_at: str, issued_at: datetime | None) -> tuple[bytes, str]:
    ctx = _context(cfg, data, anketa, number=number, signed_at=signed_at,
                   issued_at=issued_at)
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

    number = data.get("contract_no") or logic.contract_number(
        await db.next_contract_seq(), prefix=cfg.contract_prefix)
    # Момент выдачи фиксируется до сборки и тем же значением уходит в базу:
    # дата в шапке договора и contract_issued_at обязаны совпадать, иначе
    # пересборка при подписании даст другой документ.
    issued_at = data.get("contract_issued_at") or utcnow()
    pdf, digest = await _build(cfg, data, anketa, number=number,
                               signed_at=UNSIGNED, issued_at=issued_at)
    sog, sog_digest = _build_soglasie(cfg, data, anketa, number=number,
                                      signed_at=UNSIGNED, issued_at=issued_at)
    path, _ = files.store(cfg.storage_dir, tg_id, "contract", pdf)
    sog_path, _ = files.store(cfg.storage_dir, tg_id, "soglasie", sog)

    if not await db.patch(
        tg_id, expected_status=logic.ST_APPROVED,
        state=logic.WAIT_SIGN,
        contract_no=number, contract_path=str(path), contract_sha256=digest,
        soglasie_path=str(sog_path), soglasie_sha256=sog_digest,
        contract_status=logic.CT_ISSUED, contract_issued_at=issued_at,
    ):
        # Статус успел уехать - договор уже неактуален, файлы на диске не нужны.
        files.remove(path)
        files.remove(sog_path)
        raise ContractProblem(f"статус {tg_id} изменился, договор не выдан")

    # Предыдущая выдача (оператор поправил вин-номер и ответил ещё раз)
    # оставляет свои файлы на диске, а в базе их путей уже нет - ретеншен
    # такие не найдёт никогда, и договор с паспортными данными пролежит
    # там вечно. Удаляем сразу после того, как база указала на новые.
    for stale in (data.get("contract_path"), data.get("soglasie_path")):
        if stale and stale not in (str(path), str(sog_path)):
            files.remove(stale)

    await db.log_event(tg_id, "contract_issued", {"number": number})
    # Приложение уходит ПЕРВЫМ, договор с кнопками - последним: кнопки
    # подписи должны оказаться на нижнем сообщении, у самого экрана.
    await bot.send_document(
        tg_id,
        BufferedInputFile(sog, filename=_soglasie_filename(number)),
        caption=texts.SOGLASIE_CAPTION.format(number=logic.esc(number)),
    )
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
    # Дальше не Акт, а оплата: порядок «ознакомление - подписание - оплата -
    # получение». Акт приёма-передачи уйдёт после того, как оператор
    # подтвердит поступление денег.
    if not await db.patch(user["tg_id"], expected_state=logic.WAIT_SIGN,
                          state=logic.WAIT_PAYMENT,
                          contract_status=logic.CT_SIGNED,
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

    docs_ok = True
    try:
        pdf, digest = await _build(cfg, data, anketa, number=number, signed_at=stamp,
                                   issued_at=data.get("contract_issued_at"))
        sog, sog_digest = _build_soglasie(cfg, data, anketa, number=number,
                                          signed_at=stamp,
                                          issued_at=data.get("contract_issued_at"))
    except ContractProblem:
        # Подпись уже зафиксирована в базе, откатывать её нельзя. Этап оплаты
        # идёт дальше и без экземпляров, а с шаблоном разберутся по алерту.
        log.exception("подписанный экземпляр %s не собрался", tg_id)
        await bot.send_message(cfg.contract_chat_id, texts.CONTRACT_ALERT_FAILED.format(
            tg_id=tg_id, reason="не удалось пересобрать подписанный экземпляр"))
        docs_ok = False

    await db.log_event(tg_id, "contract_signed", {"number": number})
    await db.set_purge_after(tg_id, cfg.purge_approved_days)

    if docs_ok:
        old_path = data.get("contract_path")
        old_sog = data.get("soglasie_path")
        path, _ = files.store(cfg.storage_dir, tg_id, "contract", pdf)
        sog_path, _ = files.store(cfg.storage_dir, tg_id, "soglasie", sog)
        await db.patch(tg_id, contract_path=str(path), contract_sha256=digest,
                       soglasie_path=str(sog_path), soglasie_sha256=sog_digest)
        if old_path and old_path != str(path):
            files.remove(old_path)      # неподписанный экземпляр больше не нужен
        if old_sog and old_sog != str(sog_path):
            files.remove(old_sog)

        # Отправка идёт через bot по tg_id, а не через callback.message: кнопка
        # подписи живёт в чате сутками, а у старого сообщения Telegram отдаёт
        # недоступный объект без метода answer - подпись уже зафиксирована в базе,
        # и падение здесь оставило бы человека без экземпляра договора.
        await bot.send_document(
            tg_id,
            BufferedInputFile(sog, filename=_soglasie_filename(number)),
            caption=texts.SOGLASIE_SIGNED_CAPTION.format(
                number=logic.esc(number), signed_at=stamp),
        )
        await bot.send_document(
            tg_id,
            BufferedInputFile(pdf, filename=_filename(number)),
            caption=texts.CONTRACT_SIGNED_USER.format(
                number=logic.esc(number), signed_at=stamp,
                video_url=logic.esc(cfg.video_url)),
            reply_markup=kb.main_menu(),
        )

        await _fix(bot, db, cfg, data, anketa, pdf=pdf, number=number,
                   signed_at=stamp, digest=digest)
        try:
            await bot.send_document(
                cfg.fix_chat_id,
                BufferedInputFile(sog, filename=_soglasie_filename(number)),
                caption=texts.SOGLASIE_FIX_CARD.format(
                    number=logic.esc(number), signed_at=stamp, sha256=sog_digest),
                message_thread_id=cfg.fix_topic_id,
            )
        except TelegramAPIError:
            log.exception("согласие по договору %s не доставлено в чат фиксации",
                          number)
            await db.log_event(tg_id, "soglasie_fix_failed", {"number": number})

    # Форма фиксации - утверждающему: он дозаполняет прочерки и пересылает
    # в тему фиксации, где её разбирает другой бот. Собрать её можно только
    # пока жива анкета: адреса и телефоны стираются после подписи Акта приёма.
    try:
        await bot.send_message(cfg.contract_chat_id,
                               texts.FIXATION_FORM_INTRO.format(number=logic.esc(number)))
        await bot.send_message(cfg.contract_chat_id,
                               logic.fixation_form(data, anketa, data.get("issue_data")))
    except TelegramAPIError:
        # Подпись уже состоялась, откатывать её из-за формы нельзя.
        log.exception("форма фиксации по договору %s не доставлена", number)
        await db.log_event(tg_id, "fixation_form_failed", {"number": number})

    # Этап оплаты: клиенту - сумма и кнопка «Я оплатил(а)», оператору -
    # карточка с кнопкой «Оплата получена». Анкета НЕ стирается: паспортные
    # данные печатаются ещё и в Акте приёма-передачи.
    await bot.send_message(
        tg_id, texts.PAY_PROMPT.format(price=logic.esc(rent_price(data)),
                                       pay_url=logic.esc(cfg.pay_url)),
        reply_markup=kb.paid(cfg.pay_url))
    try:
        sent = await bot.send_message(
            cfg.contract_chat_id, pay_card({**data, "contract_no": number}),
            reply_markup=kb.pay_confirm(tg_id))
        await db.patch(tg_id, pay_chat_id=sent.chat.id,
                       pay_message_id=sent.message_id)
    except TelegramAPIError:
        # Без карточки оператор не подтвердит оплату кнопкой - молчать нельзя.
        log.exception("карточка оплаты по договору %s не доставлена", number)
        await db.log_event(tg_id, "pay_card_failed", {"number": number})


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
async def cb_mistake(callback: CallbackQuery, bot: Bot, db: Database,
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
    await bot.send_message(user["tg_id"], texts.CONTRACT_MISTAKE)
    await bot.send_message(user["tg_id"], texts.WELCOME, reply_markup=kb.remove())


@router.message(StateIs(logic.WAIT_SIGN))
async def st_wait_sign(message: Message, bot: Bot, db: Database, cfg: Config,
                       vault: Vault, user: dict) -> None:
    """Любое сообщение в ожидании подписи возвращает договор с кнопками.

    Кнопки живут только на том сообщении, где лежит файл договора. Если человек очистил
    переписку или потерял его в ленте, подписать становится нечем: /start сюда
    же и упирается, а никакого другого выхода из состояния нет. Поэтому
    договор пересобирается и отправляется заново - это дешевле, чем тупик,
    из которого выводить придётся руками.
    """
    row = await db.get_user(user["tg_id"])
    data = dict(row) if row else dict(user)
    anketa = vault.decrypt(data.get("anketa_enc"))
    number = data.get("contract_no") or ""
    try:
        pdf, _ = await _build(cfg, data, anketa, number=number, signed_at=UNSIGNED,
                              issued_at=data.get("contract_issued_at"))
        sog, _ = _build_soglasie(cfg, data, anketa, number=number,
                                 signed_at=UNSIGNED,
                                 issued_at=data.get("contract_issued_at"))
    except ContractProblem:
        log.exception("не удалось переотправить договор %s", user["tg_id"])
        await message.answer(texts.CONTRACT_PRESS_BUTTON)
        return
    await bot.send_document(
        user["tg_id"],
        BufferedInputFile(sog, filename=_soglasie_filename(number)),
        caption=texts.SOGLASIE_CAPTION.format(number=logic.esc(number)),
    )
    await bot.send_document(
        user["tg_id"],
        BufferedInputFile(pdf, filename=_filename(number)),
        caption=texts.CONTRACT_RESEND.format(number=logic.esc(number)),
        reply_markup=kb.sign_contract(),
    )


# ─────────────────────────── оплата ───────────────────────────

def rent_price(data: dict) -> str:
    """Сумма и способ оплаты из данных выдачи оператора."""
    return str((data.get("issue_data") or {}).get("rent_price") or "—")


def pay_card(data: dict, price: str | None = None) -> str:
    """Карточка ожидания оплаты для служебного чата.

    Одна функция на все три места (выдача карточки, обновление суммы,
    пометка «оплачено»): три копии формата разъезжались бы, и оператор
    сверял бы поступление с суммой из устаревшей карточки.
    """
    return texts.PAY_CARD.format(
        number=logic.esc(data.get("contract_no") or ""),
        fio=logic.esc(data.get("full_name")),
        tg_id=data.get("tg_id"),
        price=logic.esc(price if price is not None else rent_price(data)),
    )


async def to_operator(cfg: Config, data: dict, send: Any) -> bool:
    """Отправить что-либо оператору ОТВЕТОМ на карточку оплаты.

    Всё про оплату должно лежать в одной ветке чата: и сигнал «я оплатил»,
    и чек, и кнопка подтверждения. Карточку могли удалить - тогда шлём
    без привязки: потерять чек хуже, чем потерять красивую вложенность.
    send получает reply_to и возвращает корутину отправки.
    """
    reply_to = (data.get("pay_message_id")
                if data.get("pay_chat_id") == cfg.contract_chat_id else None)
    try:
        await send(reply_to)
        return True
    except TelegramAPIError:
        if reply_to is None:
            log.exception("оплата %s: сообщение оператору не доставлено",
                          data.get("tg_id"))
            return False
    try:
        await send(None)
        return True
    except TelegramAPIError:
        log.exception("оплата %s: сообщение оператору не доставлено",
                      data.get("tg_id"))
        return False


@router.callback_query(StateIs(logic.WAIT_PAYMENT), F.data == "paid")
async def cb_paid(callback: CallbackQuery, bot: Bot, db: Database, cfg: Config,
                  user: dict) -> None:
    """Клиент сообщает, что перевёл деньги.

    Состояние НЕ меняется: поступление проверяет оператор и подтверждает
    кнопкой «Оплата получена» на своей карточке - клиентская кнопка лишь
    зовёт его проверить.
    """
    await callback.answer(texts.PAY_NUDGE_TOAST)
    await db.log_event(user["tg_id"], "client_paid_claim")
    row = await db.get_user(user["tg_id"])
    data = dict(row) if row else dict(user)
    card = texts.PAY_NUDGE_CARD.format(
        fio=logic.esc(data.get("full_name")), tg_id=user["tg_id"],
        number=logic.esc(data.get("contract_no") or ""),
        price=logic.esc(rent_price(data)))
    await to_operator(cfg, data, lambda reply_to: bot.send_message(
        cfg.contract_chat_id, card, reply_to_message_id=reply_to))


@router.message(StateIs(logic.WAIT_PAYMENT), F.photo | F.document)
async def st_payment_receipt(message: Message, bot: Bot, db: Database,
                             cfg: Config, user: dict) -> None:
    """Чек об оплате уходит оператору - так же, как данные выдачи.

    Без этого чек оставался в переписке с ботом, куда оператор не смотрит:
    бот просил прислать его «сюда в чат», и просьба была пустой.
    Файл пересылается по file_id, без скачивания на диск: это платёжный
    документ, хранить его у себя боту незачем.
    """
    row = await db.get_user(user["tg_id"])
    data = dict(row) if row else dict(user)
    caption = texts.PAY_RECEIPT_CARD.format(
        fio=logic.esc(data.get("full_name")), tg_id=user["tg_id"],
        number=logic.esc(data.get("contract_no") or ""),
        price=logic.esc(rent_price(data)))
    # Пересылаем тем же типом, каким прислали: sendPhoto с file_id документа
    # Telegram отвергает с 400, и чек не дошёл бы вовсе.
    is_photo = bool(message.photo)
    file_id = (message.photo[-1].file_id if is_photo
               else message.document.file_id)
    send = bot.send_photo if is_photo else bot.send_document
    delivered = await to_operator(cfg, data, lambda reply_to: send(
        cfg.contract_chat_id, file_id, caption=caption,
        reply_to_message_id=reply_to))

    if delivered:
        await db.log_event(user["tg_id"], "payment_receipt")
        await message.answer(texts.PAY_RECEIPT_SENT,
                             reply_markup=kb.paid(cfg.pay_url))
        return
    # Чек не дошёл - молчать нельзя: человек считает, что оплату уже видят.
    await db.log_event(user["tg_id"], "payment_receipt_failed")
    await message.answer(texts.PAY_RECEIPT_FAILED,
                         reply_markup=kb.paid(cfg.pay_url))


@router.message(StateIs(logic.WAIT_PAYMENT))
async def st_wait_payment(message: Message, cfg: Config, user: dict) -> None:
    """Любое другое сообщение на этапе оплаты возвращает сумму, ссылку
    и кнопки: потерянное в ленте сообщение с кнопкой - это тупик
    без переотправки."""
    await message.answer(
        texts.PAY_WAIT.format(price=logic.esc(rent_price(user)),
                              pay_url=logic.esc(cfg.pay_url)),
        reply_markup=kb.paid(cfg.pay_url))


# ─────────────────────── Акт приёма-передачи ───────────────────────

def _act_filename(kind: str, number: str) -> str:
    return f"akt-{kind}-{number}.docx"


def _act_context(cfg: Config, data: dict, anketa: dict, *,
                 signed_at: str) -> dict[str, Any]:
    """Контекст актов: реквизиты договора + данные выдачи + даты акта."""
    ctx = _context(cfg, data, anketa, number=data.get("contract_no") or "",
                   signed_at=signed_at, issued_at=data.get("contract_issued_at"))
    ctx["act_date"] = utcnow().strftime("%d.%m.%Y")
    ret = dict(data.get("return_data") or {})
    ctx["return_notes"] = str(ret.get("return_notes") or "—")
    ctx["return_date"] = str(ret.get("return_date") or
                             utcnow().strftime("%d.%m.%Y"))
    return ctx


def _build_act(cfg: Config, template: Any, ctx: dict) -> tuple[bytes, str]:
    try:
        return contract_service.build(template, ctx)
    except (contract_service.TemplateProblem, OSError) as exc:
        raise ContractProblem(str(exc)) from exc


async def send_act_in(bot: Bot, db: Database, cfg: Config, data: dict,
                      anketa: dict) -> None:
    """Выдать Акт приёма-передачи на подпись.

    Сбой сборки не должен запирать человека в wait_act_sign без документа:
    состояние откатывается в approved, оператору уходит алерт.
    """
    tg_id = data["tg_id"]
    number = data.get("contract_no") or ""
    try:
        docx, _ = _build_act(cfg, cfg.act_in_template,
                             _act_context(cfg, data, anketa, signed_at=UNSIGNED))
    except ContractProblem as exc:
        log.exception("акт приёма для %s не собрался", tg_id)
        await db.patch(tg_id, expected_state=logic.WAIT_ACT_SIGN,
                       state=logic.APPROVED)
        await db.log_event(tg_id, "act_in_failed", {"error": str(exc)})
        await bot.send_message(cfg.contract_chat_id,
                               texts.CONTRACT_ALERT_FAILED.format(
                                   tg_id=tg_id, reason=logic.esc(str(exc))))
        await bot.send_message(tg_id,
                               texts.REGISTERED.format(
                                   video_url=logic.esc(cfg.video_url)),
                               reply_markup=kb.main_menu())
        return
    await bot.send_document(
        tg_id, BufferedInputFile(docx, filename=_act_filename("priema", number)),
        caption=texts.ACT_IN_READY.format(number=logic.esc(number)),
        reply_markup=kb.sign_act(),
    )


@router.callback_query(StateIs(logic.WAIT_ACT_SIGN), F.data == "act_sign")
async def cb_act_sign(callback: CallbackQuery, bot: Bot, db: Database,
                      cfg: Config, vault: Vault, user: dict) -> None:
    """Подпись Акта приёма-передачи: с этого момента имущество передано."""
    signed_at = utcnow()
    if not await db.patch(user["tg_id"], expected_state=logic.WAIT_ACT_SIGN,
                          state=logic.APPROVED, act_in_signed_at=signed_at):
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
        docx, digest = _build_act(cfg, cfg.act_in_template,
                                  _act_context(cfg, data, anketa,
                                               signed_at=stamp))
    except ContractProblem:
        # Подпись зафиксирована; без пересобранного экземпляра остаёмся,
        # но прокат не блокируем.
        log.exception("подписанный акт приёма %s не собрался", tg_id)
        await bot.send_message(cfg.contract_chat_id,
                               texts.CONTRACT_ALERT_FAILED.format(
                                   tg_id=tg_id,
                                   reason="акт приёма не пересобрался"))
        return

    path, _ = files.store(cfg.storage_dir, tg_id, "actin", docx)
    await db.patch(tg_id, act_in_path=str(path), act_in_sha256=digest)
    await db.log_event(tg_id, "act_in_signed", {"number": number})

    await bot.send_document(
        tg_id, BufferedInputFile(docx, filename=_act_filename("priema", number)),
        caption=texts.ACT_IN_SIGNED.format(
            number=logic.esc(number), signed_at=stamp,
            video_url=logic.esc(cfg.video_url)),
        reply_markup=kb.main_menu(),
    )
    try:
        await bot.send_document(
            cfg.fix_chat_id,
            BufferedInputFile(docx, filename=_act_filename("priema", number)),
            caption=texts.ACT_FIX_CARD.format(
                title="✅ Акт приёма-передачи", number=logic.esc(number),
                fio=logic.esc(data.get("full_name")), tg_id=tg_id,
                signed_at=stamp, sha256=digest),
            message_thread_id=cfg.fix_topic_id,
        )
    except TelegramAPIError:
        log.exception("акт приёма %s не доставлен в чат фиксации", number)
        await db.log_event(tg_id, "act_in_fix_failed", {"number": number})

    # Приглашение возврата: когда велосипед вернут, оператор ответит на это
    # сообщение данными возврата, и бот соберёт Акт возврата.
    try:
        sent = await bot.send_message(
            cfg.contract_chat_id,
            texts.RETURN_PROMPT.format(number=logic.esc(number), tg_id=tg_id))
        await db.patch(tg_id, return_chat_id=sent.chat.id,
                       return_message_id=sent.message_id)
    except TelegramAPIError:
        log.exception("приглашение возврата по %s не доставлено", number)

    # Паспортные данные дальше боту не нужны: договор и акт сформированы,
    # экземпляры у сторон и в чате фиксации.
    await db.clear_anketa(tg_id)


@router.callback_query(StateIs(logic.WAIT_ACT_SIGN), F.data == "act_mistake")
async def cb_act_mistake(callback: CallbackQuery, bot: Bot, db: Database,
                         cfg: Config, user: dict) -> None:
    """Ошибка в акте - чинится данными выдачи, а не переигрыванием анкеты:
    оператор отвечает на приглашение выдачи ещё раз, бот пересобирает акт."""
    await callback.answer()
    await db.log_event(user["tg_id"], "act_in_mistake")
    await bot.send_message(user["tg_id"], texts.ACT_MISTAKE_SENT)
    try:
        row = await db.get_user(user["tg_id"])
        number = (dict(row).get("contract_no") if row else "") or ""
        await bot.send_message(cfg.contract_chat_id,
                               texts.ACT_MISTAKE_ALERT.format(
                                   tg_id=user["tg_id"],
                                   number=logic.esc(number)))
    except TelegramAPIError:
        log.exception("алерт об ошибке акта не доставлен")


@router.message(StateIs(logic.WAIT_ACT_SIGN))
async def st_wait_act_sign(message: Message, bot: Bot, db: Database,
                           cfg: Config, vault: Vault, user: dict) -> None:
    """Потерянный акт переотправляется на любое сообщение - как договор."""
    row = await db.get_user(user["tg_id"])
    data = dict(row) if row else dict(user)
    anketa = vault.decrypt(data.get("anketa_enc"))
    number = data.get("contract_no") or ""
    try:
        docx, _ = _build_act(cfg, cfg.act_in_template,
                             _act_context(cfg, data, anketa,
                                          signed_at=UNSIGNED))
    except ContractProblem:
        log.exception("не удалось переотправить акт %s", user["tg_id"])
        await message.answer(texts.ACT_PRESS_BUTTON)
        return
    await bot.send_document(
        user["tg_id"],
        BufferedInputFile(docx, filename=_act_filename("priema", number)),
        caption=texts.ACT_RESEND.format(number=logic.esc(number)),
        reply_markup=kb.sign_act(),
    )


# ─────────────────────── Акт возврата ───────────────────────

async def send_act_out(bot: Bot, db: Database, cfg: Config, vault: Vault,
                       tg_id: int) -> None:
    """Собрать Акт возврата по данным оператора и отдать на подтверждение.

    Анкета к этому моменту уже стёрта - в акте только ФИО и номер договора,
    паспортные данные заменяет отсылка к договору.
    """
    row = await db.get_user(tg_id)
    if row is None:
        raise ContractProblem(f"нет записи о пользователе {tg_id}")
    data = dict(row)
    anketa = vault.decrypt(data.get("anketa_enc"))
    number = data.get("contract_no") or ""
    docx, _ = _build_act(cfg, cfg.act_out_template,
                         _act_context(cfg, data, anketa, signed_at=UNSIGNED))
    await bot.send_document(
        tg_id, BufferedInputFile(docx, filename=_act_filename("vozvrata", number)),
        caption=texts.RETURN_READY.format(number=logic.esc(number)),
        reply_markup=kb.sign_return(),
    )


@router.callback_query(StateIs(logic.WAIT_RETURN_SIGN), F.data == "return_sign")
async def cb_return_sign(callback: CallbackQuery, bot: Bot, db: Database,
                         cfg: Config, vault: Vault, user: dict) -> None:
    signed_at = utcnow()
    if not await db.patch(user["tg_id"], expected_state=logic.WAIT_RETURN_SIGN,
                          state=logic.APPROVED, act_out_signed_at=signed_at):
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
        docx, digest = _build_act(cfg, cfg.act_out_template,
                                  _act_context(cfg, data, anketa,
                                               signed_at=stamp))
    except ContractProblem:
        log.exception("подписанный акт возврата %s не собрался", tg_id)
        await bot.send_message(cfg.contract_chat_id,
                               texts.CONTRACT_ALERT_FAILED.format(
                                   tg_id=tg_id,
                                   reason="акт возврата не пересобрался"))
        return

    path, _ = files.store(cfg.storage_dir, tg_id, "actout", docx)
    await db.patch(tg_id, act_out_path=str(path), act_out_sha256=digest)
    await db.log_event(tg_id, "act_out_signed", {"number": number})

    await bot.send_document(
        tg_id, BufferedInputFile(docx, filename=_act_filename("vozvrata", number)),
        caption=texts.RETURN_SIGNED.format(number=logic.esc(number),
                                           signed_at=stamp),
        reply_markup=kb.main_menu(),
    )
    try:
        await bot.send_document(
            cfg.fix_chat_id,
            BufferedInputFile(docx, filename=_act_filename("vozvrata", number)),
            caption=texts.ACT_FIX_CARD.format(
                title="↩️ Акт возврата", number=logic.esc(number),
                fio=logic.esc(data.get("full_name")), tg_id=tg_id,
                signed_at=stamp, sha256=digest),
            message_thread_id=cfg.fix_topic_id,
        )
    except TelegramAPIError:
        log.exception("акт возврата %s не доставлен в чат фиксации", number)
        await db.log_event(tg_id, "act_out_fix_failed", {"number": number})


@router.callback_query(StateIs(logic.WAIT_RETURN_SIGN), F.data == "return_mistake")
async def cb_return_mistake(callback: CallbackQuery, bot: Bot, db: Database,
                            cfg: Config, user: dict) -> None:
    """Оператор пришлёт данные возврата заново ответом на то же приглашение."""
    await callback.answer()
    await db.log_event(user["tg_id"], "act_out_mistake")
    await bot.send_message(user["tg_id"], texts.ACT_MISTAKE_SENT)
    try:
        row = await db.get_user(user["tg_id"])
        number = (dict(row).get("contract_no") if row else "") or ""
        await bot.send_message(cfg.contract_chat_id,
                               texts.ACT_MISTAKE_ALERT.format(
                                   tg_id=user["tg_id"],
                                   number=logic.esc(number)))
    except TelegramAPIError:
        log.exception("алерт об ошибке акта возврата не доставлен")


@router.message(StateIs(logic.WAIT_RETURN_SIGN))
async def st_wait_return_sign(message: Message, bot: Bot, db: Database,
                              cfg: Config, vault: Vault, user: dict) -> None:
    try:
        await send_act_out(bot, db, cfg, vault, user["tg_id"])
    except (ContractProblem, TelegramAPIError):
        log.exception("не удалось переотправить акт возврата %s", user["tg_id"])
        await message.answer(texts.ACT_PRESS_BUTTON)
