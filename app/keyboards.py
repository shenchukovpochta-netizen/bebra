from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from . import i18n, logic

# Все подписи кнопок идут через i18n.t(lang, "BTN_*"): русский - источник,
# перевод подхватывается по языку клиента. Обработчики, которые ловят
# нажатия reply-кнопок текстом, сверяются с i18n.variants/button_key.


def subscribe(channel_url: str, lang: str = "ru") -> InlineKeyboardMarkup:
    """Гейт подписки: ссылка на канал и «я подписался».

    Кнопка со ссылкой - только если это похожа на ссылку. Telegram
    отвергает СООБЩЕНИЕ ЦЕЛИКОМ из-за кнопки с битым url, и
    `CHANNEL_URL=t.me/...` без схемы оставлял всех неподписанных вообще
    без ответа - ни текста, ни ссылки. Сама ссылка остаётся в тексте,
    там она безобидна. Так же устроена `paid()`.
    """
    rows = []
    if channel_url.startswith(("http://", "https://")):
        rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_SUBSCRIBE"),
                                          url=channel_url)])
    rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_CHECK_SUB"),
                                      callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def lang_pick() -> InlineKeyboardMarkup:
    """Выбор языка диалога - первый вопрос /start, по две кнопки в ряд.

    callback «lang:код» - отдельный от «faqlang:код»: этот меняет язык
    всего диалога на шаге регистрации, тот - пришёл из ветки вопросов
    (теперь тоже меняет общий язык, но живёт в своём обработчике).
    """
    buttons = [InlineKeyboardButton(text=i18n.LANG_TITLES[code],
                                    callback_data=f"lang:{code}")
               for code in i18n.LANGS]
    return InlineKeyboardMarkup(inline_keyboard=[
        buttons[i:i + 2] for i in range(0, len(buttons), 2)
    ])


def policy_ack(pdn_url: str = "", lang: str = "ru") -> InlineKeyboardMarkup:
    """Экран ознакомления с Политикой обработки ПДн.

    Отдельная «галочка» ПЕРЕД согласием: ознакомление с политикой и согласие
    на обработку - два разных юридических факта, и каждый фиксируется своей
    кнопкой со своим моментом в базе.
    """
    rows = []
    if pdn_url:
        rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_POLICY_WEB"),
                                          url=pdn_url)])
    rows.append([InlineKeyboardButton(
        text=i18n.t(lang, "BTN_POLICY_ACK"), callback_data="pdn_ok")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def consent(rules_url: str = "", pdn_url: str = "",
            lang: str = "ru") -> InlineKeyboardMarkup:
    """Экран согласия на обработку персональных данных.

    Обе ссылки необязательны: правила проката и отдельная политика ПДн
    появляются кнопками, как только заполнены переменные, - трогать код
    не придётся. callback остался «oferta_ok» намеренно: старые кнопки
    в открытых чатах продолжают работать.
    """
    rows = []
    if rules_url:
        rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_RULES"),
                                          url=rules_url)])
    if pdn_url:
        rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_PDN"),
                                          url=pdn_url)])
    rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_CONSENT"),
                                      callback_data="oferta_ok")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def share_contact(lang: str = "ru") -> ReplyKeyboardMarkup:
    # request_contact работает только в reply-клавиатуре и только в личном чате
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=i18n.t(lang, "BTN_SHARE_CONTACT"),
                                  request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def doc_enough(lang: str = "ru") -> InlineKeyboardMarkup:
    """Шаг второй фотографии документа: кнопка «одной достаточно».

    Вторая фотография необязательна - в паспорте без прописки её просто
    неоткуда взять, - и без выхода человек упёрся бы в шаг, который
    физически не может пройти.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=i18n.t(lang, "BTN_DOC_ENOUGH"),
                             callback_data="doc_enough")]])


def confirm(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_CONFIRM"),
                              callback_data="confirm")],
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_RESTART"),
                              callback_data="restart")],
    ])


def moderation(tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{tg_id}"),
        InlineKeyboardButton(text="⛔ Отклонить", callback_data=f"reject:{tg_id}"),
    ]])


def reject_reasons(tg_id: int, reasons: dict[str, tuple[str, str]]) -> InlineKeyboardMarkup:
    """Второй экран кнопки «Отклонить»: за что именно.

    Готовые причины, а не только свободный текст: у каждой из них есть шаг,
    на который человека вернут. Переигрывать всю анкету из-за нечитаемого
    селфи - верный способ получить брошенную заявку вместо исправленной.
    """
    rows = [[InlineKeyboardButton(text=title, callback_data=f"rj:{tg_id}:{code}")]
            for code, (title, _state) in reasons.items()]
    rows.append([InlineKeyboardButton(text="✍️ Свой текст", callback_data=f"rjc:{tg_id}")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"rjx:{tg_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def same_address(lang: str = "ru") -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=i18n.t(lang, "BTN_SAME_ADDRESS"))]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def citizenship() -> ReplyKeyboardMarkup:
    """Страны в две колонки. Не переводится намеренно: названия стран уходят
    в договор, а договор русский - выбранное человеком должно совпасть с тем,
    что будет напечатано. Своей страны в списке нет - её вписывают текстом."""
    names = list(logic.CITIZENSHIPS)
    rows = [[KeyboardButton(text=a) for a in names[i:i + 2]]
            for i in range(0, len(names), 2)]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True,
                               one_time_keyboard=True)


def sign_contract(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_SIGN"), callback_data="sign")],
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_MISTAKE"),
                              callback_data="contract_mistake")],
    ])


def paid(pay_url: str = "", lang: str = "ru") -> InlineKeyboardMarkup:
    """Этап оплаты: ссылка на расчётный счёт и «я оплатил».

    Ссылка кнопкой, а не только текстом: с телефона по ней открывается
    приложение банка, и человеку не нужно копировать длинный адрес.
    Ссылка тем не менее дублируется в тексте - на десктопе кнопку СБП
    открыть нечем. «Я оплатил(а)» сама по себе состояние не меняет:
    поступление подтверждает оператор кнопкой на своей карточке.
    """
    rows = []
    # Кнопка добавляется, только если это похоже на ссылку: Telegram
    # отвергает СООБЩЕНИЕ ЦЕЛИКОМ из-за кнопки с битым url, и опечатка
    # в PAY_URL оставила бы клиента вообще без реквизитов. Сама ссылка
    # при этом остаётся в тексте - там она безобидна.
    if pay_url.startswith(("http://", "https://")):
        rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_PAY"),
                                          url=pay_url)])
    rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_PAID"),
                                      callback_data="paid")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def extend(lang: str = "ru") -> InlineKeyboardMarkup:
    """«Продлить аренду» - кнопка под напоминанием и списком аренд.

    Инлайн, а не в меню: меню и так из шести кнопок, а продление нужно
    ровно в тот момент, когда бот напомнил о сроке.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_EXTEND"),
                              callback_data="extend")],
    ])


def pay_confirm(tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Оплата получена", callback_data=f"pay:{tg_id}")],
    ])


def sign_act(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_SIGN"),
                              callback_data="act_sign")],
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_MISTAKE"),
                              callback_data="act_mistake")],
    ])


def sign_buyout(lang: str = "ru") -> InlineKeyboardMarkup:
    """Подпись Акта о переходе права собственности."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_SIGN"),
                              callback_data="buyout_sign")],
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_MISTAKE"),
                              callback_data="buyout_mistake")],
    ])


def sign_return(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_RETURN_SIGN"),
                              callback_data="return_sign")],
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_MISTAKE"),
                              callback_data="return_mistake")],
    ])


def support_cancel(lang: str = "ru") -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=i18n.t(lang, "BTN_CANCEL"))]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def faq_topics(topics, lang: str = "ru") -> InlineKeyboardMarkup:
    """Темы частых вопросов - по кнопке на строку, на языке клиента.

    Заголовки длинные, по две в ряд Telegram обрезает их до многоточия,
    и человек не понимает, куда жмёт. Последней строкой - смена языка:
    первый выбор запоминается, и без этой кнопки ошибившийся человек
    остался бы с чужим языком навсегда.
    """
    from . import faq
    from .faq_i18n import T
    rows = [
        [InlineKeyboardButton(text=faq.topic_title(topic, lang),
                              callback_data=f"faq:{topic.code}")]
        for topic in topics
    ]
    rows.append([InlineKeyboardButton(
        text=T.get(lang, {}).get("change", "🌐 Сменить язык"),
        callback_data="faq:lang")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def faq_langs() -> InlineKeyboardMarkup:
    """Выбор языка из ветки вопросов - по две кнопки в ряд."""
    buttons = [InlineKeyboardButton(text=i18n.LANG_TITLES[code],
                                    callback_data=f"faqlang:{code}")
               for code in i18n.LANGS]
    return InlineKeyboardMarkup(inline_keyboard=[
        buttons[i:i + 2] for i in range(0, len(buttons), 2)
    ])


def faq_entry(lang: str = "ru") -> InlineKeyboardMarkup:
    """Кнопка входа в частые вопросы под приветствием - до регистрации."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_FAQ"),
                              callback_data="faq:open")],
    ])


def main_menu(lang: str = "ru") -> ReplyKeyboardMarkup:
    # Кабинет - первой строкой на всю ширину: баланс и «оплачено до» клиент
    # смотрит чаще всего остального, и искать кнопку он не должен.
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=i18n.t(lang, "BTN_CABINET"))],
            [KeyboardButton(text=i18n.t(lang, "BTN_RENT")),
             KeyboardButton(text=i18n.t(lang, "BTN_TRIPS"))],
            [KeyboardButton(text=i18n.t(lang, "BTN_TARIFFS")),
             KeyboardButton(text=i18n.t(lang, "BTN_SUPPORT"))],
            [KeyboardButton(text=i18n.t(lang, "BTN_FAQ")),
             KeyboardButton(text=i18n.t(lang, "BTN_CLOSE_RENT"))],
        ],
        resize_keyboard=True,
    )


remove = ReplyKeyboardRemove


# ─────────────────────── кабинет клиента (CRM) ───────────────────────
# callback «cab:...» - экраны кабинета у клиента, «crmpay:<id>:ok|no» -
# кнопки оператора на карточке заявки о зачислении (служебный чат).

def cabinet(lang: str = "ru", *, active: bool = False,
            booking: bool = False) -> InlineKeyboardMarkup:
    """Главный экран кабинета. При идущей аренде - ещё «продлю / сдаю»:
    намерение клиента кормит прогноз освобождения, и спрашивать его
    звонком дороже, чем двумя кнопками. Без аренды - заявка на
    велосипед или снятие уже поданной."""
    rows = [
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_CAB_TOPUP"), callback_data="cab:pay")],
    ]
    if active:
        rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_INTENT_RENEW"),
                                          callback_data="cab:intent:renew"),
                     InlineKeyboardButton(text=i18n.t(lang, "BTN_INTENT_RETURN"),
                                          callback_data="cab:intent:return")])
    elif booking:
        rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_BOOK_CANCEL"),
                                          callback_data="cab:book:cancel")])
    else:
        rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_BOOK"),
                                          callback_data="cab:book")])
    rows += [
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_CAB_HISTORY"),
                              callback_data="cab:history"),
         InlineKeyboardButton(text=i18n.t(lang, "BTN_CAB_CONTRACT"),
                              callback_data="cab:contract")],
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_CAB_FRIENDS"),
                              callback_data="cab:friends"),
         InlineKeyboardButton(text=i18n.t(lang, "BTN_CAB_REVIEW"),
                              callback_data="cab:review")],
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_CAB_REFRESH"), callback_data="cab:home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cab_choice(rows: list[tuple[str, str]], lang: str = "ru") -> InlineKeyboardMarkup:
    """Список вариантов столбиком и «назад»: шаги заявки на аренду."""
    keyboard = [[InlineKeyboardButton(text=label, callback_data=data)]
                for label, data in rows]
    keyboard.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_CAB_BACK"),
                                          callback_data="cab:home")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def cab_booking(lang: str = "ru") -> InlineKeyboardMarkup:
    """Под поданной заявкой: снять её или назад."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_BOOK_CANCEL"),
                              callback_data="cab:book:cancel")],
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_CAB_BACK"), callback_data="cab:home")],
    ])


def cab_pay_options(options: list[dict], lang: str = "ru") -> InlineKeyboardMarkup:
    """Выбор суммы: долг и периоды вперёд. В callback - код кнопки, сумма
    пересчитается при нажатии."""
    rows = []
    for option in options:
        if option["code"] == "debt":
            label = i18n.t(lang, "CAB_OPT_DEBT").format(amount=option["label_amount"])
        else:
            label = i18n.t(lang, "CAB_OPT_PERIOD").format(
                n=option["periods"], days=option["days"], amount=option["label_amount"])
        rows.append([InlineKeyboardButton(text=label,
                                          callback_data=f"cab:pay:{option['code']}")])
    rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_CAB_BACK"),
                                      callback_data="cab:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cab_pay(pay_url: str = "", lang: str = "ru", *, code: str = "") -> InlineKeyboardMarkup:
    """Пополнение без эквайринга: ссылка СБП, «я оплатил», назад.

    Кнопка-ссылка только при похожем на URL значении: Telegram отвергает
    сообщение целиком из-за кнопки с битым url (см. paid()). Код суммы
    едет в «я оплатил(а)»: заявка оператору подскажет, сколько ждать.
    """
    rows = []
    if pay_url.startswith(("http://", "https://")):
        rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_PAY"), url=pay_url)])
    rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_PAID"),
                                      callback_data=f"cab:paid:{code}" if code else "cab:paid")])
    rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_CAB_BACK"),
                                      callback_data="cab:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cab_pay_order(link: str, order_id: int, lang: str = "ru") -> InlineKeyboardMarkup:
    """Счёт эквайринга: оплатить по ссылке банка, проверить оплату, назад.
    «Я оплатил(а)» здесь не нужна: оплату подтверждает банк, а не оператор."""
    rows = []
    if link.startswith(("http://", "https://")):
        rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_PAY"), url=link)])
    rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_PAY_CHECK"),
                                      callback_data=f"cab:paycheck:{order_id}")])
    rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_CAB_BACK"),
                                      callback_data="cab:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cab_back(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_CAB_BACK"), callback_data="cab:home")],
    ])


def cab_topup(lang: str = "ru") -> InlineKeyboardMarkup:
    """Под напоминанием об оплате - сразу в пополнение."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_CAB_TOPUP"), callback_data="cab:pay")],
    ])


def cabinet_entry(lang: str = "ru") -> InlineKeyboardMarkup:
    """Под уведомлением о зачислении или новой аренде - открыть кабинет."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=i18n.t(lang, "BTN_CABINET"), callback_data="cab:home")],
    ])


def claim_confirm(claim_id: int, amount_text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Зачислить {amount_text}",
                              callback_data=f"crmpay:{claim_id}:ok"),
         InlineKeyboardButton(text="❌ Отклонить",
                              callback_data=f"crmpay:{claim_id}:no")],
    ])


def fleet_card(bike_id: int, status: str, rented: bool) -> InlineKeyboardMarkup:
    """Кнопки статуса на карточке велосипеда в служебном чате. В аренде -
    кнопок нет: статус «в аренде» снимает только закрытие аренды.
    На сборке - тоже: оттуда выпускает только ввод в эксплуатацию."""
    if rented or status == "new":
        return InlineKeyboardMarkup(inline_keyboard=[])
    options = [("repair", "🔧 В ремонт"), ("maintenance", "🛠 На ТО"),
               ("available", "✅ Свободен"), ("written_off", "🗑 Списан")]
    row = [InlineKeyboardButton(text=label, callback_data=f"bk:{bike_id}:{code}")
           for code, label in options if code != status]
    return InlineKeyboardMarkup(inline_keyboard=[row[:2], row[2:]])


def estimate_answer(order_id: int, cents: int) -> InlineKeyboardMarkup:
    """Под сметой: согласен или нет. Третьего варианта нет намеренно -
    «подумаю» оставляет технику разобранной на неопределённый срок.

    Сумма сметы (в копейках) едет в кнопке: смету могут переслать с новой
    строкой, и «Согласен» под старым сообщением на 1 000 ₽ иначе
    согласовывал бы новые 10 000 ₽."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Согласен",
                              callback_data=f"est:ok:{order_id}:{cents}"),
         InlineKeyboardButton(text="✖️ Не надо",
                              callback_data=f"est:no:{order_id}:{cents}")],
    ])


def review_sites(links: list[dict] | None = None,
                 lang: str = "ru") -> InlineKeyboardMarkup:
    """Площадки для отзыва. Пустая ссылка - площадки нет: кнопка в
    никуда хуже, чем её отсутствие."""
    rows = [[InlineKeyboardButton(text=f"⭐ {site['title']}", url=site["url"])]
            for site in (links or [])
            if str(site.get("url", "")).startswith(("http://", "https://"))]
    rows.append([InlineKeyboardButton(text=i18n.t(lang, "BTN_CAB_REFRESH"),
                                      callback_data="cab:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
