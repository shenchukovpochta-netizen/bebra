"""EN: переводы клиентского диалога. Ключи - имена констант texts.py."""

from __future__ import annotations

from ..texts import SUPPORT_CONTACT_URL as URL

T: dict[str, str] = {
    # ── подписка и старт ──
    "NOT_SUBSCRIBED": (
        "You are not subscribed to our channel!\n"
        "To use the bot, please subscribe to our news channel: {channel_url}"
    ),
    "SUB_NOT_FOUND": "Subscription not found. Please try again.",
    "SUB_CONFIRMED_TOAST": "Subscription confirmed",
    "STALE_BUTTON": "This button has expired, send /start",
    "WELCOME": "👋 Welcome! To begin, please enter your full name:",
    "FIO_AS_TEXT": "Please type your full name as text.",
    "FAQ_ENTRY_HINT": (
        "While you fill this in, you can check the frequently asked "
        "questions: addresses, rates, documents."
    ),

    # ── политика ПДн ──
    "POLICY_CAPTION": (
        "📄 <b>Personal Data Processing Policy</b>\n\n"
        "Before you continue, please read the Policy of sole proprietor "
        "Galimzyanov Edgar Airatovich on personal data processing (the "
        "document above). It explains what data is processed and why, how "
        "it is protected, retention periods, GPS trackers on the bikes, "
        "photo and video recording of handover and return, and how to "
        "withdraw consent.\n\n"
        "Once you have read it, tap “I have read the Policy”."
    ),
    "POLICY_NO_FILE": (
        "📄 <b>Personal Data Processing Policy</b>\n\n"
        "Before you continue, please read the Policy of sole proprietor "
        "Galimzyanov Edgar Airatovich on personal data processing. You can "
        "request its text from the operator: " + URL + "\n\n"
        "Once you have read it, tap “I have read the Policy”."
    ),
    "POLICY_PRESS_BUTTON": (
        "Please read the Policy and tap “I have read the Policy” to continue."
    ),
    "POLICY_ACK_TOAST": "Acknowledgement recorded",

    # ── согласие ──
    "CONSENT": (
        "Please check: <b>{fio}</b>\n\n"
        "<b>Consent to personal data processing</b>\n\n"
        "To conclude and perform the rental agreement, sole proprietor "
        "Galimzyanov Edgar Airatovich (INN 165921923517) processes your "
        "personal data: full name, date and place of birth, passport "
        "details, registration and residence addresses, phone numbers and "
        "an image of your identity document; for renters aged 16–17 — also "
        "an image of the written consent of a legal guardian.\n"
        "The data is processed by the Lessor only and is not shared with "
        "third parties except as required by law.\n"
        "Images are deleted {purge_days} days after the decision on your "
        "application. You can withdraw consent by writing to support.\n"
        "You read the Personal Data Processing Policy at the previous "
        "step.\n\n"
        "By tapping “I consent” you consent to the processing of the data "
        "listed above."
    ),
    "CONSENT_PRESS_BUTTON": "Tap the “I consent” button to continue.",
    "CONSENT_GIVEN": "Consent recorded",

    # ── контакт и анкета ──
    "ASK_CONTACT": "Please share your contact.",
    "CONTACT_USE_BUTTON": "Tap the “Share contact” button at the bottom of the screen.",
    "CONTACT_FOREIGN": "That is someone else's contact. Please send your own number using the button below.",
    "ANKETA_INTRO": (
        "Thank you! Now a few details for the rental agreement. It takes a "
        "couple of minutes — answer one message at a time, exactly as "
        "written in your passport."
    ),
    "ASK_BIRTH": "Date of birth — as DD.MM.YYYY.\nFor example: 07.03.1990",
    "ASK_BIRTH_PLACE": "Place of birth, as in your passport.\nFor example: gor. Kazan",
    "ASK_PASSPORT": "Passport series and number — 10 digits.\nFor example: 1234 567890",
    "ASK_PASSPORT_DATE": "Passport issue date — DD.MM.YYYY.\nFor example: 01.02.2015",
    "ASK_PASSPORT_CODE": "Issuing unit code — 6 digits.\nFor example: 160-002",
    "ASK_PASSPORT_ISSUER": (
        "Issuing authority — one full line, exactly as in the document.\n"
        "For example: OUFMS Rossii po Resp. Tatarstan v Vakhitovskom r-ne gor. Kazani"
    ),
    "ASK_REG_ADDR": (
        "Full registration address: city, street, building, apartment.\n"
        "For example: g. Kazan, ul. Baumana, d. 1, kv. 2"
    ),
    "ASK_LIVE_ADDR": (
        "Actual residence address.\n"
        "If it matches the registration address — tap the button below."
    ),
    "ASK_PHONE2": (
        "A second phone number — for example, of someone close to you.\n"
        "We need it to reach you if the main number is unavailable.\n"
        "For example: +7 900 123-45-67"
    ),
    "ASK_PHONE3": "And a third phone number for contact.",
    "SAME_AS_REG": "Recorded the residence address as the registration address.",
    "ANKETA_AS_TEXT": "Please send your answer as one text message.",
    "PASSPORT_DATE_BEFORE_BIRTH": (
        "The issue date does not match the date of birth: passports are "
        "issued from age 14. Please check both dates — let's start with the "
        "date of birth."
    ),
    "ASK_DOC": (
        "One last step: attach a photo of your identity document. Take the "
        "photo so that the details are clearly readable."
    ),
    "DOC_NEED_PHOTO": "We need a photo of the document. Please attach an image.",
    "ASK_PARENT_CONSENT": (
        "You are under 18, so one more document is required: the written "
        "consent of a parent (legal guardian) to conclude the rental "
        "agreement.\n\n"
        "The parent writes the consent by hand, stating their full name, "
        "passport details and your full name, with date and signature. "
        "Send a photo of this consent — make sure the text is clearly "
        "readable."
    ),
    "PARENT_NEED_PHOTO": "We need a photo of the parent's written consent. Please attach an image.",

    # ── подтверждение и проверка ──
    "CONFIRM_CAPTION": (
        "Please confirm that the details you entered are correct:\n\n"
        "Full name: <b>{fio}</b>\n"
        "Phone number: <b>+{phone}</b>"
    ),
    "CONFIRM_PRESS_BUTTON": "Tap “Confirm” or “Fill in again”.",
    "RESTART": "All right, let's start over. Please enter your full name:",
    "RESTART_TOAST": "Starting over",
    "SUBMITTED": (
        "Thank you! Your details have been sent for review. It usually "
        "takes up to 30 minutes — we will message you as soon as everything "
        "is ready."
    ),
    "SUBMITTED_TOAST": "Sent for review",
    "SUBMIT_PROBLEM": (
        "It looks like a technical hiccup occurred while submitting your "
        "application. We already know about it. If there is no reply within "
        "an hour — write to support."
    ),
    "PENDING_WAIT": "Your application is under review. We will message you as soon as it is processed.",
    "APPROVED_WAIT_ISSUE": (
        "Application approved! We are preparing the agreement: entering the "
        "bike details and equipment. We will send it here for signing."
    ),
    "REGISTERED": "You have successfully registered.\nVideo guide: {video_url}",
    "ALREADY_REGISTERED": "You are already registered. How can we help?",
    "MENU_PROMPT": "Choose an action from the menu below.",
    "REJECTED_WITH_REASON": (
        "Unfortunately, your application was declined.\n\n"
        "What needs fixing: {reason}\n\n"
        "Tap /start or simply send the required details to continue from "
        "this step."
    ),

    # ── тарифы ──
    "TARIFFS": (
        "💰 <b>Current rental rates</b>\n\n"
        "<b>Truck+ with 2 batteries</b>\n"
        "• 1 week — 3,000 ₽\n"
        "• 2 weeks — 5,400 ₽\n"
        "• 1 month — 11,000 ₽\n\n"
        "<b>Truck+ with rear shock absorbers, 2 batteries</b>\n"
        "• 1 week — 3,400 ₽\n"
        "• 2 weeks — 5,400 ₽\n"
        "• 1 month — 11,000 ₽\n\n"
        "<b>Kugoo V3 Pro with 2 batteries</b>\n"
        "• 1 week — 3,500 ₽\n"
        "• 2 weeks — 6,400 ₽\n"
        "• 1 month — 12,000 ₽\n\n"
        "<b>Kugoo V3 Pro+ with 2 batteries</b>\n"
        "• 1 week — 3,500 ₽\n"
        "• 2 weeks — 6,400 ₽\n"
        "• 1 month — 12,000 ₽\n\n"
        "Extension beyond the agreed term — 650 ₽/day (per the "
        "agreement).\n"
        "\nThe rate includes: priority repairs, a replacement bike within "
        "a day in case of breakdown, GPS protection and maintenance at our "
        "expense. <b>No deposit.</b>\n"
        "To rent, tap “🆘 Support” or message us directly: " + URL
    ),

    # ── частые вопросы и поддержка ──
    "FAQ_GUEST_CONTACT": "Message us: " + URL,
    "FAQ_HANDOFF": (
        "Please describe the details in one message — I will pass it to the "
        "manager.\nChanged your mind — tap “Cancel”."
    ),
    "FAQ_TOPIC_GONE": "This topic is no longer available, open the FAQ again.",
    "SUPPORT_SENT_ANSWERED": (
        "If this is not what you needed — the manager sees your question "
        "and will reply during working hours."
    ),
    "SUPPORT_PROMPT": (
        "Describe your question in one message — we will reply right in "
        "this chat.\nOr message us directly: " + URL + "\n\n"
        "Changed your mind — tap “Cancel”."
    ),
    "SUPPORT_AS_TEXT": "Please write your question as one text message.",
    "SUPPORT_SENT": (
        "Your question has been passed on. The reply will arrive in this "
        "chat.\nIf it is urgent — message us directly: " + URL
    ),
    "SUPPORT_FAILED": (
        "We could not pass on your question due to a technical hiccup. "
        "Message us directly: " + URL
    ),
    "SUPPORT_CANCELLED": "All right, back to the menu.",
    "SUPPORT_REPLY_USER": "💬 Support reply:\n\n{answer}",
    "RATE_LIMITED": "Too many messages. Please wait a minute.",

    # ── договор ──
    "CONTRACT_READY_USER": (
        "Rental agreement No. {number} is ready. Above is its annex: the "
        "Consent to personal data processing.\n\n"
        "Please read both documents in full and tap “Sign” — the signature "
        "covers the agreement and the annex. If you found a mistake — tap "
        "“There is a mistake” and we will send the form back for "
        "correction."
    ),
    "SOGLASIE_CAPTION": (
        "Annex to agreement No. {number}: Consent to personal data "
        "processing."
    ),
    "SOGLASIE_SIGNED_CAPTION": (
        "Consent to personal data processing (annex to agreement "
        "No. {number}) signed {signed_at}."
    ),
    "CONTRACT_SIGNED_USER": (
        "Agreement No. {number} signed {signed_at}.\n"
        "Your copy stays in this chat.\n\n"
        "Video guide: {video_url}"
    ),
    "CONTRACT_RESEND": (
        "Agreement No. {number} is not signed yet — here it is again.\n\n"
        "Read it and tap “Sign” if everything is correct, or “There is a "
        "mistake” if you found an error."
    ),
    "CONTRACT_SIGN_TOAST": "Agreement signed",
    "CONTRACT_PRESS_BUTTON": "Read the agreement and tap “Sign” or “There is a mistake”.",
    "CONTRACT_MISTAKE": (
        "All right, let's fill in the form again — this way all the details "
        "will go into the agreement correctly."
    ),
    "CONTRACT_FAILED_USER": (
        "Your application is approved, but the agreement failed to generate "
        "due to a technical error. We already know about it and will send "
        "the agreement manually."
    ),

    # ── повторная аренда ──
    "RENT_ALREADY_ACTIVE": (
        "You already have an active rental: <b>{bike}</b>, term {term}.\n"
        "To take another bike, first close the current rental — the "
        "“🔚 Close rental” button in the menu."
    ),
    "RENT_REQUEST_SENT": (
        "✅ Your rental request has been passed to the operator. They will "
        "confirm the handover and enter the bike details; after that we "
        "will send the amount to pay and the Handover Act.\n"
        "We hand over bikes daily from 10:00 to 19:00; your existing "
        "agreement remains in force."
    ),
    "RENT_REQUEST_FAILED": (
        "We could not pass on your request due to a technical hiccup. "
        "Message us directly: " + URL
    ),

    # ── оплата ──
    "PAY_PROMPT": (
        "One step left — the rental payment.\n\n"
        "Amount and method: <b>{price}</b>\n\n"
        "Pay via the link below — it is our only settlement account 👇\n"
        "{pay_url}\n\n"
        "After paying, tap “I have paid” and send the receipt here in the "
        "chat. As soon as the operator confirms the payment, we will send "
        "the Handover Act: you receive the bike against it."
    ),
    "PAY_WAIT": (
        "Awaiting the rental payment: <b>{price}</b>.\n"
        "Payment link (our only settlement account):\n{pay_url}\n\n"
        "Paid — tap “I have paid” and send the receipt; the operator will "
        "verify the transfer. After confirmation we will send the Handover "
        "Act."
    ),
    "PAY_NUDGE_TOAST": "Passed to the operator — they will verify the transfer",
    "PAY_RECEIPT_SENT": (
        "The receipt has been passed to the operator — they will verify the "
        "transfer. As soon as it is confirmed, we will send the Handover "
        "Act."
    ),
    "PAY_RECEIPT_FAILED": (
        "We could not pass on the receipt due to a technical hiccup. Tap "
        "“I have paid” — the operator will check the account for the "
        "transfer."
    ),
    "PAY_CONFIRMED_USER": (
        "Payment received, thank you! The Handover Act will arrive in the "
        "next message."
    ),

    # ── акты ──
    "ACT_IN_READY": (
        "Payment confirmed! Now the Handover Act No. {number}.\n\n"
        "Check the VIN numbers and the equipment and tap “Sign” — from that "
        "moment the property is considered handed over to you."
    ),
    "ACT_IN_SIGNED": (
        "The Handover Act under agreement No. {number} was signed "
        "{signed_at}.\nYour copy stays in this chat. Enjoy the ride!\n\n"
        "Video guide: {video_url}"
    ),
    "ACT_RESEND": (
        "The act under agreement No. {number} is not signed yet — here it "
        "is again.\nTap “Sign” or “There is a mistake”."
    ),
    "ACT_PRESS_BUTTON": "Read the act and tap “Sign” or “There is a mistake”.",
    "ACT_MISTAKE_SENT": (
        "We passed your question to the operator — they will contact you "
        "and correct the act."
    ),

    # ── закрытие аренды ──
    "CLOSE_NO_RENTAL": (
        "No active rental is registered for you. If you have a bike — "
        "write to “🆘 Support”, we will sort it out."
    ),
    "CLOSE_ASK_REASON": (
        "All right, let's arrange the rental closure.\n"
        "Write in one line why you are returning the bike — it is needed "
        "for the report (for example: “starting my main job”).\n\n"
        "Changed your mind — tap “Cancel”."
    ),
    "CLOSE_REQUESTED": (
        "The closure request has been passed to the operator. They will "
        "contact you and set a time; we accept bikes daily from 10:00 to "
        "19:00 at any of our points.\n"
        "After the inspection we will send the Return Act for confirmation."
    ),
    "CLOSE_REQUEST_FAILED": (
        "We could not pass on the request due to a technical hiccup. "
        "Message us directly: " + URL
    ),

    # ── мои аренды ──
    "TRIPS_EMPTY": "No rentals yet. Rates are in the menu, arranging one — via support.",
    "TRIPS_HEADER": "📋 <b>Your rentals</b>\n",
    "TRIPS_ACTIVE": "• No. {number} · {bike} · {term} — <b>currently rented</b>",
    "TRIPS_CLOSED": "• No. {number} · {bike} · {term} — closed {closed_at}",

    # ── возврат ──
    "RETURN_READY": (
        "The Return Act under agreement No. {number} is ready.\n\n"
        "Check the remarks and tap “Confirm” — the rental will be closed."
    ),
    "RETURN_SIGNED": (
        "The Return Act under agreement No. {number} was confirmed "
        "{signed_at}.\nThe rental is closed. Thank you for choosing us!"
    ),

    # ── кнопки ──
    "BTN_RENT": "🚲 Rent",
    "BTN_TRIPS": "📋 My rentals",
    "BTN_TARIFFS": "💰 Rates",
    "BTN_SUPPORT": "🆘 Support",
    "BTN_FAQ": "❓ Frequently asked questions",
    "BTN_CLOSE_RENT": "🔚 Close rental",
    "BTN_CANCEL": "Cancel",
    "BTN_SAME_ADDRESS": "Same as registration",
    "BTN_SHARE_CONTACT": "📱 Share contact",
    "BTN_SUBSCRIBE": "Subscribe to the channel",
    "BTN_CHECK_SUB": "Check subscription",
    "BTN_POLICY_WEB": "Policy (web version)",
    "BTN_POLICY_ACK": "✔️ I have read the Policy",
    "BTN_RULES": "Rental rules",
    "BTN_PDN": "Personal data policy",
    "BTN_CONSENT": "✅ I consent",
    "BTN_CONFIRM": "Confirm",
    "BTN_RESTART": "Fill in again",
    "BTN_SIGN": "✍️ Sign",
    "BTN_MISTAKE": "There is a mistake",
    "BTN_PAY": "💳 Pay",
    "BTN_PAID": "✅ I have paid",
    "BTN_RETURN_SIGN": "✍️ Confirm",
}

# Ошибки валидации: русский текст из logic.py -> перевод.
ERRORS: dict[str, str] = {
    "Год должен быть не раньше 1900.":
        "The year must be 1900 or later.",
    "Прокат доступен с 16 лет (до 18 - с письменного согласия родителя).":
        "Rental is available from age 16 (under 18 — with the written consent of a parent).",
    "Похоже на опечатку. Введите ФИО полностью.":
        "Looks like a typo. Please enter your full name in full.",
    "Похоже на опечатку. Введите ФИО полностью, без цифр.":
        "Looks like a typo. Please enter your full name in full, without digits.",
    "В ФИО недопустимы символы < > и &.":
        "The characters < > and & are not allowed in the name.",
    "Дата нужна в виде ДД.ММ.ГГГГ, например 07.03.1990.":
        "The date must be DD.MM.YYYY, for example 07.03.1990.",
    "Такой даты не существует. Проверьте число и месяц.":
        "That date does not exist. Check the day and the month.",
    "Дата не может быть в будущем.":
        "The date cannot be in the future.",
    "Похоже на опечатку в годе рождения. Проверьте, пожалуйста.":
        "Looks like a typo in the birth year. Please check it.",
    "Серия и номер - это 10 цифр, например 1234 567890. Проверьте, сколько получилось.":
        "The series and number are 10 digits, for example 1234 567890. Check how many you have.",
    "Адрес нужен полностью: город, улица, дом, квартира. Индекс по желанию.":
        "The full address is required: city, street, building, apartment. Postcode optional.",
    "Нужно изображение (JPG, PNG или HEIC). Пришлите фото, а не файл другого типа.":
        "An image is required (JPG, PNG or HEIC). Send a photo, not another file type.",
    "Код подразделения - 6 цифр, например 160-002.":
        "The issuing unit code is 6 digits, for example 160-002.",
    "Впишите, кем выдан паспорт, как в документе - строкой целиком.":
        "Enter the issuing authority exactly as in the document — one full line.",
    "Недопустимы символы < > и &.":
        "The characters < > and & are not allowed.",
    "Укажите место рождения, как в паспорте.":
        "Enter the place of birth as in your passport.",
    "В адресе недопустимы символы < > и &.":
        "The characters < > and & are not allowed in the address.",
    "В адресе не хватает номера дома.":
        "The address is missing a building number.",
    "Не похоже на номер телефона. Пример: +7 900 123-45-67.":
        "That does not look like a phone number. Example: +7 900 123-45-67.",
    "Этот номер уже указан. Нужен другой.":
        "This number is already provided. A different one is needed.",
    "Файл слишком большой. Пришлите фото до 12 МБ.":
        "The file is too large. Send a photo up to 12 MB.",
    "Опишите вопрос текстом, хотя бы парой слов.":
        "Describe your question in text, at least a couple of words.",
    "Слишком длинно. Уложите вопрос в 1500 символов.":
        "Too long. Keep your question within 1500 characters.",
    "Напишите причину одной строкой, 3-300 символов.":
        "Write the reason in one line, 3–300 characters.",
}
