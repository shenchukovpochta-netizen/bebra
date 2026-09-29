"""Готовность установки: что ещё не настроено и где это чинится.

Свежая установка (новый франчайзи или переезд) узнаёт о пустых реквизитах
из первого договора с прочерками, а о неподключённом банке - когда клиент
спросит, где его платёж. Страница «Готовность» собирает такие вещи в один
список заранее, и у каждой строки есть куда идти чинить.

Здесь только правила: факты собирает маршрут панели (база, конфиг, живость
бота). Панель в интернет не ходит, поэтому о фоновых опросах бота
(выписка, трекеры, Авито) судим по следам, которые они оставили в базе.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from . import company, logic

OK, TODO, WARN, OFF, UNKNOWN = "ok", "todo", "warn", "off", "unknown"
# Подпись состояния и класс метки в панели.
STATES: dict[str, tuple[str, str]] = {
    OK: ("готово", "ok"),
    TODO: ("не настроено", "bad"),
    WARN: ("проверьте", "warn"),
    OFF: ("не подключено", ""),
    UNKNOWN: ("панель не видит", ""),
}

# Реквизиты, без которых договор уходит с прочерками: те, что стоят в
# поставочных шаблонах. Почта - нет: без неё в документе честный прочерк.
COMPANY_REQUIRED: tuple[str, ...] = (
    "company_name", "company_short", "company_inn", "company_ogrn", "company_address",
    "company_phone", "company_bank", "company_account", "company_bik", "company_corr")

# Что о точке нужно клиенту, чтобы до неё доехать и дозвониться: это же
# бот отвечает на «где вы» и «до скольки работаете».
POINT_FIELDS: dict[str, str] = {"address": "адрес", "hours": "режим", "phone": "телефон"}

# Выписка приходит раз в полчаса; неделя без единой операции у живого
# проката - это не выходные, а сломанный токен или закрытый счёт.
BANK_QUIET = timedelta(days=7)


def item(code: str, title: str, state: str, text: str, *, href: str = "",
         link: str = "", how: str = "", at: datetime | None = None,
         required: bool = True) -> dict[str, Any]:
    label, css = STATES[state]
    return {"code": code, "title": title, "state": state, "label": label, "css": css,
            "text": text, "href": href, "link": link, "how": how, "at": at,
            "required": required}


def _filled(value: Any) -> bool:
    return bool(str(value or "").strip())


def check_company(settings: Mapping[str, Any]) -> dict[str, Any]:
    missing = [company.COMPANY_FIELDS[code] for code in COMPANY_REQUIRED
               if not _filled(settings.get(code))]
    if missing:
        return item("company", "Реквизиты организации", TODO,
                    "Не заполнено: " + ", ".join(missing) + ". В поставочных договоре, "
                    "актах и политике арендодатель — только подстановки: пустое поле "
                    "уйдёт клиенту прочерком.", href="/company", link="Заполнить")
    return item("company", "Реквизиты организации", OK,
                f"{settings.get('company_short') or settings.get('company_name')}, "
                f"ИНН {settings.get('company_inn')}.", href="/company", link="Реквизиты")


def check_consent(settings: Mapping[str, Any], consent: str) -> dict[str, Any]:
    """Экран согласия в боте называет оператора текстом (texts.CONSENT и
    восемь переводов), а не подстановкой: сверяем его с ИНН из реквизитов.
    Чужой ИНН на экране - согласие дано не тому, кто обрабатывает данные."""
    inn = logic.digits(settings.get("company_inn"))
    if not inn:
        return item("consent", "Согласие в боте", TODO,
                    "Сверить не с чем: заполните ИНН в реквизитах. Экран согласия в "
                    "боте называет оператора текстом, и это должны быть вы.",
                    href="/company", link="Реквизиты")
    if inn not in re.findall(r"\d{10,12}", consent or ""):
        return item("consent", "Согласие в боте", TODO,
                    "Экран согласия и подпись к политике в боте называют другого "
                    "оператора: вашего ИНН в них нет. До первого клиента их правит "
                    "разработчик.",
                    how="app/texts.py (CONSENT, POLICY_CAPTION, POLICY_NO_FILE) и те же "
                        "строки в app/i18n/*.py, затем поднять OFERTA_VERSION в .env.")
    return item("consent", "Согласие в боте", OK,
                "Экран согласия называет вас: ИНН совпадает с реквизитами.")


def check_points(locations: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    open_points = [row for row in locations if row.get("active", True)]
    if not open_points:
        return item("points", "Точки выдачи", TODO,
                    "Нет ни одной открытой точки: выдавать неоткуда, а бот не знает, "
                    "куда звать клиента.", href="/locations", link="Завести точку")
    gaps = []
    for row in open_points:
        lacks = [label for field, label in POINT_FIELDS.items() if not _filled(row.get(field))]
        if lacks:
            gaps.append(f"{row.get('name')}: нет " + ", ".join(lacks))
    if gaps:
        return item("points", "Точки выдачи", TODO,
                    "; ".join(gaps) + ". Бот отвечает клиенту на «где вы» и «до скольки» "
                    "этими полями.", href="/locations", link="Дополнить")
    return item("points", "Точки выдачи", OK,
                "Открыты: " + ", ".join(str(r.get("name")) for r in open_points) + ".",
                href="/locations", link="Точки")


def check_prices(models: Iterable[Mapping[str, Any]],
                 tariffs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Цена у каждой модели каталога: своя или запасная (тариф без модели)."""
    bike = [t for t in tariffs if t.get("active", True)
            and str(t.get("kind") or "bike") == "bike"]
    catalogue = [m for m in models if m.get("active", True)]
    if not bike:
        return item("prices", "Модели и тарифы", TODO,
                    "Нет ни одного действующего тарифа на велосипед: выдать не по чему.",
                    href="/tariffs", link="Завести тариф")
    if not catalogue:
        return item("prices", "Модели и тарифы", WARN,
                    "Каталог моделей пуст: цена одна на все, заявка из кабинета "
                    "клиента не предложит выбора.", href="/models", link="Каталог")
    own = {str(t.get("model") or "").strip() for t in bike}
    fallback = "" in own
    without = [str(m.get("title")) for m in catalogue
               if str(m.get("title") or "").strip() not in own]
    if without and not fallback:
        return item("prices", "Модели и тарифы", TODO,
                    "Без цены: " + ", ".join(without) + ". Такую модель не выдать: "
                    "своего тарифа нет, запасного тоже.", href="/tariffs", link="Тарифы")
    if without:
        return item("prices", "Модели и тарифы", WARN,
                    "По запасному тарифу: " + ", ".join(without) + ". Если цена у них "
                    "своя — заведите её, иначе выдача возьмёт общую.",
                    href="/tariffs", link="Тарифы")
    return item("prices", "Модели и тарифы", OK,
                f"Моделей в каталоге: {len(catalogue)}, у каждой своя цена. Цены и "
                "каталог свежей базы — поставочные: сверьте со своими.",
                href="/tariffs", link="Тарифы")


def check_staff(staff: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    active = [s for s in staff if s.get("active", True)]
    if len(active) <= 1:
        return item("staff", "Сотрудники", TODO,
                    "В панели только администратор. У оператора и механика — свои "
                    "входы и права: иначе в журналах один автор на всех.",
                    href="/staff", link="Завести сотрудника")
    return item("staff", "Сотрудники", OK, f"Активных входов: {len(active)}.",
                href="/staff", link="Сотрудники")


def check_bot(state: Mapping[str, Any]) -> dict[str, Any]:
    how = ("Токен — в secrets/bot_token, затем «docker compose up -d crm» "
           "(INSTALL.md, шаги 2 и 7).")
    if state.get("ok") is None:
        return item("bot", "Бот и панель", TODO,
                    "Бот к панели не подключён: о зачислении, аренде и смете клиент "
                    "из панели не узнает.", how=how)
    if not state.get("ok"):
        return item("bot", "Бот и панель", WARN,
                    f"Telegram не отвечает боту панели: {state.get('error') or 'ошибка'}.",
                    how=how)
    return item("bot", "Бот и панель", OK, f"Бот @{state.get('name')} отвечает.")


def check_acquiring(state: Mapping[str, Any]) -> dict[str, Any]:
    if not state.get("configured"):
        return item("acquiring", "Эквайринг Точки", OFF,
                    "Ссылки на оплату картой не выставляются, чек 54-ФЗ не пробивается.",
                    how="secrets/tochka_token и TOCHKA_CUSTOMER_CODE в .env, затем "
                        "«bash bootstrap.sh» (CRM.md, «Чек 54-ФЗ»).",
                    required=False)
    if not state.get("enabled"):
        return item("acquiring", "Эквайринг Точки", WARN,
                    "Настроен, но выключен в панели: ссылки на оплату не выставляются.",
                    href="/payments", link="Включить", required=False)
    return item("acquiring", "Эквайринг Точки", OK, "Настроен и включён.",
                href="/payments", link="Счета", required=False)


def check_bank(last: Mapping[str, Any] | None, now: datetime) -> dict[str, Any]:
    if not last:
        return item("bank", "Выписка по счёту", OFF,
                    "Выписка ни разу не приходила: переводы на счёт зачисляются только "
                    "руками по заявке клиента.",
                    how="Токен Точки и TOCHKA_ACCOUNT_ID в .env — выписку тянет бот "
                        "(CRM.md, «Банк: выписка Точки и зачисление»).",
                    href="/bank", link="Выписка", required=False)
    seen = last.get("created_at") or last.get("booked_at")
    if isinstance(seen, datetime) and now - seen > BANK_QUIET:
        return item("bank", "Выписка по счёту", WARN,
                    "Больше недели ни одной операции: токен или счёт, скорее всего, "
                    "уже не те. Логи бота скажут точнее.", at=seen,
                    href="/bank", link="Выписка", required=False)
    return item("bank", "Выписка по счёту", OK, "Приходит.", at=seen,
                href="/bank", link="Выписка", required=False)


def check_trackers(trackers: Iterable[Mapping[str, Any]], now: datetime,
                   settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """О живости опроса судим по last_seen - времени самого устройства, а
    не круга опроса. Ночью парк стоит, и спящий трекер выходит на связь раз
    в несколько часов, поэтому порог - тот же, что у тревоги «молчит»
    (tracker_offline_hours, 12 ч): весь парк молчит по меркам самой
    системы - это уже не ночь, а мёртвый опрос."""
    rows = list(trackers)
    seen = [t["last_seen"] for t in rows if isinstance(t.get("last_seen"), datetime)]
    if not seen:
        return item("trackers", "Трекеры StarLine", OFF,
                    "Опрос StarLine ни разу не принёс данных: карта пуста, тревог нет.",
                    how="STARLINE_APP_ID и STARLINE_LOGIN в .env, секреты "
                        "secrets/starline_* (CRM.md, «Трекеры и карта парка»).",
                    href="/trackers", link="Трекеры", required=False)
    last = max(seen)
    quiet = logic.tracker_settings(settings)["offline_hours"]
    if now - last > timedelta(hours=quiet):
        return item("trackers", "Трекеры StarLine", WARN,
                    f"Весь парк молчит дольше {quiet} ч (порог тревоги «молчит»): "
                    "опрос, скорее всего, остановился. Логи бота скажут точнее.",
                    at=last, href="/trackers", link="Трекеры", required=False)
    return item("trackers", "Трекеры StarLine", OK, f"Трекеров: {len(rows)}.", at=last,
                href="/trackers", link="Трекеры", required=False)


def check_avito(settings: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    state = logic.avito_state(settings, now=now)
    if not state["configured"]:
        return item("avito", "Авито", OFF,
                    "Сообщения из объявлений во «Входящие» не приходят.",
                    how="AVITO_CLIENT_ID в .env и secrets/avito_client_secret "
                        "(INSTALL.md, «Входящие: Авито и WhatsApp»).",
                    href="/inbox", link="Входящие", required=False)
    if not state["live"]:
        return item("avito", "Авито", WARN,
                    "Опрос Авито не отвечает" + (f": {state['error']}" if state["error"]
                                                 else "") + ".",
                    at=state["at"], href="/inbox", link="Входящие", required=False)
    return item("avito", "Авито", OK, "Сообщения приходят во «Входящие».", at=state["at"],
                href="/inbox", link="Входящие", required=False)


def check_https(trust_proxy: bool) -> dict[str, Any]:
    if trust_proxy:
        return item("https", "Домен и HTTPS", OK,
                    "Панель за Caddy по своему домену, сертификат продлевается сам.",
                    required=False)
    return item("https", "Домен и HTTPS", OFF,
                "Панель открывается только через SSH-туннель: сотруднику с телефона "
                "не войти, ссылки на подпись клиенту не открыть.",
                how="CRM_DOMAIN и COMPOSE_PROFILES=\"https\" в .env, A-запись домена на "
                    "сервер, затем «bash bootstrap.sh» (CRM.md, «Развёртывание и доступ»).",
                required=False)


def check_backup(settings: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """Каталог бэкапов панели не смонтирован намеренно (дамп всей базы не
    должен быть доступен процессу, который смотрит в интернет), поэтому
    судим по отчёту, который сервис backup кладёт в crm.settings, - тем же
    правилом, что карточка «Сервер» и сообщение владельцу."""
    backup = logic.parse_backup_status(settings.get(logic.BACKUP_STATUS_KEY))
    bad = logic.backup_problems(backup, now)
    where = {"href": "/notices", "link": "Уведомления → Сервер", "required": False}
    if bad:
        first = bad[next(part for part in logic.BACKUP_PARTS if part in bad)]
        return item("backup", "Бэкап базы", WARN, first["text"],
                    how="«docker compose logs backup»; вручную — «docker compose exec "
                        "backup backup.sh dump» (INSTALL.md, «Бэкап вне сервера»).",
                    **where)
    dump = (backup or {}).get("dump") or {}
    if not ((backup or {}).get("offsite") or {}).get("enabled"):
        return item("backup", "Бэкап базы", OFF,
                    "Дамп делается, но только на этом сервере: умрёт сервер — умрут "
                    "и дампы.", at=dump.get("last_ok"),
                    how="Бакет S3 в России, BACKUP_S3_* в .env и секрет в "
                        "secrets/backup_s3_secret, затем «bash bootstrap.sh»; ключ "
                        "secrets/backup_key сохраните вне сервера (INSTALL.md, «Бэкап "
                        "вне сервера»).", **where)
    return item("backup", "Бэкап базы", OK,
                "Дамп каждую ночь, зашифрованная копия уходит в облако.",
                at=dump.get("last_ok"), **where)


def checks(*, settings: Mapping[str, Any], consent: str,
           locations: Iterable[Mapping[str, Any]],
           models: Iterable[Mapping[str, Any]], tariffs: Iterable[Mapping[str, Any]],
           staff: Iterable[Mapping[str, Any]], bot: Mapping[str, Any],
           acquiring: Mapping[str, Any], bank_last: Mapping[str, Any] | None,
           trackers: Iterable[Mapping[str, Any]], https: bool,
           now: datetime) -> list[dict[str, Any]]:
    """Строки страницы: сначала то, без чего прокат не работает, потом
    интеграции, которые можно подключить позже."""
    return [check_company(settings), check_consent(settings, consent),
            check_points(locations),
            check_prices(models, tariffs), check_staff(staff), check_bot(bot),
            check_acquiring(acquiring), check_bank(bank_last, now),
            check_trackers(trackers, now, settings), check_avito(settings, now),
            check_https(https), check_backup(settings, now)]


def summary(items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(items)
    required = [r for r in rows if r["required"]]
    optional = [r for r in rows if not r["required"]]
    return {"required": len(required),
            "required_ok": sum(r["state"] == OK for r in required),
            "optional": len(optional),
            "optional_on": sum(r["state"] == OK for r in optional),
            "ready": all(r["state"] == OK for r in required)}
