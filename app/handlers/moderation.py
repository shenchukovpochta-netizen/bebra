"""Утверждение и отклонение заявок из служебного чата.

Одобрение здесь не заканчивает историю, а запускает договор: бот заполняет docx
и отдаёт его пользователю на подпись. Отклонение обязано объяснить, что не так,
иначе человек присылает то же самое второй раз и заявка ходит по кругу.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from .. import i18n
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

    # Договор НЕ выдаётся сразу: сначала оператор отвечает на приглашение
    # данными выдачи (вин-номера, комплектация, срок, оплата) - без них
    # в договоре и акте были бы прочерки под ручку.
    await _notify(bot, db, target, "APPROVED_WAIT_ISSUE")
    row = await db.get_user(target)
    fio = (dict(row).get("full_name") if row else "") or "без имени"
    try:
        sent = await bot.send_message(
            cfg.contract_chat_id,
            texts.ISSUE_PROMPT.format(fio=logic.esc(fio), tg_id=target,
                                      form=logic.ISSUE_FORM_TEMPLATE))
        await db.patch(target, issue_chat_id=sent.chat.id,
                       issue_message_id=sent.message_id)
    except TelegramAPIError as exc:
        log.exception("приглашение выдачи для %s не доставлено", target)
        await db.log_event(target, "issue_prompt_failed", {"error": str(exc)})
        await _alert(bot, cfg, texts.CONTRACT_ALERT_FAILED.format(
            tg_id=target, reason=logic.esc(str(exc))))


@router.callback_query(F.data.regexp(r"^pay:-?\d+$"))
async def cb_pay(callback: CallbackQuery, bot: Bot, db: Database, cfg: Config,
                 vault: Vault) -> None:
    """«Оплата получена»: перевести клиента с оплаты на Акт приёма-передачи.

    Кнопка живёт на карточке оплаты в служебном чате. Подтверждение - только
    от админов: это финансовое решение, и случайный участник чата не должен
    выдавать имущество нажатием.
    """
    if not _is_admin(callback.from_user.id, cfg):
        await callback.answer(texts.MOD_NO_RIGHTS, show_alert=True)
        return
    target = logic.parse_pay_callback(callback.data)
    if target is None:
        await callback.answer(texts.MOD_BROKEN_BUTTON, show_alert=True)
        return
    row = await db.get_user(target)
    if row is None:
        await callback.answer(texts.MOD_BROKEN_BUTTON, show_alert=True)
        return
    before = dict(row)

    # expected_state закрывает и двойное нажатие, и второй экземпляр карточки:
    # повторное подтверждение получит False и честное «не ждёт оплату».
    if not await db.patch(target, expected_state=logic.WAIT_PAYMENT,
                          state=logic.WAIT_ACT_SIGN,
                          pay_confirmed_at=utcnow()):
        await callback.answer(texts.PAY_NOT_WAITING, show_alert=True)
        return
    await db.log_event(target, "payment_confirmed", {"by": callback.from_user.id})
    await callback.answer(texts.PAY_CONFIRMED_TOAST)

    # Пометить карточку и снять кнопку. Текст собирается заново из шаблона:
    # у старого сообщения Telegram может отдать недоступный объект без текста.
    who = callback.from_user.username or callback.from_user.id
    if before.get("pay_chat_id") and before.get("pay_message_id"):
        try:
            await bot.edit_message_text(
                chat_id=before["pay_chat_id"],
                message_id=before["pay_message_id"],
                text=contract.pay_card(before)
                + f"\n\n{texts.PAY_CONFIRMED_MARK} — @{who}",
                reply_markup=None,
            )
        except TelegramAPIError:
            pass

    await _notify(bot, db, target, "PAY_CONFIRMED_USER")
    row = await db.get_user(target)
    data = dict(row) if row else before
    await contract.send_act_in(bot, db, cfg, data,
                               vault.decrypt(data.get("anketa_enc")))


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
    await _notify(bot, db, target, "REJECTED_WITH_REASON",
                  reason=logic.esc(reason))


@router.message(ServiceChatReply())
async def mod_reply(message: Message, bot: Bot, db: Database, cfg: Config,
                    vault: Vault) -> None:
    """Ответ на карточку в служебном чате: заявка или вопрос в поддержку.

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

    # Порядок веток важен: приглашения выдачи и возврата, затем поддержка,
    # и только потом карточки заявок - у одного пользователя может быть
    # живо несколько привязок сразу.
    issued = await db.user_by_issue_message(message.chat.id, replied.message_id)
    if issued is not None:
        await _issue_reply(message, bot, db, cfg, vault, dict(issued))
        return
    returned = await db.user_by_return_message(message.chat.id, replied.message_id)
    if returned is not None:
        await _return_reply(message, bot, db, cfg, vault, dict(returned))
        return

    asked = await db.user_by_support_message(message.chat.id, replied.message_id)
    if asked is not None:
        await _support_reply(message, bot, db, dict(asked))
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
    await _notify(bot, db, tg_id, "REJECTED_WITH_REASON",
                  reason=logic.esc(comment.value))
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


async def _issue_reply(message: Message, bot: Bot, db: Database,
                       cfg: Config, vault: Vault, target: dict) -> None:
    """Данные выдачи от оператора: сохранить и выдать договор.

    Повторный ответ на то же приглашение обновляет данные: до подписи
    договора - переигрывает договор, после подписи, но до подписи акта -
    пересобирает и переотправляет акт приёма.
    """
    parsed, err = logic.parse_issue_form(message.text or message.caption)
    if parsed is None:
        await message.reply(err)
        return
    tg_id = target["tg_id"]
    if not await db.patch(tg_id, expected_status=logic.ST_APPROVED,
                          issue_data=parsed):
        await message.reply(texts.MOD_REPLY_NOT_PENDING)
        return
    await db.log_event(tg_id, "issue_data_set", {"by": message.from_user.id})

    if target.get("contract_status") == logic.CT_SIGNED:
        # Договор уже подписан - переигрывать его нельзя. Данные выдачи
        # либо обновляют текущий цикл, либо начинают повторную аренду.
        if target.get("act_in_signed_at"):
            if not target.get("act_out_signed_at"):
                # Велосипед на руках - сначала возврат, потом новая выдача.
                await message.reply(texts.RENT_ACTIVE_MOD)
                return
            await _repeat_rent(message, bot, db, cfg, vault, target)
            return
        if target["state"] == logic.WAIT_PAYMENT:
            # Клиент ещё на оплате: пересобирать и слать акт рано - он уйдёт
            # после «Оплата получена» уже с обновлёнными данными. Перевести
            # состояние здесь значило бы перескочить оплату.
            price = str(parsed.get("rent_price") or "—")
            if price != contract.rent_price(target):
                await _repeat_payment(bot, db, cfg, target, price)
            await message.reply(texts.ISSUE_UPDATED_WAIT_PAY.format(
                price=logic.esc(price)))
            return
        row = await db.get_user(tg_id)
        data = dict(row) if row else dict(target)
        await db.patch(tg_id, state=logic.WAIT_ACT_SIGN)
        await contract.send_act_in(bot, db, cfg, data,
                                   vault.decrypt(data.get("anketa_enc")))
        await message.reply("Акт приёма пересобран и отправлен клиенту.")
        return

    try:
        await contract.issue(bot, db, cfg, vault, tg_id)
    except (contract.ContractProblem, TelegramAPIError) as exc:
        log.exception("договор для %s не выдан", tg_id)
        await db.log_event(tg_id, "contract_failed", {"error": str(exc)})
        await _notify(bot, db, tg_id, "CONTRACT_FAILED_USER")
        await message.reply(texts.CONTRACT_ALERT_FAILED.format(
            tg_id=tg_id, reason=logic.esc(str(exc))))
        return
    await message.reply(texts.ISSUE_SAVED)


async def _repeat_rent(message: Message, bot: Bot, db: Database, cfg: Config,
                       vault: Vault, target: dict) -> None:
    """Повторная аренда: прошлый цикл закрыт, оператор прислал новую выдачу.

    Поля прошлого цикла очищаются, клиент переводится на оплату - дальше
    цепочка та же, что после подписи договора: чек, «Оплата получена»,
    Акт приёма-передачи на подпись. Сам договор не переигрывается.
    """
    tg_id = target["tg_id"]
    cycle = dict(state=logic.WAIT_PAYMENT,
                 act_in_signed_at=None, act_out_signed_at=None,
                 pay_confirmed_at=None, return_data=None,
                 close_reason=None, close_requested_at=None)
    # Из меню или из недописанного вопроса в поддержку - но не из состояний,
    # где человек что-то подписывает. expected_state закрывает и гонку двух
    # операторов: второй ответ получит честный отказ.
    for state in (logic.APPROVED, logic.WAIT_SUPPORT):
        if await db.patch(tg_id, expected_state=state, **cycle):
            break
    else:
        await message.reply(texts.MOD_REPLY_NOT_PENDING)
        return
    await db.log_event(tg_id, "rent_repeat_started",
                       {"by": message.from_user.id})
    # Новый цикл - новый отсчёт хранения: клиент действующий.
    await db.set_purge_after(tg_id, cfg.purge_approved_days)

    row = await db.get_user(tg_id)
    data = dict(row) if row else dict(target)
    await contract.start_payment(bot, db, cfg, data)

    # Форма фиксации выдачи - как при первой аренде: утверждающий дозаполняет
    # прочерки и пересылает в тему фиксации.
    number = data.get("contract_no") or ""
    try:
        await bot.send_message(
            cfg.contract_chat_id,
            texts.FIXATION_FORM_INTRO.format(number=logic.esc(number)))
        await bot.send_message(
            cfg.contract_chat_id,
            logic.fixation_form(data, vault.decrypt(data.get("anketa_enc")),
                                data.get("issue_data")))
    except TelegramAPIError:
        log.exception("форма фиксации по договору %s не доставлена", number)
        await db.log_event(tg_id, "fixation_form_failed", {"number": number})

    await message.reply(texts.RENT_ISSUE_SAVED.format(
        price=logic.esc(contract.rent_price(data))))


async def _repeat_payment(bot: Bot, db: Database, cfg: Config, target: dict,
                          price: str) -> None:
    """Сумма изменилась, пока клиент на оплате: сказать ему новую и обновить
    карточку оператора.

    Молчать нельзя: у клиента на экране висит прошлое сообщение с прежней
    суммой, и он заплатит именно её. Карточка обновляется по той же причине -
    оператор сверяет поступление с суммой на ней.
    """
    tg_id = target["tg_id"]
    lang = i18n.user_lang(target)
    try:
        await bot.send_message(
            tg_id, i18n.t(lang, "PAY_PROMPT").format(
                price=logic.esc(price), pay_url=logic.esc(cfg.pay_url)),
            reply_markup=kb.paid(cfg.pay_url, lang))
    except TelegramAPIError:
        log.warning("новая сумма оплаты не доставлена клиенту %s", tg_id)
    await db.log_event(tg_id, "payment_amount_changed", {"price": price})
    if not (target.get("pay_chat_id") and target.get("pay_message_id")):
        return
    try:
        await bot.edit_message_text(
            chat_id=target["pay_chat_id"], message_id=target["pay_message_id"],
            text=contract.pay_card(target, price),
            reply_markup=kb.pay_confirm(tg_id))
    except TelegramAPIError:
        # Карточку могли удалить или текст совпал - подтверждать оплату
        # оператор всё равно сможет, новая сумма у него в ответе бота.
        log.info("карточка оплаты %s не обновлена", tg_id)


async def _return_reply(message: Message, bot: Bot, db: Database,
                        cfg: Config, vault: Vault, target: dict) -> None:
    """Форма закрытия: Акт возврата клиенту и отчёт о закрытии в фиксацию.

    Повторный ответ на то же приглашение перезаписывает данные и переотправляет
    акт - «Есть ошибка» у клиента чинится именно так.
    """
    if not target.get("act_in_signed_at"):
        await message.reply(texts.RETURN_NOT_READY)
        return
    parsed, err = logic.parse_close_form(message.text or message.caption,
                                         reason=target.get("close_reason") or "")
    if parsed is None:
        await message.reply(err)
        return
    # Замечания для акта собираются из формы: акт говорит о состоянии
    # имущества, а причина сдачи и отзывы - это уже отчёт.
    parsed["return_notes"] = logic.close_notes(parsed)
    parsed["return_date"] = utcnow().strftime("%d.%m.%Y")
    tg_id = target["tg_id"]
    # Из approved или из wait_return_sign (повторные данные) - но не из
    # состояний, где человек ещё что-то подписывает или спрашивает поддержку.
    if target["state"] not in (logic.APPROVED, logic.WAIT_SUPPORT,
                               logic.WAIT_RETURN_SIGN):
        await message.reply(texts.RETURN_NOT_READY)
        return
    if not await db.patch(tg_id, expected_status=logic.ST_APPROVED,
                          return_data=parsed, state=logic.WAIT_RETURN_SIGN):
        await message.reply(texts.MOD_REPLY_NOT_PENDING)
        return
    await db.log_event(tg_id, "return_data_set", {"by": message.from_user.id})
    try:
        await contract.send_act_out(bot, db, cfg, vault, tg_id)
    except (contract.ContractProblem, TelegramAPIError) as exc:
        log.exception("акт возврата для %s не выдан", tg_id)
        await message.reply(texts.CONTRACT_ALERT_FAILED.format(
            tg_id=tg_id, reason=logic.esc(str(exc))))
        return
    await message.reply(texts.RETURN_SAVED)
    # Отчёт о закрытии - отдельным сообщением, чистым текстом: его
    # пересылают в «Фиксацию сдачи», где разбирает другой бот.
    row = await db.get_user(tg_id)
    await _closure_report(message, bot, cfg, dict(row) if row else target, parsed)


async def _closure_report(message: Message, bot: Bot, cfg: Config,
                          data: dict, close: dict) -> None:
    """Отчёт о закрытии: интро оператору и следом сам текст без шапки."""
    number = data.get("contract_no") or ""
    report = logic.closure_report(data, data.get("issue_data"), close)
    try:
        await bot.send_message(cfg.contract_chat_id,
                               texts.CLOSE_REPORT_INTRO.format(
                                   number=logic.esc(number)))
        await bot.send_message(cfg.contract_chat_id, report)
    except TelegramAPIError:
        # Акт уже ушёл клиенту, откатывать из-за отчёта нечего - но
        # пересылать в фиксацию оператору будет нечего, и он должен знать.
        log.exception("отчёт о закрытии %s не доставлен", number)
        await message.reply(texts.CONTRACT_ALERT_NOT_DELIVERED.format(
            number=logic.esc(number), tg_id=data.get("tg_id")))


async def _support_reply(message: Message, bot: Bot, db: Database,
                         asked: dict) -> None:
    """Ответ модератора на вопрос в поддержку - пересылается пользователю.

    Карточка остаётся привязанной: на неё можно ответить ещё раз, и каждое
    сообщение уйдёт тому же человеку - диалог не обрывается на первом ответе.
    """
    answer = logic.support_answer(message.text or message.caption)
    if not answer.ok:
        await message.reply(answer.error)
        return
    try:
        await bot.send_message(
            asked["tg_id"],
            i18n.t(asked.get("lang"), "SUPPORT_REPLY_USER").format(
                answer=logic.esc(answer.value)),
        )
    except TelegramAPIError as exc:
        log.warning("ответ поддержки для %s не доставлен: %s", asked["tg_id"], exc)
        await message.reply(texts.SUPPORT_REPLY_NOT_DELIVERED)
        return
    await db.log_event(asked["tg_id"], "support_answered",
                       {"by": message.from_user.id})
    await message.reply(texts.SUPPORT_REPLIED)


async def _notify(bot: Bot, db: Database, tg_id: int, key: str, **fmt) -> None:
    """Уведомление пользователя о решении - на его языке.

    Пользователь мог заблокировать бота - решение модератора при этом уже
    сохранено, откатывать его не за чем.
    """
    row = await db.get_user(tg_id)
    text = i18n.t(i18n.user_lang(dict(row) if row else None), key)
    if fmt:
        text = text.format(**fmt)
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
