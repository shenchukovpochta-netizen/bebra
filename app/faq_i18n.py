"""Переводы ветки частых вопросов.

Русский - основной язык и живёт в app/faq.py вместе с фактами; здесь только
остальные семь языков. Ключа нет в словаре - бот молча отвечает по-русски:
пропуск перевода не должен оставлять клиента без ответа.

Что НЕ переводится намеренно:
- адреса и названия моделей: по-русски (и латиницей) их понимают карты
  и сами клиенты, а перевод адреса - это адрес, который никто не найдёт;
- цены, ссылки, телефон - одинаковы на всех языках;
- распознавание свободного текста в поддержке: триггеры русские, поэтому
  автоответ на набранный вопрос остаётся русским. Язык действует в ветке
  кнопок «Ответы на частые вопросы».

Переводы на туркменский, узбекский, арабский, фарси, хинди и особенно
чувашский написаны простыми фразами и не вычитаны носителями - покажите
знакомым носителям перед тем, как полагаться на формулировки дословно.
"""

from __future__ import annotations

# Порядок = порядок кнопок в выборе языка. Русский первым: основная аудитория.
LANGS: tuple[str, ...] = ("ru", "en", "uz", "tk", "ar", "fa", "hi", "cv")

# Подписи кнопок - самоназвания: человек ищет свой язык, а не его русское имя.
LANG_TITLES: dict[str, str] = {
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
    "uz": "🇺🇿 Oʻzbekcha",
    "tk": "🇹🇲 Türkmençe",
    "ar": "🇪🇬 العربية",
    "fa": "🇮🇷 فارسی",
    "hi": "🇮🇳 हिन्दी",
    "cv": "Чӑвашла",
}

CONTACT = "https://t.me/arenda_velo_kazan"

# ─────────────────────────── переводы ───────────────────────────
# Ключи: pick/menu/contact/change/after_hours/handoff/your_rate,
# week/weeks2/month + price_* - для составного ответа о тарифах,
# t_<КОД> - заголовок темы в меню, a_<КОД> - ответ по теме.

T: dict[str, dict[str, str]] = {

"en": {
    "pick": "Choose a language:",
    "menu": "Pick a topic — I'll answer right away.",
    "contact": "Write to us: " + CONTACT,
    "change": "🌐 Change language",
    "after_hours": "The points are closed right now — come from 10:00, "
                   "we are open daily until 19:00.",
    "handoff": "Describe the details in one message — I'll pass it to the "
               "manager.\nChanged your mind — tap «Отмена».",
    "your_rate": "Your contract rate: {plan}.",
    "week": "week", "weeks2": "2 weeks", "month": "month",
    "price_head": "💰 Rental rates (2 batteries and a charger included):",
    "price_ext": "Extension after the agreed term — 650 ₽/day (per contract).",
    "price_incl": "The rate includes priority repair, a replacement bike "
                  "within a day if yours breaks, GPS protection and "
                  "maintenance at our expense. No deposit.",
    "price_next": "To rent, write to us: " + CONTACT,
    "t_ADDR": "Addresses and directions",
    "t_HOURS": "Working hours",
    "t_PRICE": "Rental rates",
    "t_PAY": "Payment and receipt",
    "t_BATT_Q": "Battery range",
    "t_BATT_SWAP": "Battery swap",
    "t_BATT_3": "Third battery",
    "t_RETURN": "Returning the bike",
    "t_RENEW": "Extending the rental",
    "t_LEAD": "I want to rent",
    "t_DOCS": "Documents and deposit",
    "t_BRK_MECH": "Bike breakdown",
    "t_PICKUP": "Bike pickup",
    "t_EXT_REP": "Repair of your own vehicle",
    "t_REP_STATUS": "Repair status",
    "t_BUYOUT": "Buying the bike out",
    "a_ADDR": "We have two points in Kazan:\n"
              "📍 Adoratskogo 11A (Адоратского, 11А)\n"
              "📍 Pavlyukhina 97A — garage co-op «Sokol», box 9 "
              "(Павлюхина, 97А)\n"
              "Both are open daily 10:00–19:00. At Pavlyukhina enter GSK "
              "«Sokol» and look for box 9 — can't find it, write to us and "
              "we'll meet you.",
    "a_HOURS": "Open daily 10:00–19:00, no days off — both points:\n"
               "📍 Адоратского, 11А\n📍 Павлюхина, 97А (ГСК «Сокол»)\n"
               "Come any day.",
    "a_PAY": "Pay via the link below — it is our only bank account 👇\n"
             "{pay_url}\nAfter paying, please send the receipt here "
             "in the chat.",
    "a_BATT_Q": "Drain depends on speed: on the 3rd speed the battery lasts "
                "a couple of hours — that is normal. On speeds 1–2 two "
                "batteries cover about 50 km. If you think the batteries "
                "themselves are faulty — bring the bike to any point, we "
                "will test them under load and replace for free if needed.",
    "a_BATT_SWAP": "We swap batteries at both points daily 10:00–19:00. "
                   "Tell me which point you are going to — I'll check with "
                   "the admin whether charged ones are available.",
    "a_BATT_3": "A third battery is rented in addition to your plan — I'll "
                "check the current price with the manager. Write for how "
                "long you need it.",
    "a_RETURN": "We take the bike back any day 10:00–19:00 at either point "
                "— just tell us in advance when you'll come. About money: "
                "the bike is reserved for you for the whole paid term, so "
                "we do not recalculate unused days and do not carry them "
                "over.",
    "a_RENEW": "You can extend online, no need to come — pay via the link "
               "and send the receipt:\n{pay_url}\nThe extension is paid on "
               "the extension day in one payment.",
    "a_LEAD": "Yes, we have e-bikes for couriers — Truck+ and Kugoo V3 Pro, "
              "all with 2 batteries, a charger and a phone holder. No "
              "deposit, registration by passport. What term do you plan "
              "and which point is more convenient — Adoratskogo 11A or "
              "Pavlyukhina 97A?",
    "a_DOCS": "No deposit 🤝 Documents:\n— Russian citizens: passport;\n"
              "— foreign citizens: national passport, temporary "
              "registration and migration card.\nThe contract is signed "
              "right here in the bot once your application is checked.",
    "a_BRK_MECH": "Got it, we'll sort it out. Please record a short video "
                  "of how the bike behaves (what does not work, what is on "
                  "the display) and tell me which point is closer: "
                  "Adoratskogo 11A or Pavlyukhina 97A. Repair is priority "
                  "for renters; if it takes more than a day, we give a "
                  "replacement bike.",
    "a_PICKUP": "We have no mobile mechanic — repairs are done at the "
                "points. If the bike cannot move, we can pick it up by car "
                "by arrangement, the service is paid. Send your address — "
                "I'll pass it to the manager to agree on time and price.",
    "a_EXT_REP": "Yes, we repair not only our own vehicles: e-bikes, "
                 "e-scooters, trikes, e-motorcycles and batteries. Bring "
                 "it to any point, daily 10:00–19:00. The mechanic will "
                 "run diagnostics and name the exact price before starting "
                 "— nothing is done without your consent. Describe briefly "
                 "what is wrong and attach a photo.",
    "a_REP_STATUS": "I'll check with the mechanic what stage your bike is "
                    "at. Write which bike it is and when you brought it in "
                    "— I'll find it faster.",
    "a_BUYOUT": "🚲 You can buy the bike out: with 1 battery — 35 000 ₽, "
                "with 2 — 45 000 ₽; or in instalments without a bank and "
                "a down payment (2 batteries): 2 months at 6 250 ₽/week "
                "(50 000 ₽ total) or 4 months at 3 500 ₽/week (55 000 ₽ "
                "total). Pay like rent; after the last payment the bike is "
                "yours. I'll pass it to the manager.",
},

"uz": {
    "pick": "Tilni tanlang:",
    "menu": "Mavzuni tanlang — darhol javob beraman.",
    "contact": "Bizga yozing: " + CONTACT,
    "change": "🌐 Tilni oʻzgartirish",
    "after_hours": "Hozir punktlar yopiq — soat 10:00 dan keling, har kuni "
                   "19:00 gacha ishlaymiz.",
    "handoff": "Tafsilotlarni bitta xabarda yozing — menejerga yetkazaman."
               "\nFikringizdan qaytsangiz — «Отмена» tugmasini bosing.",
    "your_rate": "Shartnoma boʻyicha tarifingiz: {plan}.",
    "week": "hafta", "weeks2": "2 hafta", "month": "oy",
    "price_head": "💰 Ijara tariflari (2 ta AKB va zaryadlovchi bilan):",
    "price_ext": "Kelishilgan muddatdan keyin uzaytirish — kuniga 650 ₽ "
                 "(shartnoma boʻyicha).",
    "price_incl": "Tarifga kiradi: ustuvor taʼmirlash, buzilganda bir kun "
                  "ichida almashtirish velosipedi, GPS himoyasi va texnik "
                  "xizmat bizning hisobimizdan. Garov yoʻq.",
    "price_next": "Ijara uchun bizga yozing: " + CONTACT,
    "t_ADDR": "Manzillar va yoʻl",
    "t_HOURS": "Ish vaqti",
    "t_PRICE": "Ijara tariflari",
    "t_PAY": "Toʻlov va chek",
    "t_BATT_Q": "AKB: masofa",
    "t_BATT_SWAP": "AKB almashtirish",
    "t_BATT_3": "Uchinchi AKB",
    "t_RETURN": "Velosipedni topshirish",
    "t_RENEW": "Ijarani uzaytirish",
    "t_LEAD": "Ijaraga olmoqchiman",
    "t_DOCS": "Hujjatlar va garov",
    "t_BRK_MECH": "Velosiped buzildi",
    "t_PICKUP": "Olib ketish xizmati",
    "t_EXT_REP": "Oʻz texnikangizni taʼmirlash",
    "t_REP_STATUS": "Taʼmirlash holati",
    "t_BUYOUT": "Sotib olish",
    "a_ADDR": "Qozonda ikkita punktimiz bor:\n"
              "📍 Adoratskogo 11A (Адоратского, 11А)\n"
              "📍 Pavlyukhina 97A — «Sokol» GSK, 9-boks (Павлюхина, 97А)\n"
              "Ikkalasi ham har kuni 10:00–19:00 ishlaydi. Pavlyukhinada "
              "«Sokol» GSKga kirib 9-boksni qidiring — topolmasangiz, "
              "yozing, kutib olamiz.",
    "a_HOURS": "Har kuni 10:00–19:00 ishlaymiz, dam olish kunlarisiz — "
               "ikkala punkt ham:\n📍 Адоратского, 11А\n"
               "📍 Павлюхина, 97А (ГСК «Сокол»)\nIstalgan kuni keling.",
    "a_PAY": "Toʻlov quyidagi havola orqali — bu bizning yagona hisob "
             "raqamimiz 👇\n{pay_url}\nToʻlovdan soʻng chekni shu chatga "
             "yuboring.",
    "a_BATT_Q": "Sarf tezlikka bogʻliq: 3-tezlikda batareya bir necha "
                "soatda tugaydi — bu normal. 1–2-tezlikda ikkita AKB "
                "taxminan 50 km yetadi. Batareyaning oʻzida muammo deb "
                "oʻylasangiz — istalgan punktga olib keling, tekshiramiz, "
                "kerak boʻlsa bepul almashtiramiz.",
    "a_BATT_SWAP": "AKB almashtirishni ikkala punktda ham har kuni "
                   "10:00–19:00 qilamiz. Qaysi punktga borishingizni "
                   "yozing — zaryadlanganlari bor-yoʻqligini "
                   "administratordan aniqlayman.",
    "a_BATT_3": "Uchinchi AKB tarifga qoʻshimcha ijaraga beriladi — "
                "narxini menejerdan aniqlayman. Qancha muddatga "
                "kerakligini yozing.",
    "a_RETURN": "Velosipedni istalgan kuni 10:00–19:00 istalgan punktda "
                "qabul qilamiz — qachon kelishingizni oldindan yozing. "
                "Pul haqida: velosiped butun toʻlangan muddatga siz uchun "
                "band, shuning uchun foydalanilmagan kunlar uchun qayta "
                "hisob-kitob qilmaymiz va ularni keyingi davrga "
                "oʻtkazmaymiz.",
    "a_RENEW": "Uzaytirishni onlayn qilish mumkin, kelish shart emas — "
               "havola orqali toʻlab, chekni yuboring:\n{pay_url}\n"
               "Uzaytirish uzaytirish kunida bitta summa bilan toʻlanadi.",
    "a_LEAD": "Ha, kuryerlar uchun elektrovelosipedlar bor — Truck+ va "
              "Kugoo V3 Pro, hammasi 2 ta AKB, zaryadlovchi va telefon "
              "ushlagichi bilan. Garov yoʻq, rasmiylashtirish pasport "
              "boʻyicha. Qancha muddatga va qaysi punkt qulay — "
              "Adoratskogo 11A yoki Pavlyukhina 97A?",
    "a_DOCS": "Garov yoʻq 🤝 Hujjatlar:\n— Rossiya fuqarolari — pasport;\n"
              "— chet el fuqarolari — milliy pasport, vaqtinchalik "
              "roʻyxatga olish va migratsiya kartasi.\nShartnoma "
              "arizangiz tekshirilgach shu botning oʻzida imzolanadi.",
    "a_BRK_MECH": "Tushunarli, hal qilamiz. Velosiped oʻzini qanday "
                  "tutayotgani haqida qisqa video yozing (nima "
                  "ishlamayapti, displeyda nima bor) va qaysi punkt "
                  "yaqinligini ayting: Adoratskogo 11A yoki Pavlyukhina "
                  "97A. Ijarachilar uchun taʼmirlash ustuvor; ish bir "
                  "kundan uzoq davom etsa, almashtirish velosiped beramiz.",
    "a_PICKUP": "Sayyor ustamiz yoʻq — taʼmirlash punktlarda qilinadi. "
                "Velosiped yurmasa, kelishuv boʻyicha mashinada olib "
                "ketishimiz mumkin, xizmat pullik. Manzilingizni yozing — "
                "menejerga yetkazaman, vaqt va narxni kelishamiz.",
    "a_EXT_REP": "Ha, faqat oʻzimiznikini emas: elektrovelosiped, "
                 "elektrosamokat, trisikl, elektromototsikl va AKBlarni "
                 "taʼmirlaymiz. Istalgan punktga olib keling, har kuni "
                 "10:00–19:00. Usta diagnostika qilib, ishni boshlashdan "
                 "oldin aniq narxni aytadi — roziligingizsiz hech narsa "
                 "qilinmaydi. Muammoni qisqacha yozing va foto qoʻshing.",
    "a_REP_STATUS": "Velosipedingiz qaysi bosqichda ekanini ustadan "
                    "aniqlayman. Qaysi velosiped va qachon "
                    "topshirganingizni yozing — tezroq topaman.",
    "a_BUYOUT": "🚲 Velosipedni sotib olish mumkin: 1 AKB bilan — "
                "35 000 ₽, 2 AKB bilan — 45 000 ₽; yoki banksiz va "
                "boshlangʻich toʻlovsiz boʻlib toʻlash (2 AKB): 2 oy — "
                "haftasiga 6 250 ₽ (jami 50 000 ₽) yoki 4 oy — haftasiga "
                "3 500 ₽ (jami 55 000 ₽). Ijara kabi toʻlaysiz, oxirgi "
                "toʻlovdan soʻng velosiped sizniki. Menejerga yetkazaman.",
},

"tk": {
    "pick": "Dil saýlaň:",
    "menu": "Tema saýlaň — derrew jogap bererin.",
    "contact": "Bize ýazyň: " + CONTACT,
    "change": "🌐 Dili üýtgetmek",
    "after_hours": "Häzir nokatlar ýapyk — sagat 10:00-dan geliň, her gün "
                   "19:00-a çenli işleýäris.",
    "handoff": "Jikme-jiklikleri bir hatda ýazyň — menejere gowşuraryn.\n"
               "Pikiriňizi üýtgeden bolsaňyz — «Отмена» basyň.",
    "your_rate": "Şertnama boýunça tarifiňiz: {plan}.",
    "week": "hepde", "weeks2": "2 hepde", "month": "aý",
    "price_head": "💰 Kärende tarifleri (2 AKB we zarýad beriji bilen):",
    "price_ext": "Ylalaşylan möhletden soň uzaltmak — günde 650 ₽ "
                 "(şertnama boýunça).",
    "price_incl": "Tarife girýär: ileri tutulýan abatlaýyş, döwlen güni "
                  "çalşyk welosiped, GPS gorag we tehniki hyzmat biziň "
                  "hasabymyzdan. Girew ýok.",
    "price_next": "Kärende üçin bize ýazyň: " + CONTACT,
    "t_ADDR": "Salgylar we ýol",
    "t_HOURS": "Iş wagty",
    "t_PRICE": "Kärende tarifleri",
    "t_PAY": "Töleg we çek",
    "t_BATT_Q": "AKB: aralyk",
    "t_BATT_SWAP": "AKB çalyşmak",
    "t_BATT_3": "Üçünji AKB",
    "t_RETURN": "Welosipedi tabşyrmak",
    "t_RENEW": "Kärendäni uzaltmak",
    "t_LEAD": "Kärendä almak isleýärin",
    "t_DOCS": "Resminamalar we girew",
    "t_BRK_MECH": "Welosiped döwüldi",
    "t_PICKUP": "Alyp gitmek hyzmaty",
    "t_EXT_REP": "Öz tehnikaňyzy abatlamak",
    "t_REP_STATUS": "Abatlaýyş ýagdaýy",
    "t_BUYOUT": "Satyn almak",
    "a_ADDR": "Kazanda iki nokadymyz bar:\n"
              "📍 Adoratskogo 11A (Адоратского, 11А)\n"
              "📍 Pavlyukhina 97A — «Sokol» GSK, 9-njy boks "
              "(Павлюхина, 97А)\n"
              "Ikisi hem her gün 10:00–19:00 işleýär. Pavlyukhinada "
              "«Sokol» GSK-a girip 9-njy boksy gözläň — tapmasaňyz, "
              "ýazyň, garşylarys.",
    "a_HOURS": "Her gün 10:00–19:00 işleýäris, dynç günsüz — iki nokat "
               "hem:\n📍 Адоратского, 11А\n📍 Павлюхина, 97А (ГСК «Сокол»)"
               "\nIslendik gün geliň.",
    "a_PAY": "Töleg aşakdaky salgy arkaly — bu biziň ýeke-täk hasabymyz 👇"
             "\n{pay_url}\nTölegden soň çeki şu çata iberiň.",
    "a_BATT_Q": "Sarp ediş tizlige bagly: 3-nji tizlikde batareýa birnäçe "
                "sagatda gutarýar — bu adaty ýagdaý. 1–2-nji tizlikde iki "
                "AKB takmynan 50 km ýetýär. Batareýanyň özünde mesele bar "
                "diýip pikir etseňiz — islendik nokada getiriň, barlarys, "
                "gerek bolsa mugt çalşarys.",
    "a_BATT_SWAP": "AKB çalyşmagy iki nokatda hem her gün 10:00–19:00 "
                   "edýäris. Haýsy nokada barjagyňyzy ýazyň — zarýadly "
                   "AKB barmy-ýokmy, administratordan anyklaryn.",
    "a_BATT_3": "Üçünji AKB tarife goşmaça kärendä berilýär — bahasyny "
                "menejerden anyklaryn. Näçe möhlete gerekdigini ýazyň.",
    "a_RETURN": "Welosipedi islendik gün 10:00–19:00 islendik nokatda "
                "kabul edýäris — haçan geljekdigiňizi öňünden ýazyň. Pul "
                "barada: welosiped tölenen möhletiň dowamynda siziň "
                "üçin bellenen, şonuň üçin ulanylmadyk günler üçin "
                "gaýtadan hasaplamaýarys we olary indiki döwre "
                "geçirmeýäris.",
    "a_RENEW": "Uzaltmagy onlaýn etse bolýar, gelmek hökman däl — salgy "
               "arkaly töläp, çeki iberiň:\n{pay_url}\nUzaltmak uzaltma "
               "gününde bir töleg bilen tölenýär.",
    "a_LEAD": "Hawa, kurýerler üçin elektrowelosipedler bar — Truck+ we "
              "Kugoo V3 Pro, hemmesi 2 AKB, zarýad beriji we telefon "
              "saklaýjy bilen. Girew ýok, resmileşdirme pasport boýunça. "
              "Näçe möhlete we haýsy nokat amatly — Adoratskogo 11A ýa-da "
              "Pavlyukhina 97A?",
    "a_DOCS": "Girew ýok 🤝 Resminamalar:\n— Russiýa raýatlary — pasport;"
              "\n— daşary ýurt raýatlary — milli pasport, wagtlaýyn "
              "hasaba alyş we migrasiýa kartasy.\nŞertnama arzaňyz "
              "barlanandan soň şu botda gol çekilýär.",
    "a_BRK_MECH": "Düşnükli, çözeris. Welosipediň özüni nähili alyp "
                  "barýandygy barada gysga wideo ýazyň (näme işlemeýär, "
                  "displeýde näme bar) we haýsy nokat ýakyndygyny "
                  "aýdyň: Adoratskogo 11A ýa-da Pavlyukhina 97A. "
                  "Kärendeçiler üçin abatlaýyş ileri tutulýar; iş bir "
                  "günden uzaga çekse, çalşyk welosiped bereris.",
    "a_PICKUP": "Göçme ussamyz ýok — abatlaýyş nokatlarda edilýär. "
                "Welosiped ýöremeýän bolsa, ylalaşyk boýunça maşyn bilen "
                "alyp gidip bileris, hyzmat tölegli. Salgyňyzy ýazyň — "
                "menejere gowşuraryn, wagty we bahany ylalaşarys.",
    "a_EXT_REP": "Hawa, diňe özümiziňkini däl: elektrowelosiped, "
                 "elektrosamokat, trisikl, elektromotosikl we AKB-lary "
                 "abatlaýarys. Islendik nokada getiriň, her gün "
                 "10:00–19:00. Ussa diagnostika geçirip, işe başlamazdan "
                 "öň takyk bahany aýdar — razylygyňyzsyz hiç zat "
                 "edilmeýär. Meseläni gysgaça ýazyň we surat goşuň.",
    "a_REP_STATUS": "Welosipediňiziň haýsy tapgyrdadygyny ussadan "
                    "anyklaryn. Haýsy welosipeddigini we haçan "
                    "tabşyrandygyňyzy ýazyň — çalt taparyn.",
    "a_BUYOUT": "🚲 Welosipedi satyn alyp bolýar: 1 AKB bilen — 35 000 ₽, "
                "2 AKB bilen — 45 000 ₽; ýa-da banksyz we ilkinji "
                "tölegsiz bölekleýin (2 AKB): 2 aý — hepdede 6 250 ₽ "
                "(jemi 50 000 ₽) ýa-da 4 aý — hepdede 3 500 ₽ (jemi "
                "55 000 ₽). Kärende ýaly töleýärsiňiz, soňky tölegden "
                "soň welosiped siziňki. Menejere gowşuraryn.",
},

"ar": {
    "pick": "اختر اللغة:",
    "menu": "اختر الموضوع — سأجيب فورًا.",
    "contact": "راسلنا: " + CONTACT,
    "change": "🌐 تغيير اللغة",
    "after_hours": "النقاط مغلقة الآن — تعال من الساعة 10:00، نعمل يوميًا "
                   "حتى 19:00.",
    "handoff": "اكتب التفاصيل في رسالة واحدة — سأنقلها إلى المدير.\n"
               "إذا غيّرت رأيك اضغط «Отмена».",
    "your_rate": "تعريفتك حسب العقد: {plan}.",
    "week": "أسبوع", "weeks2": "أسبوعان", "month": "شهر",
    "price_head": "💰 أسعار الإيجار (تشمل بطاريتين وشاحنًا):",
    "price_ext": "التمديد بعد المدة المتفق عليها — 650 روبل يوميًا "
                 "(حسب العقد).",
    "price_incl": "السعر يشمل: صيانة بأولوية، دراجة بديلة خلال يوم عند "
                  "العطل، حماية GPS والصيانة على حسابنا. لا يوجد تأمين.",
    "price_next": "للإيجار راسلنا: " + CONTACT,
    "t_ADDR": "العناوين وكيفية الوصول",
    "t_HOURS": "مواعيد العمل",
    "t_PRICE": "أسعار الإيجار",
    "t_PAY": "الدفع والإيصال",
    "t_BATT_Q": "البطارية: المدى",
    "t_BATT_SWAP": "تبديل البطاريات",
    "t_BATT_3": "بطارية ثالثة",
    "t_RETURN": "إرجاع الدراجة",
    "t_RENEW": "تمديد الإيجار",
    "t_LEAD": "أريد استئجار دراجة",
    "t_DOCS": "المستندات والتأمين",
    "t_BRK_MECH": "عطل في الدراجة",
    "t_PICKUP": "خدمة نقل الدراجة",
    "t_EXT_REP": "إصلاح مركبتك الخاصة",
    "t_REP_STATUS": "حالة الإصلاح",
    "t_BUYOUT": "شراء الدراجة",
    "a_ADDR": "لدينا نقطتان في قازان:\n"
              "📍 Adoratskogo 11A (Адоратского, 11А)\n"
              "📍 Pavlyukhina 97A — جراجات «Sokol»، بوكس 9 "
              "(Павлюхина, 97А)\n"
              "النقطتان تعملان يوميًا 10:00–19:00. في Pavlyukhina ادخل "
              "إلى «Sokol» وابحث عن البوكس 9 — إن لم تجده راسلنا "
              "وسنستقبلك.",
    "a_HOURS": "نعمل يوميًا 10:00–19:00 بدون عطلات — النقطتان:\n"
               "📍 Адоратского, 11А\n📍 Павлюхина, 97А (ГСК «Сокол»)\n"
               "تعال في أي يوم.",
    "a_PAY": "الدفع عبر الرابط أدناه — هذا حسابنا البنكي الوحيد 👇\n"
             "{pay_url}\nبعد الدفع أرسل الإيصال هنا في المحادثة.",
    "a_BATT_Q": "الاستهلاك يعتمد على السرعة: على السرعة الثالثة تفرغ "
                "البطارية خلال ساعتين تقريبًا — هذا طبيعي. على السرعة "
                "1–2 تكفي البطاريتان لحوالي 50 كم. إن كنت تظن أن العيب "
                "في البطاريات نفسها — أحضر الدراجة إلى أي نقطة، سنفحصها "
                "تحت الحمل ونبدلها مجانًا إذا لزم.",
    "a_BATT_SWAP": "نبدل البطاريات في النقطتين يوميًا 10:00–19:00. اكتب "
                   "إلى أي نقطة ستذهب — سأتأكد من المشرف إن كانت هناك "
                   "بطاريات مشحونة.",
    "a_BATT_3": "البطارية الثالثة تؤجَّر إضافةً إلى التعريفة — سأستوضح "
                "السعر من المدير. اكتب المدة التي تحتاجها.",
    "a_RETURN": "نستلم الدراجة في أي يوم 10:00–19:00 في أي نقطة — فقط "
                "أخبرنا مسبقًا متى ستأتي. بخصوص المال: الدراجة محجوزة "
                "لك طوال المدة المدفوعة، لذلك لا نعيد حساب الأيام غير "
                "المستخدمة ولا ننقلها إلى فترة قادمة.",
    "a_RENEW": "يمكن التمديد أونلاين دون الحضور — ادفع عبر الرابط وأرسل "
               "الإيصال:\n{pay_url}\nيُدفع التمديد يوم التمديد دفعة "
               "واحدة.",
    "a_LEAD": "نعم، لدينا دراجات كهربائية للمندوبين — Truck+ و"
              "Kugoo V3 Pro، كلها ببطاريتين وشاحن وحامل هاتف. لا يوجد "
              "تأمين، والتسجيل بجواز السفر. ما المدة التي تخطط لها وأي "
              "نقطة أنسب — Adoratskogo 11A أم Pavlyukhina 97A؟",
    "a_DOCS": "لا يوجد تأمين 🤝 المستندات:\n— مواطنو روسيا: جواز السفر؛\n"
              "— الأجانب: جواز السفر الوطني والتسجيل المؤقت وبطاقة "
              "الهجرة.\nيُوقَّع العقد هنا في البوت بعد التحقق من طلبك.",
    "a_BRK_MECH": "فهمت، سنحل الأمر. صوِّر فيديو قصيرًا يوضح حالة "
                  "الدراجة (ما الذي لا يعمل وما يظهر على الشاشة) وأخبرنا "
                  "أي نقطة أقرب إليك: Adoratskogo 11A أم Pavlyukhina "
                  "97A. الإصلاح للمستأجرين له أولوية؛ وإذا استغرق أكثر "
                  "من يوم نعطيك دراجة بديلة.",
    "a_PICKUP": "ليس لدينا فني متنقل — الإصلاح في النقاط. إذا كانت "
                "الدراجة لا تتحرك يمكننا نقلها بالسيارة بالاتفاق، "
                "والخدمة مدفوعة. أرسل عنوانك — سأنقله إلى المدير "
                "للاتفاق على الوقت والسعر.",
    "a_EXT_REP": "نعم، نصلح ليس فقط دراجاتنا: الدراجات الكهربائية "
                 "والسكوترات والدراجات ثلاثية العجلات والدراجات النارية "
                 "الكهربائية والبطاريات. أحضرها إلى أي نقطة يوميًا "
                 "10:00–19:00. سيفحصها الفني ويحدد السعر الدقيق قبل بدء "
                 "العمل — لا شيء يُنفَّذ دون موافقتك. صف المشكلة باختصار "
                 "وأرفق صورة.",
    "a_REP_STATUS": "سأستوضح من الفني في أي مرحلة دراجتك. اكتب أي دراجة "
                    "ومتى سلمتها — سأجدها أسرع.",
    "a_BUYOUT": "🚲 يمكنك شراء الدراجة: ببطارية واحدة — 35 000 روبل، "
                "ببطاريتين — 45 000 روبل؛ أو بالتقسيط دون بنك ودون دفعة "
                "أولى (بطاريتان): شهران بـ 6 250 روبل أسبوعيًا (الإجمالي "
                "50 000) أو 4 أشهر بـ 3 500 روبل أسبوعيًا (الإجمالي "
                "55 000). تدفع كما تدفع الإيجار، وبعد آخر دفعة تصبح "
                "الدراجة ملكك. سأنقل الطلب إلى المدير.",
},

"fa": {
    "pick": "زبان را انتخاب کنید:",
    "menu": "موضوع را انتخاب کنید — بلافاصله پاسخ می‌دهم.",
    "contact": "به ما پیام دهید: " + CONTACT,
    "change": "🌐 تغییر زبان",
    "after_hours": "الان شعبه‌ها بسته‌اند — از ساعت 10:00 بیایید؛ هر روز "
                   "تا 19:00 باز هستیم.",
    "handoff": "جزئیات را در یک پیام بنویسید — به مدیر منتقل می‌کنم.\n"
               "اگر منصرف شدید دکمه «Отмена» را بزنید.",
    "your_rate": "تعرفه شما طبق قرارداد: {plan}.",
    "week": "هفته", "weeks2": "دو هفته", "month": "ماه",
    "price_head": "💰 تعرفه‌های اجاره (با دو باتری و شارژر):",
    "price_ext": "تمدید پس از مدت توافق‌شده — روزی 650 روبل (طبق قرارداد).",
    "price_incl": "در تعرفه هست: تعمیر با اولویت، دوچرخه جایگزین ظرف یک "
                  "روز در صورت خرابی، حفاظت GPS و سرویس به حساب ما. "
                  "ودیعه ندارد.",
    "price_next": "برای اجاره به ما پیام دهید: " + CONTACT,
    "t_ADDR": "آدرس‌ها و مسیر",
    "t_HOURS": "ساعت کاری",
    "t_PRICE": "تعرفه‌های اجاره",
    "t_PAY": "پرداخت و رسید",
    "t_BATT_Q": "باتری: برد",
    "t_BATT_SWAP": "تعویض باتری",
    "t_BATT_3": "باتری سوم",
    "t_RETURN": "تحویل دوچرخه",
    "t_RENEW": "تمدید اجاره",
    "t_LEAD": "می‌خواهم اجاره کنم",
    "t_DOCS": "مدارک و ودیعه",
    "t_BRK_MECH": "خرابی دوچرخه",
    "t_PICKUP": "حمل دوچرخه",
    "t_EXT_REP": "تعمیر وسیله خودتان",
    "t_REP_STATUS": "وضعیت تعمیر",
    "t_BUYOUT": "خرید دوچرخه",
    "a_ADDR": "دو شعبه در کازان داریم:\n"
              "📍 Adoratskogo 11A (Адоратского, 11А)\n"
              "📍 Pavlyukhina 97A — گاراژهای «Sokol»، باکس 9 "
              "(Павлюхина, 97А)\n"
              "هر دو هر روز 10:00–19:00 باز هستند. در Pavlyukhina وارد "
              "«Sokol» شوید و باکس 9 را پیدا کنید — اگر پیدا نکردید "
              "پیام دهید، به استقبالتان می‌آییم.",
    "a_HOURS": "هر روز 10:00–19:00 باز هستیم، بدون تعطیلی — هر دو شعبه:\n"
               "📍 Адоратского, 11А\n📍 Павлюхина, 97А (ГСК «Сокол»)\n"
               "هر روز که خواستید بیایید.",
    "a_PAY": "پرداخت از طریق لینک زیر — این تنها حساب بانکی ماست 👇\n"
             "{pay_url}\nپس از پرداخت، رسید را همین‌جا در چت بفرستید.",
    "a_BATT_Q": "مصرف به سرعت بستگی دارد: در دنده ۳ باتری در حدود دو "
                "ساعت تمام می‌شود — طبیعی است. در دنده ۱–۲ دو باتری "
                "حدود 50 کیلومتر می‌روند. اگر فکر می‌کنید مشکل از خود "
                "باتری‌هاست — دوچرخه را به هر شعبه بیاورید؛ زیر بار "
                "تست می‌کنیم و در صورت نیاز رایگان عوض می‌کنیم.",
    "a_BATT_SWAP": "تعویض باتری در هر دو شعبه هر روز 10:00–19:00 انجام "
                   "می‌شود. بنویسید به کدام شعبه می‌روید — از مسئول "
                   "شعبه می‌پرسم باتری شارژشده موجود است یا نه.",
    "a_BATT_3": "باتری سوم جدا از تعرفه اجاره داده می‌شود — قیمتش را از "
                "مدیر می‌پرسم. بنویسید برای چه مدتی لازم دارید.",
    "a_RETURN": "دوچرخه را هر روز 10:00–19:00 در هر شعبه تحویل می‌گیریم "
                "— فقط از قبل بگویید کی می‌آیید. درباره پول: دوچرخه در "
                "تمام مدت پرداخت‌شده برای شماست، بنابراین روزهای "
                "استفاده‌نشده را محاسبه مجدد نمی‌کنیم و به دوره بعد "
                "منتقل نمی‌کنیم.",
    "a_RENEW": "تمدید آنلاین ممکن است، نیازی به آمدن نیست — از طریق لینک "
               "پرداخت کنید و رسید را بفرستید:\n{pay_url}\nتمدید در روز "
               "تمدید و یکجا پرداخت می‌شود.",
    "a_LEAD": "بله، دوچرخه برقی برای پیک‌ها داریم — Truck+ و "
              "Kugoo V3 Pro، همه با دو باتری، شارژر و نگهدارنده گوشی. "
              "ودیعه ندارد، ثبت با پاسپورت. برای چه مدتی می‌خواهید و "
              "کدام شعبه نزدیک‌تر است — Adoratskogo 11A یا Pavlyukhina "
              "97A؟",
    "a_DOCS": "ودیعه ندارد 🤝 مدارک:\n— شهروندان روسیه: پاسپورت؛\n— "
              "اتباع خارجی: پاسپورت ملی، ثبت موقت و کارت مهاجرت.\n"
              "قرارداد پس از بررسی درخواست همین‌جا در بات امضا می‌شود.",
    "a_BRK_MECH": "متوجه شدم، حلش می‌کنیم. یک ویدیوی کوتاه بگیرید که "
                  "دوچرخه چطور رفتار می‌کند (چه چیزی کار نمی‌کند، روی "
                  "نمایشگر چیست) و بگویید کدام شعبه نزدیک‌تر است: "
                  "Adoratskogo 11A یا Pavlyukhina 97A. تعمیر برای "
                  "مستأجران اولویت دارد؛ اگر بیش از یک روز طول بکشد، "
                  "دوچرخه جایگزین می‌دهیم.",
    "a_PICKUP": "تعمیرکار سیار نداریم — تعمیر در شعبه‌ها انجام می‌شود. "
                "اگر دوچرخه حرکت نمی‌کند، با هماهنگی می‌توانیم با ماشین "
                "ببریم؛ خدمت پولی است. آدرس را بفرستید — به مدیر منتقل "
                "می‌کنم تا زمان و قیمت را هماهنگ کنیم.",
    "a_EXT_REP": "بله، فقط مال خودمان را تعمیر نمی‌کنیم: دوچرخه برقی، "
                 "اسکوتر برقی، سه‌چرخه، موتور برقی و باتری. به هر شعبه "
                 "بیاورید، هر روز 10:00–19:00. تعمیرکار عیب‌یابی می‌کند "
                 "و قیمت دقیق را قبل از شروع کار می‌گوید — بدون رضایت "
                 "شما کاری انجام نمی‌شود. مشکل را کوتاه بنویسید و عکس "
                 "پیوست کنید.",
    "a_REP_STATUS": "از تعمیرکار می‌پرسم دوچرخه شما در چه مرحله‌ای است. "
                    "بنویسید کدام دوچرخه و کی تحویل دادید — سریع‌تر "
                    "پیدا می‌کنم.",
    "a_BUYOUT": "🚲 می‌توانید دوچرخه را بخرید: با یک باتری — 35 000 "
                "روبل، با دو باتری — 45 000 روبل؛ یا قسطی بدون بانک و "
                "بدون پیش‌پرداخت (دو باتری): دو ماه هفته‌ای 6 250 روبل "
                "(جمعاً 50 000) یا چهار ماه هفته‌ای 3 500 روبل (جمعاً "
                "55 000). مثل اجاره پرداخت می‌کنید؛ بعد از آخرین قسط "
                "دوچرخه مال شماست. به مدیر منتقل می‌کنم.",
},

"hi": {
    "pick": "भाषा चुनें:",
    "menu": "विषय चुनें — तुरंत जवाब दूँगा।",
    "contact": "हमें लिखें: " + CONTACT,
    "change": "🌐 भाषा बदलें",
    "after_hours": "अभी पॉइंट बंद हैं — सुबह 10:00 से आइए; हम रोज़ 19:00 "
                   "बजे तक खुले हैं।",
    "handoff": "विवरण एक संदेश में लिखें — मैनेजर तक पहुँचा दूँगा।\n"
               "इरादा बदल गया हो तो «Отмена» दबाएँ।",
    "your_rate": "आपके अनुबंध का टैरिफ: {plan}.",
    "week": "सप्ताह", "weeks2": "2 सप्ताह", "month": "महीना",
    "price_head": "💰 किराये के टैरिफ (2 बैटरी और चार्जर शामिल):",
    "price_ext": "तय अवधि के बाद विस्तार — 650 ₽ प्रतिदिन (अनुबंध के "
                 "अनुसार)।",
    "price_incl": "टैरिफ में शामिल: प्राथमिकता से मरम्मत, खराबी पर एक "
                  "दिन में बदली साइकिल, GPS सुरक्षा और रख-रखाव हमारे "
                  "खर्च पर। कोई जमानत नहीं।",
    "price_next": "किराये के लिए हमें लिखें: " + CONTACT,
    "t_ADDR": "पते और रास्ता",
    "t_HOURS": "काम के घंटे",
    "t_PRICE": "किराये के टैरिफ",
    "t_PAY": "भुगतान और रसीद",
    "t_BATT_Q": "बैटरी: रेंज",
    "t_BATT_SWAP": "बैटरी बदलना",
    "t_BATT_3": "तीसरी बैटरी",
    "t_RETURN": "साइकिल लौटाना",
    "t_RENEW": "किराया बढ़ाना",
    "t_LEAD": "किराये पर लेना है",
    "t_DOCS": "दस्तावेज़ और जमानत",
    "t_BRK_MECH": "साइकिल खराब है",
    "t_PICKUP": "साइकिल ले जाना",
    "t_EXT_REP": "अपने वाहन की मरम्मत",
    "t_REP_STATUS": "मरम्मत की स्थिति",
    "t_BUYOUT": "साइकिल खरीदना",
    "a_ADDR": "क़ज़ान में हमारे दो पॉइंट हैं:\n"
              "📍 Adoratskogo 11A (Адоратского, 11А)\n"
              "📍 Pavlyukhina 97A — «Sokol» गैराज, बॉक्स 9 "
              "(Павлюхина, 97А)\n"
              "दोनों रोज़ 10:00–19:00 खुले हैं। Pavlyukhina पर «Sokol» "
              "में घुसकर बॉक्स 9 खोजें — न मिले तो लिखें, हम मिलने "
              "आएँगे।",
    "a_HOURS": "हम रोज़ 10:00–19:00 खुले हैं, कोई छुट्टी नहीं — दोनों "
               "पॉइंट:\n📍 Адоратского, 11А\n📍 Павлюхина, 97А "
               "(ГСК «Сокол»)\nकिसी भी दिन आइए।",
    "a_PAY": "भुगतान नीचे दिए लिंक से — यही हमारा एकमात्र बैंक खाता है 👇"
             "\n{pay_url}\nभुगतान के बाद रसीद यहीं चैट में भेजें।",
    "a_BATT_Q": "खपत रफ़्तार पर निर्भर है: तीसरी स्पीड पर बैटरी कुछ घंटों "
                "में खत्म होती है — यह सामान्य है। स्पीड 1–2 पर दो "
                "बैटरियाँ करीब 50 किमी चलती हैं। अगर लगे कि खराबी बैटरी "
                "में ही है — किसी भी पॉइंट पर लाइए, लोड पर जाँचेंगे और "
                "ज़रूरत हो तो मुफ़्त बदल देंगे।",
    "a_BATT_SWAP": "बैटरी बदलना दोनों पॉइंट पर रोज़ 10:00–19:00 होता है। "
                   "लिखें किस पॉइंट पर जाएँगे — एडमिन से पता करूँगा कि "
                   "चार्ज बैटरियाँ हैं या नहीं।",
    "a_BATT_3": "तीसरी बैटरी टैरिफ के अलावा किराये पर मिलती है — दाम "
                "मैनेजर से पता करूँगा। लिखें कितने समय के लिए चाहिए।",
    "a_RETURN": "साइकिल किसी भी दिन 10:00–19:00 किसी भी पॉइंट पर वापस ले "
                "लेते हैं — बस पहले बता दें कब आएँगे। पैसे के बारे में: "
                "साइकिल पूरी चुकाई गई अवधि के लिए आपकी है, इसलिए बिना "
                "इस्तेमाल के दिनों का पुनर्गणना नहीं करते और उन्हें आगे "
                "नहीं बढ़ाते।",
    "a_RENEW": "विस्तार ऑनलाइन हो सकता है, आने की ज़रूरत नहीं — लिंक से "
               "भुगतान करें और रसीद भेजें:\n{pay_url}\nविस्तार, विस्तार "
               "के दिन एक ही राशि में चुकाया जाता है।",
    "a_LEAD": "हाँ, कूरियर के लिए ई-बाइक हैं — Truck+ और Kugoo V3 Pro, "
              "सभी 2 बैटरी, चार्जर और फ़ोन होल्डर के साथ। कोई जमानत "
              "नहीं, पासपोर्ट से पंजीकरण। कितने समय के लिए चाहिए और कौन "
              "सा पॉइंट सुविधाजनक है — Adoratskogo 11A या Pavlyukhina "
              "97A?",
    "a_DOCS": "कोई जमानत नहीं 🤝 दस्तावेज़:\n— रूस के नागरिक: पासपोर्ट;\n"
              "— विदेशी नागरिक: राष्ट्रीय पासपोर्ट, अस्थायी पंजीकरण और "
              "माइग्रेशन कार्ड।\nआवेदन जाँचने के बाद अनुबंध इसी बोट में "
              "साइन होता है।",
    "a_BRK_MECH": "समझ गया, हल करेंगे। एक छोटा वीडियो बनाइए कि साइकिल "
                  "कैसा बर्ताव कर रही है (क्या काम नहीं करता, डिस्प्ले "
                  "पर क्या है) और बताइए कौन सा पॉइंट नज़दीक है: "
                  "Adoratskogo 11A या Pavlyukhina 97A। किरायेदारों की "
                  "मरम्मत प्राथमिकता से होती है; एक दिन से ज़्यादा लगे "
                  "तो बदली साइकिल देंगे।",
    "a_PICKUP": "हमारे पास मोबाइल मैकेनिक नहीं है — मरम्मत पॉइंट पर होती "
                "है। साइकिल चल न रही हो तो तय करके गाड़ी से ले जा सकते "
                "हैं, सेवा सशुल्क है। अपना पता भेजें — मैनेजर तक "
                "पहुँचाऊँगा, समय और दाम तय करेंगे।",
    "a_EXT_REP": "हाँ, सिर्फ़ अपनी नहीं: ई-बाइक, ई-स्कूटर, ट्राइक, "
                 "ई-मोटरसाइकिल और बैटरियाँ भी सुधारते हैं। किसी भी "
                 "पॉइंट पर लाइए, रोज़ 10:00–19:00। मिस्त्री जाँच करके "
                 "काम शुरू करने से पहले सटीक दाम बताएगा — आपकी सहमति के "
                 "बिना कुछ नहीं होता। समस्या संक्षेप में लिखें और फ़ोटो "
                 "जोड़ें।",
    "a_REP_STATUS": "मिस्त्री से पता करूँगा कि आपकी साइकिल किस चरण में "
                    "है। लिखें कौन सी साइकिल है और कब दी थी — जल्दी "
                    "ढूँढ लूँगा।",
    "a_BUYOUT": "🚲 साइकिल खरीदी जा सकती है: 1 बैटरी के साथ — 35 000 ₽, "
                "2 के साथ — 45 000 ₽; या बिना बैंक और बिना डाउन पेमेंट "
                "किस्तों में (2 बैटरी): 2 महीने 6 250 ₽/सप्ताह (कुल "
                "50 000 ₽) या 4 महीने 3 500 ₽/सप्ताह (कुल 55 000 ₽)। "
                "किराये की तरह चुकाएँ; आख़िरी किस्त के बाद साइकिल आपकी। "
                "मैनेजर तक पहुँचा दूँगा।",
},

"cv": {
    "pick": "Чӗлхе суйлӑр:",
    "menu": "Ыйту суйлӑр — тӳрех хуравлатӑп.",
    "contact": "Пире ҫырӑр: " + CONTACT,
    "change": "🌐 Чӗлхене улӑштарас",
    "after_hours": "Халӗ пунктсем хупӑ — 10:00 сехетрен килӗр; эпир кашни "
                   "кун 19:00 ҫитиччен ӗҫлетпӗр.",
    "handoff": "Тӗплӗнрех пӗр хыпарпа ҫырӑр — менеджера паратӑп.\n"
               "Шухӑша улӑштартӑр пулсан — «Отмена» пусӑр.",
    "your_rate": "Сирӗн тариф (договор тӑрӑх): {plan}.",
    "week": "эрне", "weeks2": "2 эрне", "month": "уйӑх",
    "price_head": "💰 Тара илмелли хаксем (2 АКБ тата зарядка кӗрет):",
    "price_ext": "Калаҫса татӑлнӑ срок хыҫҫӑн тӑсни — кунне 650 ₽ "
                 "(договор тӑрӑх).",
    "price_incl": "Тарифа кӗрет: васкавлӑ юсав, пӑсӑлсан пӗр кун хушшинче "
                  "улӑштармалли велосипед, GPS хӳтӗлевӗ, техобслуживани "
                  "пирӗн шутран. Залог ҫук.",
    "price_next": "Тара илме пире ҫырӑр: " + CONTACT,
    "t_ADDR": "Адрессем, мӗнле ҫитмелли",
    "t_HOURS": "Ӗҫ вӑхӑчӗ",
    "t_PRICE": "Тара илмелли хаксем",
    "t_PAY": "Тӳлев тата чек",
    "t_BATT_Q": "АКБ: мӗн чухлӗ ҫӳрет",
    "t_BATT_SWAP": "АКБ улӑштарни",
    "t_BATT_3": "Виҫҫӗмӗш АКБ",
    "t_RETURN": "Велосипеда тавӑрса пани",
    "t_RENEW": "Арендӑна тӑсни",
    "t_LEAD": "Тара илес тетӗп",
    "t_DOCS": "Документсем, залог",
    "t_BRK_MECH": "Велосипед пӑсӑлнӑ",
    "t_PICKUP": "Велосипеда илсе кайни",
    "t_EXT_REP": "Хӑвӑр техникӑна юсани",
    "t_REP_STATUS": "Юсав мӗнле пырать",
    "t_BUYOUT": "Велосипеда туянни",
    "a_ADDR": "Хусанта пирӗн икӗ пункт пур:\n"
              "📍 Адоратского, 11А\n"
              "📍 Павлюхина, 97А — ГСК «Сокол», 9-мӗш бокс\n"
              "Иккӗшӗ те кашни кун 10:00–19:00 ӗҫлеҫҫӗ. Павлюхинӑра "
              "«Сокол» ГСК-на кӗрсе 9-мӗш бокса шырӑр — тупаймасан "
              "ҫырӑр, кӗтсе илетпӗр.",
    "a_HOURS": "Кашни кун 10:00–19:00 ӗҫлетпӗр, канмалли кунсӑр — икӗ "
               "пункт та:\n📍 Адоратского, 11А\n📍 Павлюхина, 97А "
               "(ГСК «Сокол»)\nКирек хӑш кун та килӗр.",
    "a_PAY": "Тӳлев аялти ссылка урлӑ — ку пирӗн пӗртен-пӗр счёт 👇\n"
             "{pay_url}\nТӳленӗ хыҫҫӑн чека ҫак чата ярӑр.",
    "a_BATT_Q": "Мӗн чухлӗ ҫӳрени хӑвӑртлӑхран килет: 3-мӗш хӑвӑртлӑхра "
                "батарея темиҫе сехетре пӗтет — ку йӗркеллӗ. 1–2-мӗш "
                "хӑвӑртлӑхра икӗ АКБ 50 км яхӑн ҫитет. Батарейӑсем "
                "хӑйсем пӑсӑк тесе шутлатӑр пулсан — кирек хӑш пункта "
                "илсе килӗр, тӗрӗслетпӗр, кирлӗ пулсан тӳлевсӗр "
                "улӑштаратпӑр.",
    "a_BATT_SWAP": "АКБ улӑштарассине икӗ пунктра та кашни кун "
                   "10:00–19:00 тӑватпӑр. Хӑш пункта каяссине ҫырӑр — "
                   "зарядка тунӑ АКБ пуррине администраторран ыйтса "
                   "пӗлетӗп.",
    "a_BATT_3": "Виҫҫӗмӗш АКБ-на тарифсӑр пуҫне тара паратпӑр — хакне "
                "менеджертан ыйтса пӗлетӗп. Мӗн чухлӗ вӑхӑта кирлине "
                "ҫырӑр.",
    "a_RETURN": "Велосипеда кирек хӑш кун 10:00–19:00 кирек хӑш пунктра "
                "йышӑнатпӑр — хӑҫан килессине маларах ҫырӑр. Укҫа "
                "пирки: велосипед тӳленӗ пӗтӗм срок валли сирӗншӗн "
                "ҫирӗплетнӗ, ҫавӑнпа усӑ курман кунсемшӗн укҫа каялла "
                "шутламастпӑр, вӗсене тепӗр тапхӑра куҫармастпӑр.",
    "a_RENEW": "Тӑсма онлайн май пур, килме кирлӗ мар — ссылка урлӑ "
               "тӳлесе чека ярӑр:\n{pay_url}\nТӑснине тӑсас кун пӗр "
               "суммӑпа тӳлеҫҫӗ.",
    "a_LEAD": "Ҫапла, курьерсем валли электровелосипедсем пур — Truck+ "
              "тата Kugoo V3 Pro, пурте 2 АКБ-па, зарядкӑпа тата телефон "
              "тытмаллипе. Залог ҫук, паспортпа ҫырӑнатӑр. Мӗн чухлӗ "
              "вӑхӑта тата хӑш пункт меллӗрех — Адоратского 11А-и, "
              "Павлюхина 97А-и?",
    "a_DOCS": "Залог ҫук 🤝 Документсем:\n— Раҫҫей гражданӗсем — "
              "паспорт;\n— ют ҫӗршыв гражданӗсем — наци паспорчӗ, "
              "вӑхӑтлӑх регистраци тата миграци карточки.\nДоговора "
              "заявкӑна тӗрӗсленӗ хыҫҫӑн ҫак ботрах алӑ пусатӑр.",
    "a_BRK_MECH": "Ӑнлантӑм, йӗркелетпӗр. Велосипед хӑйне мӗнле "
                  "тытнине кӗске видео ӳкерӗр (мӗн ӗҫлемест, дисплей "
                  "ҫинче мӗн курӑнать) тата хӑш пункт ҫывӑхраххине "
                  "ҫырӑр: Адоратского 11А е Павлюхина 97А. Тара "
                  "илекенсемшӗн юсав васкавлӑ; ӗҫ пӗр кунран вӑрӑмрах "
                  "пулсан, улӑштармалли велосипед паратпӑр.",
    "a_PICKUP": "Тухса ҫӳрекен мастер ҫук — юсава пунктсенче тӑватпӑр. "
                "Велосипед каймасть пулсан, калаҫса татӑлса машинӑпа "
                "илсе кайма пултаратпӑр, услуга тӳлевлӗ. Адресӑра ҫырӑр "
                "— менеджера паратӑп, вӑхӑтпа хака калаҫса татӑлатпӑр.",
    "a_EXT_REP": "Ҫапла, хамӑрӑнне ҫеҫ мар юсатпӑр: электровелосипед, "
                 "электросамокат, трицикл, электромотоцикл тата АКБ. "
                 "Кирек хӑш пункта илсе килӗр, кашни кун 10:00–19:00. "
                 "Мастер диагностика тӑвать те ӗҫ пуҫличчен тӗрӗс хакне "
                 "калать — сирӗн килӗшӳсӗр нимӗн те тумастпӑр. Мӗн "
                 "пулнине кӗскен ҫырӑр, фото хушӑр.",
    "a_REP_STATUS": "Сирӗн велосипед мӗнле пусӑмра иккенне мастертан "
                    "ыйтса пӗлетӗп. Хӑш велосипед тата хӑҫан панине "
                    "ҫырӑр — хӑвӑртрах тупатӑп.",
    "a_BUYOUT": "🚲 Велосипеда туянма пулать: 1 АКБ-па — 35 000 ₽, "
                "2 АКБ-па — 45 000 ₽; е банксӑр, малтанхи тӳлевсӗр "
                "рассрочкӑпа (2 АКБ): 2 уйӑх — эрнере 6 250 ₽ (пӗтӗмпе "
                "50 000 ₽) е 4 уйӑх — эрнере 3 500 ₽ (пӗтӗмпе 55 000 ₽). "
                "Аренда пек тӳлетӗр, юлашки тӳлев хыҫҫӑн велосипед "
                "сирӗн. Менеджера паратӑп.",
},

}

# Ключи, обязательные для каждого языка: тест сверяет по этому списку,
# чтобы пропущенный перевод не обнаружился впервые у клиента.
REQUIRED_KEYS: tuple[str, ...] = (
    "pick", "menu", "contact", "change", "after_hours", "handoff",
    "your_rate", "week", "weeks2", "month",
    "price_head", "price_ext", "price_incl", "price_next",
)


def pick_prompt() -> str:
    """Приглашение выбрать язык - на всех языках сразу."""
    lines = ["🌐 Выберите язык:"]
    lines += [T[lang]["pick"] for lang in LANGS if lang != "ru"]
    return "\n".join(lines)
