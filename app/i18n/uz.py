"""UZ: переводы клиентского диалога (узбекский, латиница).

Машинный перевод, ждёт вычитки носителем.
"""

from __future__ import annotations

from ..texts import SUPPORT_CONTACT_URL as URL

T: dict[str, str] = {
    "NOT_SUBSCRIBED": (
        "Siz kanalimizga obuna bo'lmagansiz!\n"
        "Botdan foydalanish uchun yangiliklar kanalimizga obuna bo'ling: "
        "{channel_url}"
    ),
    "SUB_NOT_FOUND": "Obuna topilmadi. Yana bir bor urinib ko'ring.",
    "SUB_CONFIRMED_TOAST": "Obuna tasdiqlandi",
    "STALE_BUTTON": "Bu tugma eskirgan, /start yuboring",
    "WELCOME": "👋 Xush kelibsiz! Boshlash uchun to'liq ism-sharifingizni (F.I.O.) yozing:",
    "FIO_AS_TEXT": "Iltimos, F.I.O.ni matn bilan yozing.",
    "FAQ_ENTRY_HINT": (
        "To'ldirish davomida ko'p beriladigan savollarga javoblarni ko'rishingiz "
        "mumkin: manzillar, narxlar, hujjatlar."
    ),

    "POLICY_CAPTION": (
        "📄 <b>Shaxsiy ma'lumotlarni qayta ishlash siyosati</b>\n\n"
        "Davom etishdan oldin, iltimos, YaTB Galimzyanov Edgar Ayratovichning "
        "shaxsiy ma'lumotlarni qayta ishlash bo'yicha Siyosati bilan tanishing "
        "(yuqoridagi hujjat). Unda qanday ma'lumotlar va nima uchun qayta "
        "ishlanishi, ular qanday himoyalanishi, saqlash muddatlari, "
        "velosipedlardagi GPS-kuzatgichlar, topshirish va qaytarishning foto- "
        "va videoga olinishi, rozilikni qaytarib olish tartibi yozilgan.\n\n"
        "O'qib bo'ldingizmi — «Siyosat bilan tanishdim» tugmasini bosing."
    ),
    "POLICY_NO_FILE": (
        "📄 <b>Shaxsiy ma'lumotlarni qayta ishlash siyosati</b>\n\n"
        "Davom etishdan oldin YaTB Galimzyanov Edgar Ayratovichning shaxsiy "
        "ma'lumotlarni qayta ishlash Siyosati bilan tanishing. Matnini "
        "operatordan so'rash mumkin: " + URL + "\n\n"
        "Tanishdingizmi — «Siyosat bilan tanishdim» tugmasini bosing."
    ),
    "POLICY_PRESS_BUTTON": (
        "Iltimos, Siyosat bilan tanishing va davom etish uchun «Siyosat bilan "
        "tanishdim» tugmasini bosing."
    ),
    "POLICY_ACK_TOAST": "Tanishuv qayd etildi",

    "CONSENT": (
        "Tekshiring: <b>{fio}</b>\n\n"
        "<b>Shaxsiy ma'lumotlarni qayta ishlashga rozilik</b>\n\n"
        "Prokat shartnomasini tuzish va bajarish uchun YaTB Galimzyanov Edgar "
        "Ayratovich (INN 165921923517) sizning shaxsiy ma'lumotlaringizni "
        "qayta ishlaydi: F.I.O., tug'ilgan sana va joy, pasport ma'lumotlari, "
        "ro'yxatga olingan va yashash manzillari, telefon raqamlari va "
        "shaxsni tasdiqlovchi hujjat tasviri; 16–17 yoshli ijarachilar uchun — "
        "qonuniy vakilning yozma roziligi tasviri ham.\n"
        "Ma'lumotlar faqat Ijaraga beruvchi tomonidan qayta ishlanadi va "
        "qonunda nazarda tutilgan hollardan tashqari uchinchi shaxslarga "
        "berilmaydi.\n"
        "Tasvirlar ariza bo'yicha qaror qabul qilingandan {purge_days} kun "
        "o'tib o'chiriladi. Rozilikni qo'llab-quvvatlash xizmatiga yozib "
        "qaytarib olish mumkin.\n"
        "Shaxsiy ma'lumotlarni qayta ishlash Siyosati bilan oldingi qadamda "
        "tanishdingiz.\n\n"
        "«Rozilik beraman» tugmasini bosish orqali yuqoridagi ma'lumotlarni "
        "qayta ishlashga rozilik berasiz."
    ),
    "CONSENT_PRESS_BUTTON": "Davom etish uchun «Rozilik beraman» tugmasini bosing.",
    "CONSENT_GIVEN": "Rozilik qayd etildi",

    "ASK_CONTACT": "Iltimos, kontaktingizni ulashing.",
    "CONTACT_USE_BUTTON": "Ekran pastidagi «Kontaktni ulashish» tugmasini bosing.",
    "CONTACT_FOREIGN": "Bu boshqa odamning kontakti. Iltimos, pastdagi tugma bilan o'z raqamingizni yuboring.",
    "ANKETA_INTRO": (
        "Rahmat! Endi prokat shartnomasi uchun bir nechta ma'lumot kerak. "
        "Bu bir-ikki daqiqa oladi — pasportda yozilganidek, har bir javobni "
        "alohida xabar bilan yuboring."
    ),
    "ASK_BIRTH": "Tug'ilgan sana — KK.OO.YYYY ko'rinishida.\nMasalan: 07.03.1990",
    "ASK_BIRTH_PLACE": "Tug'ilgan joy, pasportdagidek.\nMasalan: gor. Kazan",
    "ASK_PASSPORT": "Pasport seriyasi va raqami — 10 ta raqam.\nMasalan: 1234 567890",
    "ASK_PASSPORT_DATE": "Pasport berilgan sana — KK.OO.YYYY.\nMasalan: 01.02.2015",
    "ASK_PASSPORT_CODE": "Bo'linma kodi — 6 ta raqam.\nMasalan: 160-002",
    "ASK_PASSPORT_ISSUER": (
        "Pasport kim tomonidan berilgan — hujjatdagidek, bir qatorda "
        "to'liq yozing.\n"
        "Masalan: OUFMS Rossii po Resp. Tatarstan v Vaxitovskom r-ne gor. Kazani"
    ),
    "ASK_REG_ADDR": (
        "Ro'yxatga olingan manzil to'liq: shahar, ko'cha, uy, xonadon.\n"
        "Masalan: g. Kazan, ul. Baumana, d. 1, kv. 2"
    ),
    "ASK_LIVE_ADDR": (
        "Amalda yashash manzili.\n"
        "Ro'yxatga olingan manzil bilan bir xil bo'lsa — pastdagi tugmani "
        "bosing."
    ),
    "ASK_PHONE2": (
        "Ikkinchi telefon raqami — masalan, yaqin kishingizniki.\n"
        "Asosiy raqam ishlamasa, siz bilan bog'lanish uchun kerak.\n"
        "Masalan: +7 900 123-45-67"
    ),
    "ASK_PHONE3": "Va aloqa uchun uchinchi telefon raqami.",
    "SAME_AS_REG": "Yashash manzilini ro'yxatga olingan manzil sifatida yozib qo'ydim.",
    "ANKETA_AS_TEXT": "Iltimos, javobni bitta matnli xabar bilan yuboring.",
    "PASSPORT_DATE_BEFORE_BIRTH": (
        "Berilgan sana tug'ilgan sanaga to'g'ri kelmaydi: pasport 14 yoshdan "
        "beriladi. Iltimos, ikkala sanani tekshiring — tug'ilgan sanadan "
        "boshlaymiz."
    ),
    "ASK_DOC": (
        "Oxirgi qadam qoldi: shaxsni tasdiqlovchi hujjat suratini biriktiring. "
        "Ma'lumotlar aniq o'qiladigan qilib suratga oling."
    ),
    "DOC_NEED_PHOTO": "Aynan hujjat surati kerak. Rasm biriktiring.",
    "ASK_DOC2": (
        "Surat qabul qilindi. Endi hujjatning ikkinchi suratini yuboring "
        "— ro'yxatdan o'tgan manzil sahifasi (haydovchilik guvohnomasida "
        "— orqa tomoni).\n\nAgar barcha ma'lumot birinchi suratda "
        "ko'rinsa, quyidagi tugmani bosing."
    ),
    "DOC2_NEED_PHOTO": (
        "Hujjatning ikkinchi sahifasi surati kerak. Rasm biriktiring "
        "yoki «Bitta surat yetarli» tugmasini bosing."
    ),
    "ASK_PARENT_CONSENT": (
        "Siz hali 18 yoshga to'lmagansiz, shuning uchun yana bitta hujjat "
        "kerak: ota-onaning (qonuniy vakilning) prokat shartnomasini tuzishga "
        "yozma roziligi.\n\n"
        "Ota-ona qo'lda rozilik yozadi: o'zining F.I.O.si, pasport "
        "ma'lumotlari va sizning F.I.O.ingizni ko'rsatadi, sana va imzo "
        "qo'yadi. Shu rozilikning suratini yuboring — matn aniq o'qiladigan "
        "bo'lsin."
    ),
    "PARENT_NEED_PHOTO": "Ota-onaning yozma roziligi surati kerak. Rasm biriktiring.",

    "CONFIRM_CAPTION": (
        "Iltimos, kiritilgan ma'lumotlar to'g'riligini tasdiqlang:\n\n"
        "F.I.O.: <b>{fio}</b>\n"
        "Telefon raqami: <b>+{phone}</b>"
    ),
    "CONFIRM_PRESS_BUTTON": "«Tasdiqlayman» yoki «Qaytadan to'ldirish» tugmasini bosing.",
    "RESTART": "Yaxshi, boshidan boshlaymiz. F.I.O.ingizni yozing:",
    "RESTART_TOAST": "Qaytadan to'ldiramiz",
    "SUBMITTED": (
        "Rahmat! Ma'lumotlar tekshiruvga yuborildi. Odatda bu 30 daqiqagacha "
        "vaqt oladi — hammasi tayyor bo'lishi bilan yozamiz."
    ),
    "SUBMITTED_TOAST": "Tekshiruvga yuborildi",
    "SUBMIT_PROBLEM": (
        "Arizani uzatishda texnik nosozlik yuz berganga o'xshaydi. Biz bu "
        "haqda bilamiz. Bir soat ichida javob bo'lmasa — qo'llab-quvvatlash "
        "xizmatiga yozing."
    ),
    "PENDING_WAIT": "Arizangiz tekshirilmoqda. Ko'rib chiqilishi bilan yozamiz.",
    "APPROVED_WAIT_ISSUE": (
        "Ariza ma'qullandi! Shartnomani tayyorlayapmiz: velosiped "
        "ma'lumotlari va jihozlarini kiritmoqdamiz. Imzolash uchun shu yerga "
        "yuboramiz."
    ),
    "REGISTERED": "Ro'yxatdan muvaffaqiyatli o'tdingiz.\nVideoqo'llanma: {video_url}",
    "ALREADY_REGISTERED": "Siz allaqachon ro'yxatdan o'tgansiz. Qanday yordam beraylik?",
    "MENU_PROMPT": "Pastdagi menyudan amalni tanlang.",
    "REJECTED_WITH_REASON": (
        "Afsuski, ariza rad etildi.\n\n"
        "Nimani to'g'rilash kerak: {reason}\n\n"
        "Davom etish uchun /start ni bosing yoki kerakli ma'lumotlarni "
        "shunchaki yuboring."
    ),

    "TARIFFS": (
        "💰 <b>Ijaraning amaldagi narxlari</b>\n\n"
        "<b>Truck+ 2 ta akkumulyator bilan</b>\n"
        "• 1 hafta — 3 000 ₽\n"
        "• 2 hafta — 5 400 ₽\n"
        "• 1 oy — 11 000 ₽\n\n"
        "<b>Truck+ orqa amortizatorli, 2 ta akkumulyator</b>\n"
        "• 1 hafta — 3 400 ₽\n"
        "• 2 hafta — 5 400 ₽\n"
        "• 1 oy — 11 000 ₽\n\n"
        "<b>Kugoo V3 Pro 2 ta akkumulyator bilan</b>\n"
        "• 1 hafta — 3 500 ₽\n"
        "• 2 hafta — 6 400 ₽\n"
        "• 1 oy — 12 000 ₽\n\n"
        "<b>Kugoo V3 Pro+ 2 ta akkumulyator bilan</b>\n"
        "• 1 hafta — 3 500 ₽\n"
        "• 2 hafta — 6 400 ₽\n"
        "• 1 oy — 12 000 ₽\n\n"
        "Kelishilgan muddatdan keyin uzaytirish — sutkasiga 650 ₽ "
        "(shartnoma bo'yicha).\n"
        "\nNarxga kiradi: navbatsiz ta'mirlash, buzilganda bir kun ichida "
        "almashtirish velosipedi, GPS-himoya va texnik xizmat bizning "
        "hisobimizdan. <b>Garov puli yo'q.</b>\n"
        "Ijaraga olish uchun «🆘 Qo'llab-quvvatlash» tugmasini bosing yoki "
        "to'g'ridan-to'g'ri yozing: " + URL
    ),

    "FAQ_GUEST_CONTACT": "Bizga yozing: " + URL,
    "FAQ_HANDOFF": (
        "Iltimos, tafsilotlarni bitta xabarda yozing — menejerga "
        "yetkazaman.\nFikringizdan qaytdingizmi — «Bekor qilish» tugmasini "
        "bosing."
    ),
    "FAQ_TOPIC_GONE": "Bu mavzu endi mavjud emas, «Ko'p beriladigan savollar»ni qaytadan oching.",
    "SUPPORT_SENT_ANSWERED": (
        "Agar bu kerakli javob bo'lmasa — menejer savolingizni ko'rib turibdi "
        "va ish vaqtida javob beradi."
    ),
    "SUPPORT_PROMPT": (
        "Savolingizni bitta xabarda yozing — shu chatning o'zida javob "
        "beramiz.\nYoki bizga to'g'ridan-to'g'ri yozing: " + URL + "\n\n"
        "Fikringizdan qaytdingizmi — «Bekor qilish» tugmasini bosing."
    ),
    "SUPPORT_AS_TEXT": "Savolni bitta matnli xabar bilan yozing.",
    "SUPPORT_SENT": (
        "Savol yetkazildi. Javob shu chatga keladi.\n"
        "Shoshilinch bo'lsa — to'g'ridan-to'g'ri yozing: " + URL
    ),
    "SUPPORT_FAILED": (
        "Texnik nosozlik tufayli savolni yetkazib bo'lmadi. Bizga "
        "to'g'ridan-to'g'ri yozing: " + URL
    ),
    "SUPPORT_CANCELLED": "Yaxshi, menyuga qaytdik.",
    "SUPPORT_REPLY_USER": "💬 Qo'llab-quvvatlash javobi:\n\n{answer}",
    "RATE_LIMITED": "Xabarlar juda ko'p. Bir daqiqa kuting.",

    "CONTRACT_READY_USER": (
        "№ {number} prokat shartnomasi tayyor. Yuqorida — unga ilova: "
        "shaxsiy ma'lumotlarni qayta ishlashga Rozilik.\n\n"
        "Iltimos, ikkala hujjatni to'liq o'qing va «Imzolayman» tugmasini "
        "bosing — imzo shartnoma va ilovaga taalluqli. Xato topsangiz — "
        "«Xato bor» tugmasini bosing, anketani to'g'irlashga yuboramiz."
    ),
    "SOGLASIE_CAPTION": (
        "№ {number} shartnomaga ilova: shaxsiy ma'lumotlarni qayta "
        "ishlashga Rozilik."
    ),
    "SOGLASIE_SIGNED_CAPTION": (
        "Shaxsiy ma'lumotlarni qayta ishlashga rozilik (№ {number} "
        "shartnomaga ilova) {signed_at} imzolandi."
    ),
    "CONTRACT_SIGNED_USER": (
        "№ {number} shartnoma {signed_at} imzolandi.\n"
        "Nusxasi shu chatda sizda qoladi.\n\n"
        "Videoqo'llanma: {video_url}"
    ),
    "CONTRACT_RESEND": (
        "№ {number} shartnoma hali imzolanmagan — mana u yana.\n\n"
        "O'qing va hammasi to'g'ri bo'lsa «Imzolayman», xato topsangiz "
        "«Xato bor» tugmasini bosing."
    ),
    "CONTRACT_SIGN_TOAST": "Shartnoma imzolandi",
    "CONTRACT_PRESS_BUTTON": "Shartnomani o'qing va «Imzolayman» yoki «Xato bor» tugmasini bosing.",
    "CONTRACT_MISTAKE": (
        "Yaxshi, anketani boshidan to'ldiramiz — shunda barcha ma'lumotlar "
        "shartnomaga to'g'ri tushadi."
    ),
    "CONTRACT_FAILED_USER": (
        "Ariza ma'qullandi, lekin texnik xato tufayli shartnoma "
        "shakllanmadi. Biz bu haqda bilamiz va shartnomani qo'lda yuboramiz."
    ),

    "RENT_ALREADY_ACTIVE": (
        "Sizda allaqachon faol ijara bor: <b>{bike}</b>, muddati {term}.\n"
        "Boshqa velosiped olish uchun avval joriy ijarani yoping — menyudagi "
        "«🔚 Ijarani yopish» tugmasi."
    ),
    "RENT_REQUEST_SENT": (
        "✅ Ijara arizasi operatorga yetkazildi. U topshirishni tasdiqlaydi "
        "va velosiped ma'lumotlarini kiritadi, shundan keyin to'lov summasi "
        "va topshirish Aktini yuboramiz.\n"
        "Velosipedlarni har kuni 10:00 dan 19:00 gacha beramiz; shartnoma "
        "avvalgisi amal qiladi."
    ),
    "RENT_REQUEST_FAILED": (
        "Texnik nosozlik tufayli arizani yetkazib bo'lmadi. Bizga "
        "to'g'ridan-to'g'ri yozing: " + URL
    ),

    "PAY_PROMPT": (
        "Bitta qadam qoldi — ijara to'lovi.\n\n"
        "Summa va usul: <b>{price}</b>\n\n"
        "To'lov pastdagi havola orqali — bu bizning yagona hisob "
        "raqamimiz 👇\n{pay_url}\n\n"
        "To'lagach «To'ladim» tugmasini bosing va chekni shu chatga "
        "yuboring. Operator pul kelganini tasdiqlashi bilan topshirish "
        "Aktini yuboramiz: velosipedni shu akt bo'yicha olasiz."
    ),
    "PAY_WAIT": (
        "Ijara to'lovini kutmoqdamiz: <b>{price}</b>.\n"
        "To'lov havolasi (yagona hisob raqami):\n{pay_url}\n\n"
        "To'ladingizmi — «To'ladim» tugmasini bosing va chekni yuboring, "
        "operator kelib tushganini tekshiradi. Tasdiqlangach topshirish "
        "Aktini yuboramiz."
    ),
    "PAY_NUDGE_TOAST": "Operatorga yetkazdik — u pul kelganini tekshiradi",
    "PAY_RECEIPT_SENT": (
        "Chekni operatorga yetkazdik — u pul kelganini tekshiradi. "
        "Tasdiqlashi bilan topshirish Aktini yuboramiz."
    ),
    "PAY_RECEIPT_FAILED": (
        "Texnik nosozlik tufayli chekni yetkazib bo'lmadi. «To'ladim» "
        "tugmasini bosing — operator hisob bo'yicha tekshiradi."
    ),
    "PAY_CONFIRMED_USER": (
        "To'lov qabul qilindi, rahmat! Keyingi xabar bilan topshirish "
        "Akti keladi."
    ),

    "ACT_IN_READY": (
        "To'lov tasdiqlandi! Endi № {number} topshirish Akti.\n\n"
        "VIN raqamlari va jihozlarni tekshiring va «Imzolayman» tugmasini "
        "bosing — shu paytdan mulk sizga topshirilgan hisoblanadi."
    ),
    "ACT_IN_SIGNED": (
        "№ {number} shartnoma bo'yicha topshirish Akti {signed_at} "
        "imzolandi.\nNusxasi shu chatda sizda qoladi. Yo'lingiz bexatar "
        "bo'lsin!\n\nVideoqo'llanma: {video_url}"
    ),
    "ACT_RESEND": (
        "№ {number} shartnoma bo'yicha akt hali imzolanmagan — mana u "
        "yana.\n«Imzolayman» yoki «Xato bor» tugmasini bosing."
    ),
    "ACT_PRESS_BUTTON": "Aktni o'qing va «Imzolayman» yoki «Xato bor» tugmasini bosing.",
    "ACT_MISTAKE_SENT": (
        "Savolingizni operatorga yetkazdik — u siz bilan bog'lanadi va "
        "aktni to'g'irlaydi."
    ),

    "CLOSE_NO_RENTAL": (
        "Sizda faol ijara yo'q. Agar velosiped sizda bo'lsa — "
        "«🆘 Qo'llab-quvvatlash»ga yozing, hal qilamiz."
    ),
    "CLOSE_ASK_REASON": (
        "Yaxshi, ijarani yopishni rasmiylashtiramiz.\n"
        "Nima uchun topshirayotganingizni bir qatorda yozing — bu hisobot "
        "uchun kerak (masalan: «asosiy ishga chiqyapman»).\n\n"
        "Fikringizdan qaytdingizmi — «Bekor qilish» tugmasini bosing."
    ),
    "CLOSE_REQUESTED": (
        "Yopish so'rovi operatorga yetkazildi. U siz bilan bog'lanadi va "
        "vaqtni aytadi; velosipedlarni har kuni 10:00 dan 19:00 gacha "
        "istalgan nuqtada qabul qilamiz.\n"
        "Ko'rikdan keyin tasdiqlash uchun qaytarish Aktini yuboramiz."
    ),
    "CLOSE_REQUEST_FAILED": (
        "Texnik nosozlik tufayli so'rovni yetkazib bo'lmadi. Bizga "
        "to'g'ridan-to'g'ri yozing: " + URL
    ),

    "TRIPS_EMPTY": "Hali ijaralar bo'lmagan. Narxlar — menyuda, rasmiylashtirish — qo'llab-quvvatlash orqali.",
    "TRIPS_HEADER": "📋 <b>Sizning ijaralaringiz</b>\n",
    "TRIPS_ACTIVE": "• № {number} · {bike} · {term} — <b>hozir ijarada</b>",
    "TRIPS_CLOSED": "• № {number} · {bike} · {term} — {closed_at} yopilgan",

    "RETURN_READY": (
        "№ {number} shartnoma bo'yicha qaytarish Akti tayyor.\n\n"
        "Izohlarni tekshiring va «Tasdiqlayman» tugmasini bosing — ijara "
        "yopiladi."
    ),
    "RETURN_SIGNED": (
        "№ {number} shartnoma bo'yicha qaytarish Akti {signed_at} "
        "tasdiqlandi.\nIjara yopildi. Bizni tanlaganingiz uchun rahmat!"
    ),

    # ── сроки и продление ──
    "BTN_EXTEND": "📅 Ijarani uzaytirish",
    "REMIND_SOON": "📅 Eslatma: <b>{bike}</b> ijarasi {until} tugaydi. Qolgan kunlar: {days}.\n\nUzaytirmoqchimisiz — pastdagi tugmani bosing, operator summani aytadi. Topshirmoqchimisiz — menyudagi «🔚 Ijarani yopish»; velosipedlarni har kuni 10:00 dan 19:00 gacha qabul qilamiz.",
    "REMIND_LAST_DAY": "📅 Bugun <b>{bike}</b> ijarasining oxirgi kuni ({until} gacha).\n\nUzaytirish — pastdagi tugma. Bugun topshirsangiz — menyudagi «🔚 Ijarani yopish» tugmasini bosing, vaqtni kelishamiz.",
    "REMIND_OVERDUE": "⚠️ <b>{bike}</b> ijarasi muddati {until} tugagan.\n\nIltimos, pastdagi tugma bilan uzaytiring yoki velosipedni topshiring — aks holda shartnomaga ko'ra foydalanish haqi hisoblanaveradi.\nOperator bilan allaqachon kelishgan bo'lsangiz — shu xabarga javob yozing.",
    "EXTEND_NO_RENTAL": "Sizda faol ijara yo'q — uzaytiradigan narsa yo'q. Velosiped olish uchun: «🚲 Ijaraga olish» tugmasi.",
    "EXTEND_ALREADY_ASKED": "Uzaytirish arizangiz allaqachon operatorda — u siz bilan bog'lanib summani aytadi. Qayta yuborish shart emas.",
    "EXTEND_REQUESTED": "✅ Uzaytirish arizasi operatorga yetkazildi. U yangi muddatni tasdiqlaydi va to'lov summasini yuboradi — velosiped sizda qoladi.",
    "EXTEND_REQUEST_FAILED": "Texnik nosozlik tufayli arizani yetkazib bo'lmadi. Bizga to'g'ridan-to'g'ri yozing: " + URL,
    "EXTEND_CONFIRMED": "✅ Ijara {until} gacha uzaytirildi. To'lov uchun rahmat!\nShartnoma va akt o'zgarmaydi — yangisini imzolash shart emas.",

    # ── выкуп ──
    "BUYOUT_LINE": "💎 Sotib olish: {total} dan {paid}, ({percent}%), to'lovlar {payments} dan {days}",
    "BUYOUT_LEFT_LINE": "{left} qoldi — bu {left_days} ta to'lov, {finish} gacha.",
    "BUYOUT_READY": "🎉 Tabriklaymiz! <b>{total}</b> miqdoridagi sotib olish qiymati to'liq to'landi.\n\nQuyida — <b>{bike}</b> velosipediga mulk huquqi o'tishi to'g'risidagi Akt. O'qing va «Imzolayman» tugmasini bosing — shu paytdan velosiped sizniki.\nJihozlar (akkumulyator, quvvatlagich, sumka va boshqalar) sotib olishga kirmaydi va Qaytarish akti bo'yicha qaytariladi — operator siz bilan vaqtni kelishadi.",
    "BUYOUT_SIGNED": "✅ № {number} shartnoma bo'yicha mulk huquqi o'tishi to'g'risidagi Akt {signed_at} imzolandi.\nVelosiped sizning mulkingizga o'tdi — tabriklaymiz!\n\nAkt nusxasi shu chatda sizda qoladi.",
    "BUYOUT_RESEND": "№ {number} shartnoma bo'yicha mulk huquqi o'tishi akti hali imzolanmagan — mana u yana.\n«Imzolayman» yoki «Xato bor» tugmasini bosing.",

    "BTN_RENT": "🚲 Ijaraga olish",
    "BTN_TRIPS": "📋 Mening ijaralarim",
    "BTN_TARIFFS": "💰 Narxlar",
    "BTN_SUPPORT": "🆘 Qo'llab-quvvatlash",
    "BTN_FAQ": "❓ Ko'p beriladigan savollar",
    "BTN_CLOSE_RENT": "🔚 Ijarani yopish",
    "BTN_CANCEL": "Bekor qilish",
    "BTN_SAME_ADDRESS": "Ro'yxatga olingan manzil bilan bir xil",
    "BTN_SHARE_CONTACT": "📱 Kontaktni ulashish",
    "BTN_SUBSCRIBE": "Kanalga obuna bo'lish",
    "BTN_CHECK_SUB": "Obunani tekshirish",
    "BTN_POLICY_WEB": "Siyosat (veb-nusxa)",
    "BTN_POLICY_ACK": "✔️ Siyosat bilan tanishdim",
    "BTN_RULES": "Prokat qoidalari",
    "BTN_PDN": "Shaxsiy ma'lumotlar siyosati",
    "BTN_CONSENT": "✅ Rozilik beraman",
    "BTN_CONFIRM": "Tasdiqlayman",
    "BTN_RESTART": "Qaytadan to'ldirish",
    "BTN_DOC_ENOUGH": "Bitta surat yetarli",
    "BTN_SIGN": "✍️ Imzolayman",
    "BTN_MISTAKE": "Xato bor",
    "BTN_PAY": "💳 To'lash",
    "BTN_PAID": "✅ To'ladim",
    "BTN_RETURN_SIGN": "✍️ Tasdiqlayman",
}

ERRORS: dict[str, str] = {
    "Год должен быть не раньше 1900.":
        "Yil 1900 dan oldin bo'lmasligi kerak.",
    "Прокат доступен с 16 лет (до 18 - с письменного согласия родителя).":
        "Ijara 16 yoshdan mumkin (18 gacha — ota-onaning yozma roziligi bilan).",
    "Похоже на опечатку. Введите ФИО полностью.":
        "Xatoga o'xshaydi. F.I.O.ni to'liq yozing.",
    "Похоже на опечатку. Введите ФИО полностью, без цифр.":
        "Xatoga o'xshaydi. F.I.O.ni to'liq, raqamlarsiz yozing.",
    "В ФИО недопустимы символы < > и &.":
        "F.I.O.da < > va & belgilariga ruxsat yo'q.",
    "Дата нужна в виде ДД.ММ.ГГГГ, например 07.03.1990.":
        "Sana KK.OO.YYYY ko'rinishida bo'lishi kerak, masalan 07.03.1990.",
    "Такой даты не существует. Проверьте число и месяц.":
        "Bunday sana mavjud emas. Kun va oyni tekshiring.",
    "Дата не может быть в будущем.":
        "Sana kelajakda bo'lishi mumkin emas.",
    "Похоже на опечатку в годе рождения. Проверьте, пожалуйста.":
        "Tug'ilgan yilda xatoga o'xshaydi. Iltimos, tekshiring.",
    "Серия и номер - это 10 цифр, например 1234 567890. Проверьте, сколько получилось.":
        "Seriya va raqam — 10 ta raqam, masalan 1234 567890. Nechta chiqqanini tekshiring.",
    "Адрес нужен полностью: город, улица, дом, квартира. Индекс по желанию.":
        "Manzil to'liq kerak: shahar, ko'cha, uy, xonadon. Indeks ixtiyoriy.",
    "Нужно изображение (JPG, PNG или HEIC). Пришлите фото, а не файл другого типа.":
        "Rasm kerak (JPG, PNG yoki HEIC). Boshqa turdagi fayl emas, surat yuboring.",
    "Код подразделения - 6 цифр, например 160-002.":
        "Bo'linma kodi — 6 ta raqam, masalan 160-002.",
    "Впишите, кем выдан паспорт, как в документе - строкой целиком.":
        "Pasport kim tomonidan berilganini hujjatdagidek, bir qatorda to'liq yozing.",
    "Недопустимы символы < > и &.":
        "< > va & belgilariga ruxsat yo'q.",
    "Укажите место рождения, как в паспорте.":
        "Tug'ilgan joyni pasportdagidek yozing.",
    "В адресе недопустимы символы < > и &.":
        "Manzilda < > va & belgilariga ruxsat yo'q.",
    "В адресе не хватает номера дома.":
        "Manzilda uy raqami yetishmayapti.",
    "Не похоже на номер телефона. Пример: +7 900 123-45-67.":
        "Telefon raqamiga o'xshamaydi. Namuna: +7 900 123-45-67.",
    "Этот номер уже указан. Нужен другой.":
        "Bu raqam allaqachon ko'rsatilgan. Boshqasi kerak.",
    "Файл слишком большой. Пришлите фото до 12 МБ.":
        "Fayl juda katta. 12 MB gacha surat yuboring.",
    "Опишите вопрос текстом, хотя бы парой слов.":
        "Savolni matn bilan yozing, hech bo'lmasa bir-ikki so'z.",
    "Слишком длинно. Уложите вопрос в 1500 символов.":
        "Juda uzun. Savolni 1500 belgiga sig'diring.",
    "Напишите причину одной строкой, 3-300 символов.":
        "Sababni bir qatorda yozing, 3–300 belgi.",
}
