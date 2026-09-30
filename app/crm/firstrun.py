"""Мастер первого запуска: «Готовность» по шагам, для новой установки.

Свежая установка (франчайзи, новый сервер) открывает панель без
реквизитов, без своих сотрудников, с поставочными точками и ценами.
«Готовность» говорит, что не так; мастер ведёт по тому же списку по
порядку и даёт исправить на месте. Своих проверок у него нет: состояние
шага - строка «Готовности» (readiness), а сохраняет шаг тот же код, что
и страница раздела.

Сам мастер встречает только владельца, только установку, которую он
застал свежей (ни журнала статусов, ни денег - боевая база им не
бывает), и только пока обязательный шаг не готов по его же правилу.
Скрытый или завершённый сам не возвращается. Решение «свежая» и
прогресс - в crm.settings, а не в сессии: сессия живёт в одном браузере,
а владелец начнёт с ноутбука и продолжит с телефона.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from . import readiness

# Шаги по порядку: код -> заголовок.
STEPS: dict[str, str] = {
    "company": "Организация",
    "points": "Точки",
    "prices": "Модели и тарифы",
    "staff": "Сотрудники",
    "connect": "Подключения",
    "check": "Проверка",
}
# Без этого не выдать первый договор: по ним мастер решает, нужен ли он.
# Код шага совпадает с кодом строки «Готовности».
ESSENTIAL: tuple[str, ...] = ("company", "points", "prices", "staff")
# Строки «Готовности» на шаге «Подключения»: всё это живёт в .env и
# secrets/ на сервере, форма в панели для них не нужна и вредна.
CONNECT: tuple[str, ...] = ("bot", "acquiring", "bank", "trackers", "avito", "https")

# Нет ключа - установку мастер ещё не встречал; "" - работает,
# "dismissed" - скрыт, "done" - пройден.
STATE_KEY = "setup_wizard"
STEPS_KEY = "setup_steps"       # пройденные шаги через запятую
HIDDEN = ("dismissed", "done")

# Кого заводит шаг «Сотрудники»: код формы -> (код роли, подпись).
ROLES: dict[str, tuple[str, str]] = {
    "operator": ("manager", "Администратор"),
    "mechanic": ("tech", "Мастер"),
}
# Срок, цену которого мастер спрашивает у каждой модели: аренда
# понедельная, остальные сроки правят в «Тарифах».
WEEK = 7
# Строка запасного тарифа (без модели) в форме цен.
ANY_MODEL = "any"

# После любого сохранения секрет читается процессом при старте - одна
# строка на все интеграции, чтобы подсказки не разошлись.
_RESTART = "На сервере: «bash bootstrap.sh», затем «docker compose restart bot crm»."
# Точные шаги для каждой интеграции. Через веб-форму секреты не ходят
# намеренно: страница, принимающая токен банка, стала бы самой ценной
# целью в панели, а файл в secrets/ читает только процесс на сервере.
CONNECT_HOW: dict[str, tuple[str, ...]] = {
    "bot": (
        "@BotFather → /newbot → токен вида 123456789:AAH… (INSTALL.md, шаг 2).",
        "На сервере в каталоге проката: «bash install.sh» спросит токен и проверит "
        "его через Telegram; руками — «printf '%s' 'ТОКЕН' > secrets/bot_token».",
        _RESTART,
    ),
    "acquiring": (
        "Кабинет Точки → «Интеграции» → токен: в файл secrets/tochka_token.",
        "В .env: TOCHKA_CUSTOMER_CODE — код клиента банка.",
        _RESTART + " Включается кнопкой в «Финансы → Счета».",
    ),
    "bank": (
        "Тот же токен secrets/tochka_token. В .env: TOCHKA_ACCOUNT_ID вида "
        "40802810XXXXXXXXXXXX/044525104, несколько счетов — через запятую.",
        _RESTART + " Выписку тянет бот раз в полчаса.",
    ),
    "trackers": (
        "developer.starline.ru → своё приложение. В .env: STARLINE_APP_ID и "
        "STARLINE_LOGIN (логин кабинета StarLine).",
        "Секрет приложения — в secrets/starline_app_secret, пароль — в "
        "secrets/starline_password.",
        _RESTART,
    ),
    "avito": (
        "Кабинет основного аккаунта Авито → «Для профессионалов → API»: client_id и "
        "client_secret. Нужен тариф с API сообщений, иначе Авито ответит 402.",
        "В .env: AVITO_CLIENT_ID; секрет — в secrets/avito_client_secret.",
        _RESTART,
    ),
    "https": (
        "A-запись поддомена (crm.<ваш-домен>) на IP сервера.",
        "В .env: CRM_DOMAIN=\"crm.<ваш-домен>\" и COMPOSE_PROFILES=\"https\".",
        "«bash bootstrap.sh»: сертификат Caddy получит и продлит сам.",
    ),
}


def is_owner(staff: Mapping[str, Any] | None) -> bool:
    """Встречает мастер только владельца - встроенный профиль, а не любого
    с правом на настройки: заводить входы и цены - его решение."""
    return (staff or {}).get("profile_code") == "owner"


def hidden(settings: Mapping[str, Any]) -> bool:
    return str(settings.get(STATE_KEY) or "") in HIDDEN


def started(settings: Mapping[str, Any]) -> bool:
    """Мастер уже застал эту установку свежей. Решается один раз, при
    первой встрече с владельцем, и записывается: по живому журналу первый
    же велосипед (триггер пишет статус на вставке) или импорт таблицы
    посреди настройки выключали бы мастер навсегда, без «Скрыть»."""
    return settings.get(STATE_KEY) is not None


def passed(settings: Mapping[str, Any]) -> list[str]:
    """Пройденные шаги в порядке мастера; чужое слово из базы - не шаг."""
    got = {part.strip() for part in str(settings.get(STEPS_KEY) or "").split(",")}
    return [code for code in STEPS if code in got]


def with_step(settings: Mapping[str, Any], code: str) -> str:
    """Значение STEPS_KEY после прохождения шага: повтор ничего не меняет."""
    return ",".join(c for c in STEPS if c in {*passed(settings), code})


def essentials(*, settings: Mapping[str, Any], locations: Iterable[Mapping[str, Any]],
               models: Iterable[Mapping[str, Any]], tariffs: Iterable[Mapping[str, Any]],
               staff: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Обязательные строки «Готовности» - те же функции, что у страницы.
    Бот и интеграции сюда не входят: чтобы решить, встречать ли владельца,
    незачем спрашивать Telegram."""
    return [readiness.check_company(settings), readiness.check_points(locations),
            readiness.check_prices(models, tariffs), readiness.check_staff(staff)]


def needed(rows: Iterable[Mapping[str, Any]]) -> bool:
    """Не готов обязательный шаг - по правилу самого мастера (steps): не
    пройден или «Готовность» против. По одной «Готовности» мастер уходил
    бы, не показав поставочные точки и цены: для неё они готовы с первого
    дня, а бот тем временем отвечает клиентам казанскими адресами."""
    return any(r["code"] in ESSENTIAL and not r["done"] for r in rows)


def wanted(*, staff: Mapping[str, Any] | None, settings: Mapping[str, Any],
           demo: bool, rows: Iterable[Mapping[str, Any]]) -> bool:
    """Встретить ли владельца мастером (после входа и на сводке); rows -
    строки steps. Установку, которую мастер не застал свежей (started),
    он не встречает никогда, как бы там ни были заполнены реквизиты. В
    демо он выключен: стенд настроен сидом.
    """
    if demo or not is_owner(staff) or not started(settings) or hidden(settings):
        return False
    return needed(rows)


def steps(items: Iterable[Mapping[str, Any]],
          settings: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Строки мастера. Состояние - из «Готовности», «пройден» - из настроек.

    Шаг готов, когда его прошли в мастере и строка «Готовности» не
    «не настроено». Одних фактов мало: цены и точки свежей базы -
    поставочные (Казань), их надо увидеть и подтвердить, а не принять
    молча. «Подключения» и «Проверка» готовы, когда их прошли: первое -
    инструкция к серверу, второе - решение владельца.
    """
    by = {i["code"]: i for i in items}
    done = set(passed(settings))
    rows = []
    for n, (code, title) in enumerate(STEPS.items(), 1):
        if code == "check":
            total = readiness.summary(by.values()) if by else {"ready": False}
            item = readiness.item(
                "check", title, readiness.OK if total["ready"] else readiness.TODO,
                "Обязательное готово — можно выдавать." if total["ready"] else
                "Обязательное готово не всё: список — на странице «Готовность».")
        else:
            item = by.get("bot" if code == "connect" else code) or readiness.item(
                code, title, readiness.UNKNOWN, "")
        rows.append({"code": code, "n": n, "title": title, "state": item["state"],
                     "label": item["label"], "css": item["css"], "text": item["text"],
                     "passed": code in done,
                     "done": code in done and not (code in ESSENTIAL
                                                   and item["state"] == readiness.TODO)})
    return rows


def current(rows: list[Mapping[str, Any]], requested: Any = None) -> Mapping[str, Any]:
    """Открытый шаг: запрошенный, иначе первый не готовый, иначе проверка."""
    wanted_code = str(requested or "")
    for row in rows:
        if row["code"] == wanted_code:
            return row
    return next((r for r in rows if not r["done"]), rows[-1])


def following(rows: list[Mapping[str, Any]], code: str) -> str:
    """Следующий шаг после code - для ссылки «пропустить»."""
    codes = [r["code"] for r in rows]
    index = codes.index(code) if code in codes else len(codes) - 1
    return codes[min(index + 1, len(codes) - 1)]


def progress(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    rest = [r for r in rows if not r["done"]]
    return {"done": len(rows) - len(rest), "total": len(rows),
            "next": rest[0]["title"] if rest else ""}


def price_rows(models: Iterable[Mapping[str, Any]],
               tariffs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Строки шага «Модели и тарифы»: модель каталога, её недельная цена
    (если есть) и прочие сроки - для справки. Последняя строка - запасной
    тариф без модели: с пустым каталогом цена у проката всё равно одна."""
    bike = [t for t in tariffs if t.get("active", True)
            and str(t.get("kind") or "bike") == "bike"]

    def row(key: Any, title: str, model: str, bikes: int) -> dict[str, Any]:
        own = sorted((t for t in bike if str(t.get("model") or "") == model),
                     key=lambda t: int(t.get("period_days") or 0))
        week = next((t for t in own if int(t.get("period_days") or 0) == WEEK), None)
        return {"key": key, "title": title, "model": model or None, "bikes": bikes,
                "week": week, "other": [t for t in own if t is not week]}

    rows = [row(m["id"], str(m["title"]), str(m["title"]), int(m.get("bikes") or 0))
            for m in models if m.get("active", True)]
    rows.append(row(ANY_MODEL, "Любая модель — запасной тариф", "", 0))
    return rows


def staff_roles(staff: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Кто уже заведён на каждую роль шага: логины активных входов."""
    rows = [s for s in staff if s.get("active", True)]
    return {role: [str(s.get("login")) for s in rows if s.get("profile_code") == code]
            for role, (code, _title) in ROLES.items()}
