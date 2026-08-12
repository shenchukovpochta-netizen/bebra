"""TK: переводы клиентского диалога (туркменский, латиница).

Машинный перевод, обязательно нужна вычитка носителем.
"""

from __future__ import annotations

from ..texts import SUPPORT_CONTACT_URL as URL

T: dict[str, str] = {
    "NOT_SUBSCRIBED": (
        "Siz kanalymyza agza bolmadyňyz!\n"
        "Bot bilen işlemek üçin habarlar kanalymyza agza boluň: {channel_url}"
    ),
    "SUB_NOT_FOUND": "Agzalyk tapylmady. Ýene bir gezek synanyşyň.",
    "SUB_CONFIRMED_TOAST": "Agzalyk tassyklandy",
    "STALE_BUTTON": "Bu düwme könelipdir, /start iberiň",
    "WELCOME": "👋 Hoş geldiňiz! Başlamak üçin doly adyňyzy (F.A.A.) ýazyň:",
    "FIO_AS_TEXT": "Doly adyňyzy tekst bilen ýazyň.",
    "FAQ_ENTRY_HINT": (
        "Dolduryp durkaňyz köp berilýän soraglaryň jogaplaryny görüp "
        "bilersiňiz: salgylar, nyrhlar, resminamalar."
    ),

    "POLICY_CAPTION": (
        "📄 <b>Şahsy maglumatlary işlemek syýasaty</b>\n\n"
        "Dowam etmezden öň, HTK Galimzýanow Edgar Aýratowiçiň şahsy "
        "maglumatlary işlemek boýunça Syýasaty bilen tanşyň (ýokardaky "
        "resminama). Onda haýsy maglumatlaryň näme üçin işlenýändigi, nähili "
        "goralýandygy, saklanyş möhletleri, welosipedlerdäki GPS-yzarlaýjylar, "
        "tabşyrmagyň we yzyna gaýtarmagyň foto- we wideo ýazgysy, razylygy "
        "yzyna almagyň tertibi ýazylan.\n\n"
        "Okadyňyzmy — «Syýasat bilen tanyşdym» düwmesine basyň."
    ),
    "POLICY_NO_FILE": (
        "📄 <b>Şahsy maglumatlary işlemek syýasaty</b>\n\n"
        "Dowam etmezden öň HTK Galimzýanow Edgar Aýratowiçiň şahsy "
        "maglumatlary işlemek Syýasaty bilen tanşyň. Tekstini operatordan "
        "sorap bilersiňiz: " + URL + "\n\n"
        "Tanyşdyňyzmy — «Syýasat bilen tanyşdym» düwmesine basyň."
    ),
    "POLICY_PRESS_BUTTON": (
        "Syýasat bilen tanşyň we dowam etmek üçin «Syýasat bilen tanyşdym» "
        "düwmesine basyň."
    ),
    "POLICY_ACK_TOAST": "Tanyşlyk bellige alyndy",

    "CONSENT": (
        "Barlaň: <b>{fio}</b>\n\n"
        "<b>Şahsy maglumatlary işlemäge razylyk</b>\n\n"
        "Kärende şertnamasyny baglaşmak we ýerine ýetirmek üçin HTK "
        "Galimzýanow Edgar Aýratowiç (INN 165921923517) siziň şahsy "
        "maglumatlaryňyzy işleýär: doly adyňyz, doglan senäňiz we ýeriňiz, "
        "pasport maglumatlary, hasaba alnan we ýaşaýan salgylaryňyz, telefon "
        "belgileriňiz we şahsyýeti tassyklaýan resminamanyň suraty; 16–17 "
        "ýaşly kärendeçiler üçin — kanuny wekiliň ýazmaça razylygynyň suraty "
        "hem.\n"
        "Maglumatlar diňe Kärendä beriji tarapyndan işlenýär we kanunda "
        "göz öňünde tutulan ýagdaýlardan başga üçünji taraplara berilmeýär.\n"
        "Suratlar arza boýunça karar kabul edilenden {purge_days} gün soň "
        "pozulýar. Razylygy goldaw gullugyna ýazyp yzyna alyp bolýar.\n"
        "Şahsy maglumatlary işlemek Syýasaty bilen öňki ädimde tanyşdyňyz.\n\n"
        "«Razylyk berýärin» düwmesine basmak bilen ýokarda sanalan "
        "maglumatlary işlemäge razylyk berýärsiňiz."
    ),
    "CONSENT_PRESS_BUTTON": "Dowam etmek üçin «Razylyk berýärin» düwmesine basyň.",
    "CONSENT_GIVEN": "Razylyk bellige alyndy",

    "ASK_CONTACT": "Kontaktyňyzy paýlaşyň.",
    "CONTACT_USE_BUTTON": "Ekranyň aşagyndaky «Kontakty paýlaşmak» düwmesine basyň.",
    "CONTACT_FOREIGN": "Bu başga adamyň kontakty. Aşakdaky düwme bilen öz belgiňizi iberiň.",
    "ANKETA_INTRO": (
        "Sag boluň! Indi kärende şertnamasy üçin birnäçe maglumat gerek. "
        "Bu bir-iki minut alar — pasportda ýazylyşy ýaly, her jogaby aýry "
        "habar bilen iberiň."
    ),
    "ASK_BIRTH": "Doglan sene — GG.AA.ÝÝÝÝ görnüşinde.\nMeselem: 07.03.1990",
    "ASK_BIRTH_PLACE": "Doglan ýeriňiz, pasportdaky ýaly.\nMeselem: gor. Kazan",
    "ASK_PASSPORT": "Pasportyň seriýasy we belgisi — 10 san.\nMeselem: 1234 567890",
    "ASK_PASSPORT_DATE": "Pasportyň berlen senesi — GG.AA.ÝÝÝÝ.\nMeselem: 01.02.2015",
    "ASK_PASSPORT_CODE": "Bölümiň kody — 6 san.\nMeselem: 160-002",
    "ASK_PASSPORT_ISSUER": (
        "Pasporty kimiň berendigi — resminamadaky ýaly, bir setirde doly "
        "ýazyň.\n"
        "Meselem: OUFMS Rossii po Resp. Tatarstan w Wahitowskom r-ne gor. Kazani"
    ),
    "ASK_REG_ADDR": (
        "Hasaba alnan salgy doly: şäher, köçe, jaý, öý.\n"
        "Meselem: g. Kazan, ul. Baumana, d. 1, kw. 2"
    ),
    "ASK_LIVE_ADDR": (
        "Hakykatda ýaşaýan salgyňyz.\n"
        "Hasaba alnan salgy bilen gabat gelse — aşakdaky düwmä basyň."
    ),
    "ASK_PHONE2": (
        "Ikinji telefon belgisi — meselem, ýakyn adamyňyzyňky.\n"
        "Esasy belgi elýeterli bolmasa, siziň bilen habarlaşmak üçin gerek.\n"
        "Meselem: +7 900 123-45-67"
    ),
    "ASK_PHONE3": "We aragatnaşyk üçin üçünji telefon belgisi.",
    "SAME_AS_REG": "Ýaşaýan salgyňyzy hasaba alnan salgy hökmünde ýazdym.",
    "ANKETA_AS_TEXT": "Jogaby bir tekst habary bilen iberiň.",
    "PASSPORT_DATE_BEFORE_BIRTH": (
        "Berlen sene doglan senä gabat gelmeýär: pasport 14 ýaşdan berilýär. "
        "Iki senäni hem barlaň — doglan seneden başlarys."
    ),
    "ASK_DOC": (
        "Soňky ädim galdy: şahsyýeti tassyklaýan resminamanyň suratyny "
        "goşuň. Maglumatlar aýdyň okalar ýaly surata alyň."
    ),
    "DOC_NEED_PHOTO": "Hut resminamanyň suraty gerek. Surat goşuň.",
    "ASK_PARENT_CONSENT": (
        "Siz entek 18 ýaşamadyňyz, şonuň üçin ýene bir resminama gerek: "
        "ata-enäniň (kanuny wekiliň) kärende şertnamasyny baglaşmaga ýazmaça "
        "razylygy.\n\n"
        "Ata-ene razylygy eli bilen ýazýar: öz doly adyny, pasport "
        "maglumatlaryny we siziň doly adyňyzy görkezýär, sene we gol goýýar. "
        "Şol razylygyň suratyny iberiň — teksti aýdyň okalar ýaly bolsun."
    ),
    "PARENT_NEED_PHOTO": "Ata-enäniň ýazmaça razylygynyň suraty gerek. Surat goşuň.",

    "CONFIRM_CAPTION": (
        "Girizilen maglumatlaryň dogrudygyny tassyklaň:\n\n"
        "F.A.A.: <b>{fio}</b>\n"
        "Telefon belgisi: <b>+{phone}</b>"
    ),
    "CONFIRM_PRESS_BUTTON": "«Tassyklaýaryn» ýa-da «Täzeden doldurmak» düwmesine basyň.",
    "RESTART": "Bolýar, täzeden başlarys. Doly adyňyzy ýazyň:",
    "RESTART_TOAST": "Täzeden doldurýarys",
    "SUBMITTED": (
        "Sag boluň! Maglumatlar barlaga iberildi. Adatça bu 30 minuda çenli "
        "wagt alýar — taýýar bolan badyna ýazarys."
    ),
    "SUBMITTED_TOAST": "Barlaga iberildi",
    "SUBMIT_PROBLEM": (
        "Arzany geçirmekde tehniki näsazlyk ýüze çykana meňzeýär. Biz bu "
        "barada bilýäris. Bir sagadyň içinde jogap bolmasa — goldaw "
        "gullugyna ýazyň."
    ),
    "PENDING_WAIT": "Arzaňyz barlagda. Seredilen badyna ýazarys.",
    "APPROVED_WAIT_ISSUE": (
        "Arza makullandy! Şertnamany taýýarlaýarys: welosipediň "
        "maglumatlaryny we enjamlaryny girizýäris. Gol çekmek üçin şu ýere "
        "ibereris."
    ),
    "REGISTERED": "Bellige alyş üstünlikli tamamlandy.\nWideogörkezme: {video_url}",
    "ALREADY_REGISTERED": "Siz eýýäm bellige alyndyňyz. Nähili kömek edeli?",
    "MENU_PROMPT": "Aşakdaky menýudan hereketi saýlaň.",
    "REJECTED_WITH_REASON": (
        "Gynansak-da, arza ret edildi.\n\n"
        "Nämäni düzetmeli: {reason}\n\n"
        "Dowam etmek üçin /start basyň ýa-da gerekli maglumatlary iberiň."
    ),

    "TARIFFS": (
        "💰 <b>Kärendäniň häzirki nyrhlary</b>\n\n"
        "<b>Truck+ 2 akkumulýator bilen</b>\n"
        "• 1 hepde — 3 000 ₽\n"
        "• 2 hepde — 5 400 ₽\n"
        "• 1 aý — 11 000 ₽\n\n"
        "<b>Truck+ yzky amortizatorly, 2 akkumulýator</b>\n"
        "• 1 hepde — 3 400 ₽\n"
        "• 2 hepde — 5 400 ₽\n"
        "• 1 aý — 11 000 ₽\n\n"
        "<b>Kugoo V3 Pro 2 akkumulýator bilen</b>\n"
        "• 1 hepde — 3 500 ₽\n"
        "• 2 hepde — 6 400 ₽\n"
        "• 1 aý — 12 000 ₽\n\n"
        "<b>Kugoo V3 Pro+ 2 akkumulýator bilen</b>\n"
        "• 1 hepde — 3 500 ₽\n"
        "• 2 hepde — 6 400 ₽\n"
        "• 1 aý — 12 000 ₽\n\n"
        "Ylalaşylan möhletden soň uzaltmak — günde 650 ₽ (şertnama "
        "boýunça).\n"
        "\nNyrha girýär: nobatsyz abatlaýyş, döwlen ýagdaýynda bir günüň "
        "içinde çalşyk welosipedi, GPS-gorag we tehniki hyzmat biziň "
        "hasabymyzdan. <b>Girew ýok.</b>\n"
        "Kärendä almak üçin «🆘 Goldaw» düwmesine basyň ýa-da göni ýazyň: "
        + URL
    ),

    "FAQ_GUEST_CONTACT": "Bize ýazyň: " + URL,
    "FAQ_HANDOFF": (
        "Jikme-jiklikleri bir habarda ýazyň — menejere ýetirerin.\n"
        "Pikiriňizi üýtgetdiňizmi — «Ýatyrmak» düwmesine basyň."
    ),
    "FAQ_TOPIC_GONE": "Bu tema indi elýeterli däl, «Köp berilýän soraglary» täzeden açyň.",
    "SUPPORT_SENT_ANSWERED": (
        "Eger bu gerekli jogap däl bolsa — menejer soragyňyzy görýär we iş "
        "wagtynda jogap berer."
    ),
    "SUPPORT_PROMPT": (
        "Soragyňyzy bir habarda ýazyň — şu çatda jogap bereris.\n"
        "Ýa-da bize göni ýazyň: " + URL + "\n\n"
        "Pikiriňizi üýtgetdiňizmi — «Ýatyrmak» düwmesine basyň."
    ),
    "SUPPORT_AS_TEXT": "Soragy bir tekst habary bilen ýazyň.",
    "SUPPORT_SENT": (
        "Sorag ýetirildi. Jogap şu çata geler.\n"
        "Gyssagly bolsa — göni ýazyň: " + URL
    ),
    "SUPPORT_FAILED": (
        "Tehniki näsazlyk sebäpli soragy ýetirip bolmady. Bize göni ýazyň: "
        + URL
    ),
    "SUPPORT_CANCELLED": "Bolýar, menýua gaýdyp geldik.",
    "SUPPORT_REPLY_USER": "💬 Goldawyň jogaby:\n\n{answer}",
    "RATE_LIMITED": "Habarlar gaty köp. Bir minut garaşyň.",

    "CONTRACT_READY_USER": (
        "№ {number} kärende şertnamasy taýýar. Ýokarda — oňa goşundy: şahsy "
        "maglumatlary işlemäge Razylyk.\n\n"
        "Iki resminamany hem doly okaň we «Gol çekýärin» düwmesine basyň — "
        "gol şertnama we goşunda degişli. Ýalňyş tapsaňyz — «Ýalňyş bar» "
        "düwmesine basyň, anketany düzetmäge ibereris."
    ),
    "SOGLASIE_CAPTION": (
        "№ {number} şertnama goşundy: şahsy maglumatlary işlemäge Razylyk."
    ),
    "SOGLASIE_SIGNED_CAPTION": (
        "Şahsy maglumatlary işlemäge razylyk (№ {number} şertnama goşundy) "
        "{signed_at} gol çekildi."
    ),
    "CONTRACT_SIGNED_USER": (
        "№ {number} şertnama {signed_at} gol çekildi.\n"
        "Nusgasy şu çatda sizde galýar.\n\n"
        "Wideogörkezme: {video_url}"
    ),
    "CONTRACT_RESEND": (
        "№ {number} şertnama entek gol çekilmedi — ine ol ýene.\n\n"
        "Okaň we ählisi dogry bolsa «Gol çekýärin», ýalňyş tapsaňyz "
        "«Ýalňyş bar» düwmesine basyň."
    ),
    "CONTRACT_SIGN_TOAST": "Şertnama gol çekildi",
    "CONTRACT_PRESS_BUTTON": "Şertnamany okaň we «Gol çekýärin» ýa-da «Ýalňyş bar» düwmesine basyň.",
    "CONTRACT_MISTAKE": (
        "Bolýar, anketany täzeden doldurarys — şeýdip ähli maglumatlar "
        "şertnama dogry düşer."
    ),
    "CONTRACT_FAILED_USER": (
        "Arza makullandy, ýöne tehniki ýalňyş sebäpli şertnama emele "
        "gelmedi. Biz bu barada bilýäris we şertnamany el bilen ibereris."
    ),

    "RENT_ALREADY_ACTIVE": (
        "Sizde eýýäm işjeň kärende bar: <b>{bike}</b>, möhleti {term}.\n"
        "Başga welosiped almak üçin ilki häzirki kärendäni ýapyň — menýudaky "
        "«🔚 Kärendäni ýapmak» düwmesi."
    ),
    "RENT_REQUEST_SENT": (
        "✅ Kärende arzasy operatora ýetirildi. Ol tabşyrmagy tassyklar we "
        "welosipediň maglumatlaryny girizer, soňra töleg möçberini we "
        "tabşyryş Aktyny ibereris.\n"
        "Welosipedleri her gün 10:00-dan 19:00-a çenli berýäris; şertnama "
        "öňküsi hereket edýär."
    ),
    "RENT_REQUEST_FAILED": (
        "Tehniki näsazlyk sebäpli arzany ýetirip bolmady. Bize göni ýazyň: "
        + URL
    ),

    "PAY_PROMPT": (
        "Bir ädim galdy — kärende tölegi.\n\n"
        "Möçberi we usuly: <b>{price}</b>\n\n"
        "Töleg aşakdaky salgylanma arkaly — bu biziň ýeke-täk hasaplaşyk "
        "hasabymyz 👇\n{pay_url}\n\n"
        "Töläniňizden soň «Töledim» düwmesine basyň we çegi şu çata iberiň. "
        "Operator pul gelendigini tassyklan badyna tabşyryş Aktyny "
        "ibereris: welosipedi şol akt boýunça alarsyňyz."
    ),
    "PAY_WAIT": (
        "Kärende tölegine garaşýarys: <b>{price}</b>.\n"
        "Töleg salgylanmasy (ýeke-täk hasaplaşyk hasaby):\n{pay_url}\n\n"
        "Tölediňizmi — «Töledim» düwmesine basyň we çegi iberiň, operator "
        "gelendigini barlar. Tassyklanandan soň tabşyryş Aktyny ibereris."
    ),
    "PAY_NUDGE_TOAST": "Operatora ýetirdik — ol puluň gelendigini barlar",
    "PAY_RECEIPT_SENT": (
        "Çegi operatora ýetirdik — ol puluň gelendigini barlar. Tassyklan "
        "badyna tabşyryş Aktyny ibereris."
    ),
    "PAY_RECEIPT_FAILED": (
        "Tehniki näsazlyk sebäpli çegi ýetirip bolmady. «Töledim» düwmesine "
        "basyň — operator hasaby barlar."
    ),
    "PAY_CONFIRMED_USER": (
        "Töleg alyndy, sag boluň! Indiki habar bilen tabşyryş Akty geler."
    ),

    "ACT_IN_READY": (
        "Töleg tassyklandy! Indi № {number} tabşyryş Akty.\n\n"
        "VIN belgilerini we enjamlary barlaň-da «Gol çekýärin» düwmesine "
        "basyň — şol pursatdan emläk size berlen hasaplanýar."
    ),
    "ACT_IN_SIGNED": (
        "№ {number} şertnama boýunça tabşyryş Akty {signed_at} gol "
        "çekildi.\nNusgasy şu çatda sizde galýar. Ýoluňyz ak bolsun!\n\n"
        "Wideogörkezme: {video_url}"
    ),
    "ACT_RESEND": (
        "№ {number} şertnama boýunça akt entek gol çekilmedi — ine ol "
        "ýene.\n«Gol çekýärin» ýa-da «Ýalňyş bar» düwmesine basyň."
    ),
    "ACT_PRESS_BUTTON": "Akty okaň we «Gol çekýärin» ýa-da «Ýalňyş bar» düwmesine basyň.",
    "ACT_MISTAKE_SENT": (
        "Soragyňyzy operatora ýetirdik — ol siziň bilen habarlaşar we akty "
        "düzeder."
    ),

    "CLOSE_NO_RENTAL": (
        "Sizde işjeň kärende ýok. Eger welosiped sizde bolsa — «🆘 Goldawa» "
        "ýazyň, çözeris."
    ),
    "CLOSE_ASK_REASON": (
        "Bolýar, kärendäni ýapmagy resmileşdireris.\n"
        "Näme üçin tabşyrýandygyňyzy bir setirde ýazyň — bu hasabat üçin "
        "gerek (meselem: «esasy işe çykýaryn»).\n\n"
        "Pikiriňizi üýtgetdiňizmi — «Ýatyrmak» düwmesine basyň."
    ),
    "CLOSE_REQUESTED": (
        "Ýapmak barada haýyş operatora ýetirildi. Ol siziň bilen habarlaşar "
        "we wagty aýdar; welosipedleri her gün 10:00-dan 19:00-a çenli "
        "islendik nokatda kabul edýäris.\n"
        "Gözegçilikden soň tassyklamak üçin gaýtaryş Aktyny ibereris."
    ),
    "CLOSE_REQUEST_FAILED": (
        "Tehniki näsazlyk sebäpli haýyşy ýetirip bolmady. Bize göni ýazyň: "
        + URL
    ),

    "TRIPS_EMPTY": "Entek kärende bolmady. Nyrhlar — menýuda, resmileşdirmek — goldaw arkaly.",
    "TRIPS_HEADER": "📋 <b>Siziň kärendeleriňiz</b>\n",
    "TRIPS_ACTIVE": "• № {number} · {bike} · {term} — <b>häzir kärendede</b>",
    "TRIPS_CLOSED": "• № {number} · {bike} · {term} — {closed_at} ýapyldy",

    "RETURN_READY": (
        "№ {number} şertnama boýunça gaýtaryş Akty taýýar.\n\n"
        "Bellikleri barlaň we «Tassyklaýaryn» düwmesine basyň — kärende "
        "ýapylar."
    ),
    "RETURN_SIGNED": (
        "№ {number} şertnama boýunça gaýtaryş Akty {signed_at} "
        "tassyklandy.\nKärende ýapyldy. Bizi saýlanyňyz üçin sag boluň!"
    ),

    # ── сроки и продление ──
    "BTN_EXTEND": "📅 Kärendäni uzaltmak",
    "REMIND_SOON": "📅 Ýatlatma: <b>{bike}</b> kärendesi {until} gutarýar. Galan günler: {days}.\n\nUzaltmak isleseňiz — aşakdaky düwmä basyň, operator möçberi aýdar. Tabşyrjak bolsaňyz — menýudaky «🔚 Kärendäni ýapmak»; welosipedleri her gün 10:00-dan 19:00-a çenli kabul edýäris.",
    "REMIND_LAST_DAY": "📅 Şu gün <b>{bike}</b> kärendesiniň soňky güni ({until} çenli).\n\nUzaltmak — aşakdaky düwme. Şu gün tabşyrjak bolsaňyz — menýudaky «🔚 Kärendäni ýapmak» düwmesine basyň, wagty ylalaşarys.",
    "REMIND_OVERDUE": "⚠️ <b>{bike}</b> kärendesiniň möhleti {until} gutardy.\n\nHaýyş edýäris, aşakdaky düwme bilen uzaldyň ýa-da welosipedi tabşyryň — ýogsam şertnama boýunça peýdalanmak tölegi hasaplanmagyny dowam edýär.\nOperator bilen eýýäm ylalaşan bolsaňyz — şu habara jogap ýazyň.",
    "EXTEND_NO_RENTAL": "Sizde işjeň kärende ýok — uzaltmaly zat ýok. Welosiped almak üçin: «🚲 Kärendä almak» düwmesi.",
    "EXTEND_ALREADY_ASKED": "Uzaltmak baradaky arzaňyz eýýäm operatorda — ol siziň bilen habarlaşyp möçberi aýdar. Gaýtadan ibermek gerek däl.",
    "EXTEND_REQUESTED": "✅ Uzaltmak arzasy operatora ýetirildi. Ol täze möhleti tassyklar we töleg möçberini iberer — welosiped sizde galýar.",
    "EXTEND_REQUEST_FAILED": "Tehniki näsazlyk sebäpli arzany ýetirip bolmady. Bize göni ýazyň: " + URL,
    "EXTEND_CONFIRMED": "✅ Kärende {until} çenli uzaldyldy. Töleg üçin sag boluň!\nŞertnama we akt öňküligine galýar — täzesine gol çekmek gerek däl.",

    "BTN_RENT": "🚲 Kärendä almak",
    "BTN_TRIPS": "📋 Meniň kärendelerim",
    "BTN_TARIFFS": "💰 Nyrhlar",
    "BTN_SUPPORT": "🆘 Goldaw",
    "BTN_FAQ": "❓ Köp berilýän soraglar",
    "BTN_CLOSE_RENT": "🔚 Kärendäni ýapmak",
    "BTN_CANCEL": "Ýatyrmak",
    "BTN_SAME_ADDRESS": "Hasaba alnan salgy bilen gabat gelýär",
    "BTN_SHARE_CONTACT": "📱 Kontakty paýlaşmak",
    "BTN_SUBSCRIBE": "Kanala agza bolmak",
    "BTN_CHECK_SUB": "Agzalygy barlamak",
    "BTN_POLICY_WEB": "Syýasat (web görnüşi)",
    "BTN_POLICY_ACK": "✔️ Syýasat bilen tanyşdym",
    "BTN_RULES": "Kärende düzgünleri",
    "BTN_PDN": "Şahsy maglumatlar syýasaty",
    "BTN_CONSENT": "✅ Razylyk berýärin",
    "BTN_CONFIRM": "Tassyklaýaryn",
    "BTN_RESTART": "Täzeden doldurmak",
    "BTN_SIGN": "✍️ Gol çekýärin",
    "BTN_MISTAKE": "Ýalňyş bar",
    "BTN_PAY": "💳 Tölemek",
    "BTN_PAID": "✅ Töledim",
    "BTN_RETURN_SIGN": "✍️ Tassyklaýaryn",
}

ERRORS: dict[str, str] = {
    "Год должен быть не раньше 1900.":
        "Ýyl 1900-den ir bolmaly däl.",
    "Прокат доступен с 16 лет (до 18 - с письменного согласия родителя).":
        "Kärende 16 ýaşdan elýeterli (18-e çenli — ata-enäniň ýazmaça razylygy bilen).",
    "Похоже на опечатку. Введите ФИО полностью.":
        "Ýalňyşa meňzeýär. Doly adyňyzy doly ýazyň.",
    "Похоже на опечатку. Введите ФИО полностью, без цифр.":
        "Ýalňyşa meňzeýär. Doly adyňyzy sansyz, doly ýazyň.",
    "В ФИО недопустимы символы < > и &.":
        "Doly atda < > we & belgilerine rugsat ýok.",
    "Дата нужна в виде ДД.ММ.ГГГГ, например 07.03.1990.":
        "Sene GG.AA.ÝÝÝÝ görnüşinde bolmaly, meselem 07.03.1990.",
    "Такой даты не существует. Проверьте число и месяц.":
        "Beýle sene ýok. Güni we aýy barlaň.",
    "Дата не может быть в будущем.":
        "Sene geljekde bolup bilmez.",
    "Похоже на опечатку в годе рождения. Проверьте, пожалуйста.":
        "Doglan ýylda ýalňyşa meňzeýär. Barlaň.",
    "Серия и номер - это 10 цифр, например 1234 567890. Проверьте, сколько получилось.":
        "Seriýa we belgi — 10 san, meselem 1234 567890. Näçe çykandygyny barlaň.",
    "Адрес нужен полностью: город, улица, дом, квартира. Индекс по желанию.":
        "Salgy doly gerek: şäher, köçe, jaý, öý. Indeks islege görä.",
    "Нужно изображение (JPG, PNG или HEIC). Пришлите фото, а не файл другого типа.":
        "Surat gerek (JPG, PNG ýa-da HEIC). Başga görnüşli faýl däl, surat iberiň.",
    "Код подразделения - 6 цифр, например 160-002.":
        "Bölümiň kody — 6 san, meselem 160-002.",
    "Впишите, кем выдан паспорт, как в документе - строкой целиком.":
        "Pasporty kimiň berendigini resminamadaky ýaly, bir setirde doly ýazyň.",
    "Недопустимы символы < > и &.":
        "< > we & belgilerine rugsat ýok.",
    "Укажите место рождения, как в паспорте.":
        "Doglan ýeri pasportdaky ýaly ýazyň.",
    "В адресе недопустимы символы < > и &.":
        "Salgyda < > we & belgilerine rugsat ýok.",
    "В адресе не хватает номера дома.":
        "Salgyda jaý belgisi ýetmeýär.",
    "Не похоже на номер телефона. Пример: +7 900 123-45-67.":
        "Telefon belgisine meňzemeýär. Meselem: +7 900 123-45-67.",
    "Этот номер уже указан. Нужен другой.":
        "Bu belgi eýýäm görkezildi. Başgasy gerek.",
    "Файл слишком большой. Пришлите фото до 12 МБ.":
        "Faýl gaty uly. 12 MB çenli surat iberiň.",
    "Опишите вопрос текстом, хотя бы парой слов.":
        "Soragy tekst bilen ýazyň, iň bolmanda bir-iki söz.",
    "Слишком длинно. Уложите вопрос в 1500 символов.":
        "Gaty uzyn. Soragy 1500 belgä sygdyryň.",
    "Напишите причину одной строкой, 3-300 символов.":
        "Sebäbi bir setirde ýazyň, 3–300 belgi.",
}
