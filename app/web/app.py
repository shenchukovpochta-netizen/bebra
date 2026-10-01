"""Веб-панель CRM: FastAPI + Jinja2, формы без JavaScript-фреймворков.

Всё серверное: страница - это шаблон, действие - POST формы и редирект.
Так панель открывается с любого телефона, а код читается сверху вниз.
Данные приходят из CrmDB (или его заглушки в тестах), решения - из
app.crm.logic и app.crm.service, уведомления клиентам - app.crm.notify.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .. import logic as bot_logic
from .. import texts
from ..crm import (
    banking,
    billing,
    company,
    doctemplates,
    firstrun,
    franchise,
    import_xlsx,
    incoming,
    learning,
    logic,
    mytasks,
    notices,
    notify,
    photos,
    readiness,
    service,
)
from ..services import contract as contract_service
from ..services import tochka
from . import nav
from .config import WebConfig

# Ошибка данных базы (строка, которую не принял кодек) - это негодное
# сообщение хука, а не сбой базы. У панели asyncpg есть всегда, у тестов
# на заглушке его может не быть.
try:
    from asyncpg.exceptions import DataError
except ImportError:                             # pragma: no cover
    class DataError(Exception):                 # type: ignore[no-redef]
        pass

log = logging.getLogger(__name__)

# Снимок сверки техники: телефонное фото столько и весит, а всё,
# что больше, - это чей-то скриншот экрана целиком.
BIKE_PHOTO_MAX = 8 * 1024 * 1024
HERE = Path(__file__).resolve().parent
# Страница подписания открыта клиенту: он не сотрудник и в панель
# не входит. Защита у неё одна - случайный токен в ссылке.
# Хук «Входящих» (/hook/) - для шлюзов WhatsApp и n8n: входа в панель у
# них нет, защита - свой токен в заголовке, лимит неудач и размера.
# Манифест значка браузер запрашивает без cookie, как и сами значки в
# /static: закрытый входом, он пришёл бы редиректом на /login.
PUBLIC = ("/login", "/static", "/healthz", "/sign/", "/hook/", "/manifest.webmanifest")
# Свой кабинет доступен любому сотруднику, каким бы урезанным ни был профиль.
ALWAYS_OPEN = ("/logout", "/me", "/me/password")
SESSION_DAYS = 14
# Перебор пароля: после LOGIN_LIMIT неудач по логину с одного адреса вход
# в этот логин с этого адреса закрыт на LOGIN_WINDOW секунд. Ключ - пара
# «логин + адрес»: по одному логину чужие десять ошибок с другого адреса
# запирали бы владельца, а по одному адресу за SSH-туннелем (все с
# 127.0.0.1) - всех сотрудников. Перебор с многих адресов держит
# отдельный, более щедрый предел на логин (LOGIN_ACCOUNT_LIMIT), перебор
# логинов - предел на адрес (LOGIN_IP_LIMIT). Память процесса, без базы:
# панель одна, и рестарт, обнуляющий счётчик, атакующему ничего не даёт.
LOGIN_LIMIT, LOGIN_ACCOUNT_LIMIT, LOGIN_IP_LIMIT = 10, 50, 100
LOGIN_WINDOW = 15 * 60
LOGIN_KEYS_SWEEP = 500
# Хэш-приманка для входа под несуществующим или отключённым логином:
# scrypt той же цены идёт и по нему, иначе по времени ответа видно, какой
# логин есть. Нули не совпадут ни с одним паролем.
DECOY_PASSWORD_HASH = f"scrypt${'0' * 32}${'0' * 64}"
# Учётная таблица проката - сотни строк, единицы мегабайт.
IMPORT_MAX_BYTES = 20 * 1024 * 1024
# Предел тела любого запроса - до разбора формы и до входа. Starlette
# файловые части формы не ограничивает: каждая копится в памяти до
# мегабайта и дальше пишется во временный файл, частей до тысячи. Без
# предела чужая загрузка на /login (он открыт без входа) заполняла бы
# диск, общий с базой, ещё до проверки пароля. Самая тяжёлая обычная
# форма - импорт таблицы; мегабайт сверху - разметка multipart.
BODY_MAX = max(IMPORT_MAX_BYTES, BIKE_PHOTO_MAX, logic.DOC_MAX_BYTES) + 1024 * 1024
# Закрытие аренды с шестью фото при сдаче (телефонное фото - до 8 МБ, и
# шесть таких - обычное дело) - единственная форма тяжелее, и большой
# предел только у неё. Поднятый для всей панели, он раздал бы по 49 МБ и
# открытым без входа /login, /sign/ и /hook/: десяток таких запросов
# разом - это уже память панели (предел 1 ГБ) и диск, общий с базой.
# Без входа большое тело до разбора формы не доходит: страж входа
# отвечает переадресацией раньше. Caddy делит пределы теми же путями.
PHOTOS_BODY_MAX = logic.RETURN_PHOTO_MAX_BYTES * logic.RETURN_PHOTOS_MAX + 1024 * 1024
WIDE_BODY_PATHS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"/rentals/[0-9]+/close"), PHOTOS_BODY_MAX),
)
TOO_LARGE_PAGE = (
    '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<title>Слишком большой запрос</title></head>'
    '<body style="font:16px/1.5 system-ui,sans-serif;max-width:28em;'
    'margin:15vh auto;padding:0 16px;text-align:center">'
    '<h1 style="font-size:22px">Слишком большой запрос</h1>'
    '<p>Файл или форма больше, чем принимает панель. '
    '<a href="javascript:history.back()">Вернуться</a></p></body></html>')

# ─── демо-стенд (cfg.demo, python -m app.demo) ───
# Логин демо публичен. Всё, что меняет доступ к самому стенду, пишет
# чужие файлы или пускает в базу чужие данные, закрыто здесь одним
# списком, а не проверкой в каждом обработчике: новый маршрут под тем же
# префиксом закрыт сам.
# Сколько истекающих аренд показывает сводка; остальные - по ссылке.
EXPIRING_SHOWN = 12
DEMO_BLOCKED_PATHS = frozenset({
    "/me/password",           # сменённый пароль запер бы демо для всех
    "/payments/acquiring",    # проверка эквайринга - запрос в банк
    # Импорт таблицы: тысячи строк одним запросом перекосили бы три числа
    # до ночи, а таблица покупателя с его настоящими клиентами (ФИО,
    # телефоны, адреса в заметке) стала бы видна каждому посетителю.
    # Заодно закрыт и разбор xlsx - самый тяжёлый запрос панели.
    "/import",
})
# Мастер первого запуска в демо выключен, а его шаг «Сотрудники» завёл бы
# вход мимо закрытого /staff. «Обновить сейчас» у франчайзи - запрос на
# чужой сервер.
DEMO_BLOCKED_PREFIXES = ("/staff", "/profiles", "/documents", "/setup",
                         "/franchisees/refresh")
DEMO_BLOCKED_TEXT = "В демо-версии это недоступно."
DEMO_PHOTO_TEXT = "Снимки в демо не хранятся: файл не сохранён."
# Фото номера при сверке в демо не требуется: снимки не хранятся, и
# включённое требование заперло бы ввод техники для всех посетителей.
DEMO_NO_PHOTO_TEXT = "Фото номера в демо не требуется: снимки не хранятся."
# В демо загрузок нет (импорт, шаблоны, печати закрыты, снимки не
# хранятся): форма - килобайты, мегабайта хватает с запасом.
DEMO_BODY_MAX = 1024 * 1024
# Посетитель с циклом curl не должен занимать единственный процесс
# панели: логин демо известен всем, и на сводке, выгрузках и входе
# (scrypt) сотня запросов в секунду с одного адреса заморозила бы демо
# остальным. Предел - на адрес клиента: сколько запросов сразу и какой
# темп в среднем, с запасом на всплеск - человек открывает вкладки
# подряд. Статика, манифест значка и /healthz не считаются: базы они не
# трогают, а манифест браузер берёт к каждой странице.
DEMO_INFLIGHT = 4
DEMO_RATE = 3.0
DEMO_BURST = 40
DEMO_BUSY_TEXT = "Слишком много запросов с вашего адреса — подождите немного"
# Выгрузка демо помечена внутри файла, а не только плашкой страницы:
# скачанную таблицу пересылают без страницы, и без пометки она выглядела
# бы настоящей базой клиентов или настоящими цифрами точек.
DEMO_EXPORT_NOTE = ("Демо-версия МАЙБАЙК CRM: все люди, велосипеды и деньги "
                    "вымышленные.")
# Строк в выгрузке демо - с запасом на любую честную (клиентов ~500,
# платежей за месяц ~1500); «с 2000 года» построчно в xlsx - это секунды
# процессора на запрос.
DEMO_EXPORT_ROWS = 3000
# Показанные на входе. Должны совпадать с app.demo.seed.STAFF (тест
# test_demo_mode сверяет): панель пакет демо не импортирует.
DEMO_LOGINS = (("demo", "demo", "Владелец — видит всё"),
               ("operator", "demo", "Администратор точки"),
               ("mechanic", "demo", "Мастер"))
# Демо не должно попадать в поиск: вымышленные люди с телефонами под
# брендом проката выглядели бы как утечка.
DEMO_PUBLIC = ("/robots.txt", "/learn/start")
ROBOTS_TAG = "noindex, nofollow"
ROBOTS_TXT = "User-agent: *\nDisallow: /\n"
# Телефон клиента в демо - только из вымышленного ряда +7 000: демо
# открыто всем, и настоящий номер, набранный новичком на обучении или
# посетителем «для проверки», до ночи висел бы у всех на виду. Кода,
# начинающегося с нуля, в плане нумерации нет - номер ничей.
DEMO_PHONE_PREFIX = "+7000"
DEMO_PHONE_TEXT = ("В демо телефоны только вымышленные: +7 000 …, например "
                   "+7 000 012-34-56.")
MAINTENANCE_TEXT = "Демо обновляется, минуту"
MAINTENANCE_PAGE = (
    '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<meta name="color-scheme" content="light dark">'
    '<meta http-equiv="refresh" content="30">'
    '<title>Демо обновляется</title></head>'
    '<body style="font:16px/1.5 system-ui,sans-serif;max-width:28em;'
    'margin:15vh auto;padding:0 16px;text-align:center">'
    f'<h1 style="font-size:22px">{MAINTENANCE_TEXT}</h1>'
    '<p>Данные возвращаются к исходным. Страница обновится сама.</p>'
    '</body></html>')


def demo_blocked(method: str, path: str) -> bool:
    """Закрыт ли этот запрос в демо. Чтение открыто всегда."""
    if method in ("GET", "HEAD"):
        return False
    return path in DEMO_BLOCKED_PATHS or any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in DEMO_BLOCKED_PREFIXES)


class BodyTooLarge(Exception):
    """Тело запроса перешло предел BodyLimit посреди чтения."""


class BodyLimit:
    """Предел тела запроса для всей панели - внешний слой, до сессии и входа.

    Заявленная длина больше предела - 413 сразу, тело не читается вовсе.
    Тело без длины (chunked) считается по мере чтения: перешло предел -
    чтение обрывается, и если ответ ещё не начат, уходит тот же 413.
    `wide` - свой, больший предел для POST на пути целиком по образцу
    (закрытие аренды с фото); остальным - общий `limit`.
    """

    def __init__(self, app: Any, *, limit: int,
                 wide: tuple[tuple[re.Pattern[str], int], ...] = ()) -> None:
        self.app = app
        self.limit = limit
        self.wide = wide

    def limit_for(self, scope: Any) -> int:
        if scope.get("method") == "POST":
            path = scope.get("path") or ""
            for pattern, limit in self.wide:
                if pattern.fullmatch(path):
                    return limit
        return self.limit

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope)
        for name, value in scope.get("headers") or ():
            if name == b"content-length" and value.isdigit() and int(value) > limit:
                await self.refuse(scope, receive, send)
                return
        state = {"read": 0, "over": False, "started": False}

        async def counted() -> Any:
            message = await receive()
            if message["type"] == "http.request":
                state["read"] += len(message.get("body") or b"")
                if state["read"] > limit:
                    state["over"] = True
                    raise BodyTooLarge
            return message

        async def watched(message: Any) -> None:
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        try:
            await self.app(scope, counted, watched)
        except Exception:
            # Прерванное чтение приходит сюда как есть или обёрнутым в
            # группу исключений слоя входа: решает флаг, а не тип.
            if not state["over"] or state["started"]:
                raise
            await self.refuse(scope, receive, send)

    @staticmethod
    async def refuse(scope: Any, receive: Any, send: Any) -> None:
        page = HTMLResponse(TOO_LARGE_PAGE, status_code=413,
                            headers={"Connection": "close", "Cache-Control": "no-store"})
        await page(scope, receive, send)


_NUL = re.compile(rb"%00|\x00")


class NoNul:
    """Нулевой байт в адресе - на входе, до сессии и маршрутов.

    Postgres не хранит \\x00 в тексте и отвечает на него ошибкой: `%00`
    в поиске или фильтре давал 500 на любой странице. В строке запроса
    он вырезается - одно место на все `query_params`; в пути это адрес
    без записи, 404. Поля форм чистит помощник `form()`.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            if "\x00" in (scope.get("path") or ""):
                page = PlainTextResponse("Not Found", status_code=404)
                await page(scope, receive, send)
                return
            query = scope.get("query_string") or b""
            if _NUL.search(query):
                scope = {**scope, "query_string": _NUL.sub(b"", query)}
        await self.app(scope, receive, send)


class DemoLimits:
    """Предел запросов с одного адреса в демо: сколько сразу и какой темп.

    Темп - ведро жетонов: DEMO_BURST подряд, дальше DEMO_RATE в секунду.
    Память процесса, без базы: процесс демо один, а рестарт, обнуляющий
    счётчики, атакующему ничего не даёт.
    """

    def __init__(self, *, inflight: int = DEMO_INFLIGHT, rate: float = DEMO_RATE,
                 burst: float = DEMO_BURST, clock: Any = time.monotonic) -> None:
        self.most, self.rate, self.burst, self.clock = inflight, rate, burst, clock
        self.busy: dict[str, int] = {}
        self.buckets: dict[str, tuple[float, float]] = {}

    def enter(self, ip: str) -> bool:
        now = self.clock()
        if len(self.buckets) > LOGIN_KEYS_SWEEP:
            # Адреса выбирает посетитель: без чистки словарь рос бы
            # бесконечно. Полное ведро и хранить незачем.
            for key in [k for k, (tokens, at) in self.buckets.items()
                        if tokens + (now - at) * self.rate >= self.burst]:
                self.buckets.pop(key, None)
        tokens, at = self.buckets.get(ip, (self.burst, now))
        tokens = min(self.burst, tokens + (now - at) * self.rate)
        if tokens < 1 or self.busy.get(ip, 0) >= self.most:
            self.buckets[ip] = (tokens, now)
            return False
        self.buckets[ip] = (tokens - 1, now)
        self.busy[ip] = self.busy.get(ip, 0) + 1
        return True

    def leave(self, ip: str) -> None:
        left = self.busy.get(ip, 1) - 1
        if left > 0:
            self.busy[ip] = left
        else:
            self.busy.pop(ip, None)


_DISPOSITION = re.compile(r"""(filename\*=[\w-]*''|filename=")(?!demo-)""", re.I)


def demo_disposition(value: str) -> str:
    """Имя скачанного файла демо начинается с «demo-»: clients.xlsx из
    демо в папке загрузок не спутать с настоящей выгрузкой."""
    return _DISPOSITION.sub(r"\1demo-", value)


class DemoGate:
    """Внешний слой демо: noindex на каждом ответе, 503 на время сброса,
    предел запросов с адреса и пометка «demo-» в имени скачанного файла.

    Стоит снаружи сессии и входа намеренно: сброс держит схему под замком
    одной транзакцией, и запрос, дошедший до базы, висел бы до коммита
    вместо честного «минуту». /healthz отвечает всегда - это процесс жив,
    а не данные готовы. Флаг - app.state.maintenance, его ставит app.demo;
    предел - app.state.demo_limits (None - без предела).
    """

    def __init__(self, app: Any, *, state: Any) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if getattr(self.state, "maintenance", False) and path != "/healthz":
            page = HTMLResponse(MAINTENANCE_PAGE, status_code=503,
                                headers={"Retry-After": "60", "Cache-Control": "no-store",
                                         "X-Robots-Tag": ROBOTS_TAG})
            await page(scope, receive, send)
            return

        async def tagged(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Robots-Tag"] = ROBOTS_TAG
                disposition = headers.get("content-disposition")
                if disposition and disposition.lower().startswith("attachment"):
                    headers["Content-Disposition"] = demo_disposition(disposition)
            await send(message)

        limits = getattr(self.state, "demo_limits", None)
        if (limits is None or path in ("/healthz", "/manifest.webmanifest")
                or path.startswith("/static/")):
            await self.app(scope, receive, tagged)
            return
        ip = (scope.get("client") or ("?",))[0]
        if not limits.enter(ip):
            page = HTMLResponse(
                f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
                f'<title>Подождите</title></head><body style="font:16px/1.5 '
                f'system-ui,sans-serif;max-width:28em;margin:15vh auto;padding:0 16px;'
                f'text-align:center"><h1 style="font-size:22px">{DEMO_BUSY_TEXT}</h1>'
                f'</body></html>', status_code=429,
                headers={"Retry-After": "5", "Cache-Control": "no-store",
                         "X-Robots-Tag": ROBOTS_TAG})
            await page(scope, receive, send)
            return
        try:
            await self.app(scope, receive, tagged)
        finally:
            limits.leave(ip)


def _local(value: datetime) -> datetime:
    """Момент из базы (timestamptz приходит в UTC) - в часовом поясе
    контейнера (TZ в compose), как его ждёт оператор."""
    return value.astimezone() if value.tzinfo else value


def _dmy(value: Any) -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        return _local(value).strftime("%d.%m.%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    return str(value)


def _file_exists(path: str) -> bool:
    return os.path.exists(path)


def _csv(filename: str, header: list[str], rows: list[list[Any]], *,
         note: str | None = None) -> Response:
    """CSV для Excel: BOM, точка с запятой, десятичная запятая.

    `note` - строка над шапкой (пометка демо): её видно в любой программе,
    которой откроют файл."""
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    if note:
        writer.writerow([note])
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# Символы, с которых Excel и LibreOffice начинают формулу. Имя клиента
# приходит из бота как набрал человек: «=HYPERLINK(...)» в ФИО превратил бы
# выгрузку в фишинговую ссылку у оператора. Такие строки отдаются как
# формула-строка ="...": таблица показывает текст и ничего не вычисляет.
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:.2f}".replace(".", ",")
    if isinstance(value, datetime):
        return _local(value).strftime("%d.%m.%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    text = str(value)
    if text.startswith(_FORMULA_STARTS):
        return '="' + text.replace('"', '""') + '"'
    return text


def _xlsx(filename: str, header: list[str], rows: list[list[Any]], *,
          note: str | None = None) -> Response:
    """Тот же набор строк, но настоящей таблицей Excel.

    CSV Excel открывает по-разному в зависимости от настроек локали, и
    суммы в нём - текст: выгрузку приходится доводить руками. Здесь числа
    остаются числами, даты датами, шапка закреплена - файл открывают и
    сразу считают.

    Защиты от формул тут не нужно: значение уезжает ячейкой своего типа,
    и строка, начинающаяся с «=», лежит строкой - openpyxl не делает из
    неё формулу.

    `note` (пометка демо) - первой строкой над шапкой, именем листа и в
    свойствах файла: таблицу пересылают без страницы, с которой скачали.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    book = Workbook()
    sheet = book.active
    sheet.title = "Демо" if note else "Выгрузка"
    top = 1
    if note:
        sheet.append([note])
        sheet["A1"].font = Font(bold=True, color="1F4E8C")
        if len(header) > 1:
            sheet.merge_cells(start_row=1, start_column=1, end_row=1,
                              end_column=len(header))
        book.properties.title = book.properties.subject = note
        top = 2
    sheet.append(list(header))
    for cell in sheet[top]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in rows:
        sheet.append([_xlsx_cell(v) for v in row])
    # Шапка не уезжает при прокрутке: в выгрузке парка 190 строк.
    sheet.freeze_panes = f"A{top + 1}"
    widths = [len(str(h)) for h in header]
    for row in rows:
        for i, value in enumerate(row[:len(widths)]):
            widths[i] = max(widths[i], len(_cell(value)))
    for i, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 9), 42)
    for column in sheet.iter_cols(min_row=top + 1):
        for cell in column:
            if isinstance(cell.value, (int, float)):
                cell.number_format = "# ##0.00" if isinstance(cell.value, float) \
                    else "# ##0"
            elif isinstance(cell.value, datetime):
                cell.number_format = "DD.MM.YYYY HH:MM"
            elif isinstance(cell.value, date):
                cell.number_format = "DD.MM.YYYY"
            elif isinstance(cell.value, str) and cell.value.startswith("="):
                # openpyxl по первому символу решает, что это формула.
                # Имя клиента приходит из бота как набрал человек, и
                # «=HYPERLINK(…)» в ФИО превратило бы выгрузку в ссылку
                # у оператора. Говорим явно: это строка.
                cell.data_type = "s"
    buf = io.BytesIO()
    book.save(buf)
    return Response(
        buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument."
                   "spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _xlsx_cell(value: Any) -> Any:
    """Значение как есть, чтобы Excel считал его числом или датой."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        # Excel не понимает часовой пояс: приводим к местному и снимаем его.
        return _local(value).replace(tzinfo=None)
    if isinstance(value, (int, float, date)):
        return value
    return str(value)


# В каких видах отдаются выгрузки. xlsx - рабочий: числа остаются
# числами и сумму можно поставить сразу. csv оставлен для тех, кто
# грузит выгрузку во что-то своё.
EXPORT_FORMATS = ("xlsx", "csv")


def _table(fmt: str, stem: str, header: list[str], rows: list[list[Any]], *,
           note: str | None = None, limit: int | None = None) -> Response:
    """Одна выгрузка в двух видах. Неизвестное расширение - 404.

    Молча отдать csv на запрос `.pdf` значит соврать в имени файла, и
    оператор откроет его один раз, а потом перестанет доверять выгрузке.

    `note` - пометка над шапкой (демо), `limit` - сколько строк отдать
    (демо); обрезанная выгрузка говорит об этом в той же пометке.
    """
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(status_code=404)
    if limit is not None and len(rows) > limit:
        rows = rows[:limit]
        note = f"{note or ''} Показаны первые {limit} строк.".strip()
    if fmt == "xlsx":
        return _xlsx(f"{stem}.xlsx", header, rows, note=note)
    return _csv(f"{stem}.csv", header, rows, note=note)


def _iso(value: Any) -> str:
    return value.strftime("%Y-%m-%d") if isinstance(value, date) else ""


def static_stamp(folder: Path) -> str:
    """Отпечаток статики: им помечены ссылки на style.css и fonts.css.

    Без метки браузер держит старую таблицу стилей после обновления:
    `StaticFiles` не шлёт `Cache-Control`, и браузер кэширует файл по
    своему усмотрению - на часы. Панель после деплоя выглядела сломанной
    (новая разметка со старыми стилями), а лечилось это только
    Ctrl+Shift+R, о котором оператору знать неоткуда.

    Считается один раз на старте по именам, размерам и времени правки:
    читать файлы целиком ради восьми знаков незачем.
    """
    parts = []
    for item in sorted(folder.rglob("*")):
        if item.is_file():
            stat = item.stat()
            parts.append(f"{item.name}:{stat.st_size}:{int(stat.st_mtime)}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:8]


class CachedStatic(StaticFiles):
    """Статика с честным сроком жизни в кэше.

    Год - только тем адресам, где есть метка сборки (`?v=`): такой файл
    по этому адресу уже не изменится, а новый придёт по новому адресу.
    Остальным - `no-cache`: это шрифты, на которые ссылается сам
    `fonts.css` без метки, и подменённый файл должен подхватиться сразу.
    Проверка стоит один запрос и отвечает 304.

    Без заголовков вовсе браузер решает сам и держит вчерашний
    `style.css` часами: панель после обновления выглядит сломанной.
    """

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code != 200:
            return response
        stamped = b"v=" in dict(scope).get("query_string", b"")
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if stamped else "no-cache")
        return response


# Значок на главный экран: файлы рисует app/web/icons.py. Цвет - шапки
# панели на телефоне: полоса браузера и заставка сливаются с ней.
MANIFEST_ICONS = (("icon-192.png", "192x192", "any"), ("icon-512.png", "512x512", "any"),
                  ("icon-maskable-512.png", "512x512", "maskable"))
THEME_COLOR = "#211F1D"


def app_names(title: str) -> tuple[str, str]:
    """Имя приложения на телефоне: полное и под значком. «МАЙБАЙК» -
    «МАЙБАЙК CRM» и «МАЙБАЙК»; хвост после « · » (демо) остаётся в обоих:
    демо, поставленное на экран рядом с боевой панелью, не должно
    выглядеть как она."""
    head, sep, tail = title.partition(" · ")
    return f"{head} CRM{sep}{tail}", f"{head} {tail}".strip()


def web_manifest(title: str, stamp: str) -> dict:
    """Манифест для «На экран Домой». Service worker нет намеренно: на
    страницах телефоны и долги клиентов, и копия страницы в кэше
    телефона пережила бы и выход из панели, и увольнение сотрудника."""
    name, short = app_names(title)
    return {
        "id": "/", "name": name, "short_name": short, "lang": "ru",
        "description": "Прокат: клиенты, аренды, парк и деньги",
        "start_url": "/", "scope": "/", "display": "standalone",
        "theme_color": THEME_COLOR, "background_color": THEME_COLOR,
        "icons": [{"src": f"/static/{file}?v={stamp}", "sizes": sizes,
                   "type": "image/png", "purpose": purpose}
                  for file, sizes, purpose in MANIFEST_ICONS],
    }


def create_app(*, crm: Any, db: Any, cfg: WebConfig, bot: Any = None) -> FastAPI:
    app = FastAPI(title=cfg.title, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", CachedStatic(directory=str(HERE / "static")),
              name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    static_v = static_stamp(HERE / "static")
    # Версия копии для франчайзера (/hook/metrics) и сравнения с ним.
    code_v = franchise.code_stamp()
    templates.env.globals.update(
        static_v=static_v, app_short=app_names(cfg.title)[1], THEME_COLOR=THEME_COLOR,
        # «Скоро платёж» подсвечивается с того же дня, с которого бот шлёт
        # «истекает через N дней», а не с зашитых двух.
        REMIND_BEFORE_DAYS=cfg.remind_before_days,
        money=logic.money, money_signed=logic.money_signed, period_label=logic.period_label,
        per_day=logic.per_day,
        KINDS=logic.KINDS, METHODS=logic.METHODS, BIKE_STATUSES=logic.BIKE_STATUSES,
        BIKE_MANUAL_STATUSES=logic.BIKE_MANUAL_STATUSES,
        OPERATIONAL_STATUSES=logic.OPERATIONAL_STATUSES, IDLE_STATUSES=logic.IDLE_STATUSES,
        REPAIR_NODES=logic.REPAIR_NODES,
        TRACKER_ALERTS=logic.TRACKER_ALERTS, TRACK_RANGES=logic.TRACK_RANGES,
        SIGN_STATUSES=logic.SIGN_STATUSES, SIGN_EVENTS=logic.SIGN_EVENTS,
        SIGN_DOC_KINDS=logic.SIGN_DOC_KINDS,
        SIGN_CODE_MINUTES=logic.SIGN_CODE_MINUTES,
        SIGN_LINK_DAYS=logic.SIGN_LINK_DAYS,
        TARIFF_KINDS=logic.TARIFF_KINDS, EXTRA_KINDS=logic.EXTRA_KINDS,
        MAX_EXTRA_BATTERIES=logic.MAX_EXTRA_BATTERIES,
        AUDIENCES=logic.AUDIENCES, CAMPAIGN_STATUSES=logic.CAMPAIGN_STATUSES,
        SEND_STATUSES=logic.SEND_STATUSES, SEND_CHANNELS=logic.SEND_CHANNELS,
        TEMPLATE_FIELDS=logic.TEMPLATE_FIELDS,
        CASH_MOVE_KINDS=logic.CASH_MOVE_KINDS, CASH_STATUSES=logic.CASH_STATUSES,
        CASH_DIFF_NOISE=logic.CASH_DIFF_NOISE,
        BANK_STATUSES=logic.BANK_STATUSES, MATCH_REASONS=logic.MATCH_REASONS,
        PAY_STATUSES=logic.PAY_STATUSES, PAY_KINDS=logic.PAY_KINDS,
        NOTICES=logic.NOTICES, NOTICE_GROUPS=logic.NOTICE_GROUPS,
        DOC_TEMPLATES=logic.DOC_TEMPLATES, COMPANY_MARKS=logic.COMPANY_MARKS,
        BIKE_PASSPORT=logic.BIKE_PASSPORT, TAKE_WHAT=logic.TAKE_WHAT,
        ALERT_LEVELS=logic.ALERT_LEVELS, ALERT_STATES=logic.ALERT_STATES,
        ALERT_SNOOZE_HOURS=logic.ALERT_SNOOZE_HOURS,
        BATTERY_PASSPORT=logic.BATTERY_PASSPORT,
        STOCK_STALE_DAYS=logic.STOCK_STALE_DAYS,
        LIST_SIZES=logic.LIST_SIZES,
        NOTICE_TARGETS=logic.NOTICE_TARGETS,
        NOTICE_STATUSES=logic.NOTICE_STATUSES,
        NOTICE_LOG_DAYS=logic.NOTICE_LOG_DAYS,
        PAY_METHODS=logic.PAY_METHODS, card_title=logic.card_title,
        AUTOCHARGE_HOUR=logic.AUTOCHARGE_HOUR,
        map_url=logic.map_url,
        BATTERY_STATUSES=logic.BATTERY_STATUSES,
        BATTERY_MANUAL_STATUSES=logic.BATTERY_MANUAL_STATUSES,
        BATTERY_CYCLES_WARN=logic.BATTERY_CYCLES_WARN,
        BATTERY_WEAR_REASONS=logic.BATTERY_WEAR_REASONS,
        IDLE_TARGET_PERCENT=logic.IDLE_TARGET_PERCENT, CHECK_TARGET=logic.CHECK_TARGET,
        NO_POINT_TITLE=logic.NO_POINT_TITLE,
        TARIFF_MIN_FINISHED=logic.TARIFF_MIN_FINISHED, TARIFF_MIN_DAYS=logic.TARIFF_MIN_DAYS,
        NO_RENTAL_TITLE=logic.NO_RENTAL_TITLE, BUY_PERIOD_DAYS=logic.BUY_PERIOD_DAYS,
        BUY_MIN_DAYS=logic.BUY_MIN_DAYS, BUY_VERDICTS=logic.BUY_VERDICTS,
        amortization_month=logic.amortization_month, fleet_losses=logic.fleet_losses,
        ridden=logic.ridden, ridden_per_day=logic.ridden_per_day,
        INTENTS=logic.INTENTS,
        CLIENT_STATUSES=logic.CLIENT_STATUSES, RENTAL_STATUSES=logic.RENTAL_STATUSES,
        CLIENT_GROUPS=logic.CLIENT_GROUPS,
        RISK_LEVELS=logic.RISK_LEVELS,
        BILLING=logic.BILLING, role_title=logic.role_title,
        role_summary=logic.role_summary, app_title=cfg.title,
        ORDER_STATUSES=logic.ORDER_STATUSES, PAYERS=logic.PAYERS,
        ORDER_MANUAL_STATUSES=logic.ORDER_MANUAL_STATUSES,
        ORDER_OPEN=logic.ORDER_OPEN,
        WORK_CATEGORIES=logic.WORK_CATEGORIES, ORDER_STUCK_DAYS=logic.ORDER_STUCK_DAYS,
        PRICE_SHEETS=logic.PRICE_SHEETS, TRACKER_COMMANDS=logic.TRACKER_COMMANDS,
        BLOCKABLE_ALERTS=logic.BLOCKABLE_ALERTS,
        TAKE_SCOPES=logic.TAKE_SCOPES, TAKE_STATES=logic.TAKE_STATES,
        REF_STATUSES=logic.REF_STATUSES, staff_tg_label=logic.staff_tg_label,
        BONUS_KINDS=logic.BONUS_KINDS, REVIEW_SITES=logic.REVIEW_SITES,
        feedback_stars=logic.feedback_stars, feedback_avg=logic.feedback_avg,
        FEEDBACK_SCORES=logic.FEEDBACK_SCORES, FEEDBACK_CHANNELS=logic.FEEDBACK_CHANNELS,
        FEEDBACK_COMMENT_KEEP_DAYS=logic.FEEDBACK_COMMENT_KEEP_DAYS,
        RETURN_PHOTOS_MAX=logic.RETURN_PHOTOS_MAX,
        BOOKING_STATUSES=logic.BOOKING_STATUSES, booking_line=logic.booking_line,
        PROMO_KINDS=logic.PROMO_KINDS, PROMO_PARAM_LABELS=logic.PROMO_PARAM_LABELS,
        PROMO_TEXT_FIELDS=logic.PROMO_TEXT_FIELDS,
        promo_discount_label=logic.promo_discount_label,
        promo_params=logic.promo_params,
        promo_scope=logic.promo_scope, promo_scope_label=logic.promo_scope_label,
        promo_new_only=logic.promo_new_only,
        IDLE_PROMO_LENGTH=logic.IDLE_PROMO_LENGTH,
        COMPANY_FIELDS=company.COMPANY_FIELDS,
        CONTACT_FIELDS=company.CONTACT_FIELDS,
        CLIENT_CHANNELS=logic.CLIENT_CHANNELS, channel_label=logic.channel_label,
        EMPLOYERS=logic.EMPLOYERS, EXPERIENCE=logic.EXPERIENCE,
        MOVE_KINDS=logic.MOVE_KINDS, DOC_KINDS=logic.DOC_KINDS,
        SWAP_REASONS=logic.SWAP_REASONS, in_search=logic.in_search,
        OPS_KINDS=logic.OPS_KINDS, ops_report_summary=logic.ops_report_summary,
        INBOX_CHANNELS=logic.INBOX_CHANNELS, INBOX_STATUSES=logic.INBOX_STATUSES,
        INBOX_KINDS=logic.INBOX_KINDS, INBOX_OUT_STATUSES=logic.INBOX_OUT_STATUSES,
        INBOX_KEEP_DAYS=logic.INBOX_KEEP_DAYS,
        search_days=logic.search_days,
        PART_ORDER_STATUSES=logic.PART_ORDER_STATUSES,
        NEED_SOURCES=logic.NEED_SOURCES, PART_UNITS=logic.PART_UNITS,
        INTEGRITY_KINDS=logic.INTEGRITY_KINDS, DEBT_NOISE=logic.DEBT_NOISE,
        take_title=logic.take_title,
        SECTIONS=logic.SECTIONS, ACTIONS=logic.ACTIONS, LEVELS=logic.LEVELS,
        LEVEL_ORDER=logic.LEVEL_ORDER, can_view=logic.can_view, can_edit=logic.can_edit,
        can_act=logic.can_act, visible_sections=logic.visible_sections,
        home_for=logic.home_for,
        today=date.today, timedelta=timedelta, bot_enabled=bot is not None,
        RENTAL_BACKDATE_DAYS=logic.RENTAL_BACKDATE_DAYS,
        RENTAL_AHEAD_DAYS=logic.RENTAL_AHEAD_DAYS,
        demo=cfg.demo, DEMO_LOGINS=DEMO_LOGINS,
        LEARN_TRACKS=learning.TRACKS,
        STAFF_TERMS=logic.STAFF_TERMS, staff_expired=logic.staff_expired,
        # Одноразовый ключ денежной формы: двойной клик по «Принять»
        # записывал два платежа и слал клиенту два «зачислено».
        once=lambda: secrets.token_urlsafe(12),
    )
    templates.env.filters["dmy"] = _dmy
    templates.env.filters["iso"] = _iso

    # ─────────────────────── обвязка ───────────────────────

    def render(request: Request, name: str, status_code: int = 200, **ctx: Any) -> Response:
        messages = list(request.session.get("flash") or [])
        if messages:
            request.session["flash"] = []
        staff = getattr(request.state, "staff", None)
        ctx.update(staff=staff, flash=messages,
                   learn=getattr(request.state, "learn", None))
        if staff is not None:
            # Меню и крошки - из одного дерева (app/web/nav.py): права те же,
            # что у стража маршрутов, счётчики собрал страж на входе.
            path = request.url.path
            ctx.update(nav_menu=nav.menu(staff, path,
                                         getattr(request.state, "nav_counts", None)),
                       crumbs=nav.trail(path, staff))
        page = templates.TemplateResponse(request, name, ctx, status_code=status_code)
        # Страницы панели не кэшируются вовсе: на них баланс клиента, его
        # телефон и статус аренды, а кнопка «назад» после выхода не должна
        # показывать чужую карточку из памяти браузера. Заодно после
        # обновления панели не остаётся вчерашней разметки.
        page.headers["Cache-Control"] = "no-store"
        return page

    def flash(request: Request, text: str, kind: str = "ok") -> None:
        # Присваивание, а не append: сессия Starlette пишет cookie только
        # при изменении своих ключей, правку вложенного списка она не видит.
        request.session["flash"] = [*(request.session.get("flash") or []), [kind, text]]

    def redirect(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    async def table(fmt: str, stem: str, header: list[str],
                    rows: list[list[Any]]) -> Response:
        """Выгрузка (_table) в потоке: openpyxl на тысячах строк - секунды
        процессора, и в цикле событий они заморозили бы панель всем
        остальным. В демо файл помечен и строк в нём не больше
        DEMO_EXPORT_ROWS."""
        return await asyncio.to_thread(
            _table, fmt, stem, header, rows,
            note=DEMO_EXPORT_NOTE if cfg.demo else None,
            limit=DEMO_EXPORT_ROWS if cfg.demo else None)

    def who(request: Request) -> str:
        staff = getattr(request.state, "staff", None)
        return f"staff:{staff['login']}" if staff else "staff:?"

    async def learn_state(request: Request, staff: dict) -> dict:
        """Прогресс ученика для карточки обучения (app/crm/learning.py).

        В сессии - шаги, отмеченные на прошлой странице: выполненные с тех
        пор показываются строкой «Готово». Сессии без отметки (вход с
        другого устройства) не показывают ничего: иначе новичок увидел бы
        разом весь прежний прогресс как новость.
        """
        track = learning.track_of(staff) or ""
        state = learning.progress(
            track, await crm.learn_facts(learning.actor(staff), int(staff["id"])))
        codes = learning.done_codes(state["steps"])
        seen = request.session.get("learn_seen")
        state["fresh"] = [] if seen is None else learning.fresh(state["steps"], seen)
        if seen != codes:
            request.session["learn_seen"] = codes
        if state["finished"] and not request.session.get("learn_done_at"):
            request.session["learn_done_at"] = datetime.now(UTC).isoformat()
        done_at = request.session.get("learn_done_at") if state["finished"] else None
        state.update(login=staff["login"], started_at=staff.get("created_at"),
                     done_at=datetime.fromisoformat(done_at) if done_at else None)
        return state

    def may_view(request: Request, code: str) -> bool:
        return logic.can_view(getattr(request.state, "staff", None), code)

    def may_edit(request: Request, code: str) -> bool:
        return logic.can_edit(getattr(request.state, "staff", None), code)

    def denied(request: Request, code: str) -> Response:
        """Отказ показывается страницей, а не голым 403: оператор должен
        увидеть, какого права ему не хватает, и кому писать."""
        return render(request, "denied.html", status_code=403,
                      what=logic.SECTIONS.get(code) or logic.ACTIONS.get(code, code))

    public = PUBLIC + DEMO_PUBLIC if cfg.demo else PUBLIC
    nav_cache = nav.CountsCache()

    async def nav_counts() -> dict[str, Any]:
        """Счётчики меню из кэша процесса. Сбой - меню без чисел, но
        страница открывается: число в меню не стоит упавшей страницы."""
        cached = nav_cache.fresh()
        if cached is not None:
            return cached
        generation = nav_cache.generation
        try:
            value = await nav.gather_counts(crm, today=date.today())
        except Exception:                                   # noqa: BLE001
            log.exception("счётчики меню не собрались")
            value = {}
        return nav_cache.put(value, generation=generation)

    async def auth(request: Request, call_next: Any) -> Response:
        request.state.staff = None
        staff_id = request.session.get("staff_id")
        if staff_id:
            staff = await crm.staff_by_id(int(staff_id))
            # Пароль сменили - сессии, открытые со старым, больше не
            # действуют: cookie подписан, но сам по себе живёт две недели.
            # Срок доступа прошёл - сессия умирает на следующем же запросе,
            # а не через две недели жизни cookie.
            if (staff and staff.get("active") and not logic.staff_expired(staff)
                    and request.session.get("pw")
                    == logic.session_mark(staff.get("password_hash"))):
                request.state.staff = staff
        path = request.url.path
        if request.state.staff is None and not path.startswith(public):
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return secured(redirect("/login?next=" + quote(target, safe="")))
        request.state.learn = None
        if (request.state.staff is not None and request.method == "GET"
                and learning.track_of(request.state.staff)
                and not path.startswith(("/static", "/healthz", "/manifest"))):
            request.state.learn = await learn_state(request, request.state.staff)
        if cfg.demo and demo_blocked(request.method, path):
            flash(request, DEMO_BLOCKED_TEXT, "err")
            return secured(redirect(same_origin_back(request)))
        # Один страж на все маршруты раздела: забыть его в новом обработчике
        # нельзя, поэтому дыры вида «страницу закрыли, а POST оставили» не
        # появляются. Свой пароль и выход открыты всегда.
        code = logic.section_for(path) if path not in ALWAYS_OPEN else None
        if code and request.state.staff is not None:
            allowed = (may_view(request, code) if request.method in ("GET", "HEAD")
                       else may_edit(request, code))
            if not allowed:
                return secured(denied(request, code))
        # Номер длиннее bigint: записи с ним нет, а база ответила бы 500.
        if request.state.staff is not None and not logic.path_ids_ok(path):
            return secured(render(request, "missing.html", status_code=404, what="Адрес"))
        request.state.nav_counts = None
        if (request.state.staff is not None and request.method == "GET"
                and nav.is_page(path)):
            request.state.nav_counts = await nav_counts()
        response = await call_next(request)
        if request.method not in ("GET", "HEAD"):
            # Запись могла сдвинуть число в меню: следующая страница
            # посчитает заново, а не покажет старое ещё полминуты.
            nav_cache.drop()
        return secured(response)

    @app.exception_handler(RequestValidationError)
    async def not_a_number(request: Request, exc: RequestValidationError) -> Response:
        """Номер в пути - не число («/clients/²», «/clients/abc»): это адрес
        без записи, 404 страницей, а не JSON 422 голым текстом. Клиенту на
        странице подписи - своя страница, без панели вокруг."""
        if all((err.get("loc") or ("",))[0] == "path" for err in exc.errors()):
            if request.url.path.startswith("/sign/"):
                return render(request, "sign_missing.html", status_code=404)
            if getattr(request.state, "staff", None) is not None:
                return render(request, "missing.html", status_code=404, what="Адрес")
        return await request_validation_exception_handler(request, exc)

    def secured(response: Response) -> Response:
        """Заголовки, которые браузер обязан соблюдать на каждой странице.

        Панель не встраивается в чужие сайты (подложенная поверх кнопка
        «Зачислить»); ссылка подписи с токеном не уходит в Referer на
        внешние сайты; тип файла не угадывается по содержимому. HSTS -
        только за доменом: по голому адресу сервера https нет вовсе.
        """
        headers = response.headers
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Content-Security-Policy",
                           "frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "same-origin")
        if cfg.trust_proxy:
            headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    def same_origin_back(request: Request) -> str:
        """Куда вернуть после отказа демо: страница, с которой пришли, если
        она своя, иначе сводка. Чужой Referer - не адрес для редиректа."""
        ref = urlsplit(request.headers.get("referer") or "")
        if not ref.netloc or ref.netloc != request.url.netloc:
            return "/"
        return logic.safe_next(ref.path + (f"?{ref.query}" if ref.query else ""), "/")

    # Порядок важен: последний add_middleware - внешний. Сессия должна быть
    # распакована ДО проверки входа, поэтому SessionMiddleware добавляется
    # после auth.
    app.add_middleware(BaseHTTPMiddleware, dispatch=auth)
    # https_only под доменом: в профиле https Caddy слушает и 80, и 443,
    # и адрес панели, набранный без схемы, отправил бы cookie сессии
    # открытым текстом ещё до редиректа. Без домена (панель по адресу
    # сервера, по http) флаг Secure сделал бы вход невозможным.
    # У демо своё имя cookie: cookie не различают порты, и боевая панель и
    # демо через SSH-туннель (localhost:8080 и :8081) выбивали бы друг друга.
    app.add_middleware(SessionMiddleware, secret_key=cfg.secret,
                       session_cookie="crm_demo" if cfg.demo else "crm_session",
                       same_site="strict", https_only=bool(cfg.trust_proxy),
                       max_age=SESSION_DAYS * 24 * 3600)
    # Флаг сброса демо: его переключает app.demo, читает DemoGate. Там же
    # предел запросов с адреса - тесты, которым он мешает, снимают его.
    app.state.maintenance = False
    app.state.demo_limits = DemoLimits() if cfg.demo else None
    if cfg.demo:
        app.add_middleware(DemoGate, state=app.state)
    app.add_middleware(NoNul)
    # Самый внешний слой: слишком большое тело отсекается раньше всего.
    # В демо загрузок нет вовсе - и широких путей тоже.
    app.add_middleware(BodyLimit, limit=DEMO_BODY_MAX if cfg.demo else BODY_MAX,
                       wide=() if cfg.demo else WIDE_BODY_PATHS)

    login_failures: dict[str, list[float]] = {}
    # Использованные ключи денежных форм. Процесс панели один, и проверка
    # с записью идут без await между ними - двойной клик второй раз не
    # пройдёт. Ключи старше часа выбрасываются: форма столько не живёт.
    used_once: dict[str, float] = {}

    def form_once(data: dict) -> bool:
        """False - эту форму уже отправляли. Форма без ключа (старая
        вкладка) пропускается: она ничем не хуже, чем была до ключа."""
        key = str(data.get("once") or "")[:64]
        if not key:
            return True
        now = time.monotonic()
        if len(used_once) > LOGIN_KEYS_SWEEP:
            for stale in [k for k, t in used_once.items() if now - t > 3600]:
                used_once.pop(stale, None)
        if key in used_once:
            return False
        used_once[key] = now
        return True

    def form_once_release(data: dict) -> None:
        """Форму отклонили - её ключ снова годен: F5 на странице отказа
        повторяет POST с тем же ключом, и без этого неотправленный ответ
        назывался бы «уже отправлен»."""
        used_once.pop(str(data.get("once") or "")[:64], None)

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "?"

    def login_throttled(key: str, limit: int) -> bool:
        now = time.monotonic()
        if len(login_failures) > LOGIN_KEYS_SWEEP:
            # Ключи - логины, которые выбирает атакующий: без чистки словарь
            # рос бы бесконечно. Стираются те, у кого окно уже истекло.
            for stale in [k for k, ts in login_failures.items()
                          if not ts or now - ts[-1] >= LOGIN_WINDOW]:
                login_failures.pop(stale, None)
        recent = [t for t in login_failures.get(key, ()) if now - t < LOGIN_WINDOW]
        if recent:
            login_failures[key] = recent
        else:
            login_failures.pop(key, None)
        return len(recent) >= limit

    async def form(request: Request) -> dict[str, str]:
        """Поля формы строками. Нулевой байт вырезается здесь, один раз на
        всю панель: Postgres не хранит \\x00 в тексте и отвечал на него
        ошибкой - то есть 500 на любом поле, куда его вписали."""
        data = await request.form()
        return {k: (v.replace("\x00", "") if isinstance(v, str) else "")
                for k, v in data.items()}

    async def form_ids(request: Request, name: str) -> list[int]:
        """Отмеченные галочками номера: form() оставляет только последний."""
        data = await request.form()
        return [i for v in data.getlist(name) if (i := logic.parse_id(v)) is not None]

    async def by_id(getter: Any, raw: Any) -> dict | None:
        """Запись по номеру из адреса или формы; не номер - None, как нет
        записи. parse_id, а не isdigit: «²» для isdigit - цифра, и int()
        на нём ронял страницу 500; «٢» int() молча читал как 2."""
        record_id = logic.parse_id(raw)
        return None if record_id is None else await getter(record_id)

    def cost_field(data: dict, name: str) -> logic.Check:
        """Стоимость в форме: пусто и «0» - ноль (запчастей не было, работа
        своя), иначе обычная проверка суммы."""
        raw = (data.get(name) or "").strip().replace(",", ".")
        if raw in ("", "0", "0.0", "0.00"):
            return logic.Check(True, Decimal(0))
        return logic.check_amount(raw)

    def count_field(data: dict, name: str, *, what: str, default: str = "1",
                    limit: int = 999, least: int = 0) -> logic.Check:
        """Небольшое целое из формы: количество в наряде, минуты норматива.

        `least` поднимает нижнюю границу там, где ноль бессмысленен:
        строка наряда на ноль штук и списание со склада на ноль штук -
        это не «бесплатно», это опечатка.
        """
        raw = (data.get(name) or "").strip() or default
        # parse_id: isdigit пропускал «²», и int() ронял форму 500.
        value = logic.parse_id(raw)
        if value is None or not least <= value <= limit:
            return logic.Check(False,
                               error=f"{what}: целое число от {least} до {limit}.")
        return logic.Check(True, value)

    def summarize(rental: dict | None, balance: Any) -> dict:
        return logic.rental_summary(rental, balance, today=date.today())

    async def history_floor(today: date) -> date:
        """Первый месяц истории: стрелка «прошлый месяц» сводки, сервиса и
        отчёта по точкам дальше него не ведёт."""
        return logic.history_floor(await crm.history_start(), today=today)

    async def location_names(*current: Any) -> list[str]:
        """Точки для выпадающих списков и проверок форм - один источник на
        всю панель: действующие точки справочника (sort, name) плюс текущие
        значения карточки, если точка закрыта (logic.point_choices).
        Шаблоны и проверки logic.LOCATIONS напрямую не читают: иначе
        третью точку можно завести, а поставить на неё велосипед - нет.
        """
        try:
            places = await crm.locations()
        except Exception:                                # noqa: BLE001
            places = []
        return logic.point_choices(places, *current)

    async def filter_points(current: Any = "") -> list[str]:
        """Точки для фильтра списка: действующие, за ними закрытые. Новую
        карточку на закрытую точку не ставят, а найти её аренды, наряды и
        оставшиеся на ней велосипеды надо по-прежнему. «none» - не точка,
        а «без точки», его шаблон добавляет сам."""
        try:
            places = await crm.locations()
        except Exception:                                # noqa: BLE001
            places = []
        closed = [p["name"] for p in places if p.get("active") is False]
        return logic.point_choices(places, *closed,
                                   "" if current == "none" else current)

    # ─────────────────────── вход ───────────────────────

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/manifest.webmanifest")
    async def manifest() -> Response:
        # Собирается по CRM_TITLE, а не лежит файлом: у демо на значке своё
        # имя. no-cache - как у статики без метки: сменили имя или значки,
        # телефон увидит это при следующем открытии.
        return JSONResponse(web_manifest(cfg.title, static_v),
                            media_type="application/manifest+json",
                            headers={"Cache-Control": "no-cache"})

    # Пульс процесса бота для внешнего монитора: упавший бот сам о себе не
    # напишет, а панель видит, что его круг проверки сервера замолчал.
    # 503 без подробностей - адрес открыт всем, как и /healthz. У демо
    # бота нет вовсе, и адреса нет тоже.
    if not cfg.demo:
        @app.get("/healthz/bot")
        async def healthz_bot() -> Response:
            state = logic.parse_health_state((await crm.settings()).get(logic.HEALTH_KEY))
            alive = logic.bot_alive(state, datetime.now(UTC))
            checked = state["checked_at"]
            return JSONResponse({"ok": alive,
                                 "checked_at": checked.isoformat() if checked else None},
                                status_code=200 if alive else 503)

    if cfg.demo:
        @app.get("/robots.txt")
        async def robots() -> Response:
            return PlainTextResponse(ROBOTS_TXT)

    @app.get("/login")
    async def login_form(request: Request) -> Response:
        if request.state.staff is not None:
            return redirect("/")
        return render(request, "login.html", next=request.query_params.get("next") or "/")

    @app.post("/login")
    async def login(request: Request) -> Response:
        data = await form(request)
        name = (data.get("login") or "").strip().lower()[:64]
        login_key = "login:" + name
        # Адрес - последним, после «|»: в адресе этого знака нет, и
        # выдуманный логин не склеится с чужой парой.
        pair_key = f"pair:{name}|{client_ip(request)}"
        ip_key = "ip:" + client_ip(request)
        # В демо логин общий на всех: десять чужих ошибок заперли бы его
        # каждому посетителю. Остаётся предел на адрес - против перебора.
        login_limited = not cfg.demo and (
            login_throttled(pair_key, LOGIN_LIMIT)
            or login_throttled(login_key, LOGIN_ACCOUNT_LIMIT))
        if login_limited or login_throttled(ip_key, LOGIN_IP_LIMIT):
            return render(request, "login.html", status_code=429,
                          error="Слишком много попыток входа. Подождите 15 минут.",
                          next=data.get("next") or "/")
        login_check = logic.check_login(data.get("login"))
        staff = await crm.staff_by_login(login_check.value) if login_check.ok else None
        # scrypt - десятки миллисекунд процессора: в потоке, иначе поток
        # входов (в демо пароль известен всем) останавливал бы панель. И
        # всегда, даже без логина: по быстрому отказу видно, что его нет.
        stored = (staff or {}).get("password_hash") or DECOY_PASSWORD_HASH
        password_ok = await asyncio.to_thread(logic.verify_password,
                                              data.get("password") or "", stored)
        if staff is None or not staff.get("active") or not password_ok:
            for key in (ip_key,) if cfg.demo else (pair_key, login_key, ip_key):
                login_failures.setdefault(key, []).append(time.monotonic())
            return render(request, "login.html", status_code=401,
                          error="Неверный логин или пароль.",
                          next=data.get("next") or "/")
        login_failures.pop(pair_key, None)
        if logic.staff_expired(staff):
            # Только после верного пароля: иначе форма входа отвечала бы
            # на вопрос «есть ли такой логин» кому угодно.
            return render(request, "login.html", status_code=403,
                          error="Срок доступа истёк. Продлить его может владелец "
                                "в «Сотрудниках».", next=data.get("next") or "/")
        request.session.clear()
        request.session["staff_id"] = staff["id"]
        request.session["pw"] = logic.session_mark(staff.get("password_hash"))
        # Ученик возвращается к своим шагам, а не на сводку.
        home = "/learn" if learning.track_of(staff) else logic.home_for(staff)
        target = logic.safe_next(data.get("next") or home, home)
        if target == "/" and home != "/":
            target = home
        # Свежая установка: владельца вместо сводки встречает мастер первого
        # запуска. Только вместо сводки - ссылка, по которой пришли, главнее.
        if target == "/" and await setup_wanted(staff) is not None:
            target = "/setup"
        return redirect(target)

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        request.session.clear()
        return redirect("/login")

    @app.get("/me")
    async def my_page(request: Request) -> Response:
        """Свой кабинет: пароль и список выданных прав. Открыт всем —
        сотрудник должен видеть, что ему разрешено, не спрашивая владельца."""
        return render(request, "me.html")

    @app.post("/me/password")
    async def my_password(request: Request) -> Response:
        data = await form(request)
        staff = request.state.staff
        if not await asyncio.to_thread(logic.verify_password, data.get("old") or "",
                                       staff.get("password_hash")):
            flash(request, "Текущий пароль неверный.", "err")
            return redirect("/me")
        check = logic.check_password(data.get("new"))
        if not check.ok:
            flash(request, check.error, "err")
            return redirect("/me")
        new_hash = await asyncio.to_thread(logic.hash_password, check.value)
        await crm.set_staff_password(staff["id"], new_hash)
        # Свои прочие сессии (чужой ноутбук, забытый вход) выбиты, эта - нет.
        request.session["pw"] = logic.session_mark(new_hash)
        flash(request, "Пароль изменён.")
        return redirect("/me")

    # ─────────────────────── обучение ───────────────────────
    #
    # Личный учебный вход на демо-стенде (app/crm/learning.py). Боевая
    # панель страницу /learn тоже показывает - что в обучении и где оно
    # живёт, - но учебных входов не заводит: её база настоящая.

    learn_starts: dict[str, list[float]] = {}

    @app.get("/learn")
    async def learn_page(request: Request) -> Response:
        return render(request, "learn.html", state=getattr(request.state, "learn", None),
                      demo_url=cfg.demo_url)

    @app.post("/learn/start")
    async def learn_start(request: Request) -> Response:
        """Завести учебного сотрудника маршрута и сразу впустить его.

        Только в демо: там адрес открыт без входа (DEMO_PUBLIC), а база
        вымышленная. Пароль показывается один раз - flash на первой
        странице; в базе он, как у всех, только хэшем.
        """
        if not cfg.demo:
            return render(request, "missing.html", status_code=404, what="Адрес")
        # Адрес открыт без входа, и форма с чужого сайта заводила бы
        # учебные входы руками его посетителей - с тысяч адресов, мимо
        # предела на адрес, пока стенд не заполнится до ночи. Браузер
        # пишет Origin у каждого POST с чужой страницы.
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.url.netloc:
            return PlainTextResponse("Обучение начинается со страницы входа демо.",
                                     status_code=403)
        data = await form(request)
        spec = learning.TRACKS.get(data.get("track") or "")
        if spec is None:
            flash(request, "Выберите, чему учиться: администратор точки или мастер.",
                  "err")
            return redirect("/login")
        ip, now = client_ip(request), time.monotonic()
        if len(learn_starts) > LOGIN_KEYS_SWEEP:
            for stale in [k for k, ts in learn_starts.items()
                          if not ts or now - ts[-1] > 86400]:
                learn_starts.pop(stale, None)
        recent = [t for t in learn_starts.get(ip, []) if now - t < 86400]
        full = len(recent) >= learning.PER_ADDRESS
        if not full:
            # Место занимается сразу, до первого await: параллельные запросы
            # с того же адреса иначе видели бы один и тот же старый счёт и
            # все проходили предел. Не завели вход - место возвращается.
            learn_starts[ip] = [*recent, now]

        def release() -> None:
            left = learn_starts.get(ip, [])
            if now in left:
                left.remove(now)

        if full or await crm.learn_count() >= learning.PER_DAY:
            if not full:
                release()
            flash(request, "Новых учебных входов сегодня больше не будет — войдите "
                           "под прежним или вернитесь завтра.", "err")
            return redirect("/login")
        profile = await crm.access_profile_by_code(spec.profile)
        if profile is None:
            release()
            flash(request, "Обучение на этом стенде не настроено.", "err")
            return redirect("/login")
        places = [p["name"] for p in await crm.locations() if p.get("active")]
        password = "".join(secrets.choice(learning.PASSWORD_ALPHABET)
                           for _ in range(learning.PASSWORD_LEN))
        password_hash = await asyncio.to_thread(logic.hash_password, password)
        staff_id = login = None
        for _ in range(5):
            number = 10000 + secrets.randbelow(90000)
            login = f"{learning.LOGIN_PREFIX}{number}"
            if await crm.staff_by_login(login) is not None:
                continue
            try:
                staff_id = await crm.create_staff(
                    login, password_hash, learning.trainee_name(number),
                    "manager", profile["id"], location=places[0] if places else None)
            except Exception as exc:                    # noqa: BLE001
                # Два новичка вытянули одно число разом: следующая попытка.
                if "unique" not in type(exc).__name__.lower():
                    raise
                continue
            break
        if staff_id is None:
            release()
            flash(request, "Не удалось завести учебный вход — нажмите ещё раз.", "err")
            return redirect("/login")
        # Свой велосипед на своей точке: свободных в демо с десяток, и без
        # него выдача и наряд упирались бы в чужие аренды и посетителей.
        # Не завёлся - обучение идёт на свободных из парка.
        try:
            await crm.create_bike(
                by=f"staff:{login}", code=learning.kit_code(login or ""),
                model=learning.kit_model(await crm.tariffs(active_only=True),
                                         await crm.bike_models()),
                status="available", location=places[0] if places else None,
                mileage_km=learning.KIT_MILEAGE, note=learning.KIT_NOTE)
        except Exception:                               # noqa: BLE001
            log.warning("учебный велосипед для %s не заведён", login, exc_info=True)
        request.session.clear()
        request.session["staff_id"] = staff_id
        request.session["pw"] = logic.session_mark(password_hash)
        request.session["learn_seen"] = []
        flash(request, f"Ваш учебный вход: логин {login}, пароль {password}. Запишите "
                       "его, если будете продолжать с другого устройства: он "
                       "работает до ночного обновления демо.")
        return redirect("/learn")

    # ─────────────────────── мои задачи ───────────────────────

    @app.get("/my")
    async def my_tasks_page(request: Request) -> Response:
        """Задачи этого сотрудника на сегодня (app/crm/mytasks.py): наряды
        по технику, аренды, заявки и тревоги - по его точке. Открыта всем:
        раздела у неё нет, а группа - тому, кто по ней действует
        (mytasks). Источник читается, только если его группа будет видна."""
        staff = request.state.staff
        today = date.today()
        settings = await crm.settings()
        expiring: list[dict] = []
        search = None
        if may_edit(request, "rentals"):
            rentals = await crm.active_rentals()
            rows = [{**r, "summary": summarize(r, r.get("balance", 0))} for r in rentals]
            expiring = logic.expiring(rows, today=today,
                                      before_days=cfg.remind_before_days)
            search = logic.search_rows(rentals, settings=logic.search_settings(settings),
                                       today=today)
        point = staff.get("location") or None
        shift_open = None
        if point and may_edit(request, "cash"):
            shift_open = await crm.open_shift_at(point) is not None
        groups = mytasks.my_tasks(
            staff, expiring=expiring, search=search,
            orders=(await crm.work_orders(open_only=True, limit=500)
                    if may_view(request, "service") else ()),
            bookings=(await crm.bookings(status="new")
                      if may_edit(request, "issue") else ()),
            alerts=(await crm.tracker_alerts(open_only=True, limit=500)
                    if may_view(request, "trackers") else ()),
            claims=await crm.pending_claims() if may_edit(request, "claims") else (),
            shift_open=shift_open, today=today,
            repair_norm=logic.repair_norm_default(settings),
            booking_url=booking_issue_url)
        return render(request, "my.html", groups=groups, point=point, today=today)

    # ─────────────────────── дашборд ───────────────────────

    async def network_plan(settings: dict, counts: dict[str, int],
                           places: list[dict] | None = None) -> dict:
        """План месяца сети - один путь для сводки и сервиса.

        Общий план не задан, а у каждой открытой точки свой - план сети
        это их сумма (logic.month_plan, source «points»). Без справочника
        точек сервис брал умолчание от парка, и норма «у клиента» на двух
        экранах расходилась.
        """
        if places is None:
            places = await crm.locations()
        return logic.month_plan(settings, places=places, fleet=sum(
            counts.get(code, 0) for code in logic.OPERATIONAL_STATUSES))

    @app.get("/")
    async def dashboard(request: Request) -> Response:
        rentals = await crm.active_rentals()
        rows = []
        for r in rentals:
            s = summarize(r, r.get("balance", 0))
            rows.append({**r, "summary": s})
        today = date.today()
        expiring = logic.expiring(rows, today=today, before_days=cfg.remind_before_days)
        bikes_by = await crm.bike_counts()
        fleet = await crm.bikes(limit=10000)
        own_batteries = await crm.batteries(limit=10000)
        # Одно окно на три числа и блок «По точкам»: итог точек обязан
        # совпасть с плитками над ним, а два вызова now() разошлись бы.
        window_end = datetime.now().astimezone()
        window_start = window_end - timedelta(days=logic.POINTS_PERIOD_DAYS)
        metrics = await period_metrics(since=window_start, until=window_end)
        settings = await crm.settings()
        places = await crm.locations()
        operational = sum(bikes_by.get(s, 0) for s in logic.OPERATIONAL_STATUSES)
        plan = await network_plan(settings, bikes_by, places)
        # Месяц листается стрелками: прошлый - целиком, текущий - по
        # сегодняшний день, вперёд листать некуда, назад - до начала истории.
        span = logic.month_bounds(
            logic.month_from(request.query_params.get("month"), today=today),
            today=today, floor=await history_floor(today))
        first, next_month = span["first"], span["next"]
        month_metrics = await period_metrics(
            since=datetime.combine(first, datetime.min.time()).astimezone(),
            until=(datetime.now().astimezone() if span["is_current"] else
                   datetime.combine(next_month, datetime.min.time()).astimezone()))
        soon = logic.freeing_soon(rows, today=today)
        # Плитки денег - за ТОТ ЖЕ месяц, что и всё остальное на сводке.
        # Раньше здесь стояло первое число сегодняшнего месяца без верхней
        # границы: оператор листал стрелкой на август, а плитки над планом
        # продолжали показывать сентябрь.
        month_totals = await crm.ledger_totals(since=first, until=span["last"])
        # Деньги по дням месяца: столбики «пришло», линия накопленного
        # долга и пунктир плана в день. Помесячных чисел мало - по ним
        # не видно, в какой день всё пошло не так.
        chart = logic.money_chart(
            await crm.money_by_day(first, span["last"]),
            plan_per_day=plan["per_day"], today=span["today"])
        # Задачи на сегодня: один список поверх виджетов. Каждый источник
        # читается тем же запросом, что и его раздел, - список не вправе
        # показывать не то, что покажет раздел.
        bookings = await crm.bookings(status="new")
        # Переброска и простой - та же выборка, что в отчёте по точкам и на
        # странице точки: строка задачи ведёт туда, где то же самое.
        advice = await point_advice(places, fleet, rentals=rows, bookings=bookings,
                                    settings=settings)
        tasks = logic.today_tasks(
            expiring=expiring,
            search=logic.search_rows(rentals, settings=logic.search_settings(settings),
                                     today=today),
            orders=await crm.work_orders(open_only=True, limit=500),
            claims=await crm.pending_claims(),
            bookings=bookings,
            alerts=await crm.tracker_alerts(open_only=True, limit=500),
            transfers=(advice["transfer"] or {}).get("moves", ()),
            idle=advice["idle"], today=today,
            repair_norm=logic.repair_norm_default(settings))
        return render(request, "dashboard.html",
                      tasks=tasks, setup=await setup_wanted(request.state.staff, settings),
                      inbox_waiting=(await crm.inbox_open_count()
                                     if may_view(request, "inbox") else None),
                      plan=plan, span=span, bot_state=await bot_health(),
                      progress=logic.plan_progress(
                          plan, month_metrics,
                          days_in_month=span["days"],
                          days_passed=span["passed"]),
                      soon=soon,
                      counts=await crm.counts(), bikes=bikes_by,
                      operational=operational,
                      tiles=logic.fleet_tiles(
                          bikes_by, plan,
                          spare=sum(1 for b in fleet if b.get("spare")
                                    and b.get("status") in logic.OPERATIONAL_STATUSES)),
                      metrics=metrics, losses=logic.fleet_losses(metrics),
                      loss_today=logic.loss_per_day(bikes_by),
                      # Кто именно стоит и почём: список с деньгами -
                      # это решение, а плитка «в ремонте 7» - только повод
                      # сходить в сервис и посмотреть.
                      standing=await standing_bikes(fleet),
                      amortization=logic.amortization_total(fleet, own_batteries),
                      idle_by_location=idle_by_location(
                          fleet, logic.point_choices(places)),
                      # Сравнение точек - когда есть что сравнивать: с одной
                      # точкой блок повторял бы три числа над ним.
                      points=(await points_report(places, fleet, window_start,
                                                  window_end)
                              if sum(1 for p in places if p.get("active")) > 1
                              else None),
                      claims=await crm.pending_claims(), rentals=rows,
                      # Сводка - самые срочные: при полутора сотнях аренд на
                      # «ближайшие два дня» приходится десятки строк, и блок
                      # уезжал на весь экран. Весь список - по ссылке.
                      expiring=(expiring if request.query_params.get("expiring") == "all"
                                else expiring[:EXPIRING_SHOWN]),
                      expiring_total=len(expiring), before_days=cfg.remind_before_days,
                      forecast=logic.forecast_summary(bikes_by.get("available", 0), soon),
                      debtors=await crm.debtors(10),
                      month=month_totals, chart=chart,
                      chart_total=logic.cumulative(chart["days"]),
                      chart_view=request.query_params.get("chart") or "days",
                      # Доля баллов от оплат: «0,1 %» - это скидка,
                      # «20 %» - уже бизнес-модель, и это видно сразу.
                      bonus_share=logic.bonus_totals(
                          [{"kind": "all", "amount": month_totals.get("bonus", 0)}],
                          month_totals.get("payment", 0))["share"])

    # ─────────────────── инструменты списков ───────────────────

    def list_tools(request: Request, rows: list[dict], *,
                   allowed: dict[str, str]) -> dict:
        """Сортировка, страница и подвал - одинаково для всех списков."""
        p = request.query_params
        sort = p.get("sort") or ""
        direction = p.get("dir") or "asc"
        ordered = logic.sort_rows(rows, sort, direction, allowed=allowed) \
            if sort in allowed else list(rows)
        page = logic.page_of(ordered, logic.check_list_size(p.get("rows")),
                             p.get("page"))
        # Ссылки заголовков и подвала собираются из адреса БЕЗ своего же
        # параметра: иначе каждый клик дописывал бы ещё один sort=…&dir=…,
        # адрес рос без конца, и «Мои фильтры» сохраняли бы весь этот хвост.
        return {**page, "all_rows": ordered, "sort": sort, "dir": direction,
                "query": clean_query(request, drop=("page",)),
                "q_sort": clean_query(request, drop=("page", "sort", "dir")),
                "q_rows": clean_query(request, drop=("page", "rows"))}

    def clean_query(request: Request, *, drop: tuple[str, ...] = ()) -> str:
        """Строка запроса без указанных параметров - для ссылок сортировки."""
        # Повтор ключа (старые ссылки с хвостом sort=…&sort=…) схлопывается
        # до последнего значения - его же читает и сервер.
        keep = {k: v for k, v in request.query_params.multi_items() if k not in drop}
        return "&".join(f"{k}={quote(str(v), safe='')}" for k, v in keep.items() if v != "")

    async def views_of(request: Request, section: str) -> list[dict]:
        """Свои фильтры этого списка. Чужие не показываются."""
        staff = getattr(request.state, "staff", None) or {}
        if not staff.get("id"):
            return []
        return await crm.saved_views(int(staff["id"]), section)

    @app.post("/views")
    async def view_save(request: Request) -> Response:
        """Сохранить текущий набор фильтров под именем."""
        staff = getattr(request.state, "staff", None) or {}
        data = await form(request)
        section = str(data.get("section") or "")
        back = section + (("?" + str(data.get("query") or ""))
                          if data.get("query") else "")
        if not staff.get("id") or logic.safe_next(section, "") != section:
            return redirect("/")
        name = logic.check_name(data.get("name"), what="Название фильтра")
        if not name.ok:
            flash(request, name.error, "err")
            return redirect(back)
        await crm.save_view(staff_id=int(staff["id"]), section=section,
                            name=name.value, query=str(data.get("query") or ""))
        flash(request, f"Фильтр «{name.value}» сохранён.")
        return redirect(back)

    @app.post("/views/{view_id}/delete")
    async def view_delete(request: Request, view_id: int) -> Response:
        staff = getattr(request.state, "staff", None) or {}
        data = await form(request)
        view = await crm.saved_view(view_id)
        back = str(view["section"]) if view else "/"
        if not staff.get("id") or not await crm.drop_saved_view(
                view_id, staff_id=int(staff["id"])):
            flash(request, "Такого фильтра у вас нет.", "err")
            return redirect(back)
        del data
        flash(request, "Фильтр убран.")
        return redirect(back)

    async def standing_bikes(fleet: list[dict], limit: int = 5) -> list[dict]:
        """Велосипеды, которые стоят дольше всех, с ценой простоя."""
        since = await crm.bike_status_since()
        now = datetime.now(UTC)
        rows = []
        for bike in fleet:
            if bike.get("status") not in logic.IDLE_STATUSES:
                continue
            days = logic.idle_days(since.get(bike["id"]), now=now) or 0
            rows.append({**bike, "idle_days": days, "lost": logic.idle_cost(days)})
        rows.sort(key=lambda b: (-b["idle_days"], str(b.get("code") or "")))
        return rows[:limit]

    @app.post("/plan")
    async def plan_save(request: Request) -> Response:
        """План месяца: сколько велосипедов держать в аренде и по какому чеку.

        Умолчания считаются от парка и целей, поэтому план правится, а не
        придумывается с нуля.
        """
        if not may_edit(request, "finance"):
            return denied(request, "finance")
        data = await form(request)
        # Пусто - «общий план не задан»: тогда он сумма планов точек (если
        # они есть у каждой открытой точки), иначе от парка. Раньше пустое
        # поле сохранялось нулём, и план в ноль велосипедов вытеснить было
        # нечем.
        rented_raw = (data.get("plan_rented") or "").strip()
        rented = (count_field(data, "plan_rented", what="Велосипедов в аренде",
                              limit=9999) if rented_raw else logic.Check(True, ""))
        check = cost_field(data, "plan_check")
        repair = count_field(data, "plan_repair", what="Норма ремонта",
                             default="0", limit=9999)
        spare = count_field(data, "plan_spare", what="Норма подменных",
                            default="0", limit=9999)
        free = count_field(data, "plan_free", what="Норма свободных",
                           default="0", limit=9999)
        for field in (rented, check, repair, spare, free):
            if not field.ok:
                flash(request, field.error, "err")
                return redirect("/")
        await crm.set_setting("plan_rented", str(rented.value), by=who(request))
        await crm.set_setting("plan_check", str(check.value), by=who(request))
        await crm.set_setting("plan_repair", str(repair.value), by=who(request))
        await crm.set_setting("plan_spare", str(spare.value), by=who(request))
        await crm.set_setting("plan_free", str(free.value), by=who(request))
        flash(request, "План на месяц сохранён.")
        return redirect("/")

    @app.post("/plan/points/{location_id}")
    async def point_plan_save(request: Request, location_id: int) -> Response:
        """План месяца точки: велосипедов в аренде на ней и чек. Право то же,
        что у общего плана (финансы): это деньги, а не справочник. Лежит в
        строке точки, поэтому переименование его не теряет. Пустое поле -
        «не задано»: без велосипедов у точки плана нет, без чека - общий."""
        if not may_edit(request, "finance"):
            return denied(request, "finance")
        place = next((p for p in await crm.locations() if p["id"] == location_id), None)
        if place is None:
            return render(request, "missing.html", status_code=404, what="Точка")
        back = f"/reports/points/{location_id}"
        data = await form(request)
        rented = (count_field(data, "plan_rented", what="Велосипедов в аренде",
                              limit=9999)
                  if (data.get("plan_rented") or "").strip() else logic.Check(True, None))
        check = cost_field(data, "plan_check")
        for field in (rented, check):
            if not field.ok:
                flash(request, field.error, "err")
                return redirect(back)
        await crm.update_location(location_id, plan_rented=rented.value,
                                  plan_check=check.value or None)
        flash(request, f"План точки «{place['name']}» сохранён."
              if rented.value is not None else
              f"План точки «{place['name']}» снят.")
        return redirect(back)

    async def tell_parts_arrived(orders: list[dict]) -> None:
        """В служебный чат: пришла запчасть, которую ждал наряд."""
        if not orders or bot is None or not cfg.contract_chat_id:
            return
        lines = ["📦 Пришла запчасть — наряды могут ехать дальше:"]
        # Экранирование: заметка об объекте - свободный текст оператора,
        # и «Самокат <Ninebot>» Telegram отверг бы вместе со всей сводкой.
        lines += [bot_logic.esc(f"• {o.get('no')} — "
                                f"{o.get('bike_code') or o.get('object_note') or '—'}")
                  for o in orders[:10]]
        # send_team знает про получателя, назначенного владельцем в панели.
        await notices.send_team(crm, bot, "part_arrived", "\n".join(lines),
                                cfg.contract_chat_id)

    async def referral_bonus(client: dict, amount: Decimal, by: str) -> None:
        """Друг заплатил - начислить бонус агенту и сказать ему об этом.

        Зовётся после каждого платежа клиента: платёж может прийти из
        панели, из заявки и из выдачи, а бонус обязан начисляться один раз
        и одинаково.
        """
        try:
            bonus = await service.ref_paid(crm, client, amount, by=by)
        except Exception:                                # noqa: BLE001
            log.exception("реферальный бонус за клиента %s не начислен",
                          client.get("id"))
            return
        if bonus:
            await notify.referral_bonus(bot, db, bonus["agent"], client,
                                        bonus["bonus"])

    async def period_metrics(*, days: int = 0, since: datetime | None = None,
                             until: datetime | None = None) -> dict:
        """Три числа за период: простой, средний чек, дни. По умолчанию -
        последние `days` дней до текущего момента."""
        now = datetime.now().astimezone()
        until = until or now
        since = since or (until - timedelta(days=days))
        return logic.fleet_metrics(await crm.bike_days_by_status(since, until),
                                   await crm.rental_revenue(since, until))

    async def points_report(places: list[dict], fleet: list[dict], since: datetime,
                            until: datetime, *, full: bool = False) -> dict:
        """Сравнение точек за [since, until): logic.points_rows поверх
        ответов *_by_location. Одна функция на сводку, отчёт и страницу
        точки - иначе числа одной точки на трёх экранах разошлись бы.
        Сводке нужны только три числа (full=False): аренды, долг, касса и
        сервис - лишние запросы на каждое её открытие."""
        extra: dict[str, Any] = {}
        if full:
            extra = {"rentals": await crm.rentals_by_location(since, until),
                     "debt": await crm.debt_by_location(),
                     "cash": await crm.cash_by_location(since, until),
                     "service": await crm.service_by_location(since, until)}
        return logic.points_rows(
            places, bikes=fleet,
            days=await crm.bike_days_by_location(since, until),
            money=await crm.money_by_location(since, until), **extra)

    def idle_by_location(fleet: list[dict], names: list[str]) -> list[dict]:
        """Где стоят простаивающие велосипеды: по точкам, свободные отдельно
        от ремонта, чтобы было видно, что выдавать нечего, а что чинить.
        Порядок - как в справочнике (names), чужие имена и «не на точке» -
        следом."""
        out: dict[str, dict] = {}
        for b in fleet:
            if b.get("status") not in logic.IDLE_STATUSES:
                continue
            row = out.setdefault(b.get("location") or "не на точке",
                                 {"location": b.get("location") or "не на точке",
                                  "free": 0, "repair": 0})
            if b["status"] in ("available", "reserved"):
                row["free"] += 1
            else:
                row["repair"] += 1
        order = {loc: i for i, loc in enumerate(names)}
        return sorted(out.values(),
                      key=lambda r: (order.get(r["location"], len(order)), r["location"]))

    async def point_advice(places: list[dict], fleet: list[dict], *,
                           rentals: list[dict] | None = None,
                           bookings: list[dict] | None = None,
                           settings: dict | None = None,
                           with_transfer: bool = True) -> dict[str, Any]:
        """Переброска между точками и скидка на простаивающие - одна выборка
        на сводку, отчёт по точкам и страницу точки: иначе три экрана
        советовали бы разное. Переброска - когда точек больше одной;
        странице точки она не нужна (with_transfer=False)."""
        today = date.today()
        now = datetime.now(UTC)
        if settings is None:
            settings = await crm.settings()
        names = logic.point_choices(places)
        aliases = logic.model_aliases(await crm.bike_models())
        since = await crm.bike_status_since()
        transfer = None
        if with_transfer and len(names) > 1:
            transfer = logic.transfer_plan(
                points=names, bikes=fleet,
                rentals=rentals if rentals is not None else await crm.active_rentals(),
                history=await crm.issues_by_day(
                    today - timedelta(days=7 * logic.TRANSFER_WEEKS), today),
                bookings=(bookings if bookings is not None
                          else await crm.bookings(status="new")),
                today=today, safety=logic.transfer_settings(settings)["safety"],
                aliases=aliases,
                idle={int(b["id"]): logic.idle_days(since.get(b["id"]), now=now)
                      for b in fleet})
        idle = logic.idle_promo_settings(settings)
        # Простой на точке - и от переезда: привезённый переброской стоит
        # на новой точке с приезда, а не со смены статуса.
        return {"transfer": transfer, "idle_settings": idle,
                "idle": logic.idle_promo_rows(
                    logic.idle_models(fleet, since=since, now=now, days=idle["days"],
                                      moved=await crm.bike_location_since(),
                                      points=names, aliases=aliases),
                    await crm.promos(active_only=True), today=today)}

    @app.post("/billing/run")
    async def billing_run(request: Request) -> Response:
        applied: list[dict] = []
        try:
            done = await service.charge_all(crm, today=date.today(), applied=applied)
        except service.ChargeError as exc:
            # Часть аренд начислена, часть ждёт: сказать, какие, и не
            # прятать сделанное.
            done = exc.done
            flash(request, str(exc), "err")
        # Скидки по акциям легли вместе с начислениями - клиентам о них
        # говорит тот, кто начислил, иначе дневной проход их уже не увидит.
        told = await billing.tell_promos(bot, db, crm, applied)
        flash(request, f"Начислений сделано: {done}."
                       + (f" Скидок по акциям: {len(applied)}, уведомлений: {told}."
                          if applied else ""))
        return redirect("/")

    # ─────────────────────── клиенты ───────────────────────

    async def clients_by_risk(rows: list[dict], risk: str) -> list[dict]:
        """Фильтр списка по уровню риска: оценка считается только когда
        фильтр выбран - на каждом открытии списка она не нужна."""
        if risk not in logic.RISK_LEVELS:
            return rows
        risks = await service.client_risks(crm, [r["id"] for r in rows],
                                           today=date.today())
        return [{**r, "risk": risks[r["id"]]} for r in rows
                if r["id"] in risks and risks[r["id"]]["level"] == risk]

    # Колонки сортировки списка клиентов; денежные - только с «Финансами».
    CLIENT_SORTS = {"name": "full_name", "rentals": "rentals_count", "days": "rented_days",
                    "since": "first_on", "last": "last_on", "paid": "paid_total",
                    "balance": "balance"}
    CLIENT_MONEY_SORTS = ("paid", "balance")

    async def clients_found(request: Request) -> dict:
        """Клиенты за всё время: плитки по всей базе и строки выбранной
        группы с поиском и риском - одно на страницу и её выгрузку."""
        p = request.query_params
        q = p.get("q") or ""
        status = p.get("status") or ""
        risk = p.get("risk") or ""
        risk = risk if risk in logic.RISK_LEVELS else ""
        # Должники - это деньги: без «Финансов» группы нет (как и сортировки
        # по балансу), и ?group=debt - это просто все.
        groups = client_groups(request)
        group = p.get("group") if p.get("group") in groups else "all"
        everyone = await crm.clients(limit=100000)
        found = (everyone if not (q or status) else
                 await crm.clients(q=q or None, status=status or None, limit=100000))
        found = [r for r in found if logic.client_in_group(r, group)]
        # С фильтром риска - по всей группе: иначе первые по алфавиту молча
        # прятали бы рискованных с фамилией на «Я».
        found = await clients_by_risk(found, risk)
        return {"q": q, "status": status, "risk": risk, "group": group,
                "tiles": logic.client_tiles(everyone), "found": found}

    def client_groups(request: Request) -> dict[str, str]:
        if may_view(request, "finance"):
            return logic.CLIENT_GROUPS
        return {k: v for k, v in logic.CLIENT_GROUPS.items() if k != "debt"}

    @app.get("/clients")
    async def clients(request: Request) -> Response:
        """Сводка всех клиентов за всё время и действующих: плитки по группам
        (действующие, бывшие, ни разу не брали, должники), вкладка группы и
        по каждому - сколько аренд, дней с велосипедом, когда был, сколько
        заплатил. Рубли - только с правом на «Финансы»."""
        data = await clients_found(request)
        allowed = (CLIENT_SORTS if may_view(request, "finance") else
                   {k: v for k, v in CLIENT_SORTS.items() if k not in CLIENT_MONEY_SORTS})
        tools = list_tools(request, data["found"], allowed=allowed)
        for r in tools["rows"]:
            rental = {"status": "active", "billed_until": r["billed_until"],
                      "price": r["price"], "period_days": r["period_days"],
                      "tariff_name": r["tariff_name"], "bike_model": r.get("bike_model"),
                      "bike_code": r.get("bike_code")} if r.get("rental_id") else None
            r["summary"] = summarize(rental, r.get("balance", 0))
        return render(request, "clients.html", rows=tools["rows"], tools=tools,
                      q=data["q"], status=data["status"], risk=data["risk"],
                      group=data["group"], tiles=data["tiles"],
                      groups=client_groups(request),
                      views=await views_of(request, "/clients"))

    @app.get("/clients.{ext}")
    async def clients_csv(request: Request, ext: str) -> Response:
        # В выгрузке колонка «Баланс»: она уезжает файлом, поэтому право
        # на финансы обязательно - на самой странице баланс тоже скрыт.
        if not may_view(request, "finance"):
            return denied(request, "finance")
        data = await clients_found(request)
        found = data["found"]
        # Выгружают то, что видят: та же группа, поиск и фильтр риска, что
        # у страницы, и уровень колонкой - оценка всё равно посчитана.
        if not data["risk"]:
            risks = await service.client_risks(crm, [c["id"] for c in found],
                                               today=date.today())
            found = [{**c, "risk": risks.get(c["id"])} for c in found]
        rows = []
        for c in found:
            rental = ({"status": "active", "billed_until": c["billed_until"],
                       "price": c["price"], "period_days": c["period_days"]}
                      if c.get("rental_id") else None)
            s = summarize(rental, c.get("balance", 0))
            rows.append([c["full_name"], c["phone"], logic.CLIENT_STATUSES.get(c["status"]),
                         ("@" + c["username"]) if c.get("username") else
                         ("есть" if c.get("tg_id") else ""),
                         logic.to_money(c.get("balance", 0)), c.get("bike_code"),
                         c.get("tariff_name"), s.get("covered_until"),
                         c.get("rentals_count") or 0, c.get("rented_days") or 0,
                         c.get("first_on"), c.get("last_on"),
                         logic.to_money(c.get("paid_total") or 0),
                         c.get("contract_no"), c.get("created_at"),
                         (c.get("risk") or {}).get("label")])
        return await table(ext, "clients",
                    ["ФИО", "Телефон", "Статус", "Telegram", "Баланс", "Велосипед",
                     "Тариф", "Оплачено до", "Аренд", "Дней с велосипедом",
                     "Первая аренда", "Последний день", "Оплатил за всё время",
                     "Договор", "Добавлен", "Риск"], rows)

    @app.get("/clients/new")
    async def client_new(request: Request) -> Response:
        # «Завести нового» - правка раздела, а не просмотр: страж судит
        # по методу запроса и такую страницу пропустил бы.
        if not may_edit(request, "clients"):
            return denied(request, "clients")
        return render(request, "client_form.html", client=None)

    async def _client_fields(request: Request, data: dict, *, current: dict | None) -> dict | None:
        name = logic.check_name(data.get("full_name"), what="ФИО")
        phone = bot_logic.normalize_phone(data.get("phone"))
        note = logic.check_note(data.get("note"))
        status = logic.check_choice(data.get("status") or "active", logic.CLIENT_STATUSES,
                                    what="Статус")
        contract = logic.check_name(data.get("contract_no"), what="Договор") \
            if (data.get("contract_no") or "").strip() else logic.Check(True, None)
        channel = logic.check_channel(data.get("channel"))
        employer = logic.check_employer(data.get("employer"))
        experience = logic.check_experience(data.get("experience"))
        for check in (name, note, status, contract, channel, employer, experience):
            if not check.ok:
                flash(request, check.error, "err")
                return None
        if phone is None:
            flash(request, "Телефон: не похоже на номер. Пример: +7 900 123-45-67.", "err")
            return None
        if cfg.demo and not phone.startswith(DEMO_PHONE_PREFIX):
            flash(request, DEMO_PHONE_TEXT, "err")
            return None
        # Запасные телефоны: необязательны, но если вписаны - это номера.
        spare: dict[str, str | None] = {}
        for key in ("phone2", "phone3"):
            raw = (data.get(key) or "").strip()
            if not raw:
                spare[key] = None
                continue
            normal = bot_logic.normalize_phone(raw)
            if normal is None:
                flash(request, f"Запасной телефон «{raw}»: не похоже на номер.", "err")
                return None
            if cfg.demo and not normal.startswith(DEMO_PHONE_PREFIX):
                flash(request, DEMO_PHONE_TEXT, "err")
                return None
            spare[key] = normal
        other = await crm.client_by_phone(phone)
        if other is not None and (current is None or other["id"] != current["id"]):
            flash(request, f"Этот телефон уже у клиента «{other['full_name']}».", "err")
            return None
        # MAX-аккаунт руками: мост из MAX-бота проставляет его сам, но
        # мост поднят не у всех, а рассылке нужен адрес получателя.
        raw_max = (data.get("max_id") or "").strip()
        max_id = logic.parse_id(raw_max)
        if raw_max and max_id is None:
            flash(request, "MAX id: только цифры, как в кабинете MAX.", "err")
            return None
        fields = {"full_name": name.value, "phone": phone, "note": note.value,
                  "status": status.value, "contract_no": contract.value,
                  "channel": channel.value, **spare,
                  "employer": employer.value, "experience": experience.value}
        if current is not None:
            fields["max_id"] = max_id
        return fields

    @app.post("/clients")
    async def client_create(request: Request) -> Response:
        data = await form(request)
        fields = await _client_fields(request, data, current=None)
        if fields is None:
            return redirect("/clients/new")
        client_id = await crm.create_client(full_name=fields["full_name"],
                                            phone=fields["phone"], note=fields["note"],
                                            contract_no=fields["contract_no"],
                                            created_by=who(request))
        patch = {key: fields[key] for key in ("channel",) if fields.get(key)}
        if fields["status"] != "active":
            patch["status"] = fields["status"]
        if patch:
            await crm.update_client(client_id, **patch)
        flash(request, "Клиент добавлен.")
        return redirect(f"/clients/{client_id}")

    @app.get("/clients/{client_id}")
    async def client_card(request: Request, client_id: int) -> Response:
        client = await crm.client(client_id)
        if client is None:
            return render(request, "missing.html", status_code=404, what="Клиент")
        balance = await crm.client_balance(client_id)
        rental = await crm.active_rental_of(client_id)
        bot_user = await db.get_user(client["tg_id"]) if client.get("tg_id") else None
        bot_user = dict(bot_user) if bot_user else None
        has_contract = bool(bot_user and bot_user.get("contract_status") == "signed"
                            and bot_user.get("contract_path"))
        return render(request, "client.html", client=client, balance=balance,
                      presets=logic.fine_presets(await crm.work_types(active_only=True)),
                      rental=rental, summary=summarize(rental, balance),
                      risk=await service.client_risk(crm, client_id, today=date.today()),
                      ledger=await crm.ledger_of(client_id, 100),
                      rentals=await crm.client_rentals(client_id),
                      claim=await crm.pending_claim_of(client_id),
                      bot_user=bot_user, has_contract=has_contract,
                      signings=await crm.sign_requests(client_id=client_id,
                                                       limit=20),
                      # Подсказка суммы счёта - ровно долг: чаще всего
                      # выставляют его, и набирать заново незачем.
                      pay_hint=(str(-logic.to_money(balance))
                                if logic.to_money(balance) < 0 else ""),
                      pay_orders=await crm.pay_orders(client_id=client_id,
                                                      limit=10),
                      bonuses=await crm.bonuses(client_id=client_id, limit=20),
                      feedback=await crm.client_feedback(client_id, limit=20))

    @app.post("/clients/{client_id}/edit")
    async def client_edit(request: Request, client_id: int) -> Response:
        client = await crm.client(client_id)
        if client is None:
            return render(request, "missing.html", status_code=404, what="Клиент")
        data = await form(request)
        fields = await _client_fields(request, data, current=client)
        if fields is not None:
            try:
                await crm.update_client(client_id, **fields)
            except Exception as exc:                    # noqa: BLE001
                if "unique" in type(exc).__name__.lower():
                    flash(request, "Этот MAX-аккаунт уже привязан к другому "
                                   "клиенту.", "err")
                    return redirect(f"/clients/{client_id}")
                raise
            flash(request, "Карточка сохранена.")
        return redirect(f"/clients/{client_id}")

    @app.post("/clients/{client_id}/ledger")
    async def client_ledger_add(request: Request, client_id: int) -> Response:
        if not logic.can_act(request.state.staff, "money_edit"):
            return denied(request, "money_edit")
        client = await crm.client(client_id)
        if client is None:
            return render(request, "missing.html", status_code=404, what="Клиент")
        data = await form(request)
        if not form_once(data):
            flash(request, "Эта запись уже сделана — повторное нажатие пропущено.", "err")
            return redirect(logic.safe_next(data.get("next"), f"/clients/{client_id}"))
        kind = logic.check_choice(data.get("kind"), ("payment", "fine", "refund", "adjust"),
                                  what="Вид записи")
        # Позиция прайса арендатора: подсказывает сумму и заметку. Только
        # для штрафа - «Потеря аккумулятора» платежом это описка, а не
        # запись, которую стоит принять.
        preset = None
        if logic.parse_id(data.get("preset")) is not None:
            preset = await by_id(crm.work_type, data.get("preset"))
            if preset is None or logic.sheet_price(preset, "own") is None:
                flash(request, "Такой позиции в прайсе нет.", "err")
                return redirect(f"/clients/{client_id}")
            if data.get("kind") != "fine":
                flash(request, "Позиция прайса - это штраф или ремонт: "
                               "выберите вид «Штраф / ремонт».", "err")
                return redirect(f"/clients/{client_id}")
            if not (data.get("amount") or "").strip():
                data["amount"] = str(logic.sheet_price(preset, "own"))
            if not (data.get("note") or "").strip():
                data["note"] = preset["title"]
        amount = logic.check_amount(data.get("amount"),
                                    allow_negative=data.get("kind") == "adjust")
        note = logic.check_note(data.get("note"))
        method = data.get("method") or None
        if method and not logic.check_choice(method, logic.METHODS).ok:
            method = None
        for check in (kind, amount, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/clients/{client_id}")
        rental = await crm.active_rental_of(client_id)
        await service.add_entry(crm, client, kind=kind.value, amount=amount.value,
                                method=method, note=note.value, by=who(request),
                                rental_id=rental["id"] if rental else None)
        if kind.value == "payment":
            await notices.send_client(
                crm, "pay_credited", client["id"],
                lambda: notify.payment_credited(bot, db, crm, client,
                                                amount.value))
            await referral_bonus(client, amount.value, who(request))
        flash(request, "Запись добавлена.")
        # С карточки аренды платёж принимают, не уходя с неё.
        return redirect(logic.safe_next(data.get("next"), f"/clients/{client_id}"))

    @app.get("/clients/{client_id}/contract")
    async def client_contract(request: Request, client_id: int) -> Response:
        # В договоре паспортные данные: право на него отдельное от карточки.
        if not logic.can_act(request.state.staff, "client_docs"):
            return denied(request, "client_docs")
        client = await crm.client(client_id)
        if client is None or not client.get("tg_id"):
            return render(request, "missing.html", status_code=404, what="Договор")
        row = await db.get_user(client["tg_id"])
        data = dict(row) if row else {}
        path = data.get("contract_path")
        if not path or not bot_logic.is_safe_store_path(path, cfg.storage_dir) \
                or not _file_exists(path):
            return render(request, "missing.html", status_code=404, what="Договор")
        return FileResponse(path, filename=Path(path).name)

    # ─────────────────────── парк ───────────────────────

    # Колонки, по которым можно сортировать список. Белым списком, а не
    # именем поля из адреса: имя поля из запроса - это чужая строка.
    BIKE_SORTS = {"code": "code", "model": "model", "status": "status",
                  "location": "location", "mileage": "mileage_km",
                  "client": "full_name", "idle": "idle_days",
                  "frame": "frame_no", "motor": "motor_no"}

    @app.get("/bikes")
    async def bikes(request: Request) -> Response:
        q = request.query_params.get("q") or ""
        status = request.query_params.get("status") or ""
        location = request.query_params.get("location") or ""
        rows = await crm.bikes(q=q or None, status=status or None,
                               location=location or None, limit=10000)
        since = await crm.bike_status_since()
        now = datetime.now(UTC)
        for bike in rows:
            bike["idle_days"] = (logic.idle_days(since.get(bike["id"]), now=now)
                                 if bike.get("status") in logic.IDLE_STATUSES
                                 else None)
        tools = list_tools(request, rows, allowed=BIKE_SORTS)
        return render(request, "bikes.html", q=q, status=status, location=location,
                      places=await filter_points(location),
                      rows=tools["rows"], tools=tools,
                      views=await views_of(request, "/bikes"),
                      counts=await crm.bike_counts())

    @app.get("/bikes.{ext}")
    async def bikes_csv(request: Request, ext: str) -> Response:
        """Парк файлом. Фильтры те же, что на экране: выгружают то, что
        видят, а не «всё вообще»."""
        if not may_view(request, "bikes"):
            return denied(request, "bikes")
        rows = await crm.bikes(q=request.query_params.get("q") or None,
                               status=request.query_params.get("status") or None,
                               location=request.query_params.get("location") or None,
                               limit=10000)
        money_ok = may_view(request, "finance")
        header = ["Номер", "Модель", "Статус", "Точка", "Госномер", "Пробег, км",
                  "Номер рамы", "VIN мотора", "Клиент", "Заведён"]
        if money_ok:
            header.insert(6, "Цена покупки")
        out = []
        for b in rows:
            line = [b["code"], b["model"],
                    logic.BIKE_STATUSES.get(b["status"], b["status"]),
                    b.get("location"), b.get("plate_no"), b.get("mileage_km"),
                    b.get("frame_no"), b.get("motor_no"), b.get("full_name"),
                    b.get("created_at")]
            if money_ok:
                line.insert(6, logic.to_money(b.get("purchase_price") or 0))
            out.append(line)
        return await table(ext, "bikes", header, out)

    @app.get("/bikes/new")
    async def bike_new(request: Request) -> Response:
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        return render(request, "bike_form.html", bike=None,
                      places=await location_names())

    def _bike_fields(request: Request, data: dict,
                     places: list[str]) -> dict | None:
        """Поля карточки из формы. `places` - допустимые точки
        (location_names): справочник, а не константа."""
        plate = logic.check_plate(data.get("plate_no"))
        if not plate.ok:
            flash(request, plate.error, "err")
            return None
        code = logic.check_code(data.get("code"))
        model = logic.check_name(data.get("model"), what="Модель")
        note = logic.check_note(data.get("note"))
        batteries = data.get("battery_count") or "2"
        price = (logic.check_amount(data.get("purchase_price"))
                 if (data.get("purchase_price") or "").strip() else logic.Check(True, None))
        bought = logic.check_purchase_date(data.get("purchased_on"), today=date.today())
        for check in (code, model, note, price, bought):
            if not check.ok:
                flash(request, check.error, "err")
                return None
        battery_count = logic.parse_id(batteries)
        if battery_count is None or not 0 <= battery_count <= 10:
            flash(request, "АКБ: число от 0 до 10.", "err")
            return None
        location = (data.get("location") or "").strip()
        if location and location not in places:
            flash(request, "Точка: недопустимое значение.", "err")
            return None
        months = data.get("service_months") or "24"
        bat_months = data.get("battery_service_months") or "15"
        for label, value in (("Срок службы", months), ("Срок службы АКБ", bat_months)):
            got = logic.parse_id(value)
            if got is None or not 1 <= got <= 240:
                flash(request, f"{label}: число месяцев от 1 до 240.", "err")
                return None
        residual = cost_field(data, "residual_price")
        bat_price = (logic.check_amount(data.get("battery_price"))
                     if (data.get("battery_price") or "").strip() else logic.Check(True, None))
        # Пробег правится руками: одометр могли не переписать при выдаче,
        # а здесь поле пустое означает «не трогать», а не «обнулить».
        mileage = logic.check_mileage(data.get("mileage_km"), required=False)
        for check in (residual, bat_price, mileage):
            if not check.ok:
                flash(request, check.error, "err")
                return None
        return {"code": code.value, "model": model.value, "note": note.value,
                "frame_no": (data.get("frame_no") or "").strip() or None,
                "motor_no": (data.get("motor_no") or "").strip() or None,
                "battery_count": battery_count, "purchase_price": price.value,
                "purchased_on": bought.value, "location": location or None,
                "service_months": logic.parse_id(months),
                "residual_price": residual.value,
                "battery_price": bat_price.value,
                "battery_service_months": logic.parse_id(bat_months),
                # Подменный держат под замены, а не под выдачу: своего
                # статуса у него нет, он такой же свободный.
                "spare": bool(data.get("spare")),
                "plate_no": plate.value,
                "plate_ok": bool(data.get("plate_ok")),
                "tracker_ok": bool(data.get("tracker_ok")),
                **({"mileage_km": mileage.value} if mileage.value is not None else {})}

    @app.post("/bikes")
    async def bike_create(request: Request) -> Response:
        data = await form(request)
        fields = _bike_fields(request, data, await location_names())
        if fields is None:
            return redirect("/bikes/new")
        if await crm.bike_by_code(fields["code"]) is not None:
            flash(request, f"Инвентарный номер {fields['code']} уже занят.", "err")
            return redirect("/bikes/new")
        if fields["frame_no"] and await crm.bike_by_frame(fields["frame_no"]) is not None:
            flash(request, "Велосипед с таким номером рамы уже есть.", "err")
            return redirect("/bikes/new")
        # Новая техника заводится «на сборке», когда сверка требуется:
        # велосипед, попавший в выдачу сразу после накладной, - это
        # ровно то, ради чего сверку и заводили. Требование снято -
        # ведём себя как раньше и не мешаем.
        if logic.bike_check_settings(await crm.settings())["required"]:
            fields = {**fields, "status": "new"}
        bike_id = await crm.create_bike(by=who(request), **fields)
        flash(request, "Велосипед заведён на сборку: сверьте паспорт "
                       "и введите в эксплуатацию."
              if fields.get("status") == "new" else "Велосипед добавлен.")
        return redirect(f"/bikes/{bike_id}")

    @app.get("/bikes/{bike_id}")
    async def bike_card(request: Request, bike_id: int) -> Response:
        bike = await crm.bike(bike_id)
        if bike is None:
            return render(request, "missing.html", status_code=404, what="Велосипед")
        settings = await crm.settings()
        # Сколько он уже не заработал, пока стоит. Простаивающий велосипед
        # в списке - это строка, а в рублях - решение.
        since = await crm.bike_status_since()
        idle = (logic.idle_days(since.get(bike_id), now=datetime.now(UTC))
                if bike.get("status") in logic.IDLE_STATUSES else 0)
        # Трекер - по привязке crm.trackers, а не по ручной галочке: галочка
        # говорит «поставили», привязка - «работает и где».
        tracker = await crm.tracker_of_bike(bike_id)
        orders = await crm.work_orders(bike_id=bike_id, limit=20)
        for o in orders:
            o["days"] = logic.order_days(o, today=date.today())
        return render(request, "bike.html", bike=bike, log=await crm.bike_log(bike_id),
                      rentals=await crm.bike_rentals(bike_id),
                      status_log=await crm.bike_status_log(bike_id),
                      nodes=await crm.repair_nodes(),
                      order=await crm.open_order_of(bike_id),
                      orders=orders,
                      tracker=(logic.tracker_rows([tracker], settings=settings)[0]
                               if tracker else None),
                      catalogue=logic.catalogue_entry(await crm.bike_models(),
                                                      bike.get("model")),
                      passport=logic.bike_check_state(bike, settings),
                      idle_days=idle, idle_lost=logic.idle_cost(idle),
                      amortization=logic.amortization_month(bike),
                      places=await location_names(bike.get("location")),
                      on_rent=logic.bike_on_rent(bike),
                      # Фото при сдаче - за правом на аренды, как и сами
                      # снимки: механику без него ссылки ни к чему.
                      return_photos=(await crm.return_photos(bike_id=bike_id, limit=12)
                                     if may_view(request, "rentals") else []))

    @app.post("/bikes/{bike_id}/check")
    async def bike_check(request: Request, bike_id: int) -> Response:
        """Сверка поля паспорта и ввод в эксплуатацию.

        Форма приходит multipart: к номеру на раме прикладывают снимок,
        когда владелец его потребовал.
        """
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        bike = await crm.bike(bike_id)
        if bike is None:
            return render(request, "missing.html", status_code=404, what="Велосипед")
        data = await request.form()
        action = str(data.get("action") or "")
        back = f"/bikes/{bike_id}"
        try:
            if action == "commission":
                await service.commission_bike(crm, bike, by=who(request))
                flash(request, f"Велосипед № {bike['code']} в обороте.")
            elif action == "clear":
                field = str(data.get("field") or "")
                if field not in logic.BIKE_PASSPORT:
                    flash(request, "Неизвестное поле паспорта.", "err")
                    return redirect(back)
                await crm.clear_bike_check(bike_id, field)
                flash(request, f"{logic.BIKE_PASSPORT[field]}: сверка снята.")
            else:
                field = str(data.get("field") or "")
                photo = await save_check_photo(request, "bike", bike, field,
                                               data.get("photo"),
                                               passport=logic.BIKE_PASSPORT)
                await service.check_bike_field(crm, bike, field,
                                               by=who(request), photo=photo)
                flash(request, f"{logic.BIKE_PASSPORT.get(field, field)}: сверено.")
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
        return redirect(back)

    async def save_check_photo(request: Request, prefix: str, row: dict, field: str,
                               upload: Any, *, passport: dict[str, str]) -> str | None:
        """Снимок сверки на диск. Возвращает имя или None, если не прислали.

        Имя собираем сами из вида техники, её номера и поля: имя из
        браузера - это чужая строка, и «../../etc/passwd» в ней не шутка.
        Префикс разводит велосипед и батарею: номера у них свои, и без
        него батарея № 7 затёрла бы снимок велосипеда № 7.

        Поле - тоже из формы, и проверяется по паспорту (`passport`) до
        записи: сервис отказывал неизвестному полю уже после того, как
        файл с этим полем в имени лёг на диск.
        """
        if field not in passport:
            raise service.ServiceError("Неизвестное поле паспорта.")
        filename = getattr(upload, "filename", "") or ""
        if not filename:
            return None
        if cfg.demo:
            # Демо публично: файлы посетителей на диск не пишем. Сюда
            # доходит только собранный руками запрос - поле снимка в демо
            # не показывается (фото не требуется, см. intake_save).
            await upload.close()
            flash(request, DEMO_PHOTO_TEXT)
            return None
        raw = await upload.read()
        if not raw:
            return None
        if len(raw) > BIKE_PHOTO_MAX:
            raise service.ServiceError(
                f"Снимок больше {BIKE_PHOTO_MAX // (1024 * 1024)} МБ — "
                "сфотографируйте меньшим размером.")
        suffix = Path(filename).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
            raise service.ServiceError("Снимок: только jpg, png или webp.")
        folder = Path(cfg.bike_photo_dir)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{prefix}-{int(row['id'])}-{field}{suffix}"
            (folder / name).write_bytes(raw)
        except OSError as err:
            log.warning("снимок сверки не сохранён: %s", err)
            raise service.ServiceError(
                "Снимок не сохранился — попробуйте ещё раз.") from err
        return name

    @app.get("/bikes/{bike_id}/photo/{field}")
    async def bike_photo(request: Request, bike_id: int, field: str) -> Response:
        """Снимок сверки. Имя берём из базы, а не из адреса: путь,
        собранный из параметра запроса, уводит куда угодно."""
        if not may_view(request, "bikes"):
            return denied(request, "bikes")
        bike = await crm.bike(bike_id)
        marks = (bike or {}).get("checked") or {}
        mark = marks.get(field) if isinstance(marks, dict) else None
        name = (mark or {}).get("photo") if isinstance(mark, dict) else None
        path = Path(cfg.bike_photo_dir) / str(name or "")
        if not name or not path.is_file():
            return render(request, "missing.html", status_code=404, what="Снимок")
        return FileResponse(path)

    @app.post("/bikes/{bike_id}/edit")
    async def bike_edit(request: Request, bike_id: int) -> Response:
        bike = await crm.bike(bike_id)
        if bike is None:
            return render(request, "missing.html", status_code=404, what="Велосипед")
        data = await form(request)
        fields = _bike_fields(request, data, await location_names(bike.get("location")))
        if fields is None:
            return redirect(f"/bikes/{bike_id}")
        if "location" not in data:
            # Поля нет - точку не трогаем, а не стираем. Карточку велосипеда
            # в аренде рисуют без него, и та же карточка, открытая до
            # возврата, сохраняется уже после: «нет поля» стало бы «не на
            # точке». Выбор «не на точке» браузер шлёт пустой строкой.
            fields.pop("location")
        elif logic.bike_on_rent(bike):
            # Точку велосипеда в аренде ставят выдача, возврат и замена - как
            # и сам статус «в аренде». Правка карточки увела бы его с точки
            # аренды, и дни точки разошлись бы с её деньгами. Форма такому
            # велосипеду поля не шлёт; прислали - значит, форма устарела.
            if fields["location"] != (bike.get("location") or None):
                flash(request, "Велосипед в аренде: его точку меняют возврат и "
                               "замена, а не карточка.", "err")
                return redirect(f"/bikes/{bike_id}")
            fields.pop("location")
        other = await crm.bike_by_code(fields["code"])
        if other is not None and other["id"] != bike_id:
            flash(request, f"Инвентарный номер {fields['code']} уже занят.", "err")
            return redirect(f"/bikes/{bike_id}")
        if fields["frame_no"]:
            other = await crm.bike_by_frame(fields["frame_no"])
            if other is not None and other["id"] != bike_id:
                flash(request, "Велосипед с таким номером рамы уже есть.", "err")
                return redirect(f"/bikes/{bike_id}")
        # Автор - в журнал мест: переезд между точками тоже чья-то правка.
        # Проверка «не в аренде» выше - по снимку; выдачу, успевшую между
        # ним и записью, ловит условие в самом UPDATE (keep_rented_location).
        saved = await crm.update_bike(bike_id, by=who(request),
                                      keep_rented_location=True, **fields)
        if "location" in fields and saved \
                and (saved.get("location") or None) != fields["location"]:
            flash(request, "Сохранено всё, кроме точки: велосипед успели выдать, "
                           "пока карточка была открыта, а в аренде его точку ставит "
                           "аренда.", "err")
            return redirect(f"/bikes/{bike_id}")
        flash(request, "Сохранено.")
        return redirect(f"/bikes/{bike_id}")

    @app.post("/bikes/transfer")
    async def bikes_transfer(request: Request) -> Response:
        """Переброска между точками по подсказке отчёта «По точкам»: оператор
        отметил велосипеды, панель переставила им точку той же записью, что
        и карточка. Само ничего не едет - подсказка только считает."""
        data = await form(request)
        back = "/reports/points#transfer"
        target = logic.check_location(data.get("target"), await location_names())
        if not target.ok or not target.value:
            flash(request, "Выберите точку, куда везти.", "err")
            return redirect(back)
        # Откуда везут - из формы, а не с карточки: велосипед, который
        # увезли на третью точку, пока форма была открыта, отсюда не едет.
        # Справочником имя не проверяется: оно лишь сравнивается с точкой
        # велосипеда, и чужая строка просто ни с чем не совпадёт.
        source = " ".join(str(data.get("source") or "").split())
        if not source:
            flash(request, "Форма перевозки устарела — обновите страницу.", "err")
            return redirect(back)
        ids = (await form_ids(request, "bike_ids"))[:logic.TRANSFER_MAX_BIKES]
        bikes = [b for b in [await crm.bike(i) for i in ids] if b is not None]
        if not bikes:
            flash(request, "Отметьте велосипеды, которые везёте.", "err")
            return redirect(back)
        got = await service.transfer_bikes(crm, bikes, source=source, target=target.value,
                                           by=who(request))
        if got["moved"]:
            flash(request, f"С {source} на {target.value} перевезено {len(got['moved'])}: "
                           + ", ".join(f"№ {b['code']}" for b in got["moved"]) + ".")
        if got["skipped"]:
            flash(request, f"Не перевезены — уже не свободны или уже не на {source}: "
                           + ", ".join(f"№ {b['code']}" for b in got["skipped"]) + ".",
                  "err")
        return redirect(back)

    @app.post("/bikes/{bike_id}/status")
    async def bike_status(request: Request, bike_id: int) -> Response:
        bike = await crm.bike(bike_id)
        if bike is None:
            return render(request, "missing.html", status_code=404, what="Велосипед")
        data = await form(request)
        status = logic.check_choice(data.get("status"), logic.BIKE_MANUAL_STATUSES,
                                    what="Статус")
        note = logic.check_note(data.get("note"))
        # Пробег необязателен: в мастерскую велосипед иногда закатывают
        # с мёртвым дисплеем. Зато записанный здесь попадает в журнал
        # статусов тем же триггером - и «сколько накатал между ремонтами»
        # становится видно без аренды.
        mileage = logic.check_mileage(data.get("mileage"),
                                      current=bike.get("mileage_km"),
                                      required=False)
        if not status.ok or not note.ok or not mileage.ok:
            flash(request, (status.error or note.error or mileage.error), "err")
            return redirect(f"/bikes/{bike_id}")
        if bike.get("rental_id"):
            flash(request, "Велосипед в аренде: сначала закройте аренду.", "err")
            return redirect(f"/bikes/{bike_id}")
        if bike.get("status") == "new":
            # Иначе «Свободен» из выпадающего списка выпускал бы технику
            # в оборот мимо сверки - ровно то, что она и должна ловить.
            flash(request, "Велосипед на сборке: выпускает его кнопка "
                           "«Ввести в эксплуатацию», а не смена статуса.", "err")
            return redirect(f"/bikes/{bike_id}")
        # Пробег пишется ТЕМ ЖЕ обновлением, что и статус: триггер снимает
        # одометр со строки велосипеда, и отдельный апдейт записал бы
        # в журнал старое число.
        await crm.update_bike(
            bike_id, by=who(request), status=status.value,
            **({"mileage_km": mileage.value} if mileage.value is not None else {}))
        await crm.add_bike_log(bike_id, "status",
                               f"{logic.BIKE_STATUSES[status.value]}"
                               + (f": {note.value}" if note.value else ""),
                               None, who(request))
        flash(request, "Статус изменён.")
        return redirect(f"/bikes/{bike_id}")

    @app.post("/bikes/{bike_id}/log")
    async def bike_log_add(request: Request, bike_id: int) -> Response:
        if await crm.bike(bike_id) is None:
            return render(request, "missing.html", status_code=404, what="Велосипед")
        data = await form(request)
        kind = logic.check_choice(data.get("kind") or "note", ("repair", "note"),
                                  what="Вид записи")
        note = logic.check_note(data.get("note"))
        cost = (logic.check_amount(data.get("cost"))
                if (data.get("cost") or "").strip() else logic.Check(True, None))
        for check in (kind, note, cost):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/bikes/{bike_id}")
        if not note.value and cost.value is None:
            flash(request, "Заметка пуста.", "err")
            return redirect(f"/bikes/{bike_id}")
        await crm.add_bike_log(bike_id, kind.value, note.value, cost.value, who(request))
        flash(request, "Запись добавлена.")
        return redirect(f"/bikes/{bike_id}")

    @app.post("/bikes/{bike_id}/repair")
    async def bike_repair(request: Request, bike_id: int) -> Response:
        """Ремонт по узлу: запчасти и работа отдельно. Узел - только из
        справочника, иначе отчёт «что ломается» не соберётся."""
        if await crm.bike(bike_id) is None:
            return render(request, "missing.html", status_code=404, what="Велосипед")
        data = await form(request)
        node = logic.check_choice(data.get("node"), tuple(logic.REPAIR_NODES), what="Узел")
        note = logic.check_note(data.get("note"))
        parts = cost_field(data, "parts_cost")
        labor = cost_field(data, "labor_cost")
        for check in (node, note, parts, labor):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/bikes/{bike_id}")
        await crm.create_repair(bike_id, items=[{"node": node.value, "parts_cost": parts.value,
                                                 "labor_cost": labor.value, "note": note.value}],
                                note=f"{logic.REPAIR_NODES[node.value]}"
                                     + (f": {note.value}" if note.value else ""),
                                created_by=who(request))
        flash(request, "Ремонт записан.")
        return redirect(f"/bikes/{bike_id}")

    # ─────────────────────── быстрая выдача ───────────────────────
    #
    # Мастер в четыре шага: телефон клиента -> тариф и модель -> конкретный
    # велосипед -> сводка, оплата и аренда. Состояние живёт в адресе
    # (?client=&tariff=&model=&bike=), поэтому мастер открывается и с карточки
    # клиента, и с карточки велосипеда, а «назад» - обычная ссылка.
    # Брони нет намеренно: черновики, которые никто не закрывает, держали бы
    # велосипеды «забронированными»; вместо этого свободность проверяется
    # в момент оформления, а гонку двух операторов ловит уникальный индекс.

    bot_name: dict[str, str] = {}

    async def bot_username() -> str:
        """Имя бота для ссылки-приглашения; пусто - бота нет или он недоступен."""
        if bot is None:
            return ""
        if "name" not in bot_name:
            try:
                me = await bot.get_me()
                bot_name["name"] = getattr(me, "username", "") or ""
            except Exception:                                # noqa: BLE001
                log.warning("не удалось узнать имя бота для ссылки-приглашения")
                return ""
        return bot_name["name"]

    async def bot_user_by_phone(phone: str | None) -> dict | None:
        if db is None or not phone or not hasattr(db, "user_by_phone"):
            return None
        row = await db.user_by_phone(phone)
        return dict(row) if row else None

    async def bot_user_for(client: dict) -> dict | None:
        """Строка bot.users для клиента: по Telegram, иначе по телефону."""
        row = None
        if db is not None and client.get("tg_id"):
            row = await db.get_user(client["tg_id"])
        if row is None:
            return await bot_user_by_phone(client.get("phone"))
        return dict(row)

    def issue_url(**params: Any) -> str:
        query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items()
                         if v not in (None, ""))
        return "/issue" + (f"?{query}" if query else "")

    def plain_amount(value: Decimal) -> str:
        """Сумма в поле формы: 3000, а не 3000.00."""
        text = f"{value:f}"
        return text.rstrip("0").rstrip(".") if "." in text else text

    @app.get("/issue")
    async def issue(request: Request) -> Response:
        p = request.query_params
        ctx: dict[str, Any] = {"step": 1, "client": None, "phone": p.get("phone") or "",
                               "new_client": None, "bike": None, "tariff": None,
                               "model": (p.get("model") or "").strip(),
                               "point": "", "places": []}
        # С карточки велосипеда: модель известна, шаг выбора пропускается.
        bike = await by_id(crm.bike, p.get("bike"))
        if bike is not None and bike.get("status") != "available":
            flash(request, f"Велосипед {bike['code']} сейчас "
                           f"«{logic.BIKE_STATUSES.get(bike['status'], bike['status'])}» "
                           "- выберите другой.", "err")
            bike = None
        ctx["bike"] = bike
        client = None
        if logic.parse_id(p.get("client")) is not None:
            client = await by_id(crm.client, p.get("client"))
        elif p.get("phone"):
            phone = bot_logic.normalize_phone(p["phone"])
            if phone is None:
                flash(request, "Телефон: не похоже на номер. Пример: +7 900 123-45-67.", "err")
                return render(request, "issue.html", **ctx)
            client = await crm.client_by_phone(phone)
            if client is None:
                # Новый клиент: ФИО и Telegram подсказывает бот, если человек
                # уже регистрировался там с этим номером.
                user = await bot_user_by_phone(phone)
                ctx["new_client"] = {"phone": phone,
                                     "full_name": (user or {}).get("full_name") or "",
                                     "bot": logic.bot_client_state(user)}
                ctx["phone"] = phone
                return render(request, "issue.html", **ctx)
            return redirect(issue_url(client=client["id"], bike=p.get("bike")))
        if client is None:
            # Заявки из кабинета - прямо на первом шаге: оператор начинает
            # выдачу с них, а не с поиска по телефону. Ссылка - та же, что в
            # «Бронях»: с точкой заявки.
            ctx["bookings"] = [{**b, "issue_url": booking_issue_url(b)}
                               for b in await crm.bookings(status="new")]
            return render(request, "issue.html", **ctx)
        ctx["booking_id"] = logic.parse_id(p.get("booking"))
        # Заявка и день из неё едут через все шаги мастера: без них выдача
        # по заявке оставляла её открытой (и клиент не мог подать новую),
        # а день начала сбрасывался на сегодня.
        wanted = logic.check_date(p.get("started_on")) if p.get("started_on") else None
        ctx["started_param"] = wanted.value.isoformat() if wanted and wanted.ok else ""
        # Точка выдачи едет через шаги так же: из заявки - та, что назвал
        # клиент, на шаге велосипеда она же фильтрует свободные. Имя из
        # адреса - чужая строка: принимаем только точку справочника.
        places = await location_names()
        point = (p.get("location") or "").strip()
        ctx.update(point=point if point in places else "", places=places)

        balance = await crm.client_balance(client["id"])
        active = await crm.active_rental_of(client["id"])
        bot_user = await bot_user_for(client)
        ctx.update(step=2, client=client, balance=balance, active=active, bot_user=bot_user,
                   bot_state=logic.bot_client_state(bot_user),
                   bot_username=await bot_username(),
                   # Риск - на всех шагах до подтверждения: оператор видит
                   # причины и залог, решает он сам, выдачу оценка не запирает.
                   risk=await service.client_risk(crm, client["id"], today=date.today()))
        if active is not None or client.get("status") != "active":
            # Дальше идти некуда: сначала закрыть аренду или снять блокировку.
            return render(request, "issue.html", **ctx)
        available = await crm.bikes(status="available")
        all_tariffs = await crm.tariffs(active_only=True)
        aliases = logic.model_aliases(await crm.bike_models())
        ctx["models"] = logic.model_availability(available)
        # Цена зависит от модели, поэтому плитки тарифов собираются под
        # выбранную: пока модели нет, показываем цены первой свободной -
        # пустой экран «выберите модель» оператору ничего не даёт.
        picked_model = (ctx["model"] or (ctx["bike"] or {}).get("model")
                        or (ctx["models"][0]["model"] if ctx["models"] else ""))
        ctx["tariff_model"] = picked_model
        ctx["tariffs"] = logic.tariff_tiles(
            logic.tariffs_for_model(all_tariffs, picked_model, aliases=aliases))
        # Клиенту, который приедет завтра, можно обещать конкретный день:
        # прогноз считается по «оплачено до», а не по слову оператора.
        ctx["soon"] = logic.freeing_soon(await crm.active_rentals())
        ctx["tariff"] = await by_id(crm.tariff, p.get("tariff"))
        if ctx["bike"] is not None:
            ctx["model"] = ctx["bike"]["model"]
        # Модель могли сменить последней: тариф берём того же срока, но
        # по цене выбранной модели. Иначе клиент платил бы за Kugoo
        # цену Monster Truck.
        if ctx["tariff"] is not None and ctx["model"]:
            fixed = logic.match_tariff(all_tariffs, ctx["tariff"], ctx["model"],
                                       aliases=aliases)
            if fixed is None:
                flash(request, f"Для модели «{ctx['model']}» нет тарифа на "
                               f"{ctx['tariff']['period_days']} дн. — "
                               "заведите его в тарифах.", "err")
                ctx["tariff"] = None
            elif int(fixed["id"]) != int(ctx["tariff"]["id"]):
                ctx["tariff"] = fixed
        tariff = ctx["tariff"]
        if tariff is None or not ctx["model"]:
            return render(request, "issue.html", **ctx)
        if ctx["bike"] is None:
            q = (p.get("q") or "").strip()
            since = await crm.bike_status_since()
            now = datetime.now(UTC)
            # Модель сравнивается по каталогу: в заявке из кабинета она
            # названа по-клиентски («Городской H10»), в парке - по накладной
            # («Maikaolin H10»), и прямое сравнение не находило ни одного.
            wanted = logic.catalogue_model(ctx["model"], aliases)
            rows = [dict(b) for b in available
                    if logic.catalogue_model(b.get("model"), aliases) == wanted
                    and (not q or q.lower() in (b.get("code") or "").lower())]
            # Клиент ждёт на точке выдачи, и велосипед с другой точки ему не
            # подать; сколько свободных на других - видно, выбор остаётся.
            here = [b for b in rows
                    if not ctx["point"] or b.get("location") == ctx["point"]]
            ctx["elsewhere"] = len(rows) - len(here)
            rows = here
            for b in rows:
                b["idle_days"] = logic.idle_days(since.get(b["id"]), now=now)
            # Дольше всех простаивающий - первым: выдать его и есть
            # снижение простоя, а не просто удобство оператора.
            rows.sort(key=lambda b: (-(b["idle_days"] or 0), b["code"]))
            ctx.update(step=3, bikes=rows, q=q)
            return render(request, "issue.html", **ctx)
        started = logic.check_date(p.get("started_on"), default=date.today())
        start = started.value if started.ok else date.today()
        # Батареи предлагаются те, что подходят модели: на двух точках
        # парк разношёрстный, и чужая батарея просто не встанет в раму.
        free = await crm.batteries(status="available", limit=500)
        fit = await crm.compat_for_bike_model(
            logic.catalogue_model(ctx["bike"]["model"], aliases))
        fit_ids = {m["id"] for m in fit}
        if fit_ids:
            free = [b for b in free if b.get("model_id") in fit_ids]
        # Основная батарея входит в цену велосипеда, каждая следующая -
        # платная позиция: курьер берёт её, чтобы не заряжаться в смену.
        ctx.update(batteries=logic.battery_options(free, all_tariffs,
                                                   tariff["period_days"]),
                   battery_slots=int(ctx["bike"].get("battery_count") or 0),
                   max_extra=logic.MAX_EXTRA_BATTERIES)
        # Точка выдачи: выбранная на шагах (из заявки или фильтром), иначе
        # точка заявки, иначе точка велосипеда. Велосипед встанет на неё
        # тем же UPDATE, что и в «в аренде»: пока аренда идёт, он числится
        # на её точке. Правило и список - те же, что у оформления
        # (issue_create): по этой точке считается скидка, и с ней сверится
        # пересчёт, - точка из закрытой заявки не должна разойтись с ним.
        booking = (await crm.booking(ctx["booking_id"])
                   if ctx["booking_id"] is not None else None)
        ctx.update(point=logic.issue_point(ctx["point"], booking=booking, bike=ctx["bike"]),
                   issue_places=await location_names(
                       ctx["bike"].get("location"), (booking or {}).get("location_name")))
        # Акция видна до денег: оператор называет клиенту сумму со
        # скидкой, а не объясняет баллы после оплаты. Промокод приходит
        # адресом (?promo=) - мастер без скрипта, проверка кода это
        # перезагрузка шага. Выборка та же, что у начисления, - с моделью
        # и точкой: акция на простой ограничена ими.
        preview = await service.preview_promo(crm, client=client, tariff=tariff,
                                              started_on=start, code=p.get("promo"),
                                              today=date.today(),
                                              model=ctx["bike"].get("model"),
                                              location=ctx["point"])
        discount = preview["discount"]
        # Начало в будущем: скидка ляжет в свой день, если акция доживёт, -
        # с оплаты сейчас её не снимаем, баллы зачтутся в следующий период.
        pay_due = logic.issue_payment_default(tariff["price"], balance)
        if not preview["deferred"]:
            pay_due = max(pay_due - discount, Decimal(0))
        # Доп. аккумулятор отмечают на этом же шаге, без перезагрузки:
        # сумму к оплате вместе с ним пересчитывает скрипт страницы по тем
        # же слагаемым - плюс на балансе и скидка с цены велосипеда.
        ctx.update(pay_credit=max(logic.to_money(balance), Decimal(0)),
                   pay_off=Decimal(0) if preview["deferred"] else discount)
        ctx.update(step=4, started_on=start,
                   ends_on=start + timedelta(days=int(tariff["period_days"])),
                   per_day=logic.per_day(tariff["price"], tariff["period_days"]),
                   mileage=int(ctx["bike"].get("mileage_km") or 0),
                   pay_default=plain_amount(pay_due), pay_due=pay_due,
                   contract_no=(client.get("contract_no")
                                or (bot_user or {}).get("contract_no") or ""),
                   promo=preview["promo"], promo_discount=discount,
                   promo_code=preview["code"], promo_error=preview["error"],
                   promo_note=preview["note"], promo_deferred=preview["deferred"],
                   promo_seen=logic.promo_stamp(preview["promo"], discount))
        return render(request, "issue.html", **ctx)

    @app.post("/issue/client")
    async def issue_client(request: Request) -> Response:
        data = await form(request)
        fields = await _client_fields(request, data, current=None)
        if fields is None:
            return redirect(issue_url(phone=data.get("phone"), bike=data.get("bike_id")))
        # Человек мог уже зарегистрироваться в боте с этим номером: тогда
        # карточка сразу получает его Telegram и номер договора, и
        # уведомления о сроке и оплате доходят с первого дня.
        user = await bot_user_by_phone(fields["phone"])
        tg_id = user.get("tg_id") if user else None
        if tg_id and await crm.client_by_tg(tg_id) is not None:
            tg_id = None
        client_id = await crm.create_client(
            full_name=fields["full_name"], phone=fields["phone"], note=fields["note"],
            tg_id=tg_id, username=(user or {}).get("username") if tg_id else None,
            contract_no=fields["contract_no"] or (user or {}).get("contract_no"),
            created_by=who(request))
        if tg_id:
            await service.ref_signed(crm, await crm.client(client_id) or {})
        flash(request, "Клиент добавлен." + (" Telegram подхвачен из бота." if tg_id else ""))
        return redirect(issue_url(client=client_id, bike=data.get("bike_id")))

    @app.post("/issue")
    async def issue_create(request: Request) -> Response:
        data = await form(request)
        # Назад - на тот же шаг с той же датой и кодом: ошибка в одном
        # поле не должна стирать остальные.
        back = issue_url(client=data.get("client_id"), tariff=data.get("tariff_id"),
                         bike=data.get("bike_id"), started_on=data.get("started_on"),
                         promo=logic.clean_promo_code(data.get("promo_code")),
                         booking=data.get("booking_id"), location=data.get("location"))
        client = await by_id(crm.client, data.get("client_id"))
        tariff = await by_id(crm.tariff, data.get("tariff_id"))
        bike = await by_id(crm.bike, data.get("bike_id"))
        if client is None or tariff is None or bike is None:
            flash(request, "Выберите клиента, тариф и велосипед.", "err")
            return redirect(back)
        # Последняя проверка перед деньгами: цена должна быть ценой этой
        # модели, а не той, с которой оператор начинал.
        all_tariffs = await crm.tariffs(active_only=True)
        aliases = logic.model_aliases(await crm.bike_models())
        fixed = logic.match_tariff(all_tariffs, tariff, bike.get("model"),
                                   aliases=aliases)
        if fixed is None:
            flash(request, f"Для модели «{bike.get('model')}» нет тарифа "
                           f"на {tariff['period_days']} дн.", "err")
            return redirect(back)
        tariff = fixed
        started = logic.check_date(data.get("started_on"), default=date.today())
        pay = cost_field(data, "pay_amount")
        method = data.get("pay_method") or "sbp"
        contract = (logic.check_name(data.get("contract_no"), what="Договор")
                    if (data.get("contract_no") or "").strip() else logic.Check(True, None))
        # Пробег на выдаче обязателен: без него «накатал за аренду» не
        # посчитать никогда, а переписать число с дисплея - секунда.
        mileage = logic.check_mileage(data.get("mileage"),
                                      current=bike.get("mileage_km"))
        # Точка выдачи - из справочника; точка велосипеда и точка заявки
        # допустимы, даже если их закрыли: выдача по ним уже идёт.
        booking_id = logic.parse_id(data.get("booking_id"))
        booking = await crm.booking(booking_id) if booking_id is not None else None
        # Номер заявки приходит из формы: чужая заявка не закрывается этой
        # выдачей и не даёт ей свою точку - её клиент остался бы без
        # велосипеда со «снятой» заявкой.
        if booking is not None and int(booking.get("client_id") or 0) != int(client["id"]):
            booking_id, booking = None, None
        place = logic.check_location(data.get("location"), await location_names(
            bike.get("location"), (booking or {}).get("location_name")))
        for check in (started, pay, contract, mileage, place):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(back)
        problem = logic.rental_start_problem(started.value, date.today())
        if problem:
            flash(request, problem, "err")
            return redirect(back)
        if pay.value > 0 and method not in logic.METHODS:
            flash(request, "Выберите способ оплаты.", "err")
            return redirect(back)
        # Промокод проверяется до аренды: неверный код - это отказ до
        # денег, а не выдача без скидки, о которой клиент узнает потом.
        promo_code = logic.clean_promo_code(data.get("promo_code"))
        preview = await service.preview_promo(
            crm, client=client, tariff=tariff, started_on=started.value,
            code=promo_code, today=date.today(), model=bike.get("model"),
            location=logic.issue_point(place.value, booking=booking, bike=bike))
        if promo_code and preview["error"]:
            flash(request, preview["error"], "err")
            return redirect(back)
        # Скидка, названная клиенту на шаге 4, обязана совпасть с той, что
        # ляжет в журнал. Точку выдачи, дату и код на шаге меняют без
        # перезагрузки, а акция с ограничением точкой от них зависит: иначе
        # клиент платит сумму со скидкой и уходит с долгом (или получает
        # скидку, о которой ему не сказали). Разошлось - назад на шаг с
        # новыми значениями. Форма без поля (открыта до обновления панели)
        # не сверяется: сравнить не с чем.
        seen = data.get("promo_seen")
        if seen is not None and seen != logic.promo_stamp(preview["promo"],
                                                          preview["discount"]):
            flash(request, "Скидка по акции для этой выдачи другая, чем была на экране: "
                           "сменились точка, дата или промокод. Проверьте сумму к оплате "
                           "и оформите снова.", "err")
            return redirect(back)
        # Номер договора: с формы, иначе из карточки, иначе из бота - оператор
        # его наизусть не помнит, а в акте и отчётах он нужен.
        contract_no = contract.value or client.get("contract_no")
        if not contract_no:
            contract_no = ((await bot_user_for(client)) or {}).get("contract_no")
        # Доп. аккумуляторы - платные позиции: цена считается до открытия
        # аренды, потому что начисляется цена периода целиком. Нет цены на
        # этот срок - отказ до денег, а не бесплатная батарея после.
        extra_ids = await form_ids(request, "extra_battery_ids")
        if len(extra_ids) > logic.MAX_EXTRA_BATTERIES:
            flash(request, f"Доп. аккумуляторов не больше "
                           f"{logic.MAX_EXTRA_BATTERIES} на аренду.", "err")
            return redirect(back)
        extras: list[dict] = []
        for battery_id in extra_ids:
            battery = await crm.battery(battery_id)
            if battery is None or battery.get("status") != "available":
                flash(request, "Доп. аккумулятор уже занят — обновите страницу.",
                      "err")
                return redirect(back)
            price = logic.battery_extra_price(all_tariffs, battery,
                                              tariff["period_days"])
            if price is None:
                flash(request, f"Нет тарифа на аккумулятор "
                               f"«{battery.get('model_title') or '—'}» на "
                               f"{tariff['period_days']} дн. — заведите цену "
                               "в тарифах.", "err")
                return redirect(back)
            extras.append({"kind": "battery", "battery_id": battery["id"],
                           "title": logic.extra_title("battery",
                                                      battery.get("model_title")),
                           "price": price})
        applied: list[dict] = []
        missed: list[dict] = []
        try:
            rental_id = await service.open_rental(
                crm, client=client, bike=bike, tariff=tariff, started_on=started.value,
                contract_no=contract_no, by=who(request), mileage=mileage.value,
                extras=extras, promo_code=promo_code or None, applied=applied,
                location=place.value, booking=booking, missed=missed)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(back)
        # Доп. аккумуляторы выдала сама аренда, вместе с ценой: здесь -
        # только батареи при велосипеде.
        battery_ids = [i for i in await form_ids(request, "battery_ids")
                       if i not in {e["battery_id"] for e in extras}]
        if battery_ids:
            try:
                await service.issue_with_batteries(crm, rental_id, bike=bike,
                                                   battery_ids=battery_ids,
                                                   by=who(request))
            except service.ServiceError as exc:
                # Аренда уже открыта: батарею доедем отдельно, а операцию
                # не откатываем - велосипед у клиента.
                flash(request, f"{exc} Батареи не выданы, отметьте их в карточке.",
                      "err")
        if pay.value > 0:
            # Платёж после начисления первого периода: баланс сразу честный,
            # и уведомление клиенту уходит с верной датой «оплачено до».
            await service.add_entry(crm, client, kind="payment", amount=pay.value,
                                    method=method,
                                    note=logic.ISSUE_PAY_NOTE.format(code=bike["code"]),
                                    by=who(request), rental_id=rental_id)
            await referral_bonus(client, pay.value, who(request))
        if booking_id is not None:
            # Заявка из кабинета закрывается выдачей: ссылка на аренду
            # остаётся, чтобы видеть, во что заявка превратилась.
            await service.close_booking(crm, booking_id,
                                        rental_id=rental_id, by=who(request))
        rental = await crm.rental(rental_id)
        await notify.rental_opened(bot, db, crm, client, rental)
        # Сообщение об акции - после платежа и после «аренда оформлена»:
        # в нём баланс, и он обязан быть уже с деньгами.
        await billing.tell_promos(bot, db, crm, applied)
        if pay.value > 0:
            flash(request, f"Выдача оформлена: № {bike['code']} у клиента, "
                           f"принято {logic.money(pay.value)}.")
        else:
            flash(request, f"Выдача оформлена без оплаты: № {bike['code']} у клиента, "
                           "первый период остался долгом на балансе.")
        if missed:
            flash(request, f"Доп. аккумуляторов не выдано: {len(missed)} — их успели "
                           "занять. В цену аренды они не вошли; добавьте другой "
                           "в карточке аренды.", "err")
        for got in applied:
            flash(request, f"Акция «{got['promo']['title']}»: {logic.money(got['amount'])} "
                           "начислено баллами.")
        if promo_code and not applied:
            if started.value > date.today():
                flash(request, f"Промокод {promo_code} сохранён на аренде: скидка "
                               f"начислится {started.value:%d.%m.%Y}, в день начала.")
            else:
                flash(request, f"Промокод {promo_code} к этой выдаче не подошёл: "
                               "клиент уже получал эту акцию или выбран предел.", "err")
        # Пятый шаг мастера: документы и подпись. У них они собираются до
        # аренды, у нас - после: в договор и акт идёт номер велосипеда и
        # дата выдачи, а до открытия аренды их ещё нет. Оператору это
        # всё равно одна лента, а не поход в другой раздел.
        return redirect(f"/issue/docs?rental={rental_id}")

    @app.get("/issue/docs")
    async def issue_docs(request: Request) -> Response:
        """Шаг «документы»: пакет на подпись по только что открытой аренде."""
        if not may_edit(request, "rentals"):
            return denied(request, "rentals")
        rental = await by_id(crm.rental, request.query_params.get("rental"))
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        client = await crm.client(rental["client_id"])
        if client is None:
            return render(request, "missing.html", status_code=404, what="Клиент")
        # Заявка на эту аренду уже может быть: оператор вернулся на шаг
        # назад или обновил страницу. Второй пакет на те же документы -
        # это два протокола на одну выдачу.
        rows = [r for r in await crm.sign_requests(client_id=client["id"], limit=20)
                if int(r.get("rental_id") or 0) == int(rental["id"])]
        row = next((r for r in rows if r["status"] != "cancelled"), None)
        problem = None
        if row is None:
            try:
                row = await service.start_signing(
                    crm, client=client, rental=rental, company=await sign_company(),
                    bot_user=await bot_user_for(client), by=who(request))
            except service.ServiceError as exc:
                problem = str(exc)
        code = request.session.pop("sign_code", None) if row else None
        return render(request, "issue.html", step=5, client=client, rental=rental,
                      req=row, problem=problem,
                      state=logic.sign_state(row) if row else None,
                      link=sign_link_for(request, row) if row else "",
                      code=(code or {}).get("code")
                      if row and (code or {}).get("id") == row["id"] else None,
                      bot_state=logic.bot_client_state(await bot_user_for(client)),
                      extras=logic.live_extras(
                          await crm.rental_extras(rental["id"])))

    # ─────────────────── заявки на аренду из кабинета ───────────────────
    #
    # Заявка - намерение, а не аренда: велосипед не бронируется. Оператор
    # открывает из неё мастер выдачи с готовыми полями, и заявка
    # закрывается выдачей; снятая заявка уходит клиенту сообщением.

    def booking_issue_url(booking: dict) -> str:
        """Мастер выдачи по заявке: клиент, модель, срок, день и точка -
        всё, что клиент выбрал в кабинете. Без точки выдача по заявке
        шла с точки первого попавшегося велосипеда."""
        return issue_url(client=booking["client_id"], model=booking.get("model"),
                         tariff=booking.get("tariff_id"), booking=booking["id"],
                         started_on=(booking["wanted_on"].isoformat()
                                     if booking.get("wanted_on") else None),
                         location=booking.get("location_name"))

    # ─────────────────────── входящие ───────────────────────

    INBOX_SORTS = {"wait": "waiting_since", "last": "last_in_at", "channel": "channel",
                   "name": "who", "status": "status"}
    INBOX_TABS = {"open": logic.INBOX_OPEN, "new": ("new",), "work": ("work",),
                  "done": ("done",), "spam": ("spam",), "all": None}
    inbox_vault = service.inbox_vault(getattr(cfg, "inbox_key", ""))

    async def inbox_avito() -> dict:
        return logic.avito_state(await crm.settings())

    async def inbox_wazzup() -> dict:
        return logic.wazzup_state(await crm.settings())

    async def inbox_or_404(request: Request, thread_id: int) -> dict | None:
        return await crm.inbox_thread(thread_id)

    @app.get("/inbox")
    async def inbox_page(request: Request) -> Response:
        """Входящие обращения из всех каналов - одной лентой.

        Первым стоит тот, кто ждёт ответа дольше всех: обращение, на
        которое не ответили за час, уже уходит к конкуренту.
        """
        p = request.query_params
        tab = p.get("tab") if p.get("tab") in INBOX_TABS else "open"
        channel = p.get("channel") if p.get("channel") in logic.INBOX_CHANNELS else None
        rows = logic.inbox_rows(await crm.inbox_threads(
            statuses=INBOX_TABS[tab], channel=channel, limit=2000))
        q = (p.get("q") or "").strip()
        rows = [r for r in rows if logic.inbox_matches(r, q)]
        tools = list_tools(request, rows, allowed=INBOX_SORTS)
        for row in tools["rows"]:
            row["preview"] = logic.inbox_preview(
                service.inbox_open(inbox_vault, row.get("last_body_enc")),
                row.get("last_kind"))
        counts = logic.inbox_counts(await crm.inbox_threads(statuses=logic.INBOX_OPEN,
                                                            limit=5000))
        return render(request, "inbox.html", rows=tools["rows"], tools=tools, tab=tab,
                      channel=channel or "", q=q, counts=counts,
                      avito=await inbox_avito(), wazzup=await inbox_wazzup(),
                      keyed=inbox_vault is not None,
                      hook_on=bool(getattr(cfg, "inbox_hook_token", "")),
                      views=await views_of(request, "/inbox"))

    @app.get("/incoming")
    async def incoming_page(request: Request) -> Response:
        """Всё, что клиент прислал сам, одной лентой (app/crm/incoming.py):
        сообщения, заявки на аренду, «Я оплатил». Раздела у ленты нет - она
        открыта всем, а каждая её часть читается, только если открыт её
        раздел; без всех трёх - отказ."""
        kinds = incoming.visible_kinds(request.state.staff)
        if not kinds:
            return denied(request, "issue")
        threads: list[dict] = []
        if "message" in kinds:
            threads = logic.inbox_rows(await crm.inbox_threads(statuses=logic.INBOX_OPEN,
                                                               limit=2000))
            for row in threads:
                row["preview"] = logic.inbox_preview(
                    service.inbox_open(inbox_vault, row.get("last_body_enc")),
                    row.get("last_kind"))
        rows = incoming.incoming_rows(
            threads=threads,
            bookings=await crm.bookings(status="new") if "booking" in kinds else (),
            claims=await crm.pending_claims() if "claim" in kinds else (),
            today=date.today(), money_ok=may_view(request, "finance"),
            booking_url=booking_issue_url if may_edit(request, "issue") else None)
        return render(request, "incoming.html", rows=rows,
                      incoming_counts=incoming.counts(rows))

    @app.get("/inbox/{thread_id}")
    async def inbox_card(request: Request, thread_id: int) -> Response:
        thread = await inbox_or_404(request, thread_id)
        if thread is None:
            return render(request, "missing.html", status_code=404, what="Обращение")
        return await inbox_card_page(request, thread)

    async def inbox_card_page(request: Request, thread: dict, *, draft: str = "",
                              status_code: int = 200) -> Response:
        thread_id = int(thread["id"])
        messages = []
        for m in await crm.inbox_messages(thread_id):
            messages.append({**m, "text": service.inbox_open(inbox_vault, m.get("body_enc"))})
        avito = await inbox_avito()
        can_reply, why = logic.inbox_can_reply(thread, avito_ok=avito["live"],
                                               wa=await inbox_wazzup())
        bot_state = None
        tg_id = logic.parse_id(thread["ext_id"]) if thread["channel"] == "tg" else None
        if tg_id is not None and db is not None:
            row = await db.get_user(tg_id)
            bot_state = logic.bot_client_state(dict(row) if row else None)
        [row] = logic.inbox_rows([thread])
        return render(request, "inbox_thread.html", t=row, messages=messages,
                      links=logic.inbox_links(thread), can_reply=can_reply and
                      inbox_vault is not None, why=why if not can_reply else (
                          "" if inbox_vault is not None else
                          "Не задан ключ INBOX_KEY - ответ негде хранить."),
                      bot_state=bot_state, avito=avito, draft=draft,
                      reply_limit=logic.INBOX_REPLY_LIMITS.get(thread["channel"], 3500),
                      status_code=status_code)

    @app.post("/inbox/{thread_id}/reply")
    async def inbox_reply(request: Request, thread_id: int) -> Response:
        thread = await inbox_or_404(request, thread_id)
        if thread is None:
            return render(request, "missing.html", status_code=404, what="Обращение")
        data = await form(request)
        back = f"/inbox/{thread_id}"
        if not form_once(data):
            flash(request, "Этот ответ уже отправлен — повторное нажатие пропущено.", "err")
            return redirect(back)
        try:
            await service.inbox_reply(crm, inbox_vault, thread, data.get("text"),
                                      by=who(request),
                                      avito_ok=(await inbox_avito())["live"],
                                      wa=await inbox_wazzup())
        except service.ServiceError as exc:
            # Страница сразу, а не редирект: набранный ответ остаётся в поле.
            # В сессию его не положить - cookie не вместит 3500 знаков.
            form_once_release(data)
            flash(request, str(exc), "err")
            return await inbox_card_page(request, thread, status_code=400,
                                         draft=str(data.get("text") or "")[:5000])
        flash(request, "Ответ в очереди: бот отправит его в течение минуты.")
        return redirect(back)

    @app.post("/inbox/{thread_id}/status")
    async def inbox_status(request: Request, thread_id: int) -> Response:
        thread = await inbox_or_404(request, thread_id)
        if thread is None:
            return render(request, "missing.html", status_code=404, what="Обращение")
        data = await form(request)
        try:
            await service.inbox_set_status(crm, thread, str(data.get("status") or ""),
                                           note=data.get("note"), by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/inbox/{thread_id}")
        flash(request, "Сохранено.")
        return redirect(f"/inbox/{thread_id}")

    @app.post("/inbox/{thread_id}/client")
    async def inbox_client(request: Request, thread_id: int) -> Response:
        thread = await inbox_or_404(request, thread_id)
        if thread is None:
            return render(request, "missing.html", status_code=404, what="Обращение")
        if not may_view(request, "clients"):
            # Поиск по номеру карточки или телефону показывает ФИО клиента:
            # без раздела «Клиенты» это был бы перебор базы через обращения.
            return denied(request, "clients")
        data = await form(request)
        try:
            client = await service.inbox_link_client(crm, thread, data.get("client"),
                                                     by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/inbox/{thread_id}")
        flash(request, f"Привязано к карточке: {client['full_name']}." if client
              else "Обращение отвязано от карточки.")
        return redirect(f"/inbox/{thread_id}")

    @app.post("/inbox/{thread_id}/answered")
    async def inbox_answered(request: Request, thread_id: int) -> Response:
        thread = await inbox_or_404(request, thread_id)
        if thread is None:
            return render(request, "missing.html", status_code=404, what="Обращение")
        await service.inbox_answered_elsewhere(crm, thread, by=who(request))
        flash(request, "Отмечено: ответили вне панели.")
        return redirect(f"/inbox/{thread_id}")

    @app.post("/inbox/{thread_id}/out/{message_id}/again")
    async def inbox_again(request: Request, thread_id: int, message_id: int) -> Response:
        thread = await inbox_or_404(request, thread_id)
        if thread is None:
            return render(request, "missing.html", status_code=404, what="Обращение")
        if not any(m["id"] == message_id for m in await crm.inbox_messages(thread_id)):
            return render(request, "missing.html", status_code=404, what="Ответ")
        try:
            await service.inbox_retry(crm, message_id, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/inbox/{thread_id}")
        flash(request, "Ответ снова в очереди.")
        return redirect(f"/inbox/{thread_id}")

    @app.post("/hook/inbox")
    async def inbox_hook(request: Request) -> Response:
        """Точка входа для шлюза WhatsApp (Green-API, Wazzup) и n8n.

        Входа в панель у отправителя нет, поэтому проверки свои: пустой
        токен - хука нет вовсе (404), неверный - 401 и счёт неудач с
        адреса, тело больше лимита - 413. Запрос ничего не шлёт наружу и
        не пишет в лог содержимого: только счётчики.
        """
        header = request.headers.get("authorization") or ""
        given = header[7:].strip() if header[:7].lower() == "bearer " else ""
        return await inbox_hook_in(request, given)

    @app.post("/hook/inbox/{given}")
    async def inbox_hook_path(request: Request, given: str) -> Response:
        """Тот же хук с токеном в адресе: Wazzup, подключённый своим ключом
        API, шлёт вебхук без заголовка авторизации - подписывать ему нечем.
        Адрес с токеном знает только Wazzup (подписку ставит бот)."""
        return await inbox_hook_in(request, given.strip())

    async def inbox_hook_in(request: Request, given: str) -> Response:
        token = str(getattr(cfg, "inbox_hook_token", "") or "")
        if not token:
            return JSONResponse({"ok": False}, status_code=404)
        valid = bool(given) and hmac.compare_digest(given.encode(), token.encode())
        if not valid:
            # Счёт неудач - только неверным токенам: шлюзы WhatsApp шлют с
            # общих адресов, и чужой инстанс на том же адресе не должен
            # запирать наш хук. Подбор 64 hex-знаков всё равно не выйдет.
            ip_key = "hook:" + client_ip(request)
            if login_throttled(ip_key, logic.HOOK_FAIL_LIMIT):
                return JSONResponse({"ok": False, "error": "too many attempts"},
                                    status_code=429)
            login_failures.setdefault(ip_key, []).append(time.monotonic())
            return JSONResponse({"ok": False}, status_code=401)
        declared = request.headers.get("content-length") or ""
        # isascii: заголовок читается в latin-1, и «²» в нём - «цифра»
        # для isdigit, на которой int() ронял хук 500.
        if (declared.isascii() and declared.isdigit()
                and int(declared) > logic.HOOK_MAX_BYTES):
            return JSONResponse({"ok": False, "error": "too large"}, status_code=413)
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > logic.HOOK_MAX_BYTES:
                return JSONResponse({"ok": False, "error": "too large"}, status_code=413)
        try:
            payload = json.loads(body.decode("utf-8")) if body.strip() else {}
        except (ValueError, UnicodeDecodeError, RecursionError):
            # RecursionError - тело из тысяч вложенных скобок: для хука это
            # тоже «не JSON», а не падение обработчика.
            return JSONResponse({"ok": False, "error": "not json"}, status_code=400)
        items, skipped = logic.parse_inbound(payload)
        # Разбор уже обрезал пачку и учёл лишнее в skipped: здесь каждое
        # сообщение либо пишется, либо тоже идёт в счёт.
        skipped += max(len(items) - logic.HOOK_BATCH_LIMIT, 0)
        saved = duplicates = 0
        for item in items[:logic.HOOK_BATCH_LIMIT]:
            try:
                got = await service.inbox_in(
                    crm, inbox_vault, channel=item["channel"], origin="hook",
                    ext_id=item["ext_id"], kind=item["kind"], text=item["text"],
                    msg_id=item["msg_id"], name=item["name"], phone=item["phone"],
                    subject=item["subject"], subject_url=item["subject_url"],
                    at=item["at"], announce=True, ext_channel=item.get("ext_channel"))
            except (service.ServiceError, UnicodeError, ValueError, DataError):
                # Негодное сообщение - в пропущенные, пачка идёт дальше.
                # Сбой базы - наоборот 500: шлюз повторит доставку, а уже
                # записанное отсечёт номер сообщения.
                skipped += 1
                continue
            if got["message_id"] is None:
                duplicates += 1
            else:
                saved += 1
        log.info("хук входящих: принято %s, повторов %s, пропущено %s",
                 saved, duplicates, skipped)
        return JSONResponse({"ok": True, "saved": saved, "duplicates": duplicates,
                             "skipped": skipped})

    # ─────────────────── франшиза: метрики наружу ───────────────────

    metrics_hits: dict[str, list[float]] = {}

    def metrics_rate_ok(ip: str) -> bool:
        """Предел запросов с верным токеном на адрес (METRICS_RATE_LIMIT в
        окне METRICS_RATE_WINDOW). Память процесса: рестарт обнуляет её,
        и это ничего не даёт тому, кто пришёл с утёкшим токеном."""
        now = time.monotonic()
        if len(metrics_hits) > LOGIN_KEYS_SWEEP:
            for stale in [k for k, ts in metrics_hits.items()
                          if not ts or now - ts[-1] >= logic.METRICS_RATE_WINDOW]:
                metrics_hits.pop(stale, None)
        hits = [t for t in metrics_hits.get(ip, ())
                if now - t < logic.METRICS_RATE_WINDOW]
        if len(hits) >= logic.METRICS_RATE_LIMIT:
            metrics_hits[ip] = hits
            return False
        metrics_hits[ip] = [*hits, now]
        return True

    @app.get("/hook/metrics")
    async def metrics_hook(request: Request) -> Response:
        """Агрегаты этой копии для франчайзера: парк, три числа, выручка
        по месяцам, названия точек (service.franchise_metrics).

        Входа в панель у франчайзера нет, поэтому проверки свои, как у
        /hook/inbox: пустой токен (и демо) - адреса нет вовсе (404),
        неверный - 401 и счёт неудач с адреса, верный - не чаще предела.
        Ни клиентов, ни телефонов, ни отдельных платежей в ответе нет.
        """
        token = str(getattr(cfg, "metrics_token", "") or "")
        if not token or cfg.demo:
            return JSONResponse({"ok": False}, status_code=404)
        header = request.headers.get("authorization") or ""
        given = header[7:].strip() if header[:7].lower() == "bearer " else ""
        ip = client_ip(request)
        if not (given and hmac.compare_digest(given.encode(), token.encode())):
            ip_key = "metrics:" + ip
            if login_throttled(ip_key, logic.HOOK_FAIL_LIMIT):
                return JSONResponse({"ok": False, "error": "too many attempts"},
                                    status_code=429)
            login_failures.setdefault(ip_key, []).append(time.monotonic())
            return JSONResponse({"ok": False}, status_code=401)
        if not metrics_rate_ok(ip):
            return JSONResponse({"ok": False, "error": "too many requests"},
                                status_code=429, headers={"Retry-After": "600"})
        payload = await service.franchise_metrics(
            crm, title=cfg.title, version=code_v, now=datetime.now().astimezone())
        log.info("метрики отданы франчайзеру (%s)", ip)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    # ─────────────────────── франчайзи и роялти ───────────────────────
    #
    # Кабинет франчайзера. Опрашивает франчайзи процесс бота раз в сутки
    # (app/crm/franchise.py); панель ходит к ним только по кнопке
    # «Обновить сейчас» - один запрос к одному франчайзи при владельце.
    # Всё, что пришло от франчайзи, уже прошло logic.parse_metrics и
    # выводится шаблоном с экранированием: чужая строка - не разметка.

    franchise_vault = service.franchise_vault(getattr(cfg, "franchise_key", ""))

    def franchise_months_since(count: int) -> date:
        month = date.today().replace(day=1)
        for _ in range(max(count, 1) - 1):
            month = (month - timedelta(days=1)).replace(day=1)
        return month

    async def franchisee_of(raw: str) -> dict | None:
        """Франчайзи по номеру из адреса: не номер (и не bigint) - None,
        то есть 404, а не 422 или 500 от базы."""
        franchisee_id = logic.parse_id(raw)
        return await crm.franchisee(franchisee_id) if franchisee_id is not None else None

    def royalty_span(request: Request) -> int:
        raw = request.query_params.get("months") or ""
        return int(raw) if raw in ("6", "12", "24") else logic.METRICS_MONTHS

    @app.get("/franchisees")
    async def franchisees_page(request: Request) -> Response:
        rows = await crm.franchisees()
        # Два прошлых месяца: прошлый - роялти и тренд, позапрошлый - база тренда.
        data = logic.franchise_rows(rows, await crm.franchise_months(
            franchise_months_since(3)), now=datetime.now().astimezone(), version=code_v)
        return render(request, "franchisees.html", **data, version=code_v,
                      key_ok=franchise_vault is not None,
                      metrics_on=bool(getattr(cfg, "metrics_token", "")) and not cfg.demo,
                      stale_hours=logic.FRANCHISE_STALE_HOURS)

    def franchisee_form_page(request: Request, row: dict | None, *,
                             values: dict | None = None, status_code: int = 200
                             ) -> Response:
        return render(request, "franchisee.html", status_code=status_code, row=row,
                      values=values or row or {"active": True},
                      key_ok=franchise_vault is not None,
                      stale_hours=logic.FRANCHISE_STALE_HOURS, version=code_v)

    @app.get("/franchisees/new")
    async def franchisee_new(request: Request) -> Response:
        if not may_edit(request, "franchise"):
            return denied(request, "franchise")
        return franchisee_form_page(request, None)

    async def franchisee_submit(request: Request, row: dict | None) -> Response:
        data = await form(request)
        checked = logic.check_franchisee(data)
        token = logic.check_franchise_token(data.get("token"))
        terms = logic.check_terms_from(data.get("terms_from"), today=date.today())
        error = next((c.error for c in (checked, token, terms) if not c.ok), "")
        values = {**(row or {}), **{k: v for k, v in data.items() if k != "token"},
                  "active": bool(data.get("active"))}
        if error:
            flash(request, error, "err")
            return franchisee_form_page(request, row, values=values, status_code=400)
        try:
            franchisee_id = await service.save_franchisee(
                crm, franchise_vault, row["id"] if row else None, checked.value,
                token=token.value, terms_from=terms.value)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return franchisee_form_page(request, row, values=values, status_code=400)
        flash(request, "Франчайзи сохранён." if row else
              "Франчайзи заведён: процесс бота опросит его в ближайшие минуты, "
              "или нажмите «Обновить сейчас».")
        return redirect(f"/franchisees/{franchisee_id}")

    @app.post("/franchisees/new")
    async def franchisee_create(request: Request) -> Response:
        return await franchisee_submit(request, None)

    async def royalty_data(request: Request) -> dict:
        count = royalty_span(request)
        return {"count": count, "blocks": logic.royalty_rows(
            await crm.franchisees(),
            await crm.franchise_months(franchise_months_since(count)),
            today=date.today(), count=count)}

    @app.get("/franchisees/royalty")
    async def franchise_royalty(request: Request) -> Response:
        return render(request, "franchise_royalty.html", **await royalty_data(request),
                      partial_mark=logic.ROYALTY_PARTIAL)

    @app.get("/franchisees/royalty.{ext}")
    async def franchise_royalty_table(request: Request, ext: str) -> Response:
        data = await royalty_data(request)
        rows = [[b["month"], r["name"], r["city"], r["revenue"], r["percent"], r["fixed"],
                 r["royalty"], r["mark"]]
                for b in data["blocks"] for r in b["rows"]]
        return await table(ext, f"royalty-{date.today():%Y%m}",
                           ["Месяц", "Франчайзи", "Город", "Выручка", "Роялти, %",
                            "Фикс", "Роялти", "Отметка"], rows)

    @app.get("/franchisees/{franchisee_id}")
    async def franchisee_card(request: Request, franchisee_id: str) -> Response:
        row = await franchisee_of(franchisee_id)
        if row is None:
            return render(request, "missing.html", status_code=404, what="Франчайзи")
        parsed = logic.parse_metrics(row["data"]) if row.get("data") else None
        now = datetime.now().astimezone()
        history = logic.royalty_rows([row], await crm.franchise_months(
            franchise_months_since(12)), today=now.date(), count=12)
        return render(request, "franchisee.html", row=row, values=row,
                      snap=parsed.value if parsed is not None and parsed.ok else None,
                      stale=logic.franchise_stale(row, now),
                      history=[b for b in history if b["rows"]],
                      key_ok=franchise_vault is not None,
                      stale_hours=logic.FRANCHISE_STALE_HOURS, version=code_v)

    @app.post("/franchisees/{franchisee_id}")
    async def franchisee_save(request: Request, franchisee_id: str) -> Response:
        row = await franchisee_of(franchisee_id)
        if row is None:
            return render(request, "missing.html", status_code=404, what="Франчайзи")
        return await franchisee_submit(request, row)

    @app.post("/franchisees/{franchisee_id}/delete")
    async def franchisee_delete(request: Request, franchisee_id: str) -> Response:
        row = await franchisee_of(franchisee_id)
        if row is None:
            return render(request, "missing.html", status_code=404, what="Франчайзи")
        # «Заведён по ошибке» - явная галочка: стираются и месяцы роялти.
        # Без неё история держит карточку - по ней могли выставить счёт.
        wipe = bool((await form(request)).get("wipe"))
        if not await crm.delete_franchisee(row["id"], wipe=wipe):
            flash(request, "Удалить нельзя: по франчайзи уже есть месяцы роялти. "
                           "Снимите галочку «Действует» - опрос остановится, "
                           "история останется. Заведён по ошибке - отметьте "
                           "«стереть и месяцы».", "err")
            return redirect(f"/franchisees/{row['id']}")
        flash(request, "Франчайзи удалён.")
        return redirect("/franchisees")

    @app.post("/franchisees/refresh/{franchisee_id}")
    async def franchisee_refresh(request: Request, franchisee_id: str) -> Response:
        """«Обновить сейчас»: один запрос к одному франчайзи по кнопке
        владельца. В демо закрыто стражем (DEMO_BLOCKED_PREFIXES)."""
        row = await franchisee_of(franchisee_id)
        if row is None:
            return render(request, "missing.html", status_code=404, what="Франчайзи")
        error = await franchise.refresh_one(crm, franchise_vault, row)
        if error:
            flash(request, f"Франчайзи не ответил: {error}", "err")
        else:
            flash(request, "Данные франчайзи обновлены.")
        return redirect(f"/franchisees/{row['id']}")

    @app.get("/bookings")
    async def bookings_page(request: Request) -> Response:
        rows = await crm.bookings(limit=300)
        for row in rows:
            row["issue_url"] = booking_issue_url(row)
            # Лист ожидания: звал ли бот клиента к освободившемуся велосипеду
            # и нажал ли тот «еду» - оператор видит, кого ждать сегодня.
            row["waitlist_note"] = logic.waitlist_note(row, today=date.today())
        return render(request, "bookings.html", rows=rows, today=date.today(),
                      fresh=[r for r in rows if r["status"] == "new"])

    @app.post("/bookings/{booking_id}/cancel")
    async def booking_cancel(request: Request, booking_id: int) -> Response:
        if not may_edit(request, "issue"):
            return denied(request, "issue")
        booking = await crm.booking(booking_id)
        if booking is None:
            return render(request, "missing.html", status_code=404, what="Заявка")
        note = logic.check_note((await form(request)).get("note"))
        if not note.ok:
            flash(request, note.error, "err")
            return redirect("/bookings")
        try:
            await service.cancel_booking(crm, booking, by=who(request), note=note.value)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/bookings")
        client = await crm.client(booking["client_id"])
        if client is not None:
            await notices.send_client(
                crm, "booking_cancelled", client["id"],
                lambda: notify.booking_cancelled(bot, db, client, booking, note.value))
        flash(request, f"Заявка {booking['full_name']} снята"
                       + (", клиенту сказано." if client and client.get("tg_id") else "."))
        return redirect("/bookings")

    # ─────────────────────── аренды ───────────────────────

    RENTAL_SORTS = {"no": "id", "client": "full_name", "bike": "bike_code",
                    # «Оплачено» показывает дату из баланса, а не границу
                    # начисления: сортировать надо по тому, что видно.
                    "started": "started_on", "paid": "covered_until",
                    "debt": "balance", "tariff": "tariff_name",
                    "days": "days_running", "overdue": "overdue_days",
                    "location": "location"}
    ORDER_SORTS = {"no": "no", "bike": "bike_code", "status": "status",
                   "payer": "payer", "client": "client_name",
                   "tech": "tech_name", "total": "total", "opened": "opened_at",
                   "location": "location", "days": "days", "overdue": "overdue"}
    PART_SORTS = {"title": "title", "node": "node_title", "stock": "stock",
                  "cost": "cost", "price": "price", "days": "days_on_stock",
                  "cost_total": "cost_total", "price_total": "price_total",
                  "min": "min_stock", "model": "model"}

    def rental_rows(rows: list[dict], q: str = "",
                    open_orders: dict | None = None) -> list[dict]:
        """Аренды после поиска, со сводкой, сутками и просрочкой.

        «В ремонте» у аренды - это открытый наряд на велосипеде, который
        сейчас у клиента: статус велосипеда остаётся «в аренде» (его ставит
        и снимает только аренда), а вот наряд на нём - факт сервиса.
        """
        today = date.today()
        out = logic.rental_search(rows, q)
        for r in out:
            r["summary"] = summarize(r if r["status"] == "active" else None,
                                     r.get("balance", 0))
            r["overdue_days"] = logic.overdue_days(r["summary"])
            r["covered_until"] = r["summary"].get("covered_until")
            r["days_running"] = logic.rental_days(r, today=today)
            r["in_repair"] = bool(r["status"] == "active" and r.get("bike_id")
                                  and open_orders and r["bike_id"] in open_orders)
        return out

    def rental_view(rows: list[dict], view: str) -> list[dict]:
        """Фильтр вида поверх статуса.

        «Без техники» - не статус, а состояние: аренда идёт, а велосипеда
        на руках нет. Так бывает после замены, когда подменный уже забрали,
        а новый ещё не выдали, - и такую аренду видно только отсюда.
        Вид «долг» без «Финансов» сюда не доходит (`rental_view_of`).
        """
        if view == "nobike":
            return [r for r in rows if r["status"] == "active" and not r.get("bike_id")]
        if view == "debt":
            return [r for r in rows if logic.to_money(r.get("balance")) < 0]
        if view == "search":
            return [r for r in rows if logic.in_search(r)]
        if view == "overdue":
            return [r for r in rows if r["overdue_days"] > 0]
        if view == "repair":
            return [r for r in rows if r["in_repair"]]
        return rows

    def rental_view_of(request: Request) -> str:
        """Вид списка аренд из адреса. «Долг» - это деньги: без права на
        «Финансы» его нет, как группы должников у клиентов, и ?view=debt -
        просто все аренды, а не список должников в выгрузке."""
        view = request.query_params.get("view") or ""
        return "" if view == "debt" and not may_view(request, "finance") else view

    def rental_sorts(request: Request) -> dict[str, str]:
        """Сортировка по балансу - только с «Финансами»: порядок строк по
        долгу выдаёт те же деньги, что и спрятанная колонка."""
        if may_view(request, "finance"):
            return RENTAL_SORTS
        return {k: v for k, v in RENTAL_SORTS.items() if k != "debt"}

    def rental_counts(rows: list[dict]) -> dict[str, int]:
        return {"nobike": sum(1 for r in rows if r["status"] == "active"
                              and not r.get("bike_id")),
                "debt": sum(1 for r in rows if logic.to_money(r.get("balance")) < 0),
                "search": sum(1 for r in rows if logic.in_search(r)),
                "overdue": sum(1 for r in rows if r["overdue_days"] > 0),
                "repair": sum(1 for r in rows if r["in_repair"])}

    @app.get("/rentals")
    async def rentals(request: Request) -> Response:
        status = request.query_params.get("status") or "active"
        view = rental_view_of(request)
        q = request.query_params.get("q") or ""
        # Точка выдачи; «none» - аренды без точки.
        location = request.query_params.get("location") or ""
        rows = rental_rows(await crm.rentals(status=status if status != "all" else None,
                                             location=location or None),
                           q, await crm.open_orders_by_bike())
        shown = rental_view(rows, view)
        tools = list_tools(request, shown, allowed=rental_sorts(request))
        return render(request, "rentals.html", rows=tools["rows"], tools=tools,
                      status=status, view=view, q=q, location=location,
                      places=await filter_points(location),
                      views=await views_of(request, "/rentals"),
                      # Баланс - клиентский, он приходит в каждой строке его
                      # аренд: сумма по строкам посчитала бы долг клиента с
                      # тремя арендами трижды.
                      debt_total=logic.sum_of(
                          list({r["client_id"]: r for r in tools["all_rows"]
                                if logic.to_money(r.get("balance")) < 0}.values()),
                          "balance"),
                      overdue_total=sum(1 for r in tools["all_rows"]
                                        if r["overdue_days"] > 0),
                      # Счётчики - по найденному: чипы отвечают на «сколько
                      # из этих», а не «сколько вообще».
                      counts=rental_counts(rows))

    @app.get("/rentals.{ext}")
    async def rentals_csv(request: Request, ext: str) -> Response:
        if not may_view(request, "rentals"):
            return denied(request, "rentals")
        status = request.query_params.get("status") or "active"
        rows = rental_view(
            rental_rows(await crm.rentals(
                status=status if status != "all" else None,
                location=request.query_params.get("location") or None),
                        request.query_params.get("q") or "",
                        await crm.open_orders_by_bike()),
            rental_view_of(request))
        money_ok = may_view(request, "finance")
        header = ["Аренда", "Клиент", "Телефон", "Велосипед", "Тариф",
                  "Начало", "Идёт, дн.", "Оплачено до", "Просрочка, дн.",
                  "Статус", "Договор", "Точка выдачи"]
        if money_ok:
            header.insert(9, "Баланс")
        out = []
        for r in rows:
            line = [r["id"], r.get("full_name"), r.get("phone"),
                    r.get("bike_code"), r.get("tariff_name"), r.get("started_on"),
                    r["days_running"],
                    (r["summary"] or {}).get("covered_until"),
                    r["overdue_days"],
                    logic.RENTAL_STATUSES.get(r["status"], r["status"]),
                    r.get("contract_no"), r.get("location")]
            if money_ok:
                line.insert(9, logic.to_money(r.get("balance") or 0))
            out.append(line)
        return await table(ext, "rentals", header, out)

    @app.get("/rentals/new")
    async def rental_new(request: Request) -> Response:
        if not may_edit(request, "rentals"):
            return denied(request, "rentals")
        # Весь список, как у /clients: по умолчанию 500 по имени, и
        # клиенты дальше пятисотого в выпадающий список не попадали.
        clients_all = await crm.clients(status="active", limit=100000)
        free_clients = [c for c in clients_all if not c.get("rental_id")]
        client_id = request.query_params.get("client")
        bike_id = request.query_params.get("bike")
        return render(request, "rental_form.html", clients=free_clients,
                      places=await location_names(),
                      bikes=await crm.bikes(status="available"),
                      tariffs=[t for t in await crm.tariffs(active_only=True)
                               if (t.get("kind") or "bike") == "bike"],
                      client_id=logic.parse_id(client_id),
                      bike_id=logic.parse_id(bike_id))

    async def fit_tariff(tariff: dict | None, bike: dict | None) -> tuple[dict | None, str]:
        """Тариф, который можно поставить аренде, или (None, почему нет).

        Тариф аккумулятора - не цена аренды: он живёт позицией, и аренда
        «на аккумуляторе» стоила бы 1 170 ₽ вместо 3 000 ₽. С известным
        велосипедом берётся тот же срок у его модели, как на выдаче.
        """
        if tariff is None:
            return None, "Выберите тариф."
        if (tariff.get("kind") or "bike") != "bike":
            return None, "Это тариф аккумулятора - для аренды нужен тариф велосипеда."
        if bike is None or not bike.get("model"):
            return tariff, ""
        fixed = logic.match_tariff(await crm.tariffs(active_only=True), tariff,
                                   bike.get("model"),
                                   aliases=logic.model_aliases(await crm.bike_models()))
        if fixed is None:
            return None, (f"Для модели «{bike.get('model')}» нет тарифа "
                          f"на {tariff['period_days']} дн.")
        return fixed, ""

    @app.post("/rentals")
    async def rental_create(request: Request) -> Response:
        data = await form(request)
        client = await by_id(crm.client, data.get("client_id"))
        tariff = await by_id(crm.tariff, data.get("tariff_id"))
        bike = await by_id(crm.bike, data.get("bike_id"))
        started = logic.check_date(data.get("started_on"), default=date.today())
        billing_mode = data.get("billing") or "auto"
        if client is None or tariff is None:
            flash(request, "Выберите клиента и тариф.", "err")
            return redirect("/rentals/new")
        if bike is None and (data.get("bike_id") or "").strip():
            flash(request, "Такого велосипеда нет.", "err")
            return redirect("/rentals/new")
        if not started.ok or billing_mode not in logic.BILLING:
            flash(request, started.error or "Недопустимый режим начисления.", "err")
            return redirect("/rentals/new")
        # Ручное начисление выключает биллинг аренды: дальше деньги в ней
        # пишет только право на записи в журнал. Без него это был бы
        # способ остановить начисления вовсе.
        if billing_mode == "manual" and not logic.can_act(request.state.staff,
                                                          "money_edit"):
            return denied(request, "money_edit")
        problem = logic.rental_start_problem(started.value, date.today())
        if problem:
            flash(request, problem, "err")
            return redirect("/rentals/new")
        tariff, why = await fit_tariff(tariff, bike)
        if tariff is None:
            flash(request, why, "err")
            return redirect("/rentals/new")
        # Пусто - точка велосипеда; без велосипеда аренда остаётся без
        # точки, пока её не назовёт первая замена.
        place = logic.check_location(data.get("location"),
                                     await location_names((bike or {}).get("location")))
        if not place.ok:
            flash(request, place.error, "err")
            return redirect("/rentals/new")
        contract_no = (data.get("contract_no") or "").strip() or client.get("contract_no")
        applied: list[dict] = []
        try:
            rental_id = await service.open_rental(
                crm, client=client, bike=bike, tariff=tariff, started_on=started.value,
                contract_no=contract_no, by=who(request), billing=billing_mode,
                applied=applied, location=place.value)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/rentals/new")
        rental = await crm.rental(rental_id)
        await notify.rental_opened(bot, db, crm, client, rental)
        await billing.tell_promos(bot, db, crm, applied)
        for got in applied:
            flash(request, f"Акция «{got['promo']['title']}»: {logic.money(got['amount'])} "
                           "начислено баллами.")
        if billing_mode == "manual":
            flash(request, "Аренда оформлена без начисления: записи в журнал делаете вы.")
        elif started.value > date.today():
            flash(request, f"Аренда оформлена. Первый период начислится "
                           f"{started.value:%d.%m.%Y}.")
        else:
            flash(request, "Аренда оформлена, первый период начислен.")
        return redirect(f"/rentals/{rental_id}")

    @app.get("/ops")
    async def ops_page(request: Request) -> Response:
        """Рабочая группа точек: что написали на точке и сошлось ли с базой."""
        if not may_view(request, "rentals"):
            return denied(request, "rentals")
        kind = request.query_params.get("kind") or ""
        kind = kind if kind in logic.OPS_KINDS else ""
        bad = request.query_params.get("bad") == "1"
        rows = await crm.ops_reports(kind=kind or None, ok=False if bad else None)
        return render(request, "ops.html", rows=rows, kind=kind, bad=bad)

    @app.get("/rentals/search")
    async def rentals_search(request: Request) -> Response:
        """Розыск: кто перестал платить и пропал.

        Потеря велосипеда начинается одинаково - клиент замолчал, а
        велосипед остался «в аренде», и никто его не ищет.
        """
        settings = logic.search_settings(await crm.settings())
        rows = logic.search_rows(await crm.active_rentals(), settings=settings)
        return render(request, "search.html", settings=settings, **rows)

    @app.post("/rentals/search")
    async def rentals_search_settings(request: Request) -> Response:
        if not may_edit(request, "rentals"):
            return denied(request, "rentals")
        data = await form(request)
        after = count_field(data, "search_after_days", what="Срок до розыска",
                            default=str(logic.SEARCH_AFTER_DAYS), limit=365)
        theft = count_field(data, "theft_after_days", what="Срок до признания потери",
                            default=str(logic.THEFT_AFTER_DAYS), limit=365)
        for check in (after, theft):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/rentals/search")
        await crm.set_setting("search_after_days", str(after.value), by=who(request))
        await crm.set_setting("theft_after_days", str(theft.value), by=who(request))
        flash(request, "Правило розыска сохранено.")
        return redirect("/rentals/search")

    @app.post("/rentals/{rental_id}/search")
    async def rental_search(request: Request, rental_id: int) -> Response:
        if not may_edit(request, "rentals"):
            return denied(request, "rentals")
        rental = await crm.rental(rental_id)
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        data = await form(request)
        note = logic.check_note(data.get("note"))
        if not note.ok:
            flash(request, note.error, "err")
            return redirect(f"/rentals/{rental_id}")
        action = data.get("action") or "start"
        try:
            if action == "stop":
                await service.stop_search(crm, rental, by=who(request))
                flash(request, "Розыск снят.")
            elif action == "theft":
                await service.declare_theft(crm, rental, note=note.value,
                                            by=who(request))
                flash(request, "Велосипед признан потерянным, аренда закрыта. "
                               "Долг клиента остался в журнале.")
            else:
                await service.start_search(crm, rental, note=note.value,
                                           by=who(request))
                flash(request, "Аренда в розыске.")
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
        return redirect(f"/rentals/{rental_id}")

    @app.get("/rentals/{rental_id}")
    async def rental_card(request: Request, rental_id: int) -> Response:
        rental = await crm.rental(rental_id)
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        ledger = [x for x in await crm.ledger_of(rental["client_id"], 200)
                  if x.get("rental_id") == rental_id]
        summary = summarize(rental if rental["status"] == "active" else None,
                            rental.get("balance", 0))
        bike = await crm.bike(rental["bike_id"]) if rental.get("bike_id") else None
        moves = logic.rental_bike_rows(await crm.rental_bikes(rental_id))
        # Комментарий к оценке - слова клиента, как переписка «Входящих»:
        # отчёт «Оценки» открыт только с правом на клиентов, и карточка
        # аренды (её видит и механик) не должна быть обходом. Оценка
        # остаётся: она и в служебном чате, который читают все.
        feedback = await crm.feedback_of_rental(rental_id)
        if feedback and feedback.get("comment") and not may_view(request, "clients"):
            feedback = {**feedback, "comment": None, "comment_hidden": True}
        return render(request, "rental.html", rental=rental, summary=summary, bike=bike,
                      order=(await crm.open_order_of(rental["bike_id"])
                             if rental.get("bike_id") else None),
                      days_running=logic.rental_days(rental, today=date.today()),
                      remind_kind=logic.manual_reminder_kind(summary),
                      intent=logic.intent_state(rental, summary, today=date.today()),
                      ledger=ledger,
                      tariffs=[t for t in await crm.tariffs(active_only=True)
                               if (t.get("kind") or "bike") == "bike"],
                      moves=moves,
                      total_km=logic.rental_mileage(
                          moves, current=(bike or {}).get("mileage_km")),
                      swap_bikes=logic.swap_candidates(
                          await crm.bikes(status="available", limit=10000),
                          current_id=rental.get("bike_id")),
                      batteries=logic.battery_rows(
                          await crm.batteries(rental_id=rental_id)),
                      free_batteries=logic.battery_options(
                          await crm.batteries(status="available", limit=500),
                          await crm.tariffs(active_only=True),
                          rental.get("period_days")),
                      extras=await crm.rental_extras(rental_id),
                      extras_total=logic.extras_total(
                          await crm.rental_extras(rental_id, live_only=True)),
                      max_extra=logic.MAX_EXTRA_BATTERIES,
                      ops=await crm.ops_reports_of_rental(rental_id),
                      return_photos=await crm.return_photos(rental_id=rental_id),
                      feedback=feedback,
                      # Возврат и замена - по умолчанию на точке аренды: в
                      # аренде велосипед числится именно там.
                      places=await location_names(rental.get("location"),
                                                  (bike or {}).get("location")),
                      home=rental.get("location") or (bike or {}).get("location"))

    @app.post("/rentals/{rental_id}/extras")
    async def rental_extra_add(request: Request, rental_id: int) -> Response:
        """Доп. аккумулятор в идущую аренду - платной позицией.

        Новая цена действует со следующего начисления: текущий период уже
        начислен, и менять клиенту сумму после того, как он её увидел,
        нельзя.
        """
        if not may_edit(request, "rentals"):
            return denied(request, "rentals")
        rental = await crm.rental(rental_id)
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        data = await form(request)
        battery = await by_id(crm.battery, data.get("battery_id"))
        if battery is None:
            flash(request, "Выберите аккумулятор.", "err")
            return redirect(f"/rentals/{rental_id}")
        try:
            price = await service.add_battery_extra(
                crm, rental, battery, tariffs=await crm.tariffs(active_only=True),
                by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/rentals/{rental_id}")
        flash(request, f"Доп. аккумулятор № {battery['code']} выдан"
                       + (f": +{logic.money(price)} к периоду со следующего начисления."
                          if may_view(request, "finance") else "."))
        return redirect(f"/rentals/{rental_id}")

    @app.post("/rentals/{rental_id}/extras/{extra_id}")
    async def rental_extra_drop(request: Request, rental_id: int,
                                extra_id: int) -> Response:
        if not may_edit(request, "rentals"):
            return denied(request, "rentals")
        rental = await crm.rental(rental_id)
        extra = await crm.rental_extra(extra_id)
        if rental is None or extra is None:
            return render(request, "missing.html", status_code=404, what="Позиция")
        data = await form(request)
        # Статус проверяет сервис: «у клиента» и «на сборке» руками не
        # ставятся, и подменённая форма получает отказ, а не их.
        status = data.get("status") or "available"
        try:
            await service.drop_battery_extra(crm, rental, extra, by=who(request),
                                             status=status)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/rentals/{rental_id}")
        flash(request, "Позиция снята, аккумулятор принят. "
                       "Цена периода уменьшится со следующего начисления.")
        return redirect(f"/rentals/{rental_id}")

    @app.post("/rentals/{rental_id}/remind")
    async def rental_remind(request: Request, rental_id: int) -> Response:
        """Напоминание клиенту по кнопке: тот же текст, что шлёт расписание,
        но сейчас и мимо тумблера - оператор нажал сам."""
        data = await form(request)
        nxt = data.get("next") or ""
        back = logic.safe_next(nxt, f"/rentals/{rental_id}")
        rental = await crm.rental(rental_id)
        if rental is None or rental["status"] != "active":
            flash(request, "Аренда не идёт - напоминать не о чем.", "err")
            return redirect(back)
        if not rental.get("tg_id"):
            flash(request, f"{rental['full_name']}: клиента нет в боте, "
                           "напоминание отправить некуда - позвоните.", "err")
            return redirect(back)
        if bot is None:
            flash(request, "Бот не подключён к панели.", "err")
            return redirect(back)
        kind = logic.manual_reminder_kind(summarize(rental, rental.get("balance", 0)))
        sent = await billing.send_reminder(bot, db, crm, rental, kind=kind,
                                           today=date.today(), manual=True)
        if kind:
            # Одно напоминание в день на аренду: без отметки расписание в
            # тот же день прислало бы клиенту второе.
            await crm.mark_notified(rental_id, date.today(), kind)
        flash(request, f"{rental['full_name']}: напоминание отправлено." if sent
              else f"{rental['full_name']}: не доставлено - клиент заблокировал бота?",
              "ok" if sent else "err")
        return redirect(back)

    @app.post("/rentals/{rental_id}/intent")
    async def rental_intent(request: Request, rental_id: int) -> Response:
        """Что клиент сказал про истекающий срок: продлит, сдаёт, или
        отложить строку до завтра. Хранится с датой «оплачено до» на момент
        отметки, поэтому после оплаты устаревает само."""
        data = await form(request)
        nxt = data.get("next") or ""
        back = logic.safe_next(nxt, f"/rentals/{rental_id}")
        rental = await crm.rental(rental_id)
        if rental is None or rental["status"] != "active":
            flash(request, "Аренда не идёт - отмечать нечего.", "err")
            return redirect(back)
        action = data.get("intent") or ""
        name = rental["full_name"]
        if action in logic.INTENTS:
            summary = summarize(rental, rental.get("balance", 0))
            await crm.update_rental(rental_id, intent=action,
                                    intent_until=summary["covered_until"],
                                    intent_by=who(request), intent_at=datetime.now(UTC),
                                    snooze_until=None)
            await crm.log_rental_intent(rental_id, action, who(request))
            flash(request, f"{name}: {logic.INTENTS[action]}.")
        elif action == "snooze":
            await crm.update_rental(rental_id, snooze_until=date.today() + timedelta(days=1))
            flash(request, f"{name}: отложено до завтра.")
        elif action == "clear":
            await crm.update_rental(rental_id, intent=None, intent_until=None, intent_by=None,
                                    intent_at=None, snooze_until=None)
            flash(request, f"{name}: отметка снята.")
        else:
            flash(request, "Неизвестное действие.", "err")
        return redirect(back)

    @app.post("/rentals/{rental_id}/swap")
    async def rental_swap(request: Request, rental_id: int) -> Response:
        """Заменить велосипед, не трогая деньги и сроки аренды."""
        if not may_edit(request, "rentals"):
            return denied(request, "rentals")
        rental = await crm.rental(rental_id)
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        data = await form(request)
        reason = logic.check_swap_reason(data.get("reason") or "repair")
        new_bike = await by_id(crm.bike, data.get("bike_id"))
        old_bike = await crm.bike(rental["bike_id"]) if rental.get("bike_id") else None
        mileage_old = logic.check_mileage(
            data.get("mileage_old"), current=(old_bike or {}).get("mileage_km"),
            required=False)
        # Тот же велосипед - случай отдельный: сверять его одометр «с самим
        # собой» бессмысленно, и оператор получил бы разговор про пробег
        # вместо понятного «это тот же велосипед».
        same = bool(old_bike and new_bike and int(old_bike["id"]) == int(new_bike["id"]))
        mileage_new = logic.check_mileage(
            data.get("mileage_new"),
            current=None if same else (new_bike or {}).get("mileage_km"),
            required=False)
        # Где меняли: там остаётся снятый велосипед. Пусто - точка аренды.
        place = logic.check_location(data.get("swap_location"), await location_names(
            rental.get("location"), (old_bike or {}).get("location")))
        for check in (reason, mileage_old, mileage_new, place):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/rentals/{rental_id}")
        if new_bike is None:
            flash(request, "Выберите велосипед на замену.", "err")
            return redirect(f"/rentals/{rental_id}")
        try:
            await service.swap_bike(
                crm, rental, new_bike, reason=reason.value,
                mileage_old=mileage_old.value, mileage_new=mileage_new.value,
                old_status=data.get("old_status") or None, by=who(request),
                swap_location=place.value)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/rentals/{rental_id}")
        flash(request, f"Велосипед заменён на № {new_bike['code']}. "
                       "Деньги и сроки аренды не изменились.")
        return redirect(f"/rentals/{rental_id}")

    @app.post("/rentals/{rental_id}/close")
    async def rental_close(request: Request, rental_id: int) -> Response:
        rental = await crm.rental(rental_id)
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        data = await form(request)
        note = logic.check_note(data.get("note"))
        closed = logic.check_date(data.get("closed_on"), default=date.today())
        # Пробег возврата необязателен: велосипед могли принять без дисплея
        # (разряжен, разбит). Тогда «накатал» у этой аренды останется пустым.
        bike = await crm.bike(rental["bike_id"]) if rental.get("bike_id") else None
        floor = (bike or {}).get("mileage_km", rental.get("mileage_start"))
        mileage = logic.check_mileage(data.get("mileage"), current=floor, required=False)
        # Точка возврата: сданный на другой точке велосипед дальше простаивает
        # там, а не там, где его выдали. Пусто - точка аренды.
        place = logic.check_location(data.get("return_location"), await location_names(
            rental.get("location"), (bike or {}).get("location")))
        for check in (note, closed, mileage, place):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/rentals/{rental_id}")
        # Фото проверяются до закрытия: негодный файл - повод поправить
        # форму, а не закрытая аренда без снимков.
        try:
            shots = await return_uploads(request)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/rentals/{rental_id}")
        try:
            await service.close_rental(crm, rental, closed_on=closed.value, note=note.value,
                                       bike_status=data.get("bike_status") or "available",
                                       by=who(request), mileage=mileage.value,
                                       return_location=place.value)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/rentals/{rental_id}")
        saved, failed = 0, ""
        for raw, suffix in shots:
            try:
                await photos.save(crm, cfg.bike_photo_dir, rental, raw, suffix,
                                  by=who(request))
                saved += 1
            except service.ServiceError as exc:
                failed = str(exc)
        client = await crm.client(rental["client_id"])
        await notify.rental_closed(bot, db, crm, client, rental)
        km = logic.ridden({**rental, "mileage_end": mileage.value})
        flash(request, "Аренда закрыта, велосипед освобождён."
              + (f" Накатал {km} км." if km is not None else "")
              + (f" Фото при сдаче: {saved}." if saved else ""))
        if failed:
            flash(request, f"Аренда закрыта, но не все фото сохранились: {failed}", "err")
        return redirect(f"/rentals/{rental_id}")

    async def return_uploads(request: Request) -> list[tuple[bytes, str]]:
        """Фото при сдаче из формы закрытия: не больше шести, jpg/png/webp,
        до 8 МБ каждое. Пустые поля браузера пропускаем. В демо файлы
        посетителей на диск не пишем - как снимки сверки."""
        data = await request.form()
        uploads = [u for u in data.getlist("photos")
                   if not isinstance(u, str) and getattr(u, "filename", "")]
        if cfg.demo:
            for upload in uploads:
                await upload.close()
            if uploads:
                flash(request, DEMO_PHOTO_TEXT)
            return []
        try:
            if len(uploads) > logic.RETURN_PHOTOS_MAX:
                raise service.ServiceError(
                    f"Фото при сдаче: не больше {logic.RETURN_PHOTOS_MAX}.")
            shots: list[tuple[bytes, str]] = []
            for upload in uploads:
                suffix = logic.return_photo_suffix(upload.filename)
                if suffix is None:
                    raise service.ServiceError("Фото при сдаче: только jpg, png или webp.")
                raw = await upload.read()
                if len(raw) > logic.RETURN_PHOTO_MAX_BYTES:
                    raise service.ServiceError(
                        f"Фото «{upload.filename}» больше "
                        f"{logic.RETURN_PHOTO_MAX_BYTES // (1024 * 1024)} МБ — "
                        "сфотографируйте меньшим размером.")
                if raw:
                    shots.append((raw, suffix))
            return shots
        finally:
            # Файлы формы больше мегабайта Starlette держит во временных
            # файлах на диске - закрываем сразу, а не когда соберёт мусор.
            for upload in uploads:
                await upload.close()

    @app.get("/rentals/{rental_id}/photos/{photo_id}")
    async def rental_photo(request: Request, rental_id: int, photo_id: int) -> Response:
        """Фото при сдаче - только вошедшим с правом на аренды (страж
        раздела по адресу). Путь берём из базы и сверяем с шаблоном: путь
        из адреса или из чужой строки увёл бы куда угодно."""
        row = await crm.return_photo(photo_id)
        path = photos.path_of(cfg.bike_photo_dir, (row or {}).get("path"))
        if row is None or int(row["rental_id"]) != rental_id or path is None \
                or not path.is_file():
            return render(request, "missing.html", status_code=404, what="Фото")
        return FileResponse(path)

    @app.post("/rentals/{rental_id}/tariff")
    async def rental_tariff(request: Request, rental_id: int) -> Response:
        rental = await crm.rental(rental_id)
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        # Цена аренды - деньги: форма смены тарифа видна только с
        # «Финансами», и маршрут требует того же.
        if not may_view(request, "finance"):
            return denied(request, "finance")
        data = await form(request)
        billing = data.get("billing") or rental["billing"]
        if billing not in logic.BILLING:
            flash(request, "Недопустимый режим начисления.", "err")
            return redirect(f"/rentals/{rental_id}")
        if billing != rental["billing"] and not logic.can_act(request.state.staff,
                                                              "money_edit"):
            return denied(request, "money_edit")
        tariff = await by_id(crm.tariff, data.get("tariff_id"))
        if tariff is None or rental["status"] != "active":
            flash(request, "Выберите тариф; менять можно только у идущей аренды.", "err")
            return redirect(f"/rentals/{rental_id}")
        bike = await crm.bike(rental["bike_id"]) if rental.get("bike_id") else None
        tariff, why = await fit_tariff(tariff, bike)
        if tariff is None:
            flash(request, why, "err")
            return redirect(f"/rentals/{rental_id}")
        try:
            await service.change_tariff(crm, rental, tariff, billing=billing)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/rentals/{rental_id}")
        flash(request, "Тариф изменён со следующего периода.")
        return redirect(f"/rentals/{rental_id}")

    # ─────────────────────── тарифы ───────────────────────

    @app.get("/tariffs")
    async def tariffs(request: Request) -> Response:
        rows = await crm.tariffs()
        # Порядок строки в таблице: сначала запасные «любая модель», дальше
        # модели по алфавиту, внутри модели - по сроку. Иначе одинаковые
        # тарифы разных моделей стоят вперемешку и цены не сравнить.
        rows.sort(key=lambda t: (str(t.get("model") or "").lower(),
                                 int(t.get("period_days") or 0)))
        models = await crm.bike_models(active_only=True)
        batteries = await crm.battery_models(active_only=True)
        # Модели, у которых нет ни одной своей цены: на выдаче они уедут
        # на запасной тариф, и это стоит видеть до выдачи, а не после.
        def unpriced(catalogue: list[dict], kind: str) -> list[str]:
            priced = {str(t.get("model") or "") for t in rows
                      if t.get("active") and (t.get("kind") or "bike") == kind}
            return [m["title"] for m in catalogue if m["title"] not in priced]

        def has_common(kind: str) -> bool:
            return any(not t.get("model") for t in rows
                       if t.get("active") and (t.get("kind") or "bike") == kind)

        return render(request, "tariffs.html",
                      groups=[
                          {"kind": "bike", "title": logic.TARIFF_KINDS["bike"],
                           "rows": [t for t in rows
                                    if (t.get("kind") or "bike") == "bike"],
                           "models": models, "unpriced": unpriced(models, "bike"),
                           "has_common": has_common("bike"),
                           "hint": "Цена велосипеда за период. Тариф без модели — "
                                   "запасной: он работает, пока у модели нет своей."},
                          {"kind": "battery", "title": logic.TARIFF_KINDS["battery"],
                           "rows": [t for t in rows
                                    if (t.get("kind") or "bike") == "battery"],
                           "models": batteries,
                           "unpriced": unpriced(batteries, "battery"),
                           "has_common": has_common("battery"),
                           "hint": "Цена доп. аккумулятора за тот же период, что "
                                   "и аренда. Нет цены на срок — доп. аккумулятор "
                                   "на этот срок не выдать."},
                      ])

    def _tariff_fields(request: Request, data: dict) -> dict | None:
        name = logic.check_name(data.get("name"), what="Название")
        period = logic.check_period(data.get("period_days"))
        price = logic.check_amount(data.get("price"))
        note = logic.check_note(data.get("note"))
        for check in (name, period, price, note):
            if not check.ok:
                flash(request, check.error, "err")
                return None
        kind = logic.check_tariff_kind(data.get("kind"))
        if not kind.ok:
            flash(request, kind.error, "err")
            return None
        return {"name": name.value, "period_days": period.value, "price": price.value,
                "note": note.value, "kind": kind.value,
                "model": (data.get("model") or "").strip() or None}

    async def store_tariff(request: Request, fields: dict,
                           tariff_id: int | None = None) -> bool:
        """Тариф в базу: новый или правка. «Одна цена на модель и срок»
        держит частичный уникальный индекс, здесь он переводится в слова;
        False - отказ уже во flash. Один путь для «Тарифов» и мастера
        первого запуска."""
        try:
            if tariff_id is None:
                await crm.create_tariff(**fields)
            else:
                await crm.update_tariff(tariff_id, **fields)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Такой срок для этой модели уже есть — исправьте цену "
                               "в существующем тарифе." if tariff_id is None
                      else "Такой срок для этой модели уже есть.", "err")
                return False
            raise
        return True

    @app.post("/tariffs")
    async def tariff_create(request: Request) -> Response:
        fields = _tariff_fields(request, await form(request))
        if fields is not None and await store_tariff(request, fields):
            flash(request, "Тариф добавлен.")
        return redirect("/tariffs")

    @app.post("/tariffs/{tariff_id}")
    async def tariff_edit(request: Request, tariff_id: int) -> Response:
        if await crm.tariff(tariff_id) is None:
            return render(request, "missing.html", status_code=404, what="Тариф")
        data = await form(request)
        if data.get("action") == "toggle":
            tariff = await crm.tariff(tariff_id)
            await crm.update_tariff(tariff_id, active=not tariff["active"])
            flash(request, "Тариф " + ("включён." if not tariff["active"] else "выключен."))
            return redirect("/tariffs")
        fields = _tariff_fields(request, data)
        if fields is not None and await store_tariff(request, fields, tariff_id):
            flash(request, "Тариф сохранён.")
        return redirect("/tariffs")

    # ─────────────────────── финансы и заявки ───────────────────────

    # Журнал финансов листается, как остальные списки. Месяц журнала - это
    # тысяча с лишним строк: одной страницей с подписью у каждой ячейки
    # (карточки телефона) он весил за полмегабайта, а прежний предел в
    # тысячу строк молча отрезал хвост месяца. Теперь подвал говорит,
    # сколько записей нашлось, а плитки по-прежнему считают весь период.
    LEDGER_SORTS = {"date": "created_at", "client": "full_name", "kind": "kind",
                    "amount": "amount", "method": "method", "by": "created_by"}

    @app.get("/finance")
    async def finance(request: Request) -> Response:
        since = logic.check_date(request.query_params.get("since"),
                                 default=date.today().replace(day=1))
        until = logic.check_date(request.query_params.get("until"), default=date.today())
        kind = request.query_params.get("kind") or ""
        if not since.ok or not until.ok:
            flash(request, "Дата: в виде ДД.ММ.ГГГГ.", "err")
            return redirect("/finance")
        rows = await crm.ledger(since=since.value, until=until.value,
                                kind=kind or None, limit=100000)
        tools = list_tools(request, rows, allowed=LEDGER_SORTS)
        totals = await crm.ledger_totals(since=since.value, until=until.value)
        return render(request, "finance.html", rows=tools["rows"], tools=tools,
                      totals=totals, since=since.value, until=until.value, kind=kind,
                      # Сумма по найденному - только внутри одного вида: платежи
                      # вперемешку с начислениями в одно число не складываются.
                      found_sum=logic.sum_of(tools["all_rows"], "amount") if kind
                      else None)

    @app.get("/finance.{ext}")
    async def finance_csv(request: Request, ext: str) -> Response:
        since = logic.check_date(request.query_params.get("since"),
                                 default=date.today().replace(day=1))
        until = logic.check_date(request.query_params.get("until"), default=date.today())
        kind = request.query_params.get("kind") or ""
        if not since.ok or not until.ok:
            return redirect("/finance")
        rows = [[x["created_at"], x["full_name"], logic.KINDS.get(x["kind"], x["kind"]),
                 logic.to_money(x["amount"]),
                 logic.period_label(x.get("period_from"), x.get("period_to")),
                 logic.METHODS.get(x.get("method"), x.get("method") or ""),
                 x.get("note"), x.get("created_by")]
                for x in await crm.ledger(since=since.value, until=until.value,
                                          kind=kind or None, limit=100000)]
        name = f"finance-{since.value:%Y%m%d}-{until.value:%Y%m%d}"
        return await table(ext, name, ["Дата", "Клиент", "Вид", "Сумма", "Период", "Способ",
                           "Заметка", "Кто"], rows)

    @app.get("/claims")
    async def claims(request: Request) -> Response:
        rows = await crm.pending_claims()
        for r in rows:
            r["balance"] = await crm.client_balance(r["client_id"])
        return render(request, "claims.html", rows=rows)

    @app.post("/claims/{claim_id}/confirm")
    async def claim_confirm(request: Request, claim_id: int) -> Response:
        claim = await crm.claim(claim_id)
        if claim is None:
            return render(request, "missing.html", status_code=404, what="Заявка")
        data = await form(request)
        amount = logic.check_amount(data.get("amount") or claim.get("amount_hint"))
        method = data.get("method") or "sbp"
        if not amount.ok or not logic.check_choice(method, logic.METHODS).ok:
            flash(request, amount.error or "Способ оплаты: недопустимое значение.", "err")
            return redirect("/claims")
        try:
            ledger_id = await service.credit_claim(
                crm, claim, amount.value, by=who(request), method=method,
                twice_ok=bool(data.get("twice_ok")))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/claims")
        if ledger_id is None:
            flash(request, "Заявку уже обработали.", "err")
            return redirect("/claims")
        client = await crm.client(claim["client_id"])
        await notices.send_client(
                crm, "pay_credited", client["id"],
                lambda: notify.payment_credited(bot, db, crm, client,
                                                amount.value))
        await referral_bonus(client, amount.value, who(request))
        flash(request, f"Зачислено {logic.money(amount.value)} клиенту {client['full_name']}.")
        return redirect("/claims")

    @app.post("/claims/{claim_id}/reject")
    async def claim_reject(request: Request, claim_id: int) -> Response:
        claim = await crm.claim(claim_id)
        if claim is None:
            return render(request, "missing.html", status_code=404, what="Заявка")
        if not await service.reject_claim(crm, claim, by=who(request)):
            flash(request, "Заявку уже обработали.", "err")
            return redirect("/claims")
        await notify.payment_rejected(bot, db, claim)
        flash(request, "Заявка отклонена.")
        return redirect("/claims")

    # ─────────────────────── отчёты ───────────────────────

    async def months_data(now: datetime) -> dict:
        """Три числа и деньги по месяцам - одно на свёрнутый блок отчётов и
        полную страницу /reports/months: там обязаны стоять те же числа.
        Неполный месяц - текущий или тот, где начался журнал статусов, -
        помечен: платежи месяца на несколько дней аренды раздувают чек."""
        started = (await crm.history_starts())["status"]
        months_metrics = []
        for m in logic.month_windows(now, 6):
            months_metrics.append({
                "month": m["month"],
                **await period_metrics(since=m["since"], until=m["until"]),
                "coverage": logic.month_coverage(m["since"], m["until"], start=started)})
        return {"months_metrics": months_metrics,
                "months": await crm.revenue_by_month(12)}

    @app.get("/reports/months")
    async def reports_months(request: Request) -> Response:
        """Полная история по месяцам: все колонки, которые в сводке отчётов
        свёрнуты до главных (потери, КПД, дни, возвраты)."""
        return render(request, "report_months.html",
                      **await months_data(datetime.now().astimezone()))

    @app.get("/reports")
    async def reports(request: Request) -> Response:
        bikes_by = await crm.bike_counts()
        fleet_rows = await crm.bikes(limit=10000)
        fleet = sum(bikes_by.get(s, 0) for s in logic.OPERATIONAL_STATUSES)
        rented = bikes_by.get("rented", 0)
        now = datetime.now().astimezone()
        # Ровно 12 календарных месяцев, включая текущий: тем же шагом,
        # что и таблица по месяцам, а не «минус 335 дней».
        since_year = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for _ in range(11):
            since_year = (since_year - timedelta(days=1)).replace(day=1)
        # Одно окно: сводные таблицы всех отчётов за один период. Каждый
        # блок считается тем же построителем, что его полный отчёт, и за тот
        # же период - в одном окне не бывает двух разных «выручек за
        # сентябрь». Блок - только с правом на свой полный отчёт.
        floor = await history_floor(now.date())
        span = logic.report_period(request.query_params, now=now, floor=floor)
        if span["kind"] == "days":
            # «30 дней» - целыми сутками по сегодня: полные отчёты сервиса и
            # окупаемости считают период календарными днями, и блок обязан
            # совпадать со своим «подробнее», а «Главное» - с блоками.
            days = logic.report_period(
                {"since": (now.date() - timedelta(days=logic.POINTS_PERIOD_DAYS - 1))
                 .isoformat(), "until": now.date().isoformat()}, now=now, floor=floor)
            span = {**days, "kind": "days", "label": span["label"], "query": ""}
        window = SimpleNamespace(query_params={"since": span["since"].isoformat(),
                                               "until": span["until"].isoformat()})
        money_ok = may_view(request, "finance")
        summary: dict[str, Any] = {
            "span": span, "base": "/reports",
            "q": "?since=" + span["since"].isoformat() + "&until="
                 + span["until"].isoformat(),
            "metrics": await period_metrics(since=span["start"], until=span["end"]),
            "points": (await points_data(window))["report"],
            "tariffs": (await tariffs_data(window))["rows"]}
        if money_ok:
            summary["money"] = await crm.ledger_totals(since=span["since"],
                                                       until=span["until"])
            summary["payback"] = (await payback_data(window))["rows"]
            summary["buy"] = [r for r in (await buy_data(window))["rows"]
                              if r.get("verdict") == "buy"][:5]
        if may_view(request, "service"):
            techs = logic.tech_rows(await crm.tech_work(span["start"], span["end"]))
            summary["techs"] = {"rows": techs, "total": logic.tech_total(techs)}
            summary["model_parts"] = logic.model_parts_rows(
                await crm.model_parts(span["start"], span["end"]), fleet_rows,
                days=span["days"])[:6]
        if may_view(request, "inventory"):
            summary["spend"] = logic.spend_rows(
                await crm.part_spend(span["start"], span["end"]))[:6]
        if may_view(request, "clients"):
            summary["channels"] = logic.channel_totals(
                await crm.clients_since(span["start"]), since=span["since"],
                until=span["until"])
            summary["feedback"] = logic.feedback_stats(
                [r for r in await crm.feedback_rows(span["since"])
                 if r.get("closed_on") is None or r["closed_on"] <= span["until"]])
        if may_view(request, "bikes"):
            summary["integrity"] = logic.integrity_summary(await integrity_data(request))
        # «Главное»: те же числа за выбранный период и за прошлый такой же -
        # тем же построителем, что и блоки ниже, чтобы строки сходились.
        prev = logic.report_prev_span(span, now=now)
        total = summary["points"]["total"]
        now_figures: dict[str, Any] = {
            "metrics": summary["metrics"], "issued": total["issued"],
            "renewals": total["renewals"],
            "days": logic.span_days(span["start"], min(span["end"], now)),
            "debt": total.get("debt") if money_ok else None,
            "debtors": total.get("debtors")}
        prev_rentals = await crm.rentals_by_location(prev["start"], prev["end"])
        prev_figures: dict[str, Any] = {
            "metrics": await period_metrics(since=prev["start"], until=prev["end"]),
            "issued": sum(int(r.get("issued") or 0) for r in prev_rentals.values()),
            "renewals": sum(int(r.get("renewals") or 0) for r in prev_rentals.values()),
            "days": prev["days"]}
        if "techs" in summary:
            now_figures["orders"] = summary["techs"]["total"]["orders"]
            prev_figures["orders"] = logic.tech_total(logic.tech_rows(
                await crm.tech_work(prev["start"], prev["end"])))["orders"]
        if "channels" in summary:
            now_figures["new_clients"] = sum(n for _, n in summary["channels"])
            prev_figures["new_clients"] = sum(n for _, n in logic.channel_totals(
                await crm.clients_since(prev["start"]), since=prev["since"],
                until=prev["until"]))
        if "integrity" in summary:
            now_figures["integrity"] = summary["integrity"]["total"]
        summary["prev"] = prev
        summary["headline"] = logic.report_headline(
            now_figures, prev_figures, can=lambda section: may_view(request, section))
        return render(request, "reports.html",
                      summary=summary,
                      bikes=bikes_by, fleet=fleet, rented=rented,
                      utilization=(round(100 * rented / fleet) if fleet else 0),
                      **await months_data(now),
                      amortization=logic.amortization_total(
                          fleet_rows, await crm.batteries(limit=10000)),
                      priced=sum(1 for b in fleet_rows
                                 if b.get("status") in logic.OPERATIONAL_STATUSES
                                 and b.get("purchase_price") is not None),
                      repairs=await crm.repair_stats(since_year, now),
                      debtors=await crm.debtors(50))

    async def payback_data(request: Request) -> dict:
        """Окупаемость по моделям за период. Период - как в финансах:
        с начала месяца по сегодня, если не задан другой."""
        # report_day, как в period_of: год 9999 плюс сутки - это 500.
        today = date.today()
        since = logic.report_day(request.query_params.get("since"), today=today,
                                 default=today.replace(day=1))
        until = logic.report_day(request.query_params.get("until"), today=today,
                                 default=today)
        if not since.ok or not until.ok:
            since = logic.Check(True, date.today().replace(day=1))
            until = logic.Check(True, date.today())
        tz = datetime.now().astimezone().tzinfo
        start = datetime.combine(since.value, datetime.min.time(), tzinfo=tz)
        # Верхняя граница включительно по дате: отчёт «по сегодня» обязан
        # содержать сегодняшние платежи.
        end = datetime.combine(until.value + timedelta(days=1),
                               datetime.min.time(), tzinfo=tz)
        money = await crm.model_money(start, end)
        rows = logic.payback_rows(await crm.bikes(limit=10000), money,
                                  days=(until.value - since.value).days + 1)
        return {"rows": rows, "total": logic.payback_total(rows),
                "since": since.value, "until": until.value}

    @app.get("/reports/payback")
    async def payback_report(request: Request) -> Response:
        if not may_view(request, "finance"):
            return denied(request, "finance")
        return render(request, "payback.html", **await payback_data(request))

    @app.get("/reports/payback.{ext}")
    async def payback_csv(request: Request, ext: str) -> Response:
        if not may_view(request, "finance"):
            return denied(request, "finance")
        data = await payback_data(request)
        rows = [[r["model"], r["bikes"], round(float(r["rented_days"]), 1),
                 r["check_per_day"], r["paid"], r["charged"], r["repair_cost"],
                 r["works"], r["amortization"], r["margin"], r["margin_percent"]]
                for r in data["rows"]]
        total = data["total"]
        rows.append(["ИТОГО", total["bikes"], round(float(total["rented_days"]), 1),
                     total["check_per_day"], total["paid"], total["charged"],
                     total["repair_cost"], total["works"], total["amortization"],
                     total["margin"], total["margin_percent"]])
        name = f"payback-{data['since']:%Y%m%d}-{data['until']:%Y%m%d}"
        return await table(ext, name, ["Модель", "Великов", "Дней в аренде", "Чек/день",
                           "Оплачено", "Начислено", "Ремонт", "Работы клиентам",
                           "Амортизация", "Маржа", "Маржа %"], rows)

    # ─────────────────────── выгодность тарифов ───────────────────────
    #
    # Срок аренды против чека и удержания. Период - как у «По точкам»: 30
    # дней до этой минуты (окно сводки, и чек «Итого» совпадает с ней),
    # месяц или свой интервал. Рубли - только с правом на финансы.

    async def tariffs_data(request: Request, *, by_model: bool | None = None) -> dict:
        span = logic.report_period(request.query_params, now=datetime.now().astimezone())
        if by_model is None:
            by_model = request.query_params.get("by") == "model"
        aliases = logic.model_aliases(await crm.bike_models()) if by_model else None
        report = logic.tariff_rows(await crm.tariff_rentals(span["start"], span["end"]),
                                   by_model=by_model, aliases=aliases,
                                   window_days=(span["until"] - span["since"]).days + 1)
        # Хвост адреса: период и разрез едут в выгрузку - выгружают то, что видят.
        query = "&".join(x for x in (span["query"], "by=model" if by_model else "") if x)
        return {"span": span, "by_model": by_model, "query": query, **report}

    @app.get("/reports/tariffs")
    async def tariffs_report(request: Request) -> Response:
        return render(request, "tariff_report.html", **await tariffs_data(request))

    # Разрез «срок и модель» - своим адресом: переключатели периода
    # (_points_period.html) знают только адрес страницы, и с ?by=model
    # каждая смена месяца молча возвращала таблицу по срокам. ?by=model
    # остаётся для старых ссылок и выгрузки.
    @app.get("/reports/tariffs/model")
    async def tariffs_by_model(request: Request) -> Response:
        return render(request, "tariff_report.html",
                      **await tariffs_data(request, by_model=True))

    # Колонки выгрузки: заголовок, значение строки, денежная ли.
    TARIFF_COLUMNS: tuple[tuple[str, Any, bool], ...] = (
        ("Выдано", lambda r: r["issued"], False),
        ("Закрыто", lambda r: r["finished"], False),
        ("Из них потеряно", lambda r: r["lost"], False),
        ("Средний срок, дн.", lambda r: r["avg_days"], False),
        ("Продлили хоть раз, %", lambda r: r["renewed_share"], False),
        ("Продлений в среднем", lambda r: r["avg_renewals"], False),
        ("Сдали раньше срока, %", lambda r: r["early_share"], False),
        ("Дней в аренде", lambda r: round(float(r["rented_days"]), 1), False),
        ("Цена по тарифу/день", lambda r: r["price_per_day"], True),
        ("Оплачено", lambda r: r["paid"], True),
        ("Доля выручки, %", lambda r: r["revenue_share"], True),
        ("Чек/день", lambda r: r["avg_check"], True),
        ("Долг", lambda r: r["debt"], True),
        ("Долг от начисленного, %", lambda r: r["debt_share"], True),
    )

    @app.get("/reports/tariffs.{ext}")
    async def tariffs_table(request: Request, ext: str) -> Response:
        if ext not in EXPORT_FORMATS:
            raise HTTPException(status_code=404)
        data = await tariffs_data(request)
        columns = [c for c in TARIFF_COLUMNS if may_view(request, "finance") or not c[2]]
        by_model = data["by_model"]
        out = [[logic.period_title(r["period_days"]) if r["period_days"] else r["title"],
                *([r["model"] or ""] if by_model else []),
                *(get(r) for _, get, _ in columns)]
               for r in (*data["rows"], data["total"])]
        span = data["span"]
        return await table(ext, f"tariffs-{span['since']:%Y%m%d}-{span['until']:%Y%m%d}",
                           ["Срок", *(["Модель"] if by_model else []),
                            *(title for title, _, _ in columns)], out)

    # ─────────────────────── что купить следующим ───────────────────────
    #
    # Подсказка к партии поверх окупаемости: деньги модели - тот же
    # model_money и payback_rows, дни и «без свободной» - журналы статусов и
    # мест по суткам, спрос - открытые заявки. Период по умолчанию - 90 дней
    # по сегодня: партию по одному месяцу не решают. Это рубли модели,
    # поэтому право - как у окупаемости.

    async def buy_data(request: Request) -> dict:
        now = datetime.now().astimezone()
        params: Any = request.query_params
        if not any(params.get(k) for k in ("month", "since", "until")):
            params = {"since": (now.date() - timedelta(days=logic.BUY_PERIOD_DAYS - 1))
                      .isoformat()}
        span = logic.report_period(params, now=now)
        fleet = await crm.bikes(limit=10000)
        aliases = logic.model_aliases(await crm.bike_models())
        payback = logic.payback_rows(fleet, await crm.model_money(span["start"], span["end"]),
                                     days=(span["until"] - span["since"]).days + 1)
        day_rows = await crm.model_point_days(span["since"], span["until"])
        raw = str(request.query_params.get("budget") or "").strip()
        budget = logic.check_amount(raw) if raw else None
        rows = logic.buy_rows(
            payback, days=logic.model_days(day_rows, aliases=aliases),
            zero=logic.zero_free_days(day_rows, before=now.date(), aliases=aliases),
            prices=logic.last_purchase_prices(fleet, await crm.purchases(limit=10000),
                                              aliases=aliases),
            demand=logic.booking_pressure(await crm.bookings(status="new", limit=1000),
                                          fleet, aliases=aliases),
            bikes=fleet, presence=logic.model_presence(day_rows, now=now, aliases=aliases),
            aliases=aliases)
        ok = budget is not None and budget.ok
        # Хвост адреса для выгрузки: тот же период и тот же бюджет.
        query = urlencode({"since": span["since"].isoformat(),
                           "until": span["until"].isoformat(),
                           **({"budget": raw} if ok else {})})
        return {"span": span, "rows": rows, "budget_raw": raw, "query": query,
                "budget_error": budget.error if budget is not None and not ok else "",
                "plan": logic.buy_plan(rows, budget=budget.value if ok else None)}

    @app.get("/reports/buy")
    async def buy_report(request: Request) -> Response:
        if not may_view(request, "finance"):
            return denied(request, "finance")
        return render(request, "buy.html", **await buy_data(request))

    @app.get("/reports/buy.{ext}")
    async def buy_table(request: Request, ext: str) -> Response:
        if ext not in EXPORT_FORMATS:
            raise HTTPException(status_code=404)
        if not may_view(request, "finance"):
            return denied(request, "finance")
        data = await buy_data(request)
        rows = [[r["model"], r["fleet"], r["assembly"], r["utilization"], r["idle_percent"],
                 r["repair_percent"], r["revenue_per_day"], r["repair_per_day"],
                 r["avg_check"], r["price"], r["price_no"] or "", r["payback_months"],
                 r["zero_days"], r["bookings"], r["unmet"],
                 logic.BUY_VERDICTS[r["verdict"]], r["count"] or "", r["reason"]]
                for r in data["rows"]]
        span = data["span"]
        return await table(ext, f"buy-{span['since']:%Y%m%d}-{span['until']:%Y%m%d}",
                           ["Модель", "В парке", "На сборке", "Загрузка, %", "Простой, %",
                            "Ремонт и ТО, %", "Выручка/велодень", "Ремонт/велодень",
                            "Чек/день", "Цена в последней закупке", "Закупка",
                            "Окупаемость, мес.", "Суток без свободной", "Заявок",
                            "Заявок без велосипеда", "Решение", "Сколько", "Почему"], rows)

    async def period_of(request: Request) -> dict:
        """Период отчёта: как в финансах - с начала месяца по сегодня."""
        # report_day: год 1 и 9999 - тоже даты, и «плюс сутки» на них
        # роняли отчёт 500; такие границы - мусор, будущее - сегодня.
        today = date.today()
        since = logic.report_day(request.query_params.get("since"), today=today,
                                 default=today.replace(day=1))
        until = logic.report_day(request.query_params.get("until"), today=today,
                                 default=today)
        if not since.ok or not until.ok:
            since = logic.Check(True, date.today().replace(day=1))
            until = logic.Check(True, date.today())
        tz = datetime.now().astimezone().tzinfo
        start = datetime.combine(since.value, datetime.min.time(), tzinfo=tz)
        # Верхняя граница включительно по дате: отчёт «по сегодня» обязан
        # содержать сегодняшние наряды.
        end = datetime.combine(until.value + timedelta(days=1),
                               datetime.min.time(), tzinfo=tz)
        return {"since": since.value, "until": until.value,
                "start": start, "end": end,
                "days": (until.value - since.value).days + 1}

    # ─────────────────────── отчёт по точкам ───────────────────────
    #
    # Те же три числа и те же формулы, только ограниченные точкой: дни -
    # по журналу мест, деньги - по точке аренды записи журнала. «Итого» -
    # сумма строк, то есть ровно общие числа панели.

    async def points_data(request: Request) -> dict:
        """Период, справочник, парк и сравнение точек - одно на отчёт, его
        выгрузку и страницу точки: там обязаны стоять те же числа."""
        now = datetime.now().astimezone()
        span = logic.report_period(request.query_params, now=now,
                                   floor=await history_floor(now.date()))
        places = await crm.locations()
        fleet = await crm.bikes(limit=10000)
        settings = await crm.settings()
        # Чек точки без своего - общий чек плана (не сумма точек: та сама
        # из них складывается).
        base_check = logic.month_plan(settings)["base_check"]
        return {"span": span, "places": places, "fleet": fleet,
                "base_check": base_check,
                # План точки за период - тем же ровным темпом, что на сводке.
                "report": logic.points_plan(
                    await points_report(places, fleet, span["start"], span["end"],
                                        full=True),
                    check=base_check, days=span["days"], passed=span["passed"]),
                "history_from": logic.points_history_from(settings, span["start"])}

    async def points_by_month(count: int = 6) -> dict:
        """Дни и деньги всех точек по месяцам - запрос на месяц, а не на
        точку: точек может быть и пять. starts - с какого момента у точки
        есть дни: месяц, где она открылась, неполный, как и текущий."""
        windows = logic.month_windows(datetime.now().astimezone(), count)
        months = [{**m, "days": await crm.bike_days_by_location(m["since"], m["until"]),
                   "money": await crm.money_by_location(m["since"], m["until"])}
                  for m in windows]
        return {"months": months, "starts": (await crm.history_starts())["points"],
                # Месяцы до внедрения истории мест - по карточке: одна
                # строка под таблицей, как у периода отчёта.
                "history_from": logic.points_history_from(await crm.settings(),
                                                          windows[-1]["since"])}

    @app.get("/reports/points")
    async def points_page(request: Request) -> Response:
        data = await points_data(request)
        rows = data["report"]["rows"]
        by_month = await points_by_month()
        months = by_month["months"]
        by_point = [logic.point_months(months, r["key"], starts=by_month["starts"])
                    for r in rows]
        # Переброска - на завтра и послезавтра, от периода отчёта не зависит:
        # это совет, что сделать сейчас, а не история.
        advice = await point_advice(data["places"], data["fleet"])
        return render(request, "points.html", **data,
                      transfer=advice["transfer"],
                      months_history_from=by_month["history_from"],
                      month_rows=[{"month": m["month"],
                                   "cells": [cells[i] for cells in by_point],
                                   # Текущий месяц неполный у всех точек
                                   # сразу - пометка у месяца; у ячейки -
                                   # только своя (точка открылась внутри).
                                   "coverage": logic.month_coverage(m["since"],
                                                                    m["until"])}
                                  for i, m in enumerate(months)])

    # Колонки выгрузки: заголовок, значение строки, денежная ли. Денежные
    # уходят только тем, кому открыты финансы, - как в выгрузке парка.
    POINT_COLUMNS: tuple[tuple[str, Any, bool], ...] = (
        ("Парк сейчас", lambda r: r["fleet"], False),
        ("Простой, %", lambda r: r["metrics"]["idle_percent"], False),
        ("Чек/день", lambda r: r["metrics"]["avg_check"], True),
        ("Выручка", lambda r: r["paid"], True),
        ("Выдачи", lambda r: r["issued"], False),
        ("Продления", lambda r: r["renewals"], False),
        ("Идёт аренд", lambda r: r["active"], False),
        ("Должников", lambda r: r["debtors"], True),
        ("Долг", lambda r: r["debt"], True),
        ("Наличные", lambda r: r["cash"], True),
        ("Нарядов закрыто", lambda r: r["orders"], False),
        ("Выручка сервиса", lambda r: r["service_revenue"], True),
        ("Дней парка", lambda r: round(float(r["metrics"]["operational_days"]), 1),
         False),
        ("Дней аренды", lambda r: round(float(r["metrics"]["rented_days"]), 1), False),
    )
    # План точек - только когда он есть хоть у одной точки: пустые колонки
    # в каждой выгрузке только шумели бы. План - деньги, как и выручка.
    POINT_PLAN_COLUMNS: tuple[tuple[str, Any, bool], ...] = (
        ("План", lambda r: (r.get("plan") or {}).get("target"), True),
        ("Выполнено, %", lambda r: (r.get("plan") or {}).get("percent"), True),
    )

    @app.get("/reports/points.{ext}")
    async def points_table(request: Request, ext: str) -> Response:
        if ext not in EXPORT_FORMATS:
            raise HTTPException(status_code=404)
        data = await points_data(request)
        money_ok = may_view(request, "finance")
        report = data["report"]
        columns = [c for c in POINT_COLUMNS
                   + (POINT_PLAN_COLUMNS if report["planned"] else ())
                   if money_ok or not c[2]]
        out = [["ИТОГО" if r["total"] else r["title"], *(get(r) for _, get, _ in columns)]
               for r in (*report["rows"], report["total"])]
        span = data["span"]
        return await table(ext, f"points-{span['since']:%Y%m%d}-{span['until']:%Y%m%d}",
                      ["Точка", *(title for title, _, _ in columns)], out)

    @app.get("/reports/points/{key}")
    async def point_page(request: Request, key: str) -> Response:
        """Одна точка: плитки, деньги по дням, три числа по месяцам, кто
        стоит, кто должен, что в ремонте. `none` - «без точки»: у неё тоже
        есть деньги и дни, и без своей страницы их не объяснить."""
        data = await points_data(request)
        place = None
        if key != "none":
            # parse_id, а не isdigit: «²» - «цифра», на которой int() падал 500.
            point_id = logic.parse_id(key)
            place = next((p for p in data["places"] if p["id"] == point_id), None)
            if place is None:
                return render(request, "missing.html", status_code=404, what="Точка")
        name = place["name"] if place else None
        span = data["span"]
        # График - за период, но не длиннее POINT_CHART_DAYS: последние дни.
        chart_since = max(span["since"],
                          span["until"] - timedelta(days=logic.POINT_CHART_DAYS - 1))
        chart = logic.money_chart(
            await crm.location_money_by_day(name, chart_since, span["until"]),
            today=span["until"])
        orders = await crm.work_orders(open_only=True, location=name or "none",
                                       limit=100)
        for order in orders:
            order["days"] = logic.order_days(order, today=date.today())
        money_ok = may_view(request, "finance")
        # План месяца точки - за месяц отчёта или текущий, тем же ровным
        # темпом, что на сводке; выручка - та же money_by_location.
        plan = logic.point_plan(place, check=data["base_check"])
        plan_span = logic.plan_month(span, today=date.today())
        progress = None
        if plan is not None and money_ok:
            plan_since = datetime.combine(plan_span["first"], datetime.min.time()).astimezone()
            plan_until = (datetime.now().astimezone() if plan_span["is_current"] else
                          datetime.combine(plan_span["next"],
                                           datetime.min.time()).astimezone())
            paid = ((await crm.money_by_location(plan_since, plan_until)).get(name)
                    or {}).get("paid") or 0
            progress = logic.plan_progress(plan, {"revenue": paid},
                                           days_in_month=plan_span["days"],
                                           days_passed=plan_span["passed"])
        by_month = await points_by_month()
        # Простаивающие модели этой точки - с готовой формой скидки или с
        # уже идущей акцией; у «без точки» акцию не ограничить.
        idle = ([r for r in (await point_advice(data["places"], data["fleet"],
                                                with_transfer=False))["idle"]
                 if r["location"] == name] if place else [])
        return render(request, "point.html", **data, place=place, key=key,
                      idle=idle,
                      row=logic.point_card(data["report"], name, place),
                      chart=chart, chart_since=chart_since,
                      plan=plan, plan_span=plan_span, progress=progress,
                      months=logic.point_months(by_month["months"], name,
                                                starts=by_month["starts"]),
                      months_history_from=by_month["history_from"],
                      standing=await standing_bikes(
                          [b for b in data["fleet"] if (b.get("location") or None) == name],
                          limit=10),
                      debtors=(await crm.debtors(50, location=name or "none")
                               if money_ok else []),
                      orders=orders)

    @app.get("/reports/techs")
    async def techs_report(request: Request) -> Response:
        """Выработка техников: кто сколько закрыл и на сколько."""
        if not may_view(request, "service"):
            return denied(request, "service")
        span = await period_of(request)
        rows = logic.tech_rows(await crm.tech_work(span["start"], span["end"]))
        return render(request, "techs.html", rows=rows,
                      total=logic.tech_total(rows), **span)

    @app.get("/reports/techs.{ext}")
    async def techs_csv(request: Request, ext: str) -> Response:
        if not may_view(request, "service"):
            return denied(request, "service")
        span = await period_of(request)
        rows = logic.tech_rows(await crm.tech_work(span["start"], span["end"]))
        total = logic.tech_total(rows)
        # Суммы нарядов - деньги: без «Финансов» выгрузка без них, как и
        # страница, а не только без плиток на экране.
        money_ok = may_view(request, "finance")
        header = ["Техник", "Нарядов", "Из них клиентских", "Средн. суток"]
        money_keys = ("total", "cost", "works", "avg_total")
        if money_ok:
            header += ["Сумма", "Запчасти", "Работы", "Средний наряд"]
        data = [[r["tech"], r["orders"], r["client_orders"], r["avg_days"],
                 *((r[k] for k in money_keys) if money_ok else ())] for r in rows]
        data.append(["ИТОГО", total["orders"], total["client_orders"], "",
                     *((total[k] for k in money_keys) if money_ok else ())])
        return await table(ext, f"techs-{span['since']:%Y%m%d}-{span['until']:%Y%m%d}",
                           header, data)

    @app.get("/reports/model-parts")
    async def model_parts_report(request: Request) -> Response:
        """Траты по моделям: какая модель дороже всех в запчастях."""
        if not may_view(request, "service"):
            return denied(request, "service")
        span = await period_of(request)
        rows = logic.model_parts_rows(
            await crm.model_parts(span["start"], span["end"]),
            await crm.bikes(limit=10000), days=span["days"])
        return render(request, "model_parts.html", rows=rows,
                      total=logic.spend_total(rows), **span)

    @app.get("/reports/spend")
    async def spend_report(request: Request) -> Response:
        """Расход склада за период: что уходит и на сколько."""
        if not may_view(request, "inventory"):
            return denied(request, "inventory")
        span = await period_of(request)
        rows = logic.spend_rows(await crm.part_spend(span["start"], span["end"]))
        return render(request, "spend.html", rows=rows,
                      total=logic.spend_total(rows), **span)

    @app.get("/reports/spend.{ext}")
    async def spend_csv(request: Request, ext: str) -> Response:
        if not may_view(request, "inventory"):
            return denied(request, "inventory")
        span = await period_of(request)
        rows = logic.spend_rows(await crm.part_spend(span["start"], span["end"]))
        total = logic.spend_total(rows)
        # Себестоимость - деньги: без «Финансов» выгрузка про штуки, как
        # выгрузка склада (parts_csv).
        money_ok = may_view(request, "finance")
        data = [[r["title"], r["node_title"], r["qty"], r["unit"], r["orders"],
                 *([r["cost"]] if money_ok else [])] for r in rows]
        data.append(["ИТОГО", "", total["qty"], "", "",
                     *([total["cost"]] if money_ok else [])])
        return await table(ext, f"spend-{span['since']:%Y%m%d}-{span['until']:%Y%m%d}",
                    ["Позиция", "Узел", "Ушло", "Ед.", "Нарядов",
                     *(["Себестоимость"] if money_ok else [])],
                    data)

    async def integrity_data(request: Request) -> list[dict]:
        """Расхождения между парком, арендами и нарядами. «Долг без аренды» -
        это сумма за клиентом и список должников: отчёт открыт с правом на
        парк, а деньги - только с «Финансами», как у клиентов и аренд."""
        debtors = await crm.debtors(200) if may_view(request, "finance") else []
        return logic.integrity_issues(
            await crm.bikes(limit=10000), await crm.active_rentals(),
            await crm.open_orders_by_bike(), debtors,
            batteries=await crm.batteries(limit=10000))

    @app.get("/reports/integrity")
    async def integrity_report(request: Request) -> Response:
        """Расхождение - это не «некрасиво в базе», а невидимый простой."""
        if not may_view(request, "bikes"):
            return denied(request, "bikes")
        issues = await integrity_data(request)
        return render(request, "integrity.html", issues=issues,
                      summary=logic.integrity_summary(issues))

    @app.get("/reports/channels")
    async def channels_report(request: Request) -> Response:
        """Откуда приходят клиенты - по месяцам. Куда давать рекламу.

        Это разрез клиентской базы, поэтому и право нужно на клиентов:
        механику с доступом к отчётам парка она ни к чему.
        """
        if not may_view(request, "clients"):
            return denied(request, "clients")
        months = 12
        now = datetime.now().astimezone()
        since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for _ in range(months - 1):
            since = (since - timedelta(days=1)).replace(day=1)
        data = logic.channel_rows(await crm.clients_since(since), months=months,
                                  today=now.date())
        return render(request, "channels.html", **data)

    @app.get("/reports/channels.{ext}")
    async def channels_csv(request: Request, ext: str) -> Response:
        if not may_view(request, "clients"):
            return denied(request, "clients")
        now = datetime.now().astimezone()
        since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for _ in range(11):
            since = (since - timedelta(days=1)).replace(day=1)
        data = logic.channel_rows(await crm.clients_since(since), months=12,
                                  today=now.date())
        header = ["Месяц", *(logic.channel_label(c) for c in data["columns"]), "Всего"]
        rows = [[r["month"].strftime("%m.%Y"),
                 *(r["cells"][c] for c in data["columns"]), r["total"]]
                for r in data["rows"]]
        rows.append(["ИТОГО", *(data["totals"].get(c, 0) for c in data["columns"]),
                     data["total"]])
        return await table(ext, "channels", header, rows)

    @app.get("/reports/feedback")
    async def feedback_report(request: Request) -> Response:
        """Как клиенты оценивают аренду: по месяцам сдачи и по точкам,
        и низкие оценки поимённо. В отчёте имена и слова клиентов, поэтому
        право - на клиентов, как у «Каналов»."""
        if not may_view(request, "clients"):
            return denied(request, "clients")
        months = 12
        today = date.today()
        since = today.replace(day=1)
        for _ in range(months - 1):
            since = (since - timedelta(days=1)).replace(day=1)
        data = logic.feedback_report(await crm.feedback_rows(since), months=months,
                                     today=today)
        return render(request, "feedback.html", since=since, **data)

    @app.get("/reports/referrals")
    async def referrals_report(request: Request) -> Response:
        if not may_view(request, "finance"):
            return denied(request, "finance")
        # report_day, как в period_of: год 9999 плюс сутки - это 500.
        today = date.today()
        since = logic.report_day(request.query_params.get("since"), today=today,
                                 default=today.replace(day=1))
        until = logic.report_day(request.query_params.get("until"), today=today,
                                 default=today)
        if not since.ok or not until.ok:
            since = logic.Check(True, date.today().replace(day=1))
            until = logic.Check(True, date.today())
        tz = datetime.now().astimezone().tzinfo
        start = datetime.combine(since.value, datetime.min.time(), tzinfo=tz)
        end = datetime.combine(until.value + timedelta(days=1),
                               datetime.min.time(), tzinfo=tz)
        rows = await crm.referrals(since=start, until=end, limit=5000)
        raw = await crm.settings()
        grants = await crm.bonuses(since=since.value, until=until.value, limit=2000)
        return render(request, "referrals.html", rows=rows,
                      funnel=logic.ref_funnel(rows), agents=logic.ref_agents(rows),
                      settings=logic.bonus_settings(raw),
                      links=logic.review_links(raw),
                      bonuses=grants,
                      totals=logic.bonus_totals(
                          grants, await crm.payments_total(since=since.value,
                                                           until=until.value)),
                      free_bikes=str(raw.get("free_bikes_post", "0"))
                      not in ("0", "", "false"),
                      since=since.value, until=until.value)

    @app.post("/reports/referrals")
    async def referrals_settings(request: Request) -> Response:
        """Настройки программы: включена ли, бонус и порог платежа."""
        if not may_edit(request, "finance"):
            return denied(request, "finance")
        data = await form(request)
        bonus = cost_field(data, "bonus")
        minimum = cost_field(data, "min_payment")
        friend = cost_field(data, "friend_bonus")
        review = cost_field(data, "review_bonus")
        spike = count_field(data, "spike", what="Порог всплеска",
                            default=str(logic.REF_SPIKE_DEFAULT), limit=100)
        for check in (bonus, minimum, friend, review, spike):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/reports/referrals")
        by = who(request)
        await crm.set_setting("ref_enabled", "1" if data.get("enabled") else "0", by=by)
        await crm.set_setting("ref_bonus", str(bonus.value), by=by)
        await crm.set_setting("ref_min_payment", str(minimum.value), by=by)
        await crm.set_setting("ref_friend_bonus", str(friend.value), by=by)
        await crm.set_setting("review_bonus", str(review.value), by=by)
        await crm.set_setting("ref_new_only",
                              "1" if data.get("new_only") else "0", by=by)
        await crm.set_setting("ref_spike", str(spike.value), by=by)
        for key in logic.REVIEW_SITES:
            url = (data.get(key) or "").strip()
            if url and not url.startswith(("http://", "https://")):
                flash(request, f"{logic.REVIEW_SITES[key]}: ссылка должна "
                               "начинаться с http:// или https://", "err")
                return redirect("/reports/referrals")
            await crm.set_setting(key, url, by=by)
        await crm.set_setting("free_bikes_post", "1" if data.get("free_bikes") else "0",
                              by=by)
        flash(request, "Настройки программы сохранены.")
        return redirect("/reports/referrals")

    @app.post("/clients/{client_id}/bonus")
    async def client_bonus(request: Request, client_id: int) -> Response:
        """Баллы клиенту: за отзыв или руками.

        Отзыв проверяет человек по скриншоту: у площадок нет ни API, ни
        обязанности нам отвечать, и правило в коде здесь было бы враньём.
        """
        if not logic.can_act(request.state.staff, "money_edit"):
            return denied(request, "money_edit")
        client = await crm.client(client_id)
        if client is None:
            return render(request, "missing.html", status_code=404, what="Клиент")
        data = await form(request)
        back = f"/clients/{client_id}"
        if not form_once(data):
            flash(request, "Форма уже отправлена — повторное нажатие пропущено.")
            return redirect(back)
        try:
            if (data.get("action") or "") == "review":
                amount = await service.grant_review_bonus(
                    crm, client, by=who(request))
            else:
                got = cost_field(data, "amount")
                if not got.ok:
                    form_once_release(data)
                    flash(request, got.error, "err")
                    return redirect(back)
                note = logic.check_note(data.get("note"))
                if not note.ok:
                    form_once_release(data)
                    flash(request, note.error, "err")
                    return redirect(back)
                amount = await service.grant_manual_bonus(
                    crm, client, got.value, note=note.value or "", by=who(request))
        except service.ServiceError as exc:
            form_once_release(data)
            flash(request, str(exc), "err")
            return redirect(back)
        flash(request, f"Начислено баллами: {logic.money(amount)}. "
                       "Это не платёж — в средний чек они не идут.")
        return redirect(back)

    # ─────────────────────── сотрудники ───────────────────────

    async def profile_choices() -> list[dict]:
        return await crm.access_profiles()

    def role_for(profile: dict | None) -> str:
        """Старая колонка role остаётся: журнал и импорт её пишут. Права
        решает профиль, role лишь повторяет его крупным планом."""
        return "admin" if (profile or {}).get("code") == "owner" else "manager"

    async def staff_page_response(request: Request, *, add_form: dict | None = None,
                                  add_error: str | None = None,
                                  status_code: int = 200) -> Response:
        """Сотрудники. С отказом добавления - та же страница с открытым окном
        «Добавить», ошибкой в нём и набранными полями: после редиректа
        сообщение уезжало наверх страницы, а форма оставалась пустой, и
        казалось, что кнопка просто не сработала."""
        profiles = await profile_choices()
        # Без «Администратора» (его можно удалить) отмеченной не остаётся
        # ничего, а не «Владелец» - полные права выбирают руками.
        default_role = next((p["id"] for p in profiles if p.get("code") == "manager"),
                            None)
        # В окне добавления - кого заводят чаще: администратор, мастер, их
        # «только задачи», свои роли; «Владелец» последним.
        order = {"manager": 0, "tech": 1, "tasks_operator": 2, "tasks_tech": 3,
                 "owner": 9}
        role_choices = sorted(profiles, key=lambda p: (order.get(p.get("code"), 5),
                                                       p.get("name") or ""))
        return render(request, "staff.html", rows=await crm.staff_all(),
                      profiles=profiles, default_role=default_role,
                      role_choices=role_choices,
                      places=await location_names(),
                      can_manage=may_edit(request, "staff") and not cfg.demo,
                      add_form=add_form or {}, add_error=add_error,
                      add_open=request.query_params.get("add") == "1",
                      demo_url=cfg.demo_url, status_code=status_code)

    @app.get("/staff")
    async def staff_page(request: Request) -> Response:
        return await staff_page_response(request)

    async def add_staff(request: Request, data: dict) -> dict | str:
        """Новый вход в панель из формы: словарь - заведён, строка - отказ
        словами. Один путь для «Сотрудников» и мастера первого запуска.

        Логин необязателен: пусто - собирается из ФИО (kuznetsov.t), и
        занятый получает номер. Латинский логин с кириллической клавиатуры
        - самая частая причина, по которой сотрудник «не добавлялся».
        """
        name = logic.check_name(data.get("name") or data.get("login"), what="ФИО")
        if not name.ok:
            return name.error or "ФИО: заполните поле."
        raw_login = (data.get("login") or "").strip()
        if raw_login:
            login_check = logic.check_login(raw_login)
            if not login_check.ok:
                return (f"{login_check.error} Оставьте поле пустым — логин "
                        "соберётся из ФИО латиницей.")
            login = login_check.value
            if await crm.staff_by_login(login) is not None:
                return f"Логин {login} уже занят — оставьте поле пустым или впишите другой."
        else:
            login = logic.login_from_name(
                name.value, [s["login"] for s in await crm.staff_all()])
        # Пустой пароль - придумывает панель и показывает один раз: владелец
        # не изобретает пароль стажёру и не пересылает свой любимый.
        generated = not (data.get("password") or "")
        password = (logic.Check(True, logic.generate_password(12)) if generated
                    else logic.check_password(data.get("password")))
        term = logic.check_staff_term(data.get("term"), data.get("until"))
        for check in (password, term):
            if not check.ok:
                return check.error or "Проверьте поля формы."
        profile = await by_id(crm.access_profile, data.get("profile_id"))
        if profile is None:
            return "Выберите роль."
        place = logic.check_location(data.get("location"), await location_names())
        if not place.ok:
            return place.error or "Такой точки нет."
        try:
            await crm.create_staff(login, logic.hash_password(password.value),
                                   name.value, role_for(profile), profile["id"],
                                   location=place.value, expires_at=term.value)
        except Exception as exc:                           # noqa: BLE001
            # Два добавления разом с одним собранным логином: второе
            # упирается в уникальный логин - это не 500, а «ещё раз».
            if "unique" not in type(exc).__name__.lower():
                raise
            return f"Логин {login} только что заняли — нажмите «Добавить» ещё раз."
        return {"login": login, "profile": profile,
                "password": password.value if generated else None,
                "expires_at": term.value}

    @app.post("/staff")
    async def staff_create(request: Request) -> Response:
        data = await form(request)
        added = await add_staff(request, data)
        if isinstance(added, str):
            kept = {k: str(data.get(k) or "") for k in
                    ("name", "login", "profile_id", "location", "term", "until")}
            return await staff_page_response(request, add_form=kept, add_error=added,
                                             status_code=400)
        until = (f", доступ до {_dmy(added['expires_at'])}"
                 if added["expires_at"] else "")
        flash(request, f"Сотрудник добавлен: логин {added['login']}, роль "
                       f"«{added['profile']['name']}»{until}.")
        if added["password"]:
            flash(request, f"Пароль для {added['login']}: {added['password']} — "
                           "передайте сотруднику. Больше он нигде не покажется.")
        return redirect("/staff")

    @app.post("/staff/{staff_id}/profile")
    async def staff_set_profile(request: Request, staff_id: int) -> Response:
        data = await form(request)
        target = await crm.staff_by_id(staff_id)
        if target is None:
            return render(request, "missing.html", status_code=404, what="Сотрудник")
        if target["id"] == request.state.staff["id"]:
            # Иначе владелец одним движением снимает с себя доступ к этой же
            # странице и чинить это придётся руками в базе.
            flash(request, "Свою роль менять нельзя — попросите другого "
                           "сотрудника с доступом к разделу.", "err")
            return redirect("/staff")
        profile = await by_id(crm.access_profile, data.get("profile_id"))
        if profile is None:
            flash(request, "Такой роли нет.", "err")
            return redirect("/staff")
        await crm.set_staff_profile(staff_id, profile["id"])
        flash(request, f"{target['login']}: роль «{profile['name']}».")
        return redirect("/staff")

    @app.post("/staff/{staff_id}/location")
    async def staff_set_location(request: Request, staff_id: int) -> Response:
        """Своя точка сотрудника. По ней касса находит смену, когда он принял
        наличные, не открыв своей: иначе деньги уходили в самую раннюю
        смену - то есть на чужую точку."""
        target = await crm.staff_by_id(staff_id)
        if target is None:
            return render(request, "missing.html", status_code=404, what="Сотрудник")
        place = logic.check_location((await form(request)).get("location"),
                                     await location_names(target.get("location")))
        if not place.ok:
            flash(request, place.error, "err")
            return redirect("/staff")
        await crm.set_staff_location(staff_id, place.value)
        flash(request, f"{target['login']}: "
                       + (f"своя точка «{place.value}»." if place.value
                          else "своя точка снята."))
        return redirect("/staff")

    async def drop_crm_menu(tg_id: Any) -> None:
        """Кнопку «CRM» в меню чата (handlers/staff.set_crm_menu) - обратно
        в обычное меню, когда сотрудника отвязали или отключили. За ней
        только страница входа, но держать её у того, кому доступ закрыли,
        незачем. Один запрос по кнопке оператора; сбой Telegram отвязке и
        отключению не мешает."""
        if bot is None or not tg_id or not hasattr(bot, "set_chat_menu_button"):
            return
        try:
            from aiogram.types import MenuButtonDefault
            await bot.set_chat_menu_button(chat_id=int(tg_id),
                                           menu_button=MenuButtonDefault())
        except Exception:                                # noqa: BLE001
            log.warning("кнопка CRM в чате %s не снята", tg_id, exc_info=True)

    @app.post("/staff/{staff_id}/telegram")
    async def staff_telegram(request: Request, staff_id: int) -> Response:
        """Код привязки Telegram сотруднику - или отвязка.

        Пароль от панели в переписку не отдают, а одноразовый код можно:
        он гаснет при первом применении и открывает ровно одну связь.
        """
        if not may_edit(request, "staff"):
            return denied(request, "staff")
        person = await crm.staff_by_id(staff_id)
        if person is None:
            return render(request, "missing.html", status_code=404, what="Сотрудник")
        if (await form(request)).get("unlink"):
            await crm.unlink_staff_tg(staff_id)
            await drop_crm_menu(person.get("tg_id"))
            flash(request, f"Telegram сотрудника {person['login']} отвязан.")
            return redirect("/staff")
        for _ in range(10):
            code = logic.make_link_code()
            if await crm.staff_by_link_code(code) is not None:
                continue
            if await crm.set_staff_link_code(staff_id, code):
                flash(request, f"Код для {person['login']}: {code}. Пусть отправит "
                               f"боту «/staff {code}» — код погаснет сразу после этого.")
                return redirect("/staff")
        flash(request, "Не удалось выдать код, попробуйте ещё раз.", "err")
        return redirect("/staff")

    @app.post("/staff/{staff_id}/password")
    async def staff_password(request: Request, staff_id: int) -> Response:
        data = await form(request)
        generated = not (data.get("password") or "")
        password = (logic.Check(True, logic.generate_password(12)) if generated
                    else logic.check_password(data.get("password")))
        if not password.ok:
            flash(request, password.error, "err")
            return redirect("/staff")
        target = await crm.staff_by_id(staff_id)
        if target is None:
            return render(request, "missing.html", status_code=404, what="Сотрудник")
        await crm.set_staff_password(staff_id, logic.hash_password(password.value))
        flash(request, "Пароль обновлён." if not generated else
              f"Новый пароль для {target['login']}: {password.value} — передайте "
              "сотруднику. Больше он нигде не покажется.")
        return redirect("/staff")

    @app.post("/staff/{staff_id}/term")
    async def staff_term(request: Request, staff_id: int) -> Response:
        """Срок доступа: продлить, укоротить или снять. Прошедший срок
        выбивает сессии сотрудника на следующем его запросе."""
        target = await crm.staff_by_id(staff_id)
        if target is None:
            return render(request, "missing.html", status_code=404, what="Сотрудник")
        if target["id"] == request.state.staff["id"]:
            flash(request, "Свой срок доступа не меняют — иначе можно запереться.",
                  "err")
            return redirect("/staff")
        data = await form(request)
        term = logic.check_staff_term(data.get("term"), data.get("until"),
                                      keep_empty=True)
        if not term.ok:
            flash(request, term.error, "err")
            return redirect("/staff")
        await crm.set_staff_expires(staff_id, term.value)
        flash(request, f"{target['login']}: доступ "
                       + (f"до {_dmy(term.value)}." if term.value else "бессрочный."))
        return redirect("/staff")

    @app.post("/staff/{staff_id}/toggle")
    async def staff_toggle(request: Request, staff_id: int) -> Response:
        target = await crm.staff_by_id(staff_id)
        if target is None:
            return render(request, "missing.html", status_code=404, what="Сотрудник")
        if target["id"] == request.state.staff["id"]:
            flash(request, "Себя отключить нельзя.", "err")
            return redirect("/staff")
        await crm.set_staff_active(staff_id, not target["active"])
        if target["active"]:
            await drop_crm_menu(target.get("tg_id"))
        flash(request, "Доступ " + ("включён." if not target["active"] else "отключён."))
        return redirect("/staff")

    # ─────────────────────── профили доступа ───────────────────────

    def name_taken(exc: Exception) -> bool:
        """Уникальный индекс на название профиля - единственная ошибка,
        которую здесь можно объяснить оператору; всё прочее наверх."""
        return "unique" in type(exc).__name__.lower()

    def perms_from_form(data: Any) -> dict:
        """Матрица из формы: по полю на раздел, галочки на действия."""
        return logic.normalize_perms({
            "sections": {code: (data.get(f"s_{code}") or "") for code in logic.SECTIONS},
            "actions": {code: bool(data.get(f"a_{code}")) for code in logic.ACTIONS},
        })

    @app.get("/profiles")
    async def profiles_page(request: Request) -> Response:
        return render(request, "profiles.html", rows=await crm.access_profiles(),
                      can_manage=may_edit(request, "staff"))

    @app.get("/profiles/{profile_id}")
    async def profile_page(request: Request, profile_id: int) -> Response:
        profile = await crm.access_profile(profile_id)
        if profile is None:
            return render(request, "missing.html", status_code=404, what="Роль")
        staff_on_it = [s for s in await crm.staff_all()
                       if s.get("profile_id") == profile_id]
        return render(request, "profile.html", profile=profile,
                      perms=logic.normalize_perms(profile.get("perms")),
                      staff_on_it=staff_on_it, can_manage=may_edit(request, "staff"))

    @app.post("/profiles")
    async def profile_create(request: Request) -> Response:
        data = await form(request)
        name = logic.check_profile_name(data.get("name"))
        if not name.ok:
            flash(request, name.error, "err")
            return redirect("/profiles")
        try:
            profile_id = await crm.create_access_profile(name.value, perms_from_form(data))
        except Exception as exc:                           # noqa: BLE001
            if not name_taken(exc):
                raise
            flash(request, "Роль с таким названием уже есть.", "err")
            return redirect("/profiles")
        flash(request, f"Роль «{name.value}» создана — отметьте разделы.")
        return redirect(f"/profiles/{profile_id}")

    @app.post("/profiles/{profile_id}")
    async def profile_save(request: Request, profile_id: int) -> Response:
        data = await form(request)
        profile = await crm.access_profile(profile_id)
        if profile is None:
            return render(request, "missing.html", status_code=404, what="Роль")
        if profile["built_in"]:
            flash(request, "Роль «Владелец» не меняется: это запасной ключ "
                           "от панели.", "err")
            return redirect(f"/profiles/{profile_id}")
        name = logic.check_profile_name(data.get("name"))
        if not name.ok:
            flash(request, name.error, "err")
            return redirect(f"/profiles/{profile_id}")
        perms = perms_from_form(data)
        me = request.state.staff
        if me.get("profile_id") == profile_id and perms["sections"].get("staff") != "edit":
            flash(request, "Это ваша роль: доступ к разделу «Сотрудники» "
                           "снимать нельзя — некому будет его вернуть.", "err")
            return redirect(f"/profiles/{profile_id}")
        try:
            await crm.update_access_profile(profile_id, name=name.value, perms=perms)
        except Exception as exc:                           # noqa: BLE001
            if not name_taken(exc):
                raise
            flash(request, "Роль с таким названием уже есть.", "err")
            return redirect(f"/profiles/{profile_id}")
        # Сотрудники подхватят новые права со следующего запроса: права
        # читаются из базы на каждом, а не кладутся в сессию при входе.
        flash(request, "Права сохранены.")
        return redirect(f"/profiles/{profile_id}")

    @app.post("/profiles/{profile_id}/delete")
    async def profile_delete(request: Request, profile_id: int) -> Response:
        profile = await crm.access_profile(profile_id)
        if profile is None:
            return render(request, "missing.html", status_code=404, what="Роль")
        if not await crm.delete_access_profile(profile_id):
            flash(request, "Роль встроенная или на ней ещё есть сотрудники — "
                           "сначала переведите их на другую.", "err")
            return redirect(f"/profiles/{profile_id}")
        flash(request, f"Роль «{profile['name']}» удалена.")
        return redirect("/profiles")

    # ─────────────────────── сервис: наряды ───────────────────────

    async def notify_tech(order_id: int, tech_id: int) -> None:
        """Сказать технику о наряде в Telegram. Не привязан - промолчать:
        наряд от этого не перестаёт существовать."""
        tech = await crm.staff_by_id(tech_id)
        if not tech or not tech.get("tg_id"):
            return
        order = await crm.work_order(order_id)
        if order:
            await notify.order_assigned(bot, order, tech)

    async def tech_known(tech_id: int | None) -> bool:
        """Техник из формы существует. Не выбран - тоже годится; чужой
        номер упирался в ссылку базы и ронял наряд 500."""
        return tech_id is None or await crm.staff_by_id(tech_id) is not None

    @app.get("/service")
    async def service_desk(request: Request) -> Response:
        """Рабочий стол сервиса: что стоит в ремонте и кто этим занят.

        Первыми - велосипеды в ремонте без наряда: они копят простой, а
        в отчётах выглядят как обычный ремонт, которым кто-то занимается.
        """
        bikes = await crm.bikes(limit=10000)
        since = await crm.bike_status_since()
        today, now = date.today(), datetime.now(UTC)
        for bike in bikes:
            bike["idle_days"] = logic.idle_days(since.get(bike["id"]), now=now)
        q = request.query_params.get("q") or ""
        settings = await crm.settings()
        norm = logic.repair_norm_default(settings)
        rows = logic.rows_search(
            logic.service_rows(bikes, await crm.open_orders_by_bike(), today=today,
                               norm=norm),
            q, ("code", "model", "order_no", "tech", "client", "complaint"))
        tools = list_tools(request, rows, allowed=SERVICE_SORTS)
        counts = await crm.bike_counts()
        plan = await network_plan(settings, counts)
        # График за месяц: по нему видно, ремонт у нас ровный или
        # скачет - и когда именно скакнул. Месяц листается стрелками.
        span = logic.month_bounds(
            logic.month_from(request.query_params.get("month"), today=today),
            today=today, floor=await history_floor(today))
        chart = logic.repair_chart(
            await crm.bikes_in_status_by_day("repair", span["first"], span["today"]),
            norm=int(plan["repair"]))
        # Подменный фонд - на столе сервиса: это его резерв на замены.
        spares = [b for b in bikes if b.get("spare")
                  and b.get("status") in logic.OPERATIONAL_STATUSES]
        return render(request, "service.html", rows=tools["rows"], tools=tools, q=q,
                      summary=logic.service_summary(rows), spares=spares,
                      repair_norm=norm,
                      chart=chart, plan=plan, month=span["first"], span=span,
                      tiles=logic.fleet_tiles(
                          counts, plan,
                          spare=sum(1 for b in bikes if b.get("spare")
                                    and b.get("status") in logic.OPERATIONAL_STATUSES)),
                      orders=await crm.work_orders(open_only=True, limit=200))

    SERVICE_SORTS = {"bike": "code", "model": "model", "stage": "stage",
                     "days": "days", "overdue": "overdue", "lost": "lost",
                     "order": "order_no",
                     "tech": "tech", "payer": "payer_title", "client": "client",
                     "estimate": "estimate"}

    @app.get("/service.{ext}")
    async def service_csv(request: Request, ext: str) -> Response:
        bikes = await crm.bikes(limit=10000)
        since = await crm.bike_status_since()
        today, now = date.today(), datetime.now(UTC)
        for bike in bikes:
            bike["idle_days"] = logic.idle_days(since.get(bike["id"]), now=now)
        rows = logic.rows_search(
            logic.service_rows(bikes, await crm.open_orders_by_bike(), today=today,
                               norm=logic.repair_norm_default(await crm.settings())),
            request.query_params.get("q") or "",
            ("code", "model", "order_no", "tech", "client", "complaint"))
        money_ok = may_view(request, "finance")
        header = ["Велосипед", "Модель", "Этап", "Суток", "Срок", "Сверх срока",
                  "Наряд", "Техник", "Чей ремонт", "Клиент", "Жалоба"]
        if money_ok:
            header[6:6] = ["Потеряно"]
            header.append("Смета")
        out = []
        for r in rows:
            line = [r["code"], r.get("model"), r["stage"], r["days"], r["norm"],
                    r["overdue"], r["order_no"], r["tech"], r["payer_title"],
                    r["client"], r["complaint"]]
            if money_ok:
                line[6:6] = [r["lost"]]
                line.append(r["estimate"])
            out.append(line)
        return await table(ext, "service", header, out)

    @app.get("/orders")
    async def orders_page(request: Request) -> Response:
        status = request.query_params.get("status") or ""
        payer = request.query_params.get("payer") or ""
        bike_q = request.query_params.get("bike") or ""
        # Где идёт ремонт; «none» - наряды без точки (чужая техника).
        location = request.query_params.get("location") or ""
        # Наряды одного велосипеда - его история ремонтов целиком: с карточки
        # велосипеда сюда ведёт «все наряды».
        bike_filter = await by_id(crm.bike, bike_q)
        # «Дольше срока» - открытые наряды, пересидевшие срок своего узла:
        # сюда ведут плитка рабочего стола и задача на сводке.
        overdue_only = request.query_params.get("overdue") == "1"
        rows = await crm.work_orders(status=status or None, payer=payer or None,
                                     bike_id=bike_filter["id"] if bike_filter else None,
                                     location=location or None, open_only=overdue_only,
                                     limit=300)
        norm = logic.repair_norm_default(await crm.settings())
        for order in rows:
            order["days"] = logic.order_days(order, today=date.today())
            order["norm"] = logic.order_norm(order, norm)
            order["overdue"] = logic.order_overdue(order, default=norm, today=date.today())
        if overdue_only:
            rows = [o for o in rows if o["overdue"]]
        # Итог «сколько за ремонт ещё не заплатили» считается по всем
        # закрытым клиентским нарядам, а не по видимой странице: иначе
        # он менялся бы от фильтра и ничего не значил.
        tools = list_tools(request, rows, allowed=ORDER_SORTS)
        return render(request, "orders.html", rows=tools["rows"], tools=tools,
                      bike_filter=bike_filter, overdue_only=overdue_only,
                      repair_norm=norm,
                      status=status, payer=payer, location=location,
                      places=await filter_points(location),
                      views=await views_of(request, "/orders"),
                      total=logic.sum_of(tools["all_rows"], "total"),
                      unpaid=logic.orders_unpaid(
                          await crm.work_orders(payer="client", limit=1000)))

    @app.get("/orders.{ext}")
    async def orders_csv(request: Request, ext: str) -> Response:
        if not may_view(request, "service"):
            return denied(request, "service")
        # Тот же фильтр, что у списка: выгрузка с карточки велосипеда - это
        # история ремонтов одного велосипеда, а не все наряды парка.
        bike_q = request.query_params.get("bike") or ""
        overdue_only = request.query_params.get("overdue") == "1"
        rows = await crm.work_orders(
            status=request.query_params.get("status") or None,
            payer=request.query_params.get("payer") or None,
            bike_id=logic.parse_id(bike_q),
            location=request.query_params.get("location") or None,
            open_only=overdue_only, limit=5000)
        money_ok = may_view(request, "finance")
        header = ["Наряд", "Открыт", "Объект", "Статус", "Плательщик", "Клиент",
                  "Техник", "Суток", "Закрыт", "Оплачен", "Точка", "Срок",
                  "Сверх срока"]
        if money_ok:
            header.insert(8, "Сумма")
        today = date.today()
        norm = logic.repair_norm_default(await crm.settings())
        out = []
        for o in rows:
            late = logic.order_overdue(o, default=norm, today=today)
            if overdue_only and not late:
                continue
            line = [o["no"], o.get("opened_at"),
                    o.get("bike_code") or o.get("object_note"),
                    logic.ORDER_STATUSES.get(o["status"], o["status"]),
                    logic.PAYERS.get(o["payer"], o["payer"]), o.get("client_name"),
                    o.get("tech_name"), logic.order_days(o, today=today),
                    o.get("closed_at"), o.get("paid_at"), o.get("location"),
                    logic.order_norm(o, norm), late]
            if money_ok:
                line.insert(8, logic.to_money(
                    o.get("total") if o["status"] == "done" else o.get("estimate")))
            out.append(line)
        return await table(ext, "orders", header, out)

    @app.get("/orders/new")
    async def order_new(request: Request) -> Response:
        if not may_edit(request, "service"):
            return denied(request, "service")
        bike = await by_id(crm.bike, request.query_params.get("bike"))
        return render(request, "order_form.html", bike=bike,
                      bikes=await crm.bikes(limit=10000),
                      techs=await crm.staff_all(),
                      places=await location_names())

    @app.post("/orders")
    async def order_create(request: Request) -> Response:
        data = await form(request)
        payer = logic.check_payer(data.get("payer") or "own")
        if not payer.ok:
            flash(request, payer.error, "err")
            return redirect("/orders/new")
        bike = await by_id(crm.bike, data.get("bike_id"))
        client = await by_id(crm.client, data.get("client_id"))
        estimate = cost_field(data, "estimate")
        complaint = logic.check_note(data.get("complaint"))
        # Где чиним. Пусто - точка велосипеда; у чужой техники без выбора
        # точки нет, и её ремонт идёт в отчёте строкой «без точки».
        place = logic.check_location(data.get("location"),
                                     await location_names((bike or {}).get("location")))
        for check in (estimate, complaint, place):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/orders/new")
        tech_id = logic.parse_id(data.get("tech_id"))
        if not await tech_known(tech_id):
            flash(request, "Такого сотрудника нет — обновите страницу.", "err")
            return redirect("/orders/new")
        try:
            order_id = await service.open_order(
                crm, bike=bike, payer=payer.value, client=client,
                complaint=complaint.value,
                object_note=(data.get("object_note") or "").strip() or None,
                tech_id=tech_id, estimate=estimate.value, by=who(request),
                location=place.value)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/orders/new")
        if tech_id:
            await notify_tech(order_id, tech_id)
        flash(request, "Наряд открыт.")
        return redirect(f"/orders/{order_id}")

    @app.get("/orders/{order_id}")
    async def order_page(request: Request, order_id: int) -> Response:
        order = await crm.work_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Наряд")
        items = await crm.order_items(order_id)
        stocks = await crm.stock_map()
        invoices = await crm.work_order_invoices(order_id)
        norm = logic.repair_norm_default(await crm.settings())
        return render(request, "order.html", order=order, items=items,
                      norm=logic.order_norm(order, norm),
                      norm_node=logic.order_norm_node(order, norm),
                      overdue=logic.order_overdue(order, default=norm,
                                                  today=date.today()),
                      totals=logic.order_totals(items),
                      client_total=logic.order_totals_client(items),
                      estimate=logic.estimate_state(order),
                      invoice=logic.invoice_state(order, invoices),
                      invoices=invoices,
                      days=logic.order_days(order, today=date.today()),
                      sheet=logic.price_sheet(order),
                      types=logic.priced_types(await crm.work_types(active_only=True),
                                               logic.price_sheet(order)),
                      techs=await crm.staff_all(),
                      places=await location_names(order.get("location")),
                      parts=logic.part_rows(await crm.parts(active_only=True), stocks),
                      may_stock=may_view(request, "inventory"))

    @app.post("/orders/{order_id}/estimate")
    async def order_estimate(request: Request, order_id: int) -> Response:
        """Смета: отправить клиенту или согласовать вживую."""
        if not may_edit(request, "service"):
            return denied(request, "service")
        order = await crm.work_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Наряд")
        action = (await form(request)).get("action") or "send"
        back = f"/orders/{order_id}"
        try:
            if action == "send":
                got = await service.send_estimate(crm, order, by=who(request),
                                                  bot=bot)
                flash(request, f"Смета на {logic.money(got['total'])} отправлена."
                      if got["sent"] else
                      "Смета собрана, но клиента нет в боте — "
                      "согласуйте вживую.", "ok" if got["sent"] else "err")
                if got["sent"]:
                    # Команде - сразу, а не сводкой через сутки: наряд
                    # встал, и кто-то должен знать, что ждём клиента.
                    await notices.send_team(
                        crm, bot, "estimate_waiting",
                        f"⏳ Наряд {order.get('no')} ждёт согласования: смета "
                        f"на {logic.money(got['total'])} ушла клиенту "
                        f"{order.get('client_name') or ''}.".replace("  ", " "),
                        cfg.contract_chat_id)
            elif action in ("agree", "decline"):
                # «Согласовать вживую»: клиент стоит рядом и сказал «да».
                # Пишем, кто именно согласовал - на спор «я такого не
                # заказывал» это ответ.
                await service.answer_estimate(crm, order, agree=action == "agree",
                                              by=who(request))
                flash(request, "Согласовано, наряд в работе."
                      if action == "agree" else "Отказ: наряд отменён.")
            else:
                flash(request, "Непонятное действие.", "err")
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
        return redirect(back)

    @app.post("/orders/{order_id}/invoice")
    async def order_invoice(request: Request, order_id: int) -> Response:
        """Счёт клиенту за ремонт со ссылкой на оплату."""
        if not may_edit(request, "service"):
            return denied(request, "service")
        order = await crm.work_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Наряд")
        try:
            invoice = await service.invoice_order(crm, order, by=who(request),
                                                  acquiring=await acquiring_live())
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/orders/{order_id}")
        if invoice.get("status") == "failed":
            flash(request, f"Счёт {invoice['no']} заведён, но ссылки нет: "
                           f"{invoice.get('error') or 'банк не ответил'}", "err")
        else:
            sent = await notices.send_client(
                crm, "repair_invoice", invoice["client_id"],
                lambda: notify.pay_link(bot, db, invoice))
            flash(request, f"Счёт {invoice['no']} на "
                           f"{logic.money(invoice['amount'])} "
                  + ("отправлен клиенту." if sent else
                     "готов — передайте ссылку клиенту сами."))
        return redirect(f"/orders/{order_id}")

    @app.post("/orders/{order_id}/items")
    async def order_add_item(request: Request, order_id: int) -> Response:
        order = await crm.work_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Наряд")
        if not logic.order_is_open(order):
            flash(request, "Наряд закрыт - строки больше не добавляются.", "err")
            return redirect(f"/orders/{order_id}")
        data = await form(request)
        work_type = await by_id(crm.work_type, data.get("work_type_id"))
        title = logic.check_name(data.get("title") or (work_type or {}).get("title"),
                                 what="Работа")
        qty = count_field(data, "qty", what="Количество", limit=99, least=1)
        price = cost_field(data, "price")
        # Цена из прайса: лист по объекту наряда, только когда платит
        # клиент и поле оставили пустым. «0» руками - это ноль, а не
        # просьба подставить; своему ремонту цена клиенту ни к чему.
        if (work_type and order.get("payer") == "client"
                and not (data.get("price") or "").strip()):
            sheet = logic.price_sheet(order)
            from_sheet = logic.sheet_price(work_type, sheet)
            if from_sheet is None:
                flash(request, f"«{work_type['title']}» в прайсе "
                               f"«{logic.PRICE_SHEETS[sheet]}» нет - укажите цену "
                               "клиенту руками.", "err")
                return redirect(f"/orders/{order_id}")
            price = logic.Check(True, from_sheet)
        parts = cost_field(data, "parts_cost")
        labor = cost_field(data, "labor_cost")
        for check in (title, qty, price, parts, labor):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/orders/{order_id}")
        node = (data.get("node") or (work_type or {}).get("node") or "").strip() or None
        if node and not logic.check_choice(node, logic.REPAIR_NODES).ok:
            node = None
        await crm.add_order_item(
            order_id, title=title.value, node=node,
            work_type_id=(work_type or {}).get("id"), qty=qty.value,
            price=price.value, parts_cost=parts.value, labor_cost=labor.value)
        flash(request, "Строка добавлена.")
        return redirect(f"/orders/{order_id}")

    @app.post("/orders/{order_id}/parts")
    async def order_take_part(request: Request, order_id: int) -> Response:
        """Списать запчасть со склада в наряд.

        Себестоимость строки берётся со склада, а не с потолка: до склада
        механик писал её руками, и отчёт по ремонту ничего не значил.
        """
        order = await crm.work_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Наряд")
        if not may_edit(request, "service"):
            return denied(request, "service")
        data = await form(request)
        part = await by_id(crm.part, data.get("part_id"))
        qty = count_field(data, "qty", what="Количество", default="1",
                          limit=999, least=1)
        if part is None or not qty.ok:
            flash(request, qty.error or "Выберите позицию склада.", "err")
            return redirect(f"/orders/{order_id}")
        try:
            result = await service.issue_part_to_order(crm, order, part, qty.value,
                                                       by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/orders/{order_id}")
        flash(request, f"«{part['title']}» списано со склада: {result['qty']} шт., "
                       f"на полке осталось {result['stock_left']}.")
        return redirect(f"/orders/{order_id}")

    @app.post("/orders/{order_id}/items/{item_id}/delete")
    async def order_delete_item(request: Request, order_id: int,
                                item_id: int) -> Response:
        if not may_edit(request, "service"):
            return denied(request, "service")
        order = await crm.work_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Наряд")
        if not logic.order_is_open(order):
            flash(request, "Наряд закрыт: строки в нём уже не меняются.", "err")
            return redirect(f"/orders/{order_id}")
        if not await crm.delete_order_item(order_id, item_id, by=who(request)):
            flash(request, "Строки уже нет.", "err")
        return redirect(f"/orders/{order_id}")

    @app.post("/orders/{order_id}/edit")
    async def order_edit(request: Request, order_id: int) -> Response:
        order = await crm.work_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Наряд")
        data = await form(request)
        # Выпадающий список формы - ORDER_MANUAL_STATUSES: «на согласовании»
        # ставит отправка сметы, и мимо формы его принимать нельзя - наряд
        # встал бы в approve без сметы, а вывести его оттуда нечем. «Готов»
        # в список входит нарочно: у него ниже свой ответ про кнопку
        # закрытия, и общее «недопустимое значение» его бы съело.
        allowed = {k: logic.ORDER_STATUSES[k]
                   for k in (*logic.ORDER_MANUAL_STATUSES, "done",
                             order["status"])
                   if k in logic.ORDER_STATUSES}
        status = logic.check_choice(data.get("status") or order["status"],
                                    allowed, what="Статус наряда")
        estimate = cost_field(data, "estimate")
        note = logic.check_note(data.get("note"))
        place = logic.check_location(data.get("location"),
                                     await location_names(order.get("location")))
        for check in (status, estimate, note, place):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/orders/{order_id}")
        if status.value == "done":
            flash(request, "Готовый наряд закрывается кнопкой «Закрыть наряд»: "
                           "она считает сумму и пишет ремонт в журнал.", "err")
            return redirect(f"/orders/{order_id}")
        if order["status"] == "approve" and status.value != "approve":
            # Форма на согласовании статус не отдаёт, но запрос можно
            # послать и мимо неё: молча снять ожидание ответа нельзя.
            flash(request, "Наряд на согласовании: ответьте за клиента "
                           "или отправьте смету заново.", "err")
            return redirect(f"/orders/{order_id}")
        tech_id = logic.parse_id(data.get("tech_id"))
        if not await tech_known(tech_id):
            flash(request, "Такого сотрудника нет — обновите страницу.", "err")
            return redirect(f"/orders/{order_id}")
        fields: dict[str, Any] = {"status": status.value, "tech_id": tech_id,
                                  "estimate": estimate.value, "note": note.value}
        # Велосипед перевезли чинить на другую точку - наряд едет за ним.
        # Поля нет в форме - точку не трогаем, а не стираем.
        if "location" in data:
            fields["location"] = place.value
        # Плательщик задавался при открытии и потом не менялся - а «наш»
        # ремонт, оказавшийся клиентским после разборки, приходилось
        # закрывать и заводить заново. Меняется, пока смета не ушла и
        # счёт не выставлен: после этого клиенту уже что-то обещали.
        payer = logic.check_choice(data.get("payer") or order["payer"], logic.PAYERS,
                                   what="Плательщик")
        if not payer.ok:
            flash(request, payer.error, "err")
            return redirect(f"/orders/{order_id}")
        phone = bot_logic.normalize_phone(data.get("client_phone"))
        if phone:
            found = await crm.client_by_phone(phone)
            if found is None:
                flash(request, f"Клиента с телефоном {phone} нет.", "err")
                return redirect(f"/orders/{order_id}")
            fields["client_id"] = found["id"]
        if payer.value != order["payer"]:
            if order.get("estimate_sent_at") or await crm.work_order_invoices(order_id):
                flash(request, "Плательщика не сменить: смета уже отправлена или "
                               "счёт выставлен.", "err")
                return redirect(f"/orders/{order_id}")
            if payer.value == "client" and not (fields.get("client_id")
                                                or order.get("client_id")
                                                or order.get("object_note")):
                flash(request, "Клиентский ремонт: укажите клиента по телефону.", "err")
                return redirect(f"/orders/{order_id}")
            fields["payer"] = payer.value
        await crm.update_work_order(order_id, **fields)
        # Только смена техника: иначе человек получал бы «на тебя наряд»
        # при каждой правке сметы.
        if tech_id and tech_id != order.get("tech_id"):
            await notify_tech(order_id, tech_id)
        flash(request, "Наряд сохранён.")
        return redirect(f"/orders/{order_id}")

    @app.post("/orders/{order_id}/close")
    async def order_close(request: Request, order_id: int) -> Response:
        order = await crm.work_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Наряд")
        data = await form(request)
        bike_status = logic.check_choice(data.get("bike_status") or "available",
                                         logic.BIKE_MANUAL_STATUSES, what="Статус")
        if not bike_status.ok:
            flash(request, bike_status.error, "err")
            return redirect(f"/orders/{order_id}")
        try:
            totals = await service.close_order(crm, order, by=who(request),
                                               bike_status=bike_status.value)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/orders/{order_id}")
        # Клиентский ремонт - это человек, который ждёт свою технику.
        # Свой парк чинится молча: ждать там нечего и некому.
        if order.get("payer") == "client" and order.get("client_id"):
            client = await crm.client(order["client_id"])
            if client:
                await notices.send_client(
                    crm, "repair_ready", client["id"],
                    lambda: notify.repair_ready(bot, client, order,
                                                totals["total"]))
        flash(request, f"Наряд закрыт: клиенту {logic.money(totals['total'])}, "
                       f"себестоимость {logic.money(totals['cost'])}.")
        return redirect(f"/orders/{order_id}")

    @app.post("/orders/{order_id}/paid")
    async def order_paid(request: Request, order_id: int) -> Response:
        """Отметка об оплате клиентского ремонта.

        Деньги остаются на наряде и в crm.ledger не попадают: журнал -
        это аренда, по нему считается средний чек парка.
        """
        order = await crm.work_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Наряд")
        method = (await form(request)).get("method") or None
        try:
            shift_id = await service.mark_repair_paid(crm, order, method=method,
                                                      by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/orders/{order_id}")
        if method == "cash" and shift_id is None:
            flash(request, "Отмечено как оплаченный. Открытой смены нет — наличные "
                           "в кассу не записаны: откройте смену и внесите их.", "err")
        elif method == "cash":
            flash(request, "Отмечено как оплаченный: наличные внесены в кассу смены.")
        else:
            flash(request, "Отмечено как оплаченный.")
        return redirect(f"/orders/{order_id}")

    # ─────────────────────── сервис: виды работ ───────────────────────

    WORK_SORTS = {"title": "title", "category": "category", "minutes": "minutes",
                  "price": "own_total", "price_ext": "ext_total", "used": "used"}

    def sheet_pair(data: dict, work: str, parts: str) -> logic.Check:
        """Лист прайса из формы: работа и запчасть.

        Оба поля пустые - работы в этом листе нет (None), и наряд не
        станет подставлять ноль за «нет цены». Заполнено хоть одно -
        пустое читается нулём: «Пайка фары» без запчасти это честный ноль.
        """
        raw_work = (data.get(work) or "").strip()
        raw_parts = (data.get(parts) or "").strip()
        if not raw_work and not raw_parts:
            return logic.Check(True, (None, Decimal(0)))
        checks = cost_field(data, work), cost_field(data, parts)
        for check in checks:
            if not check.ok:
                return check
        return logic.Check(True, (checks[0].value, checks[1].value))

    @app.get("/work-types")
    async def work_types_page(request: Request) -> Response:
        q = request.query_params.get("q") or ""
        rows = logic.rows_search(logic.work_type_rows(await crm.work_types()), q,
                                 ("title", "category"))
        tools = list_tools(request, rows, allowed=WORK_SORTS)
        return render(request, "work_types.html", rows=tools["rows"], tools=tools,
                      q=q, can_manage=may_edit(request, "service"),
                      nodes=await crm.repair_nodes(),
                      repair_norm=logic.repair_norm_default(await crm.settings()))

    @app.get("/work-types.{ext}")
    async def work_types_csv(request: Request, ext: str) -> Response:
        rows = logic.rows_search(logic.work_type_rows(await crm.work_types()),
                                 request.query_params.get("q") or "",
                                 ("title", "category"))

        def cell(value: Any) -> Any:
            return "" if value is None else logic.to_money(value)

        return await table(ext, "work-types",
                      ["Наименование", "Категория", "Узел", "Время, мин",
                       "Арендатору: работа", "Арендатору: запчасть", "Арендатору: итого",
                       "Стороннему: работа", "Стороннему: запчасть", "Стороннему: итого",
                       "Использований", "Статус"],
                      [[r["title"], r.get("category"),
                        logic.REPAIR_NODES.get(str(r.get("node") or ""), ""),
                        r.get("minutes"),
                        cell(r.get("price")),
                        cell(r.get("parts_price")) if r.get("price") is not None else "",
                        cell(r.get("own_total")),
                        cell(r.get("price_ext")),
                        cell(r.get("parts_price_ext")) if r.get("price_ext") is not None else "",
                        cell(r.get("ext_total")),
                        r.get("used"), "активна" if r.get("active") else "выключена"]
                       for r in rows])

    @app.post("/work-types")
    async def work_type_create(request: Request) -> Response:
        data = await form(request)
        title = logic.check_name(data.get("title"), what="Наименование")
        minutes = count_field(data, "minutes", what="Время", default="0", limit=999)
        own = sheet_pair(data, "price", "parts_price")
        ext = sheet_pair(data, "price_ext", "parts_price_ext")
        for check in (title, minutes, own, ext):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/work-types")
        category = (data.get("category") or "Прочее").strip()
        if category not in logic.WORK_CATEGORIES:
            category = "Прочее"
        node = (data.get("node") or "").strip() or None
        if node and not logic.check_choice(node, logic.REPAIR_NODES).ok:
            node = None
        try:
            await crm.create_work_type(title=title.value, category=category,
                                       minutes=minutes.value, price=own.value[0],
                                       parts_price=own.value[1],
                                       price_ext=ext.value[0],
                                       parts_price_ext=ext.value[1], node=node)
        except Exception as exc:                        # noqa: BLE001
            if not name_taken(exc):
                raise
            flash(request, "Работа с таким названием уже есть.", "err")
            return redirect("/work-types")
        flash(request, "Вид работ добавлен.")
        return redirect("/work-types")

    @app.post("/work-types/norms")
    async def repair_norms_save(request: Request) -> Response:
        """Сроки ремонта: общий и по узлам. Маршрут - раньше строки прайса,
        иначе «/work-types/{type_id}» поймал бы «norms» и ответил 422.

        Пустое поле узла - «своего срока нет, общий». Сохраняется одной
        формой: срок ставят, глядя на соседние узлы, а не по одному.
        """
        data = await form(request)
        common = count_field(data, "repair_norm_days", what="Общий срок",
                             default=str(logic.ORDER_STUCK_DAYS),
                             limit=logic.REPAIR_NORM_MAX)
        if not common.ok:
            flash(request, common.error, "err")
            return redirect("/work-types#norms")
        nodes = await crm.repair_nodes()
        changes: list[tuple[str, int | None]] = []
        for node in nodes:
            field = f"norm_{node['code']}"
            if field not in data:
                continue
            got = logic.check_norm_days(data.get(field))
            if not got.ok:
                flash(request, f"{node['title']}: {got.error}", "err")
                return redirect("/work-types#norms")
            if got.value != node.get("norm_days"):
                changes.append((node["code"], got.value))
        await crm.set_setting("repair_norm_days", str(common.value), by=who(request))
        for code, value in changes:
            await crm.set_node_norm(code, value)
        flash(request, "Сроки ремонта сохранены"
              + (f": узлов изменено {len(changes)}." if changes else "."))
        return redirect("/work-types#norms")

    @app.post("/work-types/{type_id}")
    async def work_type_edit(request: Request, type_id: int) -> Response:
        if await crm.work_type(type_id) is None:
            return render(request, "missing.html", status_code=404, what="Вид работ")
        data = await form(request)
        if data.get("action") == "toggle":
            current = await crm.work_type(type_id)
            await crm.update_work_type(type_id, active=not current["active"])
            return redirect("/work-types")
        title = logic.check_name(data.get("title"), what="Наименование")
        minutes = count_field(data, "minutes", what="Время", default="0", limit=999)
        own = sheet_pair(data, "price", "parts_price")
        ext = sheet_pair(data, "price_ext", "parts_price_ext")
        current = await crm.work_type(type_id)
        # Категорию и узел завели при создании и потом не трогали - а
        # «Замена мотор-колеса» в «Электрике» вместо «Ходовой» портила
        # отчёт «что ломается» навсегда.
        category = logic.check_choice(data.get("category") or current["category"],
                                      logic.WORK_CATEGORIES, what="Категория")
        node = str(data.get("node") if "node" in data else current.get("node") or "")
        if node and node not in logic.REPAIR_NODES:
            flash(request, "Узел: только из справочника.", "err")
            return redirect("/work-types")
        for check in (title, minutes, own, ext, category):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/work-types")
        try:
            await crm.update_work_type(type_id, title=title.value, price=own.value[0],
                                       parts_price=own.value[1], price_ext=ext.value[0],
                                       parts_price_ext=ext.value[1],
                                       minutes=minutes.value, category=category.value,
                                       node=node or None)
        except Exception as exc:                        # noqa: BLE001
            if not name_taken(exc):
                raise
            flash(request, "Работа с таким названием уже есть.", "err")
            return redirect("/work-types")
        flash(request, "Сохранено.")
        return redirect("/work-types")

    # ─────────────────── реквизиты и шаблоны документов ───────────────────

    def template_rows(request: Request) -> list[dict]:
        """Шаблоны документов: что есть, читается ли и какие в нём поля.

        Панель не хранит шаблоны у себя - она показывает те, из которых бот
        собирает документы прямо сейчас. Проверка чтением: битый файл лучше
        увидеть здесь, чем в момент, когда клиенту уже сказали «договор готов».
        """
        del request
        rows = []
        for title, path in (("Договор аренды", cfg_path("contract_template")),
                            ("Акт приёма-передачи", cfg_path("act_in_template")),
                            ("Акт возврата", cfg_path("act_out_template")),
                            ("Согласие на обработку ПДн", cfg_path("soglasie_template")),
                            ("Договор выкупа", cfg_path("buyout_template")),
                            ("Политика обработки ПДн", cfg_path("pdn_policy_file"))):
            row = {"title": title, "path": str(path) if path else "—",
                   "ok": False, "fields": [], "error": ""}
            if path is None:
                row["error"] = "путь не задан"
            elif not _file_exists(str(path)):
                row["error"] = "файла нет на диске"
            else:
                row["size"] = os.path.getsize(str(path))
                try:
                    data = contract_service.load_template(Path(str(path)))
                    row["ok"] = True
                    row["fields"] = sorted(contract_service.placeholders(data))
                except Exception as exc:                 # noqa: BLE001
                    row["error"] = str(exc)
            rows.append(row)
        return rows

    def cfg_path(name: str) -> Any:
        return getattr(cfg, name, None)

    @app.get("/company")
    async def company_page(request: Request) -> Response:
        if not may_view(request, "settings"):
            return denied(request, "settings")
        settings = await crm.settings()
        return render(request, "company.html",
                      values={code: settings.get(code, "")
                              for code in company.ALL_FIELDS},
                      default_contact=texts.SUPPORT_CONTACT_URL,
                      templates=template_rows(request))

    async def save_company(request: Request, data: dict) -> bool:
        """Реквизиты из формы - в настройки; ошибка уже во flash, тогда False.
        Один путь для страницы реквизитов и мастера первого запуска."""
        clean: dict[str, str] = {}
        for code, label in company.ALL_FIELDS.items():
            # Контакт менеджера проверяется строже реквизита: это ссылка,
            # по которой пойдёт клиент, а не строка в шапке договора.
            check = (company.check_contact if code in company.CONTACT_FIELDS
                     else company.check_value)
            value, error = check(data.get(code))
            if error:
                flash(request, f"{label}: {error}.", "err")
                return False
            clean[code] = value
        for code, value in clean.items():
            await crm.set_setting(code, value, by=who(request))
        # Панель и бот читают одни и те же настройки: снимок в этом
        # процессе обновляем сразу, чтобы не ждать своего же TTL.
        company.set_snapshot(await crm.settings())
        return True

    @app.post("/company")
    async def company_save(request: Request) -> Response:
        """Реквизиты организации: их подставляют договор и акты.

        Бот - другой процесс, он подхватывает правку снимком в течение
        нескольких минут; в панели об этом написано прямо.
        """
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        if await save_company(request, await form(request)):
            flash(request, "Сохранено. Бот подхватит правку в течение "
                           "нескольких минут.")
        return redirect("/company")

    async def readiness_facts(settings: dict | None = None) -> dict[str, Any]:
        """Факты «Готовности» из базы - одним набором для страницы, мастера
        первого запуска и решения, встречать ли им владельца."""
        return {"settings": await crm.settings() if settings is None else settings,
                "locations": await crm.locations(),
                "models": await crm.bike_models(active_only=True),
                "tariffs": await crm.tariffs(active_only=True, kind="bike"),
                "staff": await crm.staff_all()}

    async def readiness_items(facts: dict[str, Any]) -> list[dict]:
        settings = facts["settings"]
        return readiness.checks(
            settings=settings, consent=texts.CONSENT, locations=facts["locations"],
            models=facts["models"], tariffs=facts["tariffs"], staff=facts["staff"],
            bot=await bot_health(), acquiring=acquiring_state(settings),
            bank_last=next(iter(await crm.bank_txns(limit=1)), None),
            trackers=await crm.trackers(), https=cfg.trust_proxy,
            now=datetime.now(UTC))

    @app.get("/readiness")
    async def readiness_page(request: Request) -> Response:
        """Готовность установки: что не настроено и куда идти чинить.

        Только чтение и только то, что панель видит сама: база, свой конфиг
        и живость бота. О фоновых опросах бота судим по их следам в базе.
        """
        items = await readiness_items(await readiness_facts())
        return render(request, "readiness.html", items=items,
                      summary=readiness.summary(items))

    # ─────────────────── мастер первого запуска ───────────────────
    #
    # «Готовность» по шагам с формами на месте (app/crm/firstrun.py). Своих
    # проверок и своей записи у мастера нет: состояние шага - строка
    # «Готовности», сохраняет шаг тот же помощник, что и страница раздела
    # (save_company, add_location, store_tariff, add_staff).

    async def setup_wanted(staff: dict | None,
                           settings: dict | None = None) -> dict | None:
        """Встретить ли владельца мастером: прогресс для плашки, None - нет.

        Отказы без запросов и дешёвые - первыми: боевая база отвечает
        «уже работали» одним запросом. Сбой здесь не мешает ни входу, ни
        сводке: мастер - подсказка, а не ворота.
        """
        if cfg.demo or not firstrun.is_owner(staff):
            return None
        try:
            settings = await crm.settings() if settings is None else settings
            if firstrun.hidden(settings):
                return None
            if not firstrun.started(settings):
                if await crm.history_start() is not None:
                    return None
                # Свежую базу застали - решение записываем сразу: велосипед,
                # заведённый на полпути, журнал статусов уже не пуст.
                await crm.set_setting(firstrun.STATE_KEY, "",
                                      by=f"staff:{(staff or {}).get('login')}")
                settings = {**settings, firstrun.STATE_KEY: ""}
            items = firstrun.essentials(**await readiness_facts(settings))
            rows = firstrun.steps(items, settings)
            if not firstrun.wanted(staff=staff, settings=settings, demo=cfg.demo,
                                   rows=rows):
                return None
            return firstrun.progress(rows)
        except Exception:                                # noqa: BLE001
            log.exception("мастер первого запуска: установку проверить не удалось")
            return None

    # Пароль входа, заведённого мастером, - до следующей страницы мастера
    # этого владельца: staff_id -> (когда, что показать). В памяти процесса,
    # а не в cookie: при двойном клике второй ответ перезаписывает cookie
    # первого, и единственная копия пароля пропадала бы вместе с ней.
    # Процесс панели один, как и у ключей форм; дольше срока не ждём.
    setup_issued: dict[int, tuple[float, dict]] = {}
    SETUP_ISSUED_TTL = 600

    def setup_shown(request: Request) -> dict | None:
        staff_id = request.state.staff["id"]
        issued = setup_issued.pop(staff_id, None)
        if issued is None or time.monotonic() - issued[0] > SETUP_ISSUED_TTL:
            return None
        return issued[1]

    async def setup_pass(request: Request, code: str) -> None:
        """Шаг пройден - в настройки: прогресс переживает выход и другой
        браузер. Повтор ничего не меняет."""
        settings = await crm.settings()
        await crm.set_setting(firstrun.STEPS_KEY, firstrun.with_step(settings, code),
                              by=who(request))

    @app.get("/setup")
    async def setup_page(request: Request) -> Response:
        """Мастер первого запуска: шаг, формы на месте и прогресс.

        Страница открыта и скрытому мастеру, и в демо (только посмотреть:
        POST там закрыт стражем демо). Сама она ничего не решает - сам
        мастер встречает владельца только по setup_wanted.
        """
        facts = await readiness_facts()
        settings = facts["settings"]
        items = await readiness_items(facts)
        by = {i["code"]: i for i in items}
        rows = firstrun.steps(items, settings)
        step = firstrun.current(rows, request.query_params.get("step"))
        ctx: dict[str, Any] = {}
        if step["code"] == "company":
            ctx.update(values={code: settings.get(code, "") for code in company.ALL_FIELDS},
                       default_contact=texts.SUPPORT_CONTACT_URL, consent=by.get("consent"))
        elif step["code"] == "points":
            ctx.update(points=[p for p in facts["locations"] if p.get("active", True)],
                       closed=[p["name"] for p in facts["locations"]
                               if not p.get("active", True)])
        elif step["code"] == "prices":
            ctx.update(prices=firstrun.price_rows(facts["models"], facts["tariffs"]),
                       archived=[m["title"] for m in await crm.bike_models()
                                 if not m.get("active", True)])
        elif step["code"] == "staff":
            # /setup открыт по праву на настройки, а логины и имена - раздел
            # «Сотрудники»: без него - только число входов из «Готовности».
            see = may_view(request, "staff")
            profiles = {p.get("code"): p for p in await crm.access_profiles()}
            taken = firstrun.staff_roles(facts["staff"] if see else [])
            ctx.update(people=[s for s in facts["staff"] if see and s.get("active", True)],
                       staff_closed=not see, places=await location_names(),
                       roles=[{"code": role, "title": title, "logins": taken[role],
                               "profile": profiles.get(code)}
                              for role, (code, title) in firstrun.ROLES.items()])
        elif step["code"] == "connect":
            ctx["connect"] = [by[code] for code in firstrun.CONNECT if code in by]
        return render(request, "setup.html", steps=rows, step=step,
                      skip=firstrun.following(rows, step["code"]),
                      progress=firstrun.progress(rows), hidden=firstrun.hidden(settings),
                      finished=settings.get(firstrun.STATE_KEY) == "done",
                      summary=readiness.summary(items), connect_how=firstrun.CONNECT_HOW,
                      shown=setup_shown(request), **ctx)

    @app.post("/setup/company")
    async def setup_company(request: Request) -> Response:
        # Раздел /setup - настройки: право на правку проверил страж.
        if not await save_company(request, await form(request)):
            return redirect("/setup?step=company")
        await setup_pass(request, "company")
        flash(request, "Реквизиты сохранены. Бот подхватит их в течение "
                       "нескольких минут.")
        return redirect("/setup")

    @app.post("/setup/points")
    async def setup_points(request: Request) -> Response:
        """Своя точка - той же проверкой, что в «Точках», но с адресом, режимом
        и телефоном обязательно: ими бот отвечает клиенту «где вы» и «до
        скольки». Поставочную точку (Казань) можно тут же закрыть."""
        data = await form(request)
        back = "/setup?step=points"
        action = data.get("action") or ""
        if action == "close":
            place_id = logic.parse_id(data.get("location_id"))
            rows = [x for x in await crm.locations() if x["id"] == place_id]
            if not rows:
                return render(request, "missing.html", status_code=404, what="Точка")
            # Как «Закрыть» в «Точках»: карточки на ней остаются.
            await crm.update_location(rows[0]["id"], active=False)
            flash(request, f"Точка «{rows[0]['name']}» закрыта: в ответах бота и "
                           "на выдаче её больше нет, открыть снова — в «Точках».")
        elif action == "add":
            # Город - тоже: пустой «Точки» читают как Казань, а у
            # франчайзи свой город.
            missing = [label for field, label in {"city": "город",
                                                  **readiness.POINT_FIELDS}.items()
                       if not (data.get(field) or "").strip()]
            if missing:
                flash(request, "Заполните " + ", ".join(missing) + ": этими полями "
                               "бот отвечает клиенту «где вы» и «до скольки».", "err")
                return redirect(back)
            name = await add_location(request, data)
            if name is None:
                return redirect(back)
            flash(request, f"Точка «{name}» добавлена.")
        await setup_pass(request, "points")
        return redirect(back if action in ("close", "add") else "/setup")

    @app.post("/setup/prices")
    async def setup_prices(request: Request) -> Response:
        """Модели, которые сдаём, и цена недели у каждой - тем же путём, что
        «Тарифы» (_tariff_fields, store_tariff и уникальность «модель + срок»).
        Есть недельный тариф - правится его цена, нет - заводится новый;
        та же цена ничего не пишет. Снятая галочка убирает модель в архив
        каталога, как кнопка в «Каталоге». Пустое поле - цену не трогаем."""
        if not may_edit(request, "tariffs"):
            return denied(request, "tariffs")
        data = await form(request)
        back = "/setup?step=prices"
        rows = firstrun.price_rows(await crm.bike_models(active_only=True),
                                   await crm.tariffs(active_only=True, kind="bike"))
        keep = [r for r in rows if r["key"] == firstrun.ANY_MODEL
                or data.get(f"use_{r['key']}")]
        writes = []
        for row in keep:
            raw = (data.get(f"week_{row['key']}") or "").strip()
            week = row["week"]
            if not raw or (week is not None and logic.parse_money(raw) == week["price"]):
                continue
            price = logic.check_amount(raw)
            if not price.ok:
                flash(request, f"{row['title']}: {price.error}", "err")
                return redirect(back)
            fields = _tariff_fields(request, {
                "name": week["name"] if week else "Неделя", "period_days": str(firstrun.WEEK),
                "price": raw, "note": (week or {}).get("note") or "", "kind": "bike",
                "model": row["model"] or ""})
            if fields is None:
                return redirect(back)
            writes.append((fields, week["id"] if week else None))
        for fields, tariff_id in writes:
            if not await store_tariff(request, fields, tariff_id):
                return redirect(back)
        dropped = [r for r in rows if r not in keep]
        for row in dropped:
            await crm.update_bike_model(row["key"], active=False)
        await setup_pass(request, "prices")
        flash(request, f"Цен недели сохранено: {len(writes)}"
                       + (f"; в архив каталога: {', '.join(r['title'] for r in dropped)}"
                          if dropped else "") + ".")
        return redirect("/setup")

    @app.post("/setup/staff")
    async def setup_staff(request: Request) -> Response:
        """Администратор или мастер на своей роли. Пароль придумывает
        панель и показывает один раз: в базе только хэш, повторить его
        нечем, а забытый задаётся заново в «Сотрудниках»."""
        if not may_edit(request, "staff"):
            return denied(request, "staff")
        data = await form(request)
        back = "/setup?step=staff"
        if not form_once(data):
            # Двойной клик: вход завело первое нажатие, его пароль ждёт в
            # setup_issued - страница мастера покажет его и после этого ответа.
            flash(request, "Форма уже отправлена — повторное нажатие пропущено.")
            return redirect(back)
        role = firstrun.ROLES.get(data.get("role") or "")
        if role is None:
            form_once_release(data)
            flash(request, "Выберите, кого заводите: администратора или мастера.", "err")
            return redirect(back)
        profile = await crm.access_profile_by_code(role[0])
        if profile is None:
            form_once_release(data)
            flash(request, "Такой роли в панели нет — заведите сотрудника в "
                           "«Сотрудниках».", "err")
            return redirect(back)
        password = logic.generate_password()
        added = await add_staff(request, {**data, "password": password,
                                          "profile_id": str(profile["id"])})
        if isinstance(added, str):
            form_once_release(data)
            flash(request, added, "err")
            return redirect(back)
        await setup_pass(request, "staff")
        # До следующей страницы мастера, и только её: она забирает пароль,
        # и обновление страницы его уже не покажет.
        setup_issued[request.state.staff["id"]] = (time.monotonic(), {
            "login": added["login"], "password": password, "role": role[1],
            "profile": profile["name"]})
        return redirect(back)

    @app.post("/setup/pass")
    async def setup_step_pass(request: Request) -> Response:
        """«Дальше» без записи: точки и сотрудники такие, как есть, а
        подключения делаются на сервере. Проверка - своей кнопкой."""
        code = (await form(request)).get("step") or ""
        if code in firstrun.STEPS and code != "check":
            await setup_pass(request, code)
        return redirect("/setup")

    @app.post("/setup/finish")
    async def setup_finish(request: Request) -> Response:
        await setup_pass(request, "check")
        await crm.set_setting(firstrun.STATE_KEY, "done", by=who(request))
        flash(request, "Мастер первого запуска пройден. Что ещё не готово — здесь, "
                       "на «Готовности».")
        return redirect("/readiness")

    @app.post("/setup/dismiss")
    async def setup_dismiss(request: Request) -> Response:
        """Скрыть: после входа и на сводке мастера больше нет. Работе он не
        мешал и так - страница остаётся по ссылке с «Готовности»."""
        await crm.set_setting(firstrun.STATE_KEY, "dismissed", by=who(request))
        flash(request, "Мастер скрыт. Вернуться к нему — «Настройки → Готовность».")
        return redirect("/")

    @app.post("/setup/resume")
    async def setup_resume(request: Request) -> Response:
        await crm.set_setting(firstrun.STATE_KEY, "", by=who(request))
        return redirect("/setup")

    # ───────────────── справочники: точки, модели, совместимость ─────────────────

    @app.get("/locations")
    async def locations_page(request: Request) -> Response:
        if not may_view(request, "settings"):
            return denied(request, "settings")
        rows = await crm.locations()
        return render(request, "locations.html", rows=rows,
                      cities=logic.by_city(rows),
                      bikes=logic.bikes_by_point(await crm.bikes(limit=10000)))

    def location_extra(data: dict) -> dict:
        """Телефон, режим и координаты пункта: по ним клиент находит точку,
        а карта - центр города, когда трекеров ещё нет."""
        def coord(name: str) -> float | None:
            raw = (data.get(name) or "").strip().replace(",", ".")
            try:
                value = float(raw) if raw else None
            except (TypeError, ValueError):
                return None
            return value if value is not None and -180 <= value <= 180 else None

        # «Как найти» - для клиента: бот называет его под адресом («заезд
        # в ГСК, 9-й бокс»). Описание (note) остаётся для своих.
        return {"public_title": (data.get("public_title") or "").strip() or None,
                "phone": (data.get("phone") or "").strip() or None,
                "hours": (data.get("hours") or "").strip() or None,
                "directions": (data.get("directions") or "").strip() or None,
                "lat": coord("lat"), "lon": coord("lon")}

    async def add_location(request: Request, data: dict) -> str | None:
        """Новая точка справочника из формы: её имя, None - ошибка уже во
        flash. Один путь для «Точек» и мастера первого запуска."""
        name = logic.check_name(data.get("name"), what="Название точки")
        city = logic.check_name(data.get("city") or "Казань", what="Город")
        note = logic.check_note(data.get("note"))
        for check in (name, city, note):
            if not check.ok:
                flash(request, check.error, "err")
                return None
        if name.value.lower() == "none":
            # «none» в адресе фильтра значит «без точки».
            flash(request, "Такое название занято фильтром «без точки».", "err")
            return None
        try:
            await crm.create_location(
                name=name.value, city=city.value,
                address=(data.get("address") or "").strip() or None,
                note=note.value, **location_extra(data))
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Точка с таким названием уже есть.", "err")
                return None
            raise
        return name.value

    @app.post("/locations")
    async def location_create(request: Request) -> Response:
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        if await add_location(request, await form(request)) is not None:
            flash(request, "Точка добавлена.")
        return redirect("/locations")

    # До /locations/{location_id}: иначе «transfer» ушёл бы туда номером точки.
    @app.post("/locations/transfer")
    async def transfer_settings_save(request: Request) -> Response:
        """Запас на точке для переброски: сколько свободных каждой модели
        точка оставляет себе сверх прогноза. Правится в блоке «Переброска»
        отчёта по точкам, а право - настроек: это правило сети, а не отчёт."""
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        data = await form(request)
        got = count_field(data, "transfer_safety", what="Запас на точке",
                          default=str(logic.TRANSFER_SAFETY),
                          limit=logic.TRANSFER_SAFETY_MAX)
        if not got.ok:
            flash(request, got.error, "err")
            return redirect("/reports/points#transfer")
        await crm.set_setting("transfer_safety", str(got.value), by=who(request))
        flash(request, f"Запас на точке для переброски: {got.value}.")
        return redirect("/reports/points#transfer")

    @app.post("/locations/{location_id}")
    async def location_edit(request: Request, location_id: int) -> Response:
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        rows = [x for x in await crm.locations() if x["id"] == location_id]
        if not rows:
            return render(request, "missing.html", status_code=404, what="Точка")
        data = await form(request)
        note = logic.check_note(data.get("note"))
        city = logic.check_name(data.get("city") or rows[0]["city"], what="Город")
        # Порядок - в списках панели и в ответе бота «где вы»: первой
        # стоит главная точка, а не та, что раньше по алфавиту.
        sort = count_field(data, "sort", what="Порядок",
                           default=str(100 if rows[0].get("sort") is None
                                       else rows[0]["sort"]), limit=9999)
        for check in (note, city, sort):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/locations")
        await crm.update_location(
            location_id, address=(data.get("address") or "").strip() or None,
            note=note.value, city=city.value, sort=sort.value,
            # Только поля, что пришли в форме: форма, открытая до выката
            # нового поля («Как найти»), иначе стёрла бы его значение.
            **{k: v for k, v in location_extra(data).items() if k in data})
        flash(request, "Точка сохранена.")
        return redirect("/locations")

    @app.post("/locations/{location_id}/rename")
    async def location_rename(request: Request, location_id: int) -> Response:
        """Новое имя точки - каскадом по всему, что хранит её имя: парк,
        батареи, кассы, пересчёты, аренды, наряды, сотрудники, журнал мест.
        Одной транзакцией: наполовину переименованная точка раскидала бы
        её велосипеды и деньги по двум строкам отчёта."""
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        name = logic.check_name((await form(request)).get("name"),
                                what="Название точки")
        if not name.ok:
            flash(request, name.error, "err")
            return redirect("/locations")
        if name.value.lower() == "none":
            # «none» в адресе фильтра значит «без точки».
            flash(request, "Такое название занято фильтром «без точки».", "err")
            return redirect("/locations")
        done = await crm.rename_location(location_id, name.value)
        if done is None:
            return render(request, "missing.html", status_code=404, what="Точка")
        if done == "taken":
            flash(request, f"Точка «{name.value}» уже есть — имя должно быть "
                           "своим.", "err")
            return redirect("/locations")
        if done == "orphan":
            # Склейка необратима: строки с новым именем после каскада не
            # отличить от строк точки, и обратное переименование увело бы обе.
            flash(request, f"Имя «{name.value}» уже стоит в записях вне справочника "
                           "(в отчёте «По точкам» — строка «нет в справочнике»). "
                           "Переименование навсегда смешало бы их с этой точкой. "
                           "Чтобы взять эти записи в справочник, добавьте точку "
                           "с таким названием.", "err")
            return redirect("/locations")
        flash(request, f"Точка переименована в «{name.value}»: карточки, аренды, "
                       "наряды, кассы, история и сохранённые фильтры перенесены "
                       "на новое имя.")
        return redirect("/locations")

    @app.post("/locations/{location_id}/toggle")
    async def location_toggle(request: Request, location_id: int) -> Response:
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        rows = [x for x in await crm.locations() if x["id"] == location_id]
        if not rows:
            return render(request, "missing.html", status_code=404, what="Точка")
        # Закрытая точка остаётся в карточках парка: велосипеды на ней
        # никуда не делись, и переписывать их ради красоты справочника
        # значит потерять, где они стоят.
        await crm.update_location(location_id, active=not rows[0]["active"])
        return redirect("/locations")

    @app.get("/models")
    async def models_page(request: Request) -> Response:
        if not may_view(request, "settings"):
            return denied(request, "settings")
        bikes = await crm.bike_models()
        batteries = await crm.battery_models()
        tariffs = [t for t in await crm.tariffs(active_only=True) if t.get("model")]
        prices: dict[str, list] = {}
        for tariff in sorted(tariffs, key=lambda t: int(t["period_days"])):
            prices.setdefault(str(tariff["model"]), []).append(tariff)
        # Названия в парке и в каталоге связаны текстом: расхождение
        # стоит показать здесь, а не выяснять на выдаче.
        known = {m["title"] for m in bikes}
        park = {str(b.get("model") or "").strip()
                for b in await crm.bikes(limit=10000)}
        return render(request, "models.html", bike_models=bikes,
                      battery_models=batteries, prices=prices,
                      unknown_models=sorted(m for m in park if m and m not in known),
                      matrix=logic.compat_matrix(
                          [m for m in bikes if m["active"]],
                          [m for m in batteries if m["active"]],
                          await crm.compat_pairs()))

    def model_specs(request: Request, data: dict) -> dict | None:
        """Характеристики модели из формы. Пустое поле - это «не знаем»,
        а не ноль: «максимальная скорость 0» хуже прочерка. Не число или
        больше колонки - None и сообщение на форме, а не 500."""
        numbers = logic.check_model_specs(data)
        if not numbers.ok:
            flash(request, numbers.error, "err")
            return None
        return {**numbers.value,
                "wheel_size": (data.get("wheel_size") or "").strip() or None,
                "size_note": (data.get("size_note") or "").strip() or None,
                "photo_url": (data.get("photo_url") or "").strip() or None,
                "description": (data.get("description") or "").strip() or None}

    @app.post("/models/bikes")
    async def bike_model_create(request: Request) -> Response:
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        data = await form(request)
        title = logic.check_name(data.get("title"), what="Название модели")
        note = logic.check_note(data.get("note"))
        slots = count_field(data, "battery_slots", what="Слотов АКБ", default="2",
                            limit=10)
        for check in (title, note, slots):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/models")
        specs = model_specs(request, data)
        if specs is None:
            return redirect("/models")
        try:
            await crm.create_bike_model(
                title=title.value, brand=(data.get("brand") or "").strip() or None,
                factory_title=(data.get("factory_title") or "").strip() or None,
                battery_slots=slots.value, note=note.value, **specs)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Такая модель уже есть.", "err")
                return redirect("/models")
            raise
        flash(request, "Модель добавлена.")
        return redirect("/models")

    @app.post("/models/bikes/{model_id}")
    async def bike_model_edit(request: Request, model_id: int) -> Response:
        """Характеристики правятся после заведения: в первый раз их обычно
        переписывают с коробки, а коробка не всегда под рукой."""
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        if await crm.bike_model(model_id) is None:
            return render(request, "missing.html", status_code=404, what="Модель")
        data = await form(request)
        if (data.get("action") or "") == "toggle":
            model = await crm.bike_model(model_id)
            await crm.update_bike_model(model_id, active=not model["active"])
            flash(request, "Модель убрана в архив." if model["active"]
                  else "Модель вернулась в каталог.")
            return redirect("/models")
        note = logic.check_note(data.get("note"))
        if not note.ok:
            flash(request, note.error, "err")
            return redirect("/models")
        specs = model_specs(request, data)
        if specs is None:
            return redirect("/models")
        await crm.update_bike_model(model_id, note=note.value, **specs)
        flash(request, "Модель сохранена.")
        return redirect("/models")

    @app.post("/models/batteries/{model_id}")
    async def battery_model_edit(request: Request, model_id: int) -> Response:
        """Правка модели АКБ: цена и срок службы меняются, и амортизация
        батарей этой модели пересчитывается с ними - каталог и есть
        источник этих чисел."""
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        if await crm.battery_model(model_id) is None:
            return render(request, "missing.html", status_code=404, what="Модель АКБ")
        data = await form(request)
        if data.get("action") == "toggle":
            current = await crm.battery_model(model_id)
            await crm.update_battery_model(model_id, active=not current["active"])
            flash(request, "Модель убрана в архив." if current["active"]
                  else "Модель возвращена из архива.")
            return redirect("/models")
        title = logic.check_name(data.get("title"), what="Название модели")
        price = cost_field(data, "price")
        months = count_field(data, "service_months", what="Срок службы",
                             default="15", limit=240)
        volt = count_field(data, "voltage", what="Напряжение", default="0", limit=200)
        # Ресурс в циклах: пусто или ноль - «своего нет», действует общий
        # из плана замены.
        cycles = count_field(data, "max_cycles", what="Ресурс, циклов", default="0",
                             limit=logic.BATTERY_CYCLES_MAX)
        # Ёмкость - та же проверка, что у таблички батареи: сумма до
        # десяти миллионов не влезала в numeric(6,2) и роняла форму 500.
        capacity = logic.check_amp_hours(data.get("capacity"))
        for check in (title, price, months, volt, cycles, capacity):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/models")
        try:
            await crm.update_battery_model(
                model_id, title=title.value,
                brand=(data.get("brand") or "").strip() or None,
                voltage=volt.value or None,
                capacity=capacity.value,
                price=price.value, service_months=months.value or 15,
                max_cycles=cycles.value or None)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Модель АКБ с таким названием уже есть.", "err")
                return redirect("/models")
            raise
        flash(request, "Модель АКБ сохранена.")
        return redirect("/models")

    @app.post("/models/batteries")
    async def battery_model_create(request: Request) -> Response:
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        data = await form(request)
        title = logic.check_name(data.get("title"), what="Название модели")
        note = logic.check_note(data.get("note"))
        price = cost_field(data, "price")
        months = count_field(data, "service_months", what="Срок службы",
                             default="15", limit=240)
        volt = count_field(data, "voltage", what="Напряжение", default="0", limit=200)
        cycles = count_field(data, "max_cycles", what="Ресурс, циклов", default="0",
                             limit=logic.BATTERY_CYCLES_MAX)
        capacity = logic.check_amp_hours(data.get("capacity"))
        for check in (title, note, price, months, volt, cycles, capacity):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/models")
        try:
            await crm.create_battery_model(
                title=title.value, brand=(data.get("brand") or "").strip() or None,
                voltage=volt.value or None,
                capacity=capacity.value,
                price=price.value, service_months=months.value or 15, note=note.value,
                max_cycles=cycles.value or None)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Такая модель АКБ уже есть.", "err")
                return redirect("/models")
            raise
        flash(request, "Модель АКБ добавлена.")
        return redirect("/models")

    @app.post("/models/compat")
    async def compat_set(request: Request) -> Response:
        """Клетка матрицы совместимости: подходит, основная или пусто."""
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        data = await form(request)
        bike_model_id = logic.parse_id(data.get("bike_model_id"))
        battery_model_id = logic.parse_id(data.get("battery_model_id"))
        if (bike_model_id is None or battery_model_id is None
                or await crm.bike_model(bike_model_id) is None
                or await crm.battery_model(battery_model_id) is None):
            flash(request, "Выберите модели из каталога.", "err")
            return redirect("/models")
        mode = data.get("mode") or "none"
        await crm.set_compat(bike_model_id, battery_model_id,
                             fits=mode in ("fits", "primary"),
                             primary_fit=mode == "primary")
        return redirect("/models")

    # ───────────────────────────── батареи ─────────────────────────────

    @app.get("/batteries")
    async def batteries_page(request: Request) -> Response:
        status = request.query_params.get("status") or ""
        q = request.query_params.get("q") or ""
        location = request.query_params.get("location") or ""
        view = request.query_params.get("view") or ""
        cycles = logic.battery_max_cycles(await crm.settings())
        rows = logic.battery_rows(await crm.batteries(
            status=status or None, q=q or None, location=location or None,
            in_search=view == "search"), since=await crm.battery_status_since(),
            max_cycles=cycles)
        return render(request, "batteries.html", rows=rows,
                      summary=logic.battery_summary(
                          logic.battery_rows(await crm.batteries(), max_cycles=cycles)),
                      status=status, q=q, location=location, view=view,
                      max_cycles=cycles,
                      locations=await filter_points(location),
                      models=await crm.battery_models(active_only=True))

    @app.get("/batteries/new")
    async def battery_new(request: Request) -> Response:
        if not may_edit(request, "batteries"):
            return denied(request, "batteries")
        # Из плана замены приходят с моделью: форма сразу с ней и её сроком
        # службы, иначе новая батарея получила бы срок по умолчанию.
        models = await crm.battery_models(active_only=True)
        wanted = logic.parse_id(request.query_params.get("model_id"))
        return render(request, "battery_form.html", battery=None, models=models,
                      model=next((m for m in models if m["id"] == wanted), None),
                      locations=await location_names())

    # ─────────────────── план замены аккумуляторов ───────────────────
    #
    # Маршруты - раньше карточки «/batteries/{battery_id}»: иначе «plan»
    # ушёл бы туда номером батареи и ответил 422.

    async def battery_plan_data() -> dict[str, Any]:
        cycles = logic.battery_max_cycles(await crm.settings())
        return logic.battery_wear_plan(await crm.batteries(limit=10000),
                                       today=date.today(), max_cycles=cycles)

    @app.get("/batteries/plan")
    async def battery_plan_page(request: Request) -> Response:
        """Что менять в ближайшие 1/3/6 месяцев и сколько на это отложить."""
        plan = await battery_plan_data()
        return render(request, "battery_plan.html", plan=plan,
                      summary=logic.battery_summary(logic.battery_rows(
                          await crm.batteries(), max_cycles=plan["max_cycles"])))

    @app.get("/batteries/plan.{ext}")
    async def battery_plan_table(request: Request, ext: str) -> Response:
        plan = await battery_plan_data()
        money_ok = may_view(request, "finance")
        header = ["Номер", "Модель", "Статус", "Точка", "Куплена", "Срок, мес.",
                  "Возраст, мес.", "Циклов", "Ресурс", "Заменить до", "Причина"]
        if money_ok:
            header.append("Цена замены")
        out = []
        for r in plan["rows"]:
            line = [r["code"], r.get("model_title"),
                    logic.BATTERY_STATUSES.get(r["status"], r["status"]),
                    r.get("location"), r.get("purchased_on"), r["service_months"],
                    r["age_months"], r["cycles"], r["cycle_limit"], r["replace_on"],
                    logic.BATTERY_WEAR_REASONS.get(r["reason"] or "", "")]
            if money_ok:
                line.append(r["price"])
            out.append(line)
        return await table(ext, "battery-plan", header, out)

    @app.post("/batteries/plan")
    async def battery_plan_settings(request: Request) -> Response:
        """Общий ресурс АКБ в циклах - для моделей без своего."""
        if not may_edit(request, "batteries"):
            return denied(request, "batteries")
        data = await form(request)
        got = count_field(data, "battery_max_cycles", what="Ресурс, циклов",
                          default=str(logic.BATTERY_CYCLES_WARN),
                          limit=logic.BATTERY_CYCLES_MAX, least=1)
        if not got.ok:
            flash(request, got.error, "err")
            return redirect("/batteries/plan")
        await crm.set_setting("battery_max_cycles", str(got.value), by=who(request))
        flash(request, f"Общий ресурс: {got.value} циклов.")
        return redirect("/batteries/plan")

    async def battery_fields(request: Request, data: dict,
                             current: str | None = None) -> dict | None:
        """Поля батареи из формы. current - её точка сейчас: закрытая точка
        остаётся в карточке, а не отбивается проверкой при сохранении."""
        code = logic.check_code(data.get("code"))
        note = logic.check_note(data.get("note"))
        price = (logic.check_amount(data.get("purchase_price"))
                 if (data.get("purchase_price") or "").strip() else logic.Check(True, None))
        bought = logic.check_purchase_date(data.get("purchased_on"), today=date.today())
        location = logic.check_location(data.get("location"),
                                        await location_names(current))
        months = data.get("service_months") or "15"
        cycles = count_field(data, "cycles", what="Циклы", default="0", limit=99999)
        volts = logic.check_volts(data.get("volts"))
        amps = logic.check_amp_hours(data.get("amp_hours"))
        for check in (code, note, price, bought, location, cycles, volts, amps):
            if not check.ok:
                flash(request, check.error, "err")
                return None
        service_months = logic.parse_id(months)
        if service_months is None or not 1 <= service_months <= 240:
            flash(request, "Срок службы: число месяцев от 1 до 240.", "err")
            return None
        model_id = logic.parse_id(data.get("model_id"))
        # Модель - строка каталога: чужой номер упирался в ссылку базы.
        if model_id is not None and await crm.battery_model(model_id) is None:
            flash(request, "Модель АКБ: такой нет в каталоге — обновите страницу.", "err")
            return None
        return {"code": code.value, "model_id": model_id,
                "serial_no": (data.get("serial_no") or "").strip() or None,
                "location": location.value, "purchase_price": price.value,
                "purchased_on": bought.value, "service_months": service_months,
                "cycles": cycles.value, "note": note.value,
                "volts": volts.value, "amp_hours": amps.value}

    @app.post("/batteries")
    async def battery_create(request: Request) -> Response:
        if not may_edit(request, "batteries"):
            return denied(request, "batteries")
        fields = await battery_fields(request, await form(request))
        if fields is None:
            return redirect("/batteries/new")
        # Новая батарея заводится «на сборке», если владелец требует
        # сверку: недособранную выдавать нечего. Требование снято -
        # заводится сразу свободной, как было раньше.
        checks = logic.bike_check_settings(await crm.settings())
        try:
            battery_id = await crm.create_battery(
                by=who(request), status="new" if checks["required"] else "available",
                **fields)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Батарея с таким номером уже есть.", "err")
                return redirect("/batteries/new")
            raise
        flash(request, "Батарея заведена.")
        return redirect(f"/batteries/{battery_id}")

    @app.get("/batteries/{battery_id}")
    async def battery_card(request: Request, battery_id: int) -> Response:
        battery = await crm.battery(battery_id)
        if battery is None:
            return render(request, "missing.html", status_code=404, what="Батарея")
        cycles = logic.battery_max_cycles(await crm.settings())
        row = logic.battery_rows([battery], since=await crm.battery_status_since(),
                                 max_cycles=cycles)[0]
        return render(request, "battery.html", battery=row,
                      wear=logic.battery_wear(battery, today=date.today(),
                                              max_cycles=cycles),
                      log=await crm.battery_status_log(battery_id),
                      models=await crm.battery_models(active_only=True),
                      locations=await location_names(battery.get("location")),
                      checks=logic.battery_check_state(battery,
                                                       await crm.settings()),
                      amortization=logic.battery_amortization(battery))

    @app.post("/batteries/{battery_id}/check")
    async def battery_check(request: Request, battery_id: int) -> Response:
        """Сверка поля паспорта батареи и ввод её в эксплуатацию."""
        if not may_edit(request, "batteries"):
            return denied(request, "batteries")
        battery = await crm.battery(battery_id)
        if battery is None:
            return render(request, "missing.html", status_code=404, what="Батарея")
        data = await request.form()
        action = str(data.get("action") or "")
        back = f"/batteries/{battery_id}"
        try:
            if action == "commission":
                await service.commission_battery(crm, battery, by=who(request))
                flash(request, f"Аккумулятор № {battery['code']} в обороте.")
            elif action == "clear":
                field = str(data.get("field") or "")
                if field not in logic.BATTERY_PASSPORT:
                    flash(request, "Неизвестное поле паспорта.", "err")
                    return redirect(back)
                await crm.clear_battery_check(battery_id, field)
                flash(request, f"{logic.BATTERY_PASSPORT[field]}: сверка снята.")
            else:
                field = str(data.get("field") or "")
                photo = await save_check_photo(request, "akb", battery, field,
                                               data.get("photo"),
                                               passport=logic.BATTERY_PASSPORT)
                await service.check_battery_field(crm, battery, field,
                                                  by=who(request), photo=photo)
                flash(request, f"{logic.BATTERY_PASSPORT.get(field, field)}: сверено.")
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
        return redirect(back)

    @app.get("/batteries/{battery_id}/photo/{field}")
    async def battery_photo(request: Request, battery_id: int, field: str) -> Response:
        if not may_view(request, "batteries"):
            return denied(request, "batteries")
        battery = await crm.battery(battery_id)
        marks = (battery or {}).get("checked") or {}
        mark = marks.get(field) if isinstance(marks, dict) else None
        name = (mark or {}).get("photo") if isinstance(mark, dict) else None
        path = Path(cfg.bike_photo_dir) / str(name or "")
        if not name or not path.is_file():
            return render(request, "missing.html", status_code=404, what="Снимок")
        return FileResponse(path)

    @app.post("/batteries/{battery_id}/edit")
    async def battery_edit(request: Request, battery_id: int) -> Response:
        if not may_edit(request, "batteries"):
            return denied(request, "batteries")
        battery = await crm.battery(battery_id)
        if battery is None:
            return render(request, "missing.html", status_code=404, what="Батарея")
        fields = await battery_fields(request, await form(request),
                                      battery.get("location"))
        if fields is None:
            return redirect(f"/batteries/{battery_id}")
        try:
            await crm.update_battery(battery_id, by=who(request), **fields)
        except Exception as exc:                        # noqa: BLE001
            if not name_taken(exc):
                raise
            flash(request, "Батарея с таким номером уже есть.", "err")
            return redirect(f"/batteries/{battery_id}")
        flash(request, "Батарея сохранена.")
        return redirect(f"/batteries/{battery_id}")

    @app.post("/batteries/{battery_id}/status")
    async def battery_status(request: Request, battery_id: int) -> Response:
        if not may_edit(request, "batteries"):
            return denied(request, "batteries")
        battery = await crm.battery(battery_id)
        if battery is None:
            return render(request, "missing.html", status_code=404, what="Батарея")
        data = await form(request)
        status = logic.check_choice(data.get("status"), logic.BATTERY_MANUAL_STATUSES,
                                    what="Статус батареи")
        if not status.ok:
            flash(request, status.error, "err")
            return redirect(f"/batteries/{battery_id}")
        if battery["status"] == "rented":
            flash(request, "Батарея у клиента: её снимает возврат или замена, "
                           "а не смена статуса.", "err")
            return redirect(f"/batteries/{battery_id}")
        if battery["status"] == "new":
            flash(request, "Батарея на сборке: из этого состояния её выводит "
                           "только кнопка «Ввести в эксплуатацию».", "err")
            return redirect(f"/batteries/{battery_id}")
        await crm.update_battery(battery_id, status=status.value, by=who(request))
        flash(request, f"Статус: {logic.BATTERY_STATUSES[status.value]}.")
        return redirect(f"/batteries/{battery_id}")

    @app.post("/rentals/{rental_id}/battery")
    async def rental_battery_swap(request: Request, rental_id: int) -> Response:
        """Замена батареи у клиента: аренду это не трогает."""
        if not may_edit(request, "rentals"):
            return denied(request, "rentals")
        rental = await crm.rental(rental_id)
        if rental is None:
            return render(request, "missing.html", status_code=404, what="Аренда")
        data = await form(request)
        new = await by_id(crm.battery, data.get("battery_id"))
        old = await by_id(crm.battery, data.get("old_id"))
        if new is None:
            flash(request, "Выберите батарею на замену.", "err")
            return redirect(f"/rentals/{rental_id}")
        try:
            await service.swap_battery(crm, rental, old, new, by=who(request),
                                       old_status=data.get("old_status") or "repair")
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/rentals/{rental_id}")
        flash(request, f"Батарея заменена на {new['code']}." if old is not None
              else f"Батарея {new['code']} выдана клиенту.")
        return redirect(f"/rentals/{rental_id}")

    # ───────────── подписание документов (ПЭП) ─────────────

    def sign_link(request: Request, token: str) -> str:
        """Ссылка для клиента - абсолютная: её отправляют в мессенджер."""
        return str(request.base_url).rstrip("/") + f"/sign/{token}"

    def sign_link_for(request: Request, row: dict) -> str:
        """Ссылка - только с правом `client_docs`: по ней открывается тот же
        договор с паспортными данными, что /signings/{id}/doc, и в чужом
        окне без входа право уже никто не спросит."""
        if not logic.can_act(request.state.staff, "client_docs"):
            return ""
        return sign_link(request, row["token"])

    async def sign_company() -> dict:
        settings = await crm.settings()
        return {code: settings.get(code, "") for code in company.COMPANY_FIELDS}

    @app.get("/signings")
    async def signings_page(request: Request) -> Response:
        if not may_view(request, "clients"):
            return denied(request, "clients")
        rows = logic.sign_rows(await crm.sign_requests(limit=200))
        return render(request, "signings.html", rows=rows,
                      summary=logic.sign_summary(rows))

    @app.post("/clients/{client_id}/sign")
    async def sign_start(request: Request, client_id: int) -> Response:
        """Собрать пакет документов и ссылку на подписание."""
        if not may_edit(request, "clients"):
            return denied(request, "clients")
        client = await crm.client(client_id)
        if client is None:
            return render(request, "missing.html", status_code=404, what="Клиент")
        bot_user = await db.get_user(client["tg_id"]) if client.get("tg_id") else None
        try:
            created = await service.start_signing(
                crm, client=client, rental=await crm.active_rental_of(client_id),
                company=await sign_company(),
                bot_user=dict(bot_user) if bot_user else None, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/clients/{client_id}")
        flash(request, f"Заявка на подпись {created['no']} готова. "
                       "Отправьте клиенту ссылку и продиктуйте код, когда он "
                       "его запросит.")
        return redirect(f"/signings/{created['id']}")

    @app.get("/signings/{request_id}")
    async def sign_card(request: Request, request_id: int) -> Response:
        if not may_view(request, "clients"):
            return denied(request, "clients")
        row = await crm.sign_request(request_id)
        if row is None:
            return render(request, "missing.html", status_code=404, what="Заявка")
        return render(request, "signing.html", req=row,
                      state=logic.sign_state(row),
                      link=sign_link_for(request, row),
                      digest=logic.sign_docs_digest(row.get("docs") or []),
                      events=await crm.sign_events(request_id))

    @app.get("/signings/{request_id}/doc/{index}")
    async def sign_card_doc(request: Request, request_id: int, index: int) -> Response:
        """Файл пакета для оператора - тот же, что видит клиент по ссылке.

        Путь берётся из списка документов заявки, а не из запроса: по
        индексу нельзя дотянуться до чужого файла.
        """
        if not may_view(request, "clients"):
            return denied(request, "clients")
        # В пакете лежит договор с паспортными данными - ровно тот файл,
        # который /clients/{id}/contract отдаёт только по праву
        # `client_docs`. Без этой проверки право не защищало ничего:
        # тот же документ открывался со страницы заявки на подпись.
        if not logic.can_act(request.state.staff, "client_docs"):
            return denied(request, "client_docs")
        row = await crm.sign_request(request_id)
        if row is None:
            return render(request, "missing.html", status_code=404, what="Заявка")
        docs = list(row.get("docs") or [])
        if not 0 <= index < len(docs) or not docs[index].get("path"):
            return render(request, "missing.html", status_code=404, what="Документ")
        path = Path(str(docs[index]["path"]))
        if not path.is_file():
            return render(request, "missing.html", status_code=404, what="Файл документа")
        return FileResponse(path, filename=f"{docs[index]['title']}{path.suffix}")

    @app.post("/signings/{request_id}/code")
    async def sign_code_send(request: Request, request_id: int) -> Response:
        """Код по просьбе оператора: клиент без Telegram узнаёт его
        голосом, по телефону."""
        if not may_edit(request, "clients"):
            return denied(request, "clients")
        row = await crm.sign_request(request_id)
        if row is None:
            return render(request, "missing.html", status_code=404, what="Заявка")
        try:
            code = await service.issue_sign_code(crm, row, ip=client_ip(request),
                                                 by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/signings/{request_id}")
        await notify.sign_code(bot, row, code)
        request.session["sign_code"] = {"id": request_id, "code": code}
        flash(request, f"Код {code} действует {logic.SIGN_CODE_MINUTES} минут."
                       + (" Он же ушёл клиенту в Telegram." if row.get("tg_id")
                          else " Клиент не в боте — продиктуйте код."))
        return redirect(f"/signings/{request_id}")

    @app.post("/signings/{request_id}/cancel")
    async def sign_cancel(request: Request, request_id: int) -> Response:
        if not may_edit(request, "clients"):
            return denied(request, "clients")
        row = await crm.sign_request(request_id)
        if row is None:
            return render(request, "missing.html", status_code=404, what="Заявка")
        if row["status"] == "signed":
            flash(request, "Подписанное не отменяется: заявка - это протокол.",
                  "err")
            return redirect(f"/signings/{request_id}")
        await crm.cancel_sign_request(request_id, by=who(request))
        flash(request, "Заявка отменена, ссылка больше не работает.")
        return redirect(f"/signings/{request_id}")

    # ─── страница клиента: без входа в панель, по токену из ссылки ───

    async def sign_by_token(token: str) -> dict | None:
        return await crm.sign_request_by_token(token)

    def agent_of(request: Request) -> str:
        return (request.headers.get("user-agent") or "")[:300]

    @app.get("/sign/{token}")
    async def sign_page(request: Request, token: str) -> Response:
        row = await sign_by_token(token)
        if row is None:
            return render(request, "sign_missing.html", status_code=404)
        state = logic.sign_state(row)
        if state["open"]:
            await crm.log_sign_event(row["id"], kind="opened",
                                     ip=client_ip(request), agent=agent_of(request))
        return render(request, "sign.html", req=row, state=state,
                      digest=logic.sign_docs_digest(row.get("docs") or []),
                      company=await sign_company())

    def sign_link_alive(row: dict) -> bool:
        """Живая ли ссылка. Подписанная заявка остаётся открытой: пакет
        документов принадлежит клиенту, и забрать свой экземпляр он
        вправе. Отменённая и просроченная - нет: оператор отменил её
        именно для того, чтобы ссылка перестала работать."""
        return bool(logic.sign_state(row)["open"] or row.get("status") == "signed")

    @app.get("/sign/{token}/agreement")
    async def sign_agreement(request: Request, token: str) -> Response:
        """Соглашение об ЭП - ровно тот текст, который подписывают."""
        row = await sign_by_token(token)
        if row is None or not sign_link_alive(row):
            return render(request, "sign_missing.html", status_code=404)
        return render(request, "sign_agreement.html", req=row,
                      text=row.get("agreement") or "")

    @app.get("/sign/{token}/doc/{index}")
    async def sign_doc(request: Request, token: str, index: int) -> Response:
        """Файл из пакета. Отдаём только то, что лежит в самой заявке:
        путь приходит не из запроса, а из её списка документов."""
        # Клиент в панель не входит, и файл ему отдаётся по токену. Вошедший
        # сотрудник без `client_docs` - нет: иначе ссылка из заявки
        # открывала бы ему договор мимо права.
        staff = request.state.staff
        if staff is not None and not logic.can_act(staff, "client_docs"):
            return denied(request, "client_docs")
        row = await sign_by_token(token)
        if row is None or not sign_link_alive(row):
            return render(request, "sign_missing.html", status_code=404)
        docs = list(row.get("docs") or [])
        if not 0 <= index < len(docs) or not docs[index].get("path"):
            return render(request, "sign_missing.html", status_code=404)
        path = Path(str(docs[index]["path"]))
        if not path.is_file():
            return render(request, "sign_missing.html", status_code=404)
        return FileResponse(path, filename=f"{docs[index]['title']}{path.suffix}")

    @app.post("/sign/{token}/code")
    async def sign_ask_code(request: Request, token: str) -> Response:
        row = await sign_by_token(token)
        if row is None:
            return render(request, "sign_missing.html", status_code=404)
        try:
            code = await service.issue_sign_code(crm, row, ip=client_ip(request),
                                                 agent=agent_of(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/sign/{token}")
        sent = await notify.sign_code(bot, row, code)
        flash(request, "Код отправлен в Telegram." if sent
              else "Код готов — позвоните оператору, он его продиктует.")
        return redirect(f"/sign/{token}")

    @app.post("/sign/{token}")
    async def sign_submit(request: Request, token: str) -> Response:
        row = await sign_by_token(token)
        if row is None:
            return render(request, "sign_missing.html", status_code=404)
        data = await form(request)
        try:
            await service.verify_sign(crm, row, data.get("code"),
                                      ip=client_ip(request),
                                      agent=agent_of(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/sign/{token}")
        flash(request, "Документы подписаны. Экземпляры остаются доступны "
                       "по этой ссылке.")
        return redirect(f"/sign/{token}")

    # ─────────────────────── рассылки ───────────────────────

    async def audience_people(code: str) -> list[dict]:
        return logic.pick_audience(code, await crm.clients_for_mailing(),
                                   await crm.active_rentals(),
                                   before_days=cfg.remind_before_days)

    @app.get("/mailing")
    async def mailing_page(request: Request) -> Response:
        if not may_view(request, "mailing"):
            return denied(request, "mailing")
        sizes = {code: len(await audience_people(code)) for code in logic.AUDIENCES}
        return render(request, "mailing.html",
                      rows=logic.campaign_rows(await crm.campaigns(limit=100)),
                      templates=await crm.templates(),
                      sizes=sizes)

    @app.post("/mailing/templates")
    async def template_save(request: Request) -> Response:
        """Новый шаблон или правка существующего."""
        if not may_edit(request, "mailing"):
            return denied(request, "mailing")
        data = await form(request)
        title = logic.check_name(data.get("title"), what="Название шаблона")
        body = logic.check_template_body(data.get("body"), what="Текст")
        raw_max = (data.get("body_max") or "").strip()
        body_max = (logic.check_template_body(raw_max, what="Текст для MAX")
                    if raw_max else logic.Check(True, None))
        note = logic.check_note(data.get("note"))
        for check in (title, body, body_max, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/mailing")
        template_id = logic.parse_id(data.get("id"))
        if template_id:
            await crm.update_template(template_id, title=title.value,
                                      body=body.value, body_max=body_max.value,
                                      note=note.value)
            flash(request, "Шаблон сохранён.")
            return redirect("/mailing")
        code = logic.check_slug(data.get("code"), what="Код шаблона")
        if not code.ok:
            flash(request, code.error, "err")
            return redirect("/mailing")
        try:
            await crm.create_template(code=code.value, title=title.value,
                                      body=body.value, body_max=body_max.value,
                                      note=note.value)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Шаблон с таким кодом уже есть.", "err")
                return redirect("/mailing")
            raise
        flash(request, "Шаблон добавлен.")
        return redirect("/mailing")

    @app.post("/mailing")
    async def campaign_create(request: Request) -> Response:
        if not may_edit(request, "mailing"):
            return denied(request, "mailing")
        data = await form(request)
        title = logic.check_name(data.get("title"), what="Название рассылки")
        audience = logic.check_audience(data.get("audience"))
        note = logic.check_note(data.get("note"))
        for check in (title, audience, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/mailing")
        template = await by_id(crm.template, data.get("template_id"))
        if template is None:
            flash(request, "Выберите шаблон.", "err")
            return redirect("/mailing")
        try:
            created = await service.create_campaign(
                crm, title=title.value, template=template,
                audience=audience.value, note=note.value, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/mailing")
        flash(request, f"Черновик собран: {created['queued']} получателей. "
                       "Посмотрите список и запустите отправку.")
        return redirect(f"/mailing/{created['id']}")

    @app.get("/mailing/{campaign_id}")
    async def campaign_card(request: Request, campaign_id: int) -> Response:
        if not may_view(request, "mailing"):
            return denied(request, "mailing")
        campaign = await crm.campaign(campaign_id)
        if campaign is None:
            return render(request, "missing.html", status_code=404, what="Рассылка")
        sends = await crm.campaign_sends(campaign_id, limit=1000)
        preview = ""
        if sends:
            client = await crm.client(sends[0]["client_id"])
            if client is not None:
                values = logic.template_context(
                    client, await crm.active_rental_of(client["id"]),
                    await crm.client_balance(client["id"]),
                    pay_url=cfg.pay_url)
                preview = logic.render_template(campaign.get("body") or "", values)
        return render(request, "campaign.html", campaign=campaign, sends=sends,
                      progress=logic.campaign_progress(sends), preview=preview)

    @app.post("/mailing/{campaign_id}/start")
    async def campaign_start(request: Request, campaign_id: int) -> Response:
        if not may_edit(request, "mailing"):
            return denied(request, "mailing")
        campaign = await crm.campaign(campaign_id)
        if campaign is None:
            return render(request, "missing.html", status_code=404, what="Рассылка")
        try:
            await service.start_campaign(crm, campaign)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/mailing/{campaign_id}")
        flash(request, "Отправка началась. Сообщения уходят из процесса бота, "
                       "по несколько в секунду.")
        return redirect(f"/mailing/{campaign_id}")

    @app.post("/mailing/{campaign_id}/cancel")
    async def campaign_cancel(request: Request, campaign_id: int) -> Response:
        if not may_edit(request, "mailing"):
            return denied(request, "mailing")
        campaign = await crm.campaign(campaign_id)
        if campaign is None:
            return render(request, "missing.html", status_code=404, what="Рассылка")
        try:
            left = await service.cancel_campaign(crm, campaign)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/mailing/{campaign_id}")
        flash(request, f"Рассылка остановлена, снято из очереди: {left}. "
                       "Отправленное не отзывается — ни Telegram, ни MAX этого "
                       "не умеют.")
        return redirect(f"/mailing/{campaign_id}")

    # ─────────────────────── акции ───────────────────────
    #
    # Акция - правило начисления баллов, шаблон в коде, параметры в строке.
    # Раздел свой, а не вкладка приглашений: акции живут рядом с
    # рассылками, а не с отчётами, и правит их тот, кто ведёт клиентов.

    async def promo_choices(promo: dict | None = None) -> dict[str, list[str]]:
        """Модели и точки для ограничения акции - и для списка в форме, и для
        проверки. Модели - каталог и то, что стоит в парке, по-клиентски
        (как у цены); точки - справочник. Текущее значение акции остаётся
        в списке, даже если модель убрали из каталога: иначе первая же
        правка акции стёрла бы её ограничение."""
        scope = logic.promo_scope(promo or {})
        aliases = logic.model_aliases(catalogue := await crm.bike_models())
        models = {str(m["title"]) for m in catalogue if m.get("active") and m.get("title")}
        models |= {logic.catalogue_model(b.get("model"), aliases)
                   for b in await crm.bikes(limit=10000) if b.get("model")}
        if scope["model"]:
            models.add(scope["model"])
        return {"models": sorted(m for m in models if m),
                "places": await location_names(scope["location"])}

    @app.get("/promos")
    async def promos_page(request: Request) -> Response:
        rows = await crm.promos()
        return render(request, "promos.html", rows=rows,
                      totals=logic.promo_totals(rows), today=date.today(),
                      idle_settings=logic.idle_promo_settings(await crm.settings()),
                      recent=await crm.bonuses(kind="promo", limit=20))

    @app.get("/promos/new")
    async def promo_new(request: Request) -> Response:
        if not may_edit(request, "promos"):
            return denied(request, "promos")
        kind = (request.query_params.get("kind") or "").strip()
        if kind not in logic.PROMO_KINDS:
            flash(request, "Выберите шаблон акции.", "err")
            return redirect("/promos")
        choices = await promo_choices()
        promo = logic.promo_form_defaults(kind)
        if kind == logic.IDLE_PROMO_KIND:
            # Из подсказки о простое: модель, точка и скидка уже стоят, но
            # заводит акцию человек - форма только заполнена.
            promo = logic.idle_promo_form(
                request.query_params, today=date.today(),
                percent=logic.idle_promo_settings(await crm.settings())["percent"],
                **choices)
        return render(request, "promo_form.html", promo=promo, **choices,
                      kind=kind, spec=logic.PROMO_KINDS[kind], is_new=True)

    # До /promos/{promo_id}: иначе «settings» ушло бы туда номером акции.
    @app.post("/promos/settings")
    async def promo_settings_save(request: Request) -> Response:
        """Порог простоя и скидка заготовки для подсказки «простаивает»."""
        if not may_edit(request, "promos"):
            return denied(request, "promos")
        data = await form(request)
        days = count_field(data, "idle_promo_days", what="Простаивает дольше",
                           default=str(logic.IDLE_PROMO_DAYS), limit=365, least=1)
        percent = count_field(data, "idle_promo_percent", what="Скидка",
                              default=str(logic.IDLE_PROMO_PERCENT), limit=100, least=1)
        for field in (days, percent):
            if not field.ok:
                flash(request, field.error, "err")
                return redirect("/promos")
        await crm.set_setting("idle_promo_days", str(days.value), by=who(request))
        await crm.set_setting("idle_promo_percent", str(percent.value), by=who(request))
        flash(request, f"Подсказка о простое: от {days.value} дн., скидка "
                       f"{percent.value} %.")
        return redirect("/promos")

    @app.post("/promos")
    async def promo_create(request: Request) -> Response:
        if not may_edit(request, "promos"):
            return denied(request, "promos")
        data = await form(request)
        choices = await promo_choices()

        def again(error: str) -> Response:
            # Форма с введённым, а не редирект на заготовку: чистая форма
            # теряла модель, точку, срок и «один раз на клиента», и после
            # правки одного поля заводилась скидка на всю сеть без срока.
            flash(request, error, "err")
            kind = str(data.get("kind") or "").strip()
            if kind not in logic.PROMO_KINDS:
                return redirect("/promos")
            return render(request, "promo_form.html", status_code=400,
                          promo=logic.promo_form_echo(data, kind), **choices,
                          kind=kind, spec=logic.PROMO_KINDS[kind], is_new=True)

        got = logic.check_promo_form(data, **choices)
        if not got.ok:
            return again(got.error)
        try:
            promo_id = await crm.create_promo(**got.value, by=who(request))
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                return again(f"Промокод {got.value['code']} уже действует у другой "
                             "акции: выключите её или выберите другое слово.")
            raise
        flash(request, f"Акция «{got.value['title']}» заведена и действует.")
        return redirect(f"/promos/{promo_id}")

    @app.get("/promos/{promo_id}")
    async def promo_card(request: Request, promo_id: int) -> Response:
        promo = await crm.promo(promo_id)
        if promo is None:
            return render(request, "missing.html", status_code=404, what="Акция")
        kind = promo["kind"]
        return render(request, "promo_form.html", promo=promo, kind=kind,
                      **(await promo_choices(promo)),
                      spec=logic.PROMO_KINDS.get(kind, {}), is_new=False,
                      grants=await crm.bonuses(promo_id=promo_id, limit=50),
                      alive=logic.promo_alive(promo, today=date.today()),
                      mailing_body=logic.promo_mailing_body(promo))

    @app.post("/promos/{promo_id}")
    async def promo_edit(request: Request, promo_id: int) -> Response:
        if not may_edit(request, "promos"):
            return denied(request, "promos")
        promo = await crm.promo(promo_id)
        if promo is None:
            return render(request, "missing.html", status_code=404, what="Акция")
        data = await form(request)
        # Шаблон у заведённой акции не меняется: у каждого свои параметры,
        # и «сезонная», ставшая «промокодом», потеряла бы смысл журнала.
        got = logic.check_promo_form(data, kind=promo["kind"],
                                     **(await promo_choices(promo)))
        if not got.ok:
            flash(request, got.error, "err")
            return redirect(f"/promos/{promo_id}")
        fields = {k: v for k, v in got.value.items() if k != "kind"}
        try:
            await crm.update_promo(promo_id, **fields)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, f"Промокод {fields['code']} уже действует у "
                               "другой акции.", "err")
                return redirect(f"/promos/{promo_id}")
            raise
        flash(request, "Акция сохранена. Правка действует со следующего "
                       "начисления: начисленное задним числом не переписывается.")
        return redirect(f"/promos/{promo_id}")

    @app.post("/promos/{promo_id}/toggle")
    async def promo_toggle(request: Request, promo_id: int) -> Response:
        if not may_edit(request, "promos"):
            return denied(request, "promos")
        promo = await crm.promo(promo_id)
        if promo is None:
            return render(request, "missing.html", status_code=404, what="Акция")
        try:
            await crm.update_promo(promo_id, active=not promo["active"])
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, f"Промокод {promo.get('code')} уже действует у "
                               "другой акции: сначала выключите её.", "err")
                return redirect(f"/promos/{promo_id}")
            raise
        flash(request, "Акция выключена: новых скидок по ней не будет, "
                       "начисленное остаётся." if promo["active"]
              else "Акция включена.")
        return redirect(f"/promos/{promo_id}")

    @app.post("/promos/{promo_id}/mailing")
    async def promo_mailing(request: Request, promo_id: int) -> Response:
        """Текст акции - шаблоном рассылки: рассказать о ней клиентам.

        Код шаблона привязан к акции, поэтому вторая кнопка обновляет
        тот же шаблон, а не плодит копии.
        """
        if not may_edit(request, "promos") or not may_edit(request, "mailing"):
            return denied(request, "mailing")
        promo = await crm.promo(promo_id)
        if promo is None:
            return render(request, "missing.html", status_code=404, what="Акция")
        body = logic.check_template_body(logic.promo_mailing_body(promo))
        if not body.ok:
            flash(request, body.error, "err")
            return redirect(f"/promos/{promo_id}")
        code = f"promo_{promo_id}"
        existing = next((t for t in await crm.templates() if t["code"] == code), None)
        if existing is not None:
            # body_max тоже сбрасывается: иначе MAX получал бы прошлый текст.
            await crm.update_template(existing["id"], title=f"Акция: {promo['title']}",
                                      body=body.value, body_max=None, active=True)
        else:
            await crm.create_template(code=code, title=f"Акция: {promo['title']}",
                                      body=body.value, body_max=None,
                                      note=f"Из акции #{promo_id}")
        flash(request, f"Шаблон «Акция: {promo['title']}» готов — соберите "
                       "рассылку по нему.")
        return redirect("/mailing")

    # ─────────────────────── касса и банк ───────────────────────

    @app.get("/cash")
    async def cash_page(request: Request) -> Response:
        if not may_view(request, "cash"):
            return denied(request, "cash")
        rows = logic.shift_rows(await crm.cash_shifts(limit=100))
        # Точек две, и смены на них открыты одновременно: показывать одну
        # «текущую» значило спрятать от второй точки и её смену, и форму
        # открытия - наличные второй точки тогда падали в чужую смену.
        opened = []
        for shift in await crm.open_shifts():
            opened.append({"shift": shift, "state": logic.shift_state(
                shift, await crm.shift_payments(shift["id"]),
                await crm.cash_moves(shift["id"]),
                other=await crm.shift_payments(shift["id"], cash=False))})
        busy = {str(o["shift"].get("location") or "") for o in opened}
        names = await location_names()
        free = [loc for loc in names if loc not in busy]
        # Что должно лежать в ящике при открытии - «насчитали» прошлой
        # смены на той же точке: открывать с нуля, не глядя, нельзя.
        previous = sorted(await crm.last_closed_shifts(),
                          key=lambda x: str(x.get("location") or ""))
        return render(request, "cash.html", rows=rows, opened=opened,
                      previous=previous, locations=free,
                      can_open=bool(free) or (not names and "" not in busy))

    @app.post("/cash")
    async def cash_open(request: Request) -> Response:
        if not may_edit(request, "cash"):
            return denied(request, "cash")
        data = await form(request)
        opening = cost_field(data, "opening")
        note = logic.check_note(data.get("note"))
        location = logic.check_location(data.get("location"), await location_names())
        for check in (opening, note, location):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/cash")
        try:
            shift_id = await service.open_cash_shift(
                crm, location=location.value, opening=opening.value,
                note=note.value, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/cash")
        flash(request, "Смена открыта.")
        return redirect(f"/cash/{shift_id}")

    @app.get("/cash/{shift_id}")
    async def cash_shift_card(request: Request, shift_id: int) -> Response:
        if not may_view(request, "cash"):
            return denied(request, "cash")
        shift = await crm.cash_shift(shift_id)
        if shift is None:
            return render(request, "missing.html", status_code=404, what="Смена")
        payments = await crm.shift_payments(shift_id)
        other = await crm.shift_payments(shift_id, cash=False)
        moves = await crm.cash_moves(shift_id)
        return render(request, "cash_shift.html", shift=shift, payments=payments,
                      other=other, moves=moves,
                      state=logic.shift_state(shift, payments, moves, other=other))

    @app.post("/cash/{shift_id}/move")
    async def cash_move(request: Request, shift_id: int) -> Response:
        if not may_edit(request, "cash"):
            return denied(request, "cash")
        shift = await crm.cash_shift(shift_id)
        if shift is None:
            return render(request, "missing.html", status_code=404, what="Смена")
        data = await form(request)
        amount = logic.check_amount(data.get("amount"))
        kind = logic.check_cash_move(data.get("kind"))
        reason = logic.check_note(data.get("reason"))
        for check in (amount, kind, reason):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/cash/{shift_id}")
        try:
            await service.cash_move(crm, shift, kind=kind.value, amount=amount.value,
                                    reason=reason.value, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/cash/{shift_id}")
        flash(request, f"{logic.CASH_MOVE_KINDS[kind.value]}: "
                       f"{logic.money(amount.value)}.")
        return redirect(f"/cash/{shift_id}")

    @app.post("/cash/{shift_id}/close")
    async def cash_close(request: Request, shift_id: int) -> Response:
        if not may_edit(request, "cash"):
            return denied(request, "cash")
        shift = await crm.cash_shift(shift_id)
        if shift is None:
            return render(request, "missing.html", status_code=404, what="Смена")
        data = await form(request)
        counted = cost_field(data, "counted")
        note = logic.check_note(data.get("note"))
        for check in (counted, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect(f"/cash/{shift_id}")
        try:
            state = await service.close_cash_shift(
                crm, shift, counted=counted.value, note=note.value, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/cash/{shift_id}")
        if state["diff"]:
            sign = "излишек" if state["diff"] > 0 else "недостача"
            flash(request, f"Смена закрыта. Расхождение: {sign} "
                           f"{logic.money(abs(state['diff']))}.",
                  "err" if state["big_diff"] else "ok")
        else:
            flash(request, "Смена закрыта, касса сошлась.")
        return redirect(f"/cash/{shift_id}")

    @app.get("/bank")
    async def bank_page(request: Request) -> Response:
        if not may_view(request, "cash"):
            return denied(request, "cash")
        status = request.query_params.get("status") or "new"
        settings = await crm.settings()
        clients = await crm.clients(limit=10000)
        txns = await crm.bank_txns(status=None if status == "all" else status, limit=200)
        # Те же деньги, уже зачисленные заявкой или счётом, - в подсказке:
        # автозачисление такую строку не трогает, решает оператор.
        rows = logic.bank_rows(txns, clients, settings=settings,
                               credits=await banking.credited_around(crm, txns))
        if status == "new":
            # Списания в «не разобрано» не показываем: разбирать в них
            # нечего, они никому не зачисляются и висели бы вечно.
            rows = [r for r in rows if r["direction"] == "credit"]
        # В выпадающем списке - те, кто платит: должники и те, у кого идёт
        # аренда. Весь список клиентов в select не помещается и не нужен:
        # платёж от закрывшегося год назад - повод открыть его карточку,
        # а не искать в двух сотнях строк.
        picks = {c["id"]: c for c in await crm.debtors(200)}
        for rental in await crm.active_rentals():
            picks.setdefault(rental["client_id"],
                             {"id": rental["client_id"],
                              "full_name": rental.get("full_name"),
                              "phone": rental.get("phone")})
        return render(request, "bank.html", rows=rows, status=status,
                      summary=logic.bank_summary(
                          logic.bank_rows(await crm.bank_txns(limit=500))),
                      auto=logic.bank_settings(settings)["auto_credit"],
                      clients_for_pick=sorted(
                          picks.values(), key=lambda c: str(c.get("full_name") or "")),
                      last_at=await crm.last_bank_txn_at())

    @app.post("/bank/settings")
    async def bank_settings(request: Request) -> Response:
        # Деньги на баланс без человека - решение владельца, как и
        # автосписание с карты: одного права на кассу (оно есть у
        # администратора точки) мало, нужны «Финансы».
        if not may_edit(request, "cash"):
            return denied(request, "cash")
        if not may_edit(request, "finance"):
            return denied(request, "finance")
        data = await form(request)
        await crm.set_setting("bank_auto_credit", "1" if data.get("auto") else "0",
                              by=who(request))
        flash(request, "Автозачисление включено: строки с номером договора "
                       "в назначении будут зачисляться сами."
              if data.get("auto") else "Автозачисление выключено.")
        return redirect("/bank")

    # Раньше /bank/{txn_id}: иначе «settings» уедет в числовой параметр.
    @app.post("/bank/{txn_id}")
    async def bank_handle(request: Request, txn_id: int) -> Response:
        """Зачислить поступление клиенту или отметить «не наш»."""
        if not may_edit(request, "cash"):
            return denied(request, "cash")
        txn = await crm.bank_txn(txn_id)
        if txn is None:
            return render(request, "missing.html", status_code=404,
                          what="Строка выписки")
        data = await form(request)
        if (data.get("action") or "") == "ignore":
            try:
                await service.ignore_bank_txn(crm, txn, by=who(request))
            except service.ServiceError as exc:
                flash(request, str(exc), "err")
                return redirect("/bank")
            flash(request, "Отмечено: платёж не наш.")
            return redirect("/bank")
        client = await by_id(crm.client, data.get("client_id"))
        if client is None:
            flash(request, "Выберите клиента, которому зачислить.", "err")
            return redirect("/bank")
        try:
            await service.credit_bank_txn(crm, txn, client, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/bank")
        # Как после заявки: клиент, заплативший переводом, узнаёт, что
        # деньги дошли, и видит новую дату «оплачено до».
        await notices.send_client(
            crm, "pay_credited", client["id"],
            lambda: notify.payment_credited(bot, db, crm, client,
                                            logic.to_money(txn["amount"])))
        await referral_bonus(client, logic.to_money(txn["amount"]), who(request))
        flash(request, f"{logic.money(txn['amount'])} зачислено: "
                       f"{client['full_name']}.")
        return redirect("/bank")

    # ─────────────────── документы: свой шаблон ───────────────────

    # Наши шаблоны лежат в образе бота; панель их только отдаёт на
    # скачивание, чтобы было с чего начинать свой.
    OUR_TEMPLATES = {
        "contract": Path("app/contract_template.docx"),
        "act_in": Path("app/act_priema_template.docx"),
        "act_out": Path("app/act_vozvrata_template.docx"),
        "buyout": Path("app/act_vykup_template.docx"),
        "consent": Path("app/soglasie_template.docx"),
    }

    def our_template(kind: str) -> Path | None:
        # В демо наших шаблонов нет. Поставочные реквизитов уже не несут,
        # но образ демо собирается из каталога сервера, а там лежат docx
        # владельца с его ФИО, ИНН и счётом (update.sh их не трогает), а
        # демо публично. Страница не показывает «Скачать наш», адрес - 404.
        if cfg.demo:
            return None
        path = OUR_TEMPLATES.get(kind)
        if path is None:
            return None
        here = Path(__file__).resolve().parent.parent.parent / path
        return here if here.is_file() else None

    @app.get("/documents")
    async def documents_page(request: Request) -> Response:
        if not may_view(request, "settings"):
            return denied(request, "settings")
        stored = await crm.doc_templates()
        return render(request, "documents.html",
                      rows=logic.doc_rows(stored),
                      summary=logic.doc_summary(stored),
                      marks={m["kind"]: m for m in await crm.company_marks()},
                      ours={k: our_template(k) is not None
                            for k in logic.DOC_TEMPLATES})

    @app.get("/documents/ours/{kind}")
    async def document_ours(request: Request, kind: str) -> Response:
        """Наш шаблон на скачивание: с него начинают свой."""
        if not may_view(request, "settings"):
            return denied(request, "settings")
        path = our_template(kind)
        if path is None:
            return render(request, "missing.html", status_code=404, what="Шаблон")
        return FileResponse(path, filename=f"{kind}-наш{logic.DOC_SUFFIX}")

    @app.get("/documents/mine/{template_id}")
    async def document_mine(request: Request, template_id: int) -> Response:
        """Загруженный шаблон: скачать и посмотреть, что именно включено."""
        if not may_view(request, "settings"):
            return denied(request, "settings")
        row = await crm.doc_template(template_id)
        path = Path(cfg.doc_dir) / str((row or {}).get("filename") or "")
        if row is None or not path.is_file():
            return render(request, "missing.html", status_code=404, what="Шаблон")
        return FileResponse(path, filename=str(row.get("original")
                                               or row["filename"]))

    @app.post("/documents/{kind}")
    async def document_upload(request: Request, kind: str) -> Response:
        """Загрузить свой шаблон, включить наш обратно или убрать из архива."""
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        if kind not in logic.DOC_TEMPLATES or kind in logic.DOC_CODE_ONLY:
            return render(request, "missing.html", status_code=404,
                          what="Вид документа")
        data = await request.form()
        action = str(data.get("action") or "upload")
        by = who(request)
        if action == "ours":
            await crm.disable_doc_templates(kind)
            flash(request, f"«{logic.DOC_TEMPLATES[kind]['title']}»: "
                           "вернули наш шаблон. Ваш остался в архиве.")
            return redirect("/documents")
        if action in ("enable", "drop"):
            row = await by_id(crm.doc_template, data.get("template_id"))
            if row is None or row["kind"] != kind:
                flash(request, "Шаблон не найден.", "err")
                return redirect("/documents")
            if action == "enable":
                await crm.enable_doc_template(row["id"])
                flash(request, f"«{logic.DOC_TEMPLATES[kind]['title']}»: "
                               "включён ваш шаблон.")
            else:
                dropped = await crm.drop_doc_template(row["id"])
                if dropped is None:
                    flash(request, "Включённый шаблон не удаляется: "
                                   "сначала верните наш.", "err")
                else:
                    # Файл - только ничей: прежняя нумерация отдавала одно
                    # имя двум строкам, и удаление архивной стирало файл
                    # включённой.
                    if not any(r["filename"] == dropped["filename"]
                               for r in await crm.doc_templates()):
                        (Path(cfg.doc_dir) / str(dropped["filename"])).unlink(
                            missing_ok=True)
                    flash(request, "Шаблон убран из архива.")
            return redirect("/documents")
        upload = data.get("template")
        filename = getattr(upload, "filename", "") or ""
        raw = await upload.read() if filename else b""
        if not raw:
            flash(request, "Выберите файл шаблона.", "err")
            return redirect("/documents")
        try:
            doctemplates.check_upload(raw, filename)
        except contract_service.TemplateProblem as exc:
            flash(request, str(exc), "err")
            return redirect("/documents")
        folder = Path(cfg.doc_dir)
        first = logic.doc_next_number(
            kind, [r["filename"] for r in await crm.doc_templates(kind)])
        try:
            folder.mkdir(parents=True, exist_ok=True)
            # Только новый файл («x»): имя, занятое на диске (две загрузки
            # разом, файл без строки), не затирается - берётся следующее.
            for number in range(first, first + 100):
                name = logic.doc_filename(kind, number)
                try:
                    with (folder / name).open("xb") as out:
                        out.write(raw)
                    break
                except FileExistsError:
                    continue
            else:
                raise OSError("нет свободного имени файла")
        except OSError as err:
            log.warning("шаблон не сохранён: %s", err)
            flash(request, "Шаблон не сохранился — попробуйте ещё раз.", "err")
            return redirect("/documents")
        template_id = await crm.add_doc_template(
            kind=kind, filename=name, original=filename[:200],
            size_bytes=len(raw), sha256=doctemplates.digest(raw), by=by)
        await crm.enable_doc_template(template_id)
        flash(request, f"«{logic.DOC_TEMPLATES[kind]['title']}»: ваш шаблон "
                       "загружен и включён. Наш выключился сам.")
        return redirect("/documents")

    @app.post("/documents/marks/{kind}")
    async def company_mark(request: Request, kind: str) -> Response:
        """Подпись и печать: png на прозрачном фоне."""
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        if kind not in logic.COMPANY_MARKS:
            return render(request, "missing.html", status_code=404, what="Файл")
        data = await request.form()
        if str(data.get("action") or "") == "drop":
            await crm.drop_company_mark(kind)
            (Path(cfg.doc_dir) / logic.mark_filename(kind)).unlink(missing_ok=True)
            flash(request, f"{logic.COMPANY_MARKS[kind]} убрана: "
                           "подстановка в документах просто исчезнет.")
            return redirect("/documents")
        upload = data.get("mark")
        filename = getattr(upload, "filename", "") or ""
        raw = await upload.read() if filename else b""
        if not raw:
            flash(request, "Выберите файл.", "err")
            return redirect("/documents")
        if Path(filename).suffix.lower() not in logic.MARK_SUFFIXES:
            flash(request, "Только png: прозрачный фон бывает только у него, "
                           "а подпись на белом квадрате закроет текст.", "err")
            return redirect("/documents")
        if len(raw) > logic.MARK_MAX_BYTES:
            flash(request, f"Файл больше "
                           f"{logic.MARK_MAX_BYTES // (1024 * 1024)} МБ.", "err")
            return redirect("/documents")
        name = logic.mark_filename(kind)
        try:
            Path(cfg.doc_dir).mkdir(parents=True, exist_ok=True)
            (Path(cfg.doc_dir) / name).write_bytes(raw)
        except OSError as err:
            log.warning("подпись не сохранена: %s", err)
            flash(request, "Файл не сохранился — попробуйте ещё раз.", "err")
            return redirect("/documents")
        await crm.set_company_mark(kind, filename=name, size_bytes=len(raw),
                                   by=who(request))
        flash(request, f"{logic.COMPANY_MARKS[kind]} загружена: она встанет "
                       "на место подстановки в шаблоне.")
        return redirect("/documents")

    @app.get("/documents/marks/{kind}")
    async def company_mark_file(request: Request, kind: str) -> Response:
        if not may_view(request, "settings"):
            return denied(request, "settings")
        path = Path(cfg.doc_dir) / logic.mark_filename(kind)
        if kind not in logic.COMPANY_MARKS or not path.is_file():
            return render(request, "missing.html", status_code=404, what="Файл")
        return FileResponse(path)

    # ─────────────────── ввод техники в эксплуатацию ───────────────────

    @app.get("/intake")
    async def intake_page(request: Request) -> Response:
        if not may_view(request, "settings"):
            return denied(request, "settings")
        settings = await crm.settings()
        rows = await crm.bikes_on_assembly()
        cells = await crm.batteries_on_assembly()
        return render(request, "intake.html",
                      checks=logic.bike_check_settings(settings),
                      search=logic.search_settings(settings),
                      return_photo_days=logic.return_photo_days(settings),
                      rows=[{**b, "state": logic.bike_check_state(b, settings)}
                            for b in rows],
                      cells=[{**b, "state": logic.battery_check_state(b, settings)}
                             for b in cells])

    @app.post("/intake")
    async def intake_save(request: Request) -> Response:
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        data = await form(request)
        by = who(request)
        await crm.set_setting("bike_check_required",
                              "1" if data.get("required") else "0", by=by)
        # В демо снимки не хранятся, и требование фото заперло бы ввод
        # техники всем посетителям: оно всегда выключено.
        if cfg.demo and data.get("photo"):
            flash(request, DEMO_NO_PHOTO_TEXT)
        await crm.set_setting("bike_photo_required",
                              "1" if data.get("photo") and not cfg.demo else "0", by=by)
        for key, what in (("search_after_days", "Розыск"),
                          ("theft_after_days", "Кража")):
            got = count_field(data, key, what=what, default="0", limit=365)
            if not got.ok:
                flash(request, got.error, "err")
                return redirect("/intake")
            if got.value:
                await crm.set_setting(key, str(got.value), by=by)
        # Срок хранения фото при сдаче: пусто - не трогаем, ноль не
        # принимаем - «хранить 0 дней» стёрло бы вчерашние снимки.
        if (data.get("return_photo_days") or "").strip():
            days = count_field(data, "return_photo_days", what="Срок хранения фото",
                               default=str(logic.RETURN_PHOTO_DAYS), limit=3650, least=1)
            if not days.ok:
                flash(request, days.error, "err")
                return redirect("/intake")
            await crm.set_setting("return_photo_days", str(days.value), by=by)
        flash(request, "Правила ввода техники сохранены.")
        return redirect("/intake")

    # ─────────────────── оценка риска клиента ───────────────────

    @app.get("/risk")
    async def risk_page(request: Request) -> Response:
        """Правило оценки словами, залог по уровням и сколько клиентов на
        каждом уровне: владельцу видно, кого коснётся новый залог."""
        if not may_view(request, "settings"):
            return denied(request, "settings")
        counts = dict.fromkeys(logic.RISK_LEVELS, 0)
        for got in (await service.client_risks(crm, None, today=date.today())).values():
            counts[got["level"]] += 1
        deposits = logic.risk_settings(await crm.settings())
        return render(request, "risk.html", rules=logic.risk_rules(), counts=counts,
                      deposits={k: plain_amount(v) for k, v in deposits.items()},
                      medium=logic.RISK_MEDIUM, high=logic.RISK_HIGH)

    @app.post("/risk")
    async def risk_save(request: Request) -> Response:
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        data = await form(request)
        values: dict[str, Decimal] = {}
        for level, key in logic.RISK_DEPOSIT_KEYS.items():
            got = cost_field(data, key)
            if not got.ok:
                flash(request, f"Залог «{logic.RISK_LEVELS[level]}»: {got.error}", "err")
                return redirect("/risk")
            values[key] = got.value
        for key, value in values.items():
            await crm.set_setting(key, plain_amount(value), by=who(request))
        flash(request, "Залог по уровням риска сохранён.")
        return redirect("/risk")

    # ─────────────────────── уведомления ───────────────────────

    @app.get("/notices")
    async def notices_page(request: Request) -> Response:
        if not may_view(request, "settings"):
            return denied(request, "settings")
        state = logic.notice_settings(await crm.notices())
        counts = await crm.notice_counts(logic.NOTICE_LOG_DAYS)
        settings = await crm.settings()
        return render(request, "notices.html",
                      # Карточка «Сервер»: отчёт сервиса backup и память
                      # проверки, которую раз в час пишет процесс бота.
                      server=logic.server_rows(
                          logic.parse_backup_status(settings.get(logic.BACKUP_STATUS_KEY)),
                          logic.parse_health_state(settings.get(logic.HEALTH_KEY)),
                          datetime.now(UTC), bot=not cfg.demo),
                      pulse_url=None if cfg.demo else str(request.url_for("healthz_bot")),
                      groups=logic.notice_rows(state, counts),
                      log=await crm.notice_log(limit=50),
                      bot_ready=bot is not None, bot_state=await bot_health(),
                      # Адресат командного уведомления - сотрудник с Telegram
                      # вместо служебного чата: техник получает «ждёт
                      # запчасть» лично, а не в общем потоке.
                      recipients=[x for x in await crm.staff_all()
                                  if x.get("tg_id") and x.get("active")],
                      chat_ready=bool(cfg.contract_chat_id))

    @app.post("/notices/{code}")
    async def notice_save(request: Request, code: str) -> Response:
        if not may_edit(request, "settings"):
            return denied(request, "settings")
        if code not in logic.NOTICES:
            return render(request, "missing.html", status_code=404,
                          what="Уведомление")
        data = await form(request)
        default = logic.notice_defaults(code)
        at_hour: int | None = None
        if default["at_hour"] is not None:
            # «Сразу» остаётся «сразу»: перенести событийное уведомление на
            # час нельзя - события не ждут расписания.
            hour = count_field(data, "at_hour", what="Час",
                               default=str(default["at_hour"]), limit=23)
            minute = count_field(data, "at_minute", what="Минуты",
                                 default="0", limit=59)
            for check in (hour, minute):
                if not check.ok:
                    flash(request, check.error, "err")
                    return redirect("/notices")
            at_hour, at_minute = hour.value, minute.value
        else:
            at_minute = 0
        extra = {}
        for key, fallback in (default["extra"] or {}).items():
            # Подпись и пределы - по виду параметра (logic.NOTICE_PARAMS): час
            # не бывает 300-м, число клиентов на велосипед - нулём, день
            # недели - 1..7.
            what, _, _, least, limit = logic.notice_param_label(code, key)
            got = count_field(data, key, what=what, default=str(fallback),
                              limit=limit, least=least)
            if not got.ok:
                flash(request, got.error, "err")
                return redirect("/notices")
            extra[key] = got.value
        await crm.set_notice(code, enabled=bool(data.get("enabled")),
                             at_hour=at_hour, at_minute=at_minute,
                             chat_id=(data.get("chat_id") or "").strip() or None,
                             extra=extra, by=who(request))
        flash(request, f"«{default['title']}»: "
              + ("включено." if data.get("enabled") else "выключено."))
        return redirect("/notices")

    # ─────────────────────── счета на оплату ───────────────────────

    # Живость бота: get_me раз в пять минут, не на каждый экран. Если бот
    # молчит, слать об этом в Telegram тем же ботом бессмысленно - поэтому
    # предупреждение живёт в панели, а не в уведомлениях.
    _bot_health: dict[str, Any] = {"at": 0.0, "ok": None, "name": "", "error": ""}

    async def bot_health() -> dict[str, Any]:
        if bot is None:
            return {"ok": None, "name": "", "error": "бот к панели не подключён"}
        now = time.monotonic()
        if now - _bot_health["at"] < 300 and _bot_health["ok"] is not None:
            return dict(_bot_health)
        try:
            me = await asyncio.wait_for(bot.get_me(), timeout=5)
            _bot_health.update(at=now, ok=True, name=getattr(me, "username", "") or "",
                               error="")
        except Exception as exc:                         # noqa: BLE001
            _bot_health.update(at=now, ok=False, error=str(exc) or type(exc).__name__)
        return dict(_bot_health)

    def acquiring() -> Any:
        """Эквайринг Точки для одного запроса.

        Панель ходит в банк только здесь и только по нажатию кнопки:
        ссылку оператор просит при клиенте, и ждать круга опроса в
        процессе бота ему негде. Сами опросы статусов там и остались.
        """
        if not (cfg.tochka_token and cfg.tochka_customer_code):
            return None
        return tochka.TochkaClient(token=cfg.tochka_token,
                                   customer_code=cfg.tochka_customer_code)

    async def acquiring_live() -> Any:
        """Эквайринг, если он настроен И не выключен владельцем в панели.

        Выключатель - настройка, а не удаление токена из окружения:
        отключить на день эквайринг, который спорит с кассой, должен
        мочь владелец, а не тот, кто правит .env на сервере.
        """
        if not logic.acquiring_enabled(await crm.settings()):
            return None
        return acquiring()

    def acquiring_state(settings: dict[str, Any]) -> dict[str, Any]:
        code = str(cfg.tochka_customer_code or "")
        return {"configured": acquiring() is not None,
                "enabled": logic.acquiring_enabled(settings),
                "code": ("•••" + code[-4:]) if code else ""}

    @app.post("/payments/acquiring")
    async def acquiring_toggle(request: Request) -> Response:
        """Включить, выключить или проверить эквайринг из панели."""
        if not may_edit(request, "finance"):
            return denied(request, "finance")
        action = (await form(request)).get("action") or ""
        if action in ("on", "off"):
            await crm.set_setting("acquiring_enabled", "1" if action == "on" else "0",
                                  by=who(request))
            flash(request, "Эквайринг включён." if action == "on"
                  else "Эквайринг выключен: ссылки на оплату не выставляются.")
        elif action == "check":
            client = acquiring()
            if client is None:
                flash(request, "Эквайринг не настроен: нет токена или кода клиента "
                               "в окружении панели.", "err")
            else:
                try:
                    got = await client.ping()
                    flash(request, f"Банк принял токен: торговых точек "
                                   f"эквайринга - {got['retailers']}.")
                except Exception as exc:                 # noqa: BLE001
                    flash(request, f"Банк не принял: {exc}", "err")
        else:
            flash(request, "Непонятное действие.", "err")
        return redirect("/payments")

    @app.get("/payments")
    async def payments_page(request: Request) -> Response:
        if not may_view(request, "finance"):
            return denied(request, "finance")
        status = request.query_params.get("status") or ""
        orders = await crm.pay_orders(status=status or None, limit=200)
        settings = logic.pay_settings(await crm.settings())
        # В выпадающем списке - те, кто платит: должники и действующие
        # аренды. Весь список клиентов сюда не влезает и не нужен.
        picks = {c["id"]: c for c in await crm.debtors(200)}
        active = await crm.active_rentals()
        for rental in active:
            picks.setdefault(rental["client_id"],
                             {"id": rental["client_id"],
                              "full_name": rental.get("full_name"),
                              "phone": rental.get("phone")})
        raw_settings = await crm.settings()
        # Кому автосписание не поможет: идущая аренда, а карты нет. Когда
        # бот предлагал привязку - отметка на карточке клиента (приезжает
        # со строкой аренды); готовность предлагать - автосписание плюс
        # карты от банка.
        seen = await crm.cards_seen()
        return render(request, "payments.html",
                      rows=logic.pay_rows(orders), status=status,
                      settings=settings, acq=acquiring_state(raw_settings),
                      summary=logic.pay_summary(
                          await crm.pay_orders(limit=500)),
                      online=await acquiring_live() is not None,
                      clients_for_pick=sorted(
                          picks.values(), key=lambda c: str(c.get("full_name") or "")),
                      nocard=logic.renters_without_card(active, await crm.cards()),
                      renters=len({r["client_id"] for r in active}),
                      cards_seen=seen,
                      nudge_ready=logic.card_nudge_ready(raw_settings, seen))

    @app.post("/payments")
    async def payment_create(request: Request) -> Response:
        """Выставить счёт: сумма и клиент. Ссылку берём у банка сразу."""
        if not may_edit(request, "finance"):
            return denied(request, "finance")
        data = await form(request)
        client = await by_id(crm.client, data.get("client_id"))
        if client is None:
            flash(request, "Выберите клиента, которому выставить счёт.", "err")
            return redirect("/payments")
        amount = logic.check_amount(data.get("amount"))
        if not amount.ok:
            flash(request, amount.error, "err")
            return redirect("/payments")
        rental = await crm.active_rental_of(client["id"])
        order = await service.create_pay_order(
            crm, client=client, rental=rental, amount=amount.value,
            by=who(request), acquiring=await acquiring_live())
        if order.get("status") == "failed":
            flash(request, f"Счёт {order['no']} заведён, но ссылки нет: "
                           f"{order.get('error') or 'банк не ответил'}", "err")
        else:
            flash(request, f"Счёт {order['no']} на {logic.money(order['amount'])} "
                           f"готов — отправьте ссылку клиенту.")
        return redirect(f"/payments/{order['id']}")

    @app.post("/payments/settings")
    async def payments_settings(request: Request) -> Response:
        if not may_edit(request, "finance"):
            return denied(request, "finance")
        data = await request.form()
        methods = [m for m in data.getlist("methods") if m in logic.PAY_METHODS]
        if not methods:
            flash(request, "Хотя бы один способ приёма должен остаться.", "err")
            return redirect("/payments")
        hour = count_field({"hour": (data.get("autocharge_hour") or "")},
                           "hour", what="Час автосписания",
                           default=str(logic.AUTOCHARGE_HOUR), limit=23)
        if not hour.ok:
            flash(request, hour.error, "err")
            return redirect("/payments")
        by = who(request)
        await crm.set_setting("pay_methods", ",".join(methods), by=by)
        await crm.set_setting("autocharge",
                              "1" if data.get("autocharge") else "0", by=by)
        await crm.set_setting("autocharge_hour", str(hour.value), by=by)
        flash(request, "Настройки приёма оплаты сохранены."
              if not data.get("autocharge") else
              "Автосписание включено: долг у клиентов с привязанной картой "
              f"будет списываться в {hour.value}:00.")
        return redirect("/payments")

    # Раньше /payments/{order_id}: иначе «settings» уедет в число.
    @app.get("/payments/{order_id}")
    async def payment_page(request: Request, order_id: int) -> Response:
        if not may_view(request, "finance"):
            return denied(request, "finance")
        order = await crm.pay_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Счёт")
        return render(request, "payment.html", order=order,
                      expired=logic.pay_expired(order),
                      card=await crm.card_of(order["client_id"]),
                      methods=logic.pay_methods(await crm.settings()))

    @app.post("/payments/{order_id}")
    async def payment_handle(request: Request, order_id: int) -> Response:
        """Действия по счёту: отправить клиенту, закрыть руками, снять."""
        if not may_edit(request, "finance"):
            return denied(request, "finance")
        order = await crm.pay_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Счёт")
        action = (await form(request)).get("action") or "send"
        back = f"/payments/{order_id}"
        if action == "cancel":
            try:
                await service.cancel_pay_order(crm, order, by=who(request))
            except service.ServiceError as exc:
                flash(request, str(exc), "err")
                return redirect(back)
            flash(request, f"Счёт {order['no']} снят.")
            return redirect(back)
        if action == "send":
            sent = await notify.pay_link(bot, db, order)
            flash(request, "Ссылка отправлена клиенту." if sent else
                  "Клиента нет в боте — скопируйте ссылку и передайте сами.",
                  "ok" if sent else "err")
            return redirect(back)
        if action in ("cash", "transfer"):
            try:
                ledger_id = await service.credit_pay_order(
                    crm, order, by=who(request), method=action)
            except service.ServiceError as exc:
                flash(request, str(exc), "err")
                return redirect(back)
            # Бонус за друга - только за настоящий платёж в журнале. У
            # счёта за ремонт записи в журнале нет вовсе (красная линия:
            # журнал - это аренда), и бонус агенту шёл бы за человека,
            # который аренду не брал.
            client = (await crm.client(order["client_id"])
                      if ledger_id else None)
            if client is not None:
                await referral_bonus(client, logic.to_money(order["amount"]),
                                     who(request))
            flash(request, f"{logic.money(order['amount'])} зачислено "
                           f"по счёту {order['no']}.")
            # Наличные за ремонт идут в ящик движением смены (в журнал -
            # нет); смены нет - деньги в кассе ничем не объяснены.
            if (order.get("work_order_id") is not None and action == "cash"
                    and await service.cash_shift_id(crm, action, who(request)) is None):
                flash(request, "Открытой смены нет — наличные за ремонт в кассу не "
                               "записаны: откройте смену и внесите их.", "err")
            return redirect(back)
        if action == "drop_card":
            await crm.drop_card(order["client_id"])
            flash(request, "Карта отвязана: автосписания больше не будет.")
            return redirect(back)
        flash(request, "Непонятное действие.", "err")
        return redirect(back)

    # ─────────────────────── трекеры и карта ───────────────────────

    async def tracker_rows_now() -> list[dict]:
        """Трекеры с состоянием: панель в StarLine не ходит, она читает базу.

        Опрос живёт в процессе бота: у него уже есть расписание и бот
        для тревог, а веб-процессов может быть несколько — и каждый
        опрашивал бы StarLine по своему кругу.
        """
        return logic.tracker_rows(await crm.trackers(), settings=await crm.settings())

    @app.get("/map")
    async def fleet_map(request: Request) -> Response:
        if not may_view(request, "trackers"):
            return denied(request, "trackers")
        settings = await crm.settings()
        rows = logic.tracker_rows(await crm.trackers(), settings=settings)
        q = request.query_params.get("q") or ""
        found = logic.rows_search(rows, q, TRACKER_SEARCH)
        tools = list_tools(request, found, allowed=TRACKER_SORTS)
        # Точки на карте - по найденному: поиск и есть «выделить найденное».
        points = logic.map_points(tools["all_rows"])
        return render(request, "map.html", rows=tools["rows"], tools=tools, q=q,
                      points=points,
                      # Точки выдачи - своим слоем: далеко ли велосипед от точки.
                      places=logic.map_places(await crm.locations()),
                      map_cfg=logic.map_config(settings),
                      summary=logic.tracker_summary(rows),
                      alerts=await crm.tracker_alerts(open_only=True, limit=50))

    TRACKER_SORTS = {"bike": "bike_code", "speed": "speed", "silent": "silent_hours",
                     "client": "client_name"}
    TRACKER_SEARCH = ("bike_code", "bike_model", "client_name", "alias", "device_id")

    @app.get("/map.{ext}")
    async def map_csv(request: Request, ext: str) -> Response:
        if not may_view(request, "trackers"):
            return denied(request, "trackers")
        rows = logic.rows_search(
            logic.tracker_rows(await crm.trackers(), settings=await crm.settings()),
            request.query_params.get("q") or "", TRACKER_SEARCH)
        return await table(ext, "map",
                      ["Велосипед", "Модель", "Трекер", "Состояние", "Скорость",
                       "Связь, ч назад", "У кого", "Широта", "Долгота"],
                      [[r.get("bike_code"), r.get("bike_model"),
                        r.get("alias") or r.get("device_id"),
                        logic.tracker_state_title(r), r.get("speed"),
                        r.get("silent_hours"), r.get("client_name"),
                        r.get("lat"), r.get("lon")] for r in rows])

    @app.get("/alerts")
    async def alerts_page(request: Request) -> Response:
        """Реестр тревог: что случилось, кто взял и что с этим делают."""
        if not may_view(request, "trackers"):
            return denied(request, "trackers")
        rows, view, level, kind, q = await alert_list(request)
        tools = list_tools(request, rows, allowed=ALERT_SORTS)
        return render(request, "alerts.html", rows=tools["rows"], tools=tools,
                      view=view, level=level, kind=kind, q=q,
                      limits=logic.tracker_settings(await crm.settings()),
                      summary=logic.alert_summary(
                          logic.alert_rows(await crm.tracker_alerts(open_only=False,
                                                                    limit=2000))))

    @app.post("/alerts/settings")
    async def alerts_settings(request: Request) -> Response:
        """Пороги тревог трекеров. Опрос читает их на каждом круге, так что
        новые пороги действуют со следующего круга, без перезапуска."""
        if not may_edit(request, "trackers"):
            return denied(request, "trackers")
        values, error = logic.check_tracker_limits(await form(request))
        if error:
            flash(request, error, "err")
            return redirect("/alerts")
        for key, value in values.items():
            await crm.set_setting(key, value, by=who(request))
        flash(request, "Пороги тревог сохранены: действуют со следующего круга опроса.")
        return redirect("/alerts")

    ALERT_SORTS = {"level": "level", "title": "title", "bike": "bike_code",
                   "client": "client_name", "created": "created_at",
                   "state": "state"}

    async def alert_list(request: Request) -> tuple[list[dict], str, str, str, str]:
        view = request.query_params.get("view") or "needs"
        level = request.query_params.get("level") or ""
        kind = request.query_params.get("kind") or ""
        q = request.query_params.get("q") or ""
        rows = logic.alert_rows(await crm.tracker_alerts(
            open_only=view != "all", level=level or None, kind=kind or None,
            limit=2000))
        shown = [r for r in rows if r["needs"]] if view == "needs" else rows
        return (logic.rows_search(shown, q, ("title", "note", "bike_code",
                                             "client_name", "alias", "device_id")),
                view, level, kind, q)

    @app.get("/alerts.{ext}")
    async def alerts_csv(request: Request, ext: str) -> Response:
        if not may_view(request, "trackers"):
            return denied(request, "trackers")
        rows, *_ = await alert_list(request)
        return await table(ext, "alerts",
                      ["Уровень", "Что случилось", "Подробности", "Велосипед",
                       "Клиент", "Когда", "Состояние", "Кто взял", "Закрыта"],
                      [[logic.ALERT_LEVELS.get(r["level"], r["level"]), r["title"],
                        r.get("note"), r.get("bike_code") or r.get("alias")
                        or r.get("device_id"), r.get("client_name"),
                        r.get("created_at"),
                        logic.ALERT_STATES.get(r["state"], r["state"]) if r["open"]
                        else "закрыта",
                        r.get("taken_by"), r.get("handled_at")]
                       for r in rows])

    @app.post("/alerts/{alert_id}")
    async def alert_action(request: Request, alert_id: int) -> Response:
        """Взять, отложить, признать нормой или закрыть.

        «Это норма» - не закрытие: тревога остаётся открытой, поэтому
        второй раз она не поднимется, пока причина держится, а исчезнет
        причина - опрос закроет её сам.
        """
        if not may_edit(request, "trackers"):
            return denied(request, "trackers")
        if await crm.tracker_alert(alert_id) is None:
            return render(request, "missing.html", status_code=404, what="Тревога")
        data = await form(request)
        action = str(data.get("action") or "")
        nxt = data.get("next") or ""
        back = logic.safe_next(nxt, "/alerts")
        by = who(request)
        if action == "close":
            await crm.handle_alert(alert_id, by=by)
            flash(request, "Тревога закрыта.")
        elif action == "take":
            await crm.set_alert_state(alert_id, state="working", by=by)
            flash(request, "Взяли в работу.")
        elif action == "snooze":
            until = logic.snooze_until(data.get("hours"))
            await crm.set_alert_state(alert_id, state="snoozed", by=by,
                                      snooze_until=until)
            flash(request, f"Отложено до {_dmy(until)}.")
        elif action == "normal":
            await crm.set_alert_state(alert_id, state="normal", by=by)
            flash(request, "Помечено нормой: пока причина держится, "
                           "тревога больше не поднимется.")
        else:
            flash(request, "Непонятное действие.", "err")
        return redirect(back)

    @app.get("/trackers")
    async def trackers_page(request: Request) -> Response:
        if not may_view(request, "trackers"):
            return denied(request, "trackers")
        rows = await tracker_rows_now()
        # Списка парка здесь нет: привязка живёт на карточке трекера. Список
        # в каждой строке - 130 трекеров на 190 велосипедов - это 2 МБ и
        # треть секунды на страницу.
        return render(request, "trackers.html", rows=rows,
                      summary=logic.tracker_summary(rows),
                      alerts=await crm.tracker_alerts(open_only=True, limit=50))

    @app.post("/trackers")
    async def tracker_create(request: Request) -> Response:
        """Метка заводится руками, если её ещё не видел опрос."""
        if not may_edit(request, "trackers"):
            return denied(request, "trackers")
        data = await form(request)
        device = logic.check_code(data.get("device_id"))
        note = logic.check_note(data.get("note"))
        for check in (device, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/trackers")
        try:
            await crm.create_tracker(
                device_id=device.value,
                alias=(data.get("alias") or "").strip() or None, note=note.value)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Трекер с таким номером устройства уже заведён.", "err")
                return redirect("/trackers")
            raise
        flash(request, "Трекер заведён. Привяжите его к велосипеду.")
        return redirect("/trackers")

    @app.post("/trackers/{tracker_id}/bike")
    async def tracker_bind(request: Request, tracker_id: int) -> Response:
        """Привязка трекера к велосипеду - и есть весь смысл раздела:
        без неё координаты принадлежат неизвестно чему."""
        if not may_edit(request, "trackers"):
            return denied(request, "trackers")
        tracker = await crm.tracker(tracker_id)
        if tracker is None:
            return render(request, "missing.html", status_code=404, what="Трекер")
        data = await form(request)
        # Форма привязки - на карточке, туда и возвращаемся при отказе.
        card = f"/trackers/{tracker_id}"
        raw = (data.get("bike_id") or "").strip()
        bike_id = logic.parse_id(raw)
        if raw and (bike_id is None or await crm.bike(bike_id) is None):
            flash(request, "Такого велосипеда нет.", "err")
            return redirect(card)
        try:
            await crm.update_tracker(tracker_id, bike_id=bike_id)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "На этом велосипеде уже стоит другой трекер.", "err")
                return redirect(card)
            raise
        flash(request, "Трекер привязан." if bike_id else "Трекер отвязан.")
        return redirect(card)

    @app.post("/trackers/{tracker_id}/toggle")
    async def tracker_toggle(request: Request, tracker_id: int) -> Response:
        if not may_edit(request, "trackers"):
            return denied(request, "trackers")
        tracker = await crm.tracker(tracker_id)
        if tracker is None:
            return render(request, "missing.html", status_code=404, what="Трекер")
        await crm.update_tracker(tracker_id, active=not tracker["active"])
        flash(request, "Трекер снят с наблюдения." if tracker["active"]
              else "Трекер снова под наблюдением.")
        return redirect(f"/trackers/{tracker_id}")

    @app.post("/trackers/{tracker_id}/phone")
    async def tracker_phone(request: Request, tracker_id: int) -> Response:
        """Номер SIM трекера - руками: StarLine отдаёт его не всегда."""
        if not may_edit(request, "trackers"):
            return denied(request, "trackers")
        if await crm.tracker(tracker_id) is None:
            return render(request, "missing.html", status_code=404, what="Трекер")
        data = await request.form()
        raw = str(data.get("phone") or "").strip()
        phone = bot_logic.normalize_phone(raw) if raw else None
        if raw and phone is None:
            flash(request, "Номер SIM: нужен телефон вида +7 900 123-45-67.", "err")
            return redirect(f"/trackers/{tracker_id}")
        await crm.update_tracker(tracker_id, phone=phone)
        flash(request, "Номер SIM сохранён." if phone else "Номер SIM стёрт.")
        return redirect(f"/trackers/{tracker_id}")

    @app.post("/trackers/{tracker_id}/command")
    async def tracker_command(request: Request, tracker_id: int) -> Response:
        """Заблокировать мотор или снять блокировку.

        Панель в StarLine не ходит: команда ложится в очередь, опрос в
        процессе бота относит её на ближайшем круге и пишет ответ на
        карточку. Блокировка у StarLine срабатывает после остановки
        велосипеда, поэтому кнопка безопасна для курьера на дороге.
        """
        if not may_edit(request, "trackers"):
            return denied(request, "trackers")
        tracker = await crm.tracker(tracker_id)
        if tracker is None:
            return render(request, "missing.html", status_code=404, what="Трекер")
        data = await form(request)
        nxt = data.get("next") or ""
        back = logic.safe_next(nxt, f"/trackers/{tracker_id}")
        command = logic.check_command(data.get("command"))
        if not command.ok:
            flash(request, command.error, "err")
            return redirect(back)
        if not tracker.get("active"):
            flash(request, "Трекер снят с наблюдения - опрос до него не дойдёт.", "err")
            return redirect(back)
        alert_id = logic.parse_id(data.get("alert_id"))
        if alert_id is not None:
            # Тревога, по которой блокируют, - этого трекера: чужой номер
            # упирался в ссылку базы, а тревога другого трекера связала бы
            # команду не с той историей.
            alert = await crm.tracker_alert(alert_id)
            if alert is None or int(alert["tracker_id"]) != tracker_id:
                flash(request, "Тревога не найдена — обновите страницу.", "err")
                return redirect(back)
        note = logic.check_note(data.get("note"))
        try:
            await crm.queue_tracker_command(
                tracker_id=tracker_id, command=command.value, by=who(request),
                alert_id=alert_id, note=note.value if note.ok else None)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower() or "pending" in str(exc):
                flash(request, "Команда уже в очереди: опрос отнесёт её в StarLine "
                               "на ближайшем круге.", "err")
                return redirect(back)
            raise
        flash(request, f"«{logic.TRACKER_COMMANDS[command.value]}» поставлена в "
                       "очередь: опрос отнесёт её в StarLine в ближайшие минуты, "
                       "ответ появится на карточке трекера и в служебном чате.")
        return redirect(back)

    @app.post("/trackers/alerts/{alert_id}")
    async def tracker_alert_handle(request: Request, alert_id: int) -> Response:
        if not may_edit(request, "trackers"):
            return denied(request, "trackers")
        data = await form(request)
        nxt = data.get("next") or ""
        back = logic.safe_next(nxt, "/trackers")
        await crm.handle_alert(alert_id, by=who(request))
        flash(request, "Тревога снята.")
        return redirect(back)

    @app.get("/trackers/{tracker_id}")
    async def tracker_card(request: Request, tracker_id: int) -> Response:
        if not may_view(request, "trackers"):
            return denied(request, "trackers")
        tracker = await crm.tracker(tracker_id)
        if tracker is None:
            return render(request, "missing.html", status_code=404, what="Трекер")
        settings = await crm.settings()
        row = logic.tracker_rows([tracker], settings=settings)[0]
        # Трек за период: «где он был вчера» - главный вопрос к трекеру,
        # и отвечать на него списком координат было бы издевательством.
        kind = request.query_params.get("range") or "today"
        since_q = logic.check_date(request.query_params.get("since"), default=None)
        until_q = logic.check_date(request.query_params.get("until"), default=None)
        first, last = logic.track_period(
            kind, since=since_q.value if since_q.ok else None,
            until=until_q.value if until_q.ok else None)
        # Тревоги - этого трекера, отбором в запросе, а не из полусотни
        # тревог всего парка. Открытые - все и первыми: история закрытых
        # длинная, и предел не должен прятать то, что ещё горит.
        opened = await crm.tracker_alerts(open_only=True, tracker_id=tracker_id)
        seen = {a["id"] for a in opened}
        alerts = opened + [a for a in await crm.tracker_alerts(
            open_only=False, tracker_id=tracker_id, limit=50) if a["id"] not in seen]
        tz = datetime.now().astimezone().tzinfo
        track = await crm.track_between(
            tracker_id,
            since=datetime.combine(first, datetime.min.time(), tzinfo=tz),
            until=datetime.combine(last + timedelta(days=1), datetime.min.time(),
                                   tzinfo=tz))
        return render(request, "tracker.html", tracker=row, track=track,
                      block=logic.block_state(row, await crm.pending_command_of(tracker_id)),
                      commands=logic.command_rows(await crm.tracker_commands(tracker_id)),
                      run_km=logic.track_distance(track),
                      track_range=kind, track_since=first, track_until=last,
                      line=logic.track_line(track),
                      map_cfg=logic.map_config(settings),
                      points=logic.map_points([row]),
                      free_bikes=await crm.bikes(limit=10000),
                      alerts=alerts)

    # ─────────────────── закупки основных средств ───────────────────

    @app.get("/assets")
    async def assets_page(request: Request) -> Response:
        """Парк как основные средства: сколько вложено, сколько осталось."""
        if not may_view(request, "finance"):
            return denied(request, "finance")
        tab = request.query_params.get("tab") or "all"
        rows = logic.asset_rows(await crm.bikes(limit=10000))
        summary = logic.asset_summary(rows)
        if tab == "worn":
            rows = [b for b in rows if b["worn_out"]
                    and b.get("status") not in ("sold", "written_off")]
        elif tab == "written_off":
            rows = [b for b in rows if b.get("status") in ("sold", "written_off")]
        elif tab == "live":
            rows = [b for b in rows if b.get("status") not in ("sold", "written_off")]
        return render(request, "assets.html", rows=rows, summary=summary, tab=tab,
                      places=await location_names(),
                      purchases=await crm.purchases(limit=100),
                      suppliers=await crm.suppliers(active_only=True))

    @app.post("/assets")
    async def asset_purchase(request: Request) -> Response:
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        data = await form(request)
        codes, error = logic.purchase_codes(data.get("codes"))
        model = logic.check_name(data.get("model"), what="Модель")
        price = logic.check_amount(data.get("purchase_price")) \
            if (data.get("purchase_price") or "").strip() else logic.Check(True, None)
        bought = logic.check_purchase_date(data.get("purchased_on"), today=date.today(),
                                           default=date.today())
        residual = cost_field(data, "residual_price")
        note = logic.check_note(data.get("note"))
        if error:
            flash(request, error, "err")
            return redirect("/assets")
        for check in (model, price, bought, residual, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/assets")
        months = data.get("service_months") or "24"
        bat_months = data.get("battery_service_months") or "15"
        batteries = data.get("battery_count") or "2"
        # Пределы те же, что у карточки велосипеда: число за пределами
        # integer роняло закупку 500, а не возвращало на форму.
        for label, value, least, most in (("Срок службы", months, 1, 240),
                                          ("Срок службы АКБ", bat_months, 1, 240),
                                          ("АКБ", batteries, 0, 10)):
            got = logic.parse_id(value)
            if got is None or not least <= got <= most:
                flash(request, f"{label}: число от {least} до {most}.", "err")
                return redirect("/assets")
        bat_price = logic.check_amount(data.get("battery_price")) \
            if (data.get("battery_price") or "").strip() else logic.Check(True, None)
        if not bat_price.ok:
            flash(request, bat_price.error, "err")
            return redirect("/assets")
        # Партия встаёт на точку справочника - третья точка ничем не хуже.
        place = logic.check_location(data.get("location"), await location_names())
        if not place.ok:
            flash(request, place.error, "err")
            return redirect("/assets")
        location = place.value
        supplier_id = logic.parse_id(data.get("supplier_id"))
        if supplier_id is not None and await crm.supplier(supplier_id) is None:
            flash(request, "Такого поставщика нет — обновите страницу.", "err")
            return redirect("/assets")
        try:
            result = await service.buy_bikes(
                crm, supplier_id=supplier_id, purchased_on=bought.value, codes=codes,
                model=model.value, price=price.value or Decimal(0),
                battery_count=logic.parse_id(batteries),
                service_months=logic.parse_id(months),
                residual=residual.value, battery_price=bat_price.value,
                battery_months=logic.parse_id(bat_months), location=location,
                note=note.value,
                by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/assets")
        purchase = await crm.purchase(result["purchase_id"])
        flash(request, f"Закупка {purchase['no']}: заведено велосипедов "
                       f"{result['bikes']} на {logic.money(purchase['total'])}.")
        return redirect("/assets")

    # ─────────────────────── склад запчастей ───────────────────────

    def doc_lines(data: dict, *, limit: int = 8) -> list[dict]:
        """Строки складского документа из формы без JS: фиксированное число
        пустых строк, заполненные берём, пустые молча пропускаем."""
        lines = []
        for i in range(limit):
            part_id = logic.parse_id(data.get(f"part_id_{i}"))
            qty = logic.parse_id(data.get(f"qty_{i}"))
            if part_id is None or not qty:
                continue
            price = cost_field(data, f"price_{i}")
            lines.append({"part_id": part_id, "qty": qty,
                          "price": price.value if price.ok else Decimal(0)})
        return lines

    @app.get("/parts")
    async def parts_page(request: Request) -> Response:
        node = request.query_params.get("node") or ""
        q = request.query_params.get("q") or ""
        rows = logic.part_rows(await crm.parts(node=node or None, q=q or None),
                               await crm.stock_map(), await crm.part_last_moved(),
                               transit=await crm.parts_in_transit())
        tools = list_tools(request, rows, allowed=PART_SORTS)
        # График денег на полке - только тем, кому открыты деньги: это
        # сумма, а не количество гаек.
        chart = (logic.stock_value_chart(await crm.stock_value_by_month())
                 if may_view(request, "finance") else None)
        return render(request, "parts.html", rows=tools["rows"], tools=tools,
                      summary=logic.stock_summary(tools["all_rows"]),
                      node=node, q=q, chart=chart,
                      views=await views_of(request, "/parts"),
                      nodes=await crm.repair_nodes())

    @app.get("/parts.{ext}")
    async def parts_csv(request: Request, ext: str) -> Response:
        if not may_view(request, "inventory"):
            return denied(request, "inventory")
        rows = logic.part_rows(
            await crm.parts(node=request.query_params.get("node") or None,
                            q=request.query_params.get("q") or None),
            await crm.stock_map(), await crm.part_last_moved(),
            transit=await crm.parts_in_transit())
        money_ok = may_view(request, "finance")
        header = ["Позиция", "Узел", "Совместимость", "Остаток", "Ед.",
                  "Неснижаемый", "Не хватает", "В пути", "Дней на складе"]
        if money_ok:
            header += ["Себестоимость", "Σ себестоимость", "Цена клиенту",
                       "Σ по клиенту"]
        out = []
        for r in rows:
            line = [r["title"], logic.REPAIR_NODES.get(r.get("node"), ""),
                    r.get("model") or "все", r["stock"], r.get("unit"),
                    r.get("min_stock"), r["short"] or "", r["transit"] or "",
                    r.get("days_on_stock")]
            if money_ok:
                line += [logic.to_money(r.get("cost") or 0), r["cost_total"],
                         logic.to_money(r.get("price") or 0), r["price_total"]]
            out.append(line)
        return await table(ext, "parts", header, out)

    @app.get("/parts/new")
    async def part_new(request: Request) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        return render(request, "part_form.html", part=None,
                      nodes=await crm.repair_nodes())

    @app.post("/parts")
    async def part_create(request: Request) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        data = await form(request)
        fields = part_fields(request, data)
        if fields is None:
            return redirect("/parts/new")
        try:
            part_id = await crm.create_part(**fields)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Позиция с таким названием уже есть.", "err")
                return redirect("/parts/new")
            raise
        flash(request, "Позиция заведена.")
        return redirect(f"/parts/{part_id}")

    def part_fields(request: Request, data: dict) -> dict | None:
        title = logic.check_name(data.get("title"), what="Название")
        unit = logic.check_unit(data.get("unit"))
        cost = cost_field(data, "cost")
        price = cost_field(data, "price")
        minimum = count_field(data, "min_stock", what="Неснижаемый остаток",
                              default="0", limit=9999)
        note = logic.check_note(data.get("note"))
        for check in (title, unit, cost, price, minimum, note):
            if not check.ok:
                flash(request, check.error, "err")
                return None
        node = (data.get("node") or "").strip() or None
        if node and not logic.check_choice(node, logic.REPAIR_NODES).ok:
            node = None
        return {"title": title.value, "node": node, "unit": unit.value,
                "cost": cost.value, "price": price.value, "min_stock": minimum.value,
                "model": (data.get("model") or "").strip() or None, "note": note.value}

    @app.get("/parts/receipts")
    async def part_receipts(request: Request) -> Response:
        return render(request, "part_docs.html", kind="receipt",
                      rows=await crm.part_docs(kind="receipt"),
                      parts=await crm.parts(active_only=True),
                      suppliers=await crm.suppliers(active_only=True))

    @app.post("/parts/receipts")
    async def part_receipt_create(request: Request) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        data = await form(request)
        note = logic.check_note(data.get("note"))
        if not note.ok:
            flash(request, note.error, "err")
            return redirect("/parts/receipts")
        supplier_id = logic.parse_id(data.get("supplier_id"))
        try:
            doc_id = await service.receive_parts(
                crm, supplier_id=supplier_id, lines=doc_lines(data),
                note=note.value, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/parts/receipts")
        doc = await crm.part_doc(doc_id)
        flash(request, f"Приход {doc['no']} проведён на {logic.money(doc['total'])}.")
        return redirect("/parts/receipts")

    @app.get("/parts/write-offs")
    async def part_write_offs(request: Request) -> Response:
        return render(request, "part_docs.html", kind="write_off",
                      rows=await crm.part_docs(kind="write_off"),
                      parts=await crm.parts(active_only=True), suppliers=[])

    @app.post("/parts/write-offs")
    async def part_write_off_create(request: Request) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        data = await form(request)
        note = logic.check_note(data.get("note"))
        if not note.ok:
            flash(request, note.error, "err")
            return redirect("/parts/write-offs")
        try:
            doc_id = await service.write_off_parts(
                crm, lines=doc_lines(data, limit=5), note=note.value, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/parts/write-offs")
        doc = await crm.part_doc(doc_id)
        flash(request, f"Списание {doc['no']} проведено.")
        return redirect("/parts/write-offs")

    @app.get("/parts/moves")
    async def part_moves_page(request: Request) -> Response:
        kind = request.query_params.get("kind") or ""
        return render(request, "part_moves.html", kind=kind,
                      rows=await crm.part_moves(kind=kind or None, limit=300))

    @app.get("/parts/{part_id}")
    async def part_card(request: Request, part_id: int) -> Response:
        part = await crm.part(part_id)
        if part is None:
            return render(request, "missing.html", status_code=404, what="Позиция")
        return render(request, "part.html", part=part,
                      stock=await crm.part_stock(part_id),
                      transit=(await crm.parts_in_transit()).get(part_id, 0),
                      moves=await crm.part_moves(part_id=part_id, limit=100),
                      nodes=await crm.repair_nodes())

    @app.post("/parts/{part_id}/edit")
    async def part_edit(request: Request, part_id: int) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        part = await crm.part(part_id)
        if part is None:
            return render(request, "missing.html", status_code=404, what="Позиция")
        data = await form(request)
        fields = part_fields(request, data)
        if fields is None:
            return redirect(f"/parts/{part_id}")
        # Себестоимость правится только приходом: руками её поставить -
        # значит разойтись со складом на первом же ремонте.
        fields.pop("cost", None)
        fields["active"] = bool(data.get("active"))
        try:
            await crm.update_part(part_id, **fields)
        except Exception as exc:                        # noqa: BLE001
            # Переименование в занятое название - тот же уникальный индекс,
            # что у новой позиции: ответ на форме, а не 500.
            if not name_taken(exc):
                raise
            flash(request, "Позиция с таким названием уже есть.", "err")
            return redirect(f"/parts/{part_id}")
        flash(request, "Позиция сохранена.")
        return redirect(f"/parts/{part_id}")

    @app.post("/parts/{part_id}/count")
    async def part_count(request: Request, part_id: int) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        part = await crm.part(part_id)
        if part is None:
            return render(request, "missing.html", status_code=404, what="Позиция")
        data = await form(request)
        fact = count_field(data, "fact", what="Факт на полке", default="0", limit=99999)
        if not fact.ok:
            flash(request, fact.error, "err")
            return redirect(f"/parts/{part_id}")
        try:
            result = await service.count_part(crm, part, fact.value, by=who(request),
                                              note=(data.get("note") or "").strip() or None)
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/parts/{part_id}")
        if result["delta"] == 0:
            flash(request, "Сошлось: остаток и факт совпадают.")
        else:
            flash(request, f"Поправлено на {result['delta']:+d}, "
                           f"остаток {result['stock']}.")
        return redirect(f"/parts/{part_id}")

    # ─────────────────────── склад: поставщики ───────────────────────

    @app.get("/suppliers")
    async def suppliers_page(request: Request) -> Response:
        return render(request, "suppliers.html", rows=await crm.suppliers())

    @app.post("/suppliers")
    async def supplier_create(request: Request) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        data = await form(request)
        name = logic.check_name(data.get("name"), what="Поставщик")
        note = logic.check_note(data.get("note"))
        for check in (name, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/suppliers")
        phone = bot_logic.normalize_phone(data.get("phone")) \
            if (data.get("phone") or "").strip() else None
        try:
            await crm.create_supplier(name=name.value, phone=phone, note=note.value)
        except Exception as exc:                        # noqa: BLE001
            if "unique" in type(exc).__name__.lower():
                flash(request, "Такой поставщик уже есть.", "err")
                return redirect("/suppliers")
            raise
        flash(request, "Поставщик добавлен.")
        return redirect("/suppliers")

    @app.post("/suppliers/{supplier_id}/toggle")
    async def supplier_toggle(request: Request, supplier_id: int) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        supplier = await crm.supplier(supplier_id)
        if supplier is None:
            return render(request, "missing.html", status_code=404, what="Поставщик")
        await crm.update_supplier(supplier_id, active=not supplier["active"])
        return redirect("/suppliers")

    # ─────────────────────── склад: заказ запчастей ───────────────────────

    @app.get("/part-orders")
    async def part_orders_page(request: Request) -> Response:
        rows = logic.part_rows(await crm.parts(active_only=True), await crm.stock_map())
        order = await crm.open_part_order()
        return render(request, "part_orders.html",
                      needs=logic.part_needs(rows, await crm.waiting_orders_parts(),
                                             await crm.parts_in_transit()),
                      orders=await crm.part_orders(limit=100), current=order,
                      items=await crm.part_order_items(order["id"]) if order else [],
                      parts=await crm.parts(active_only=True),
                      suppliers=await crm.suppliers(active_only=True))

    @app.post("/part-orders/collect")
    async def part_order_collect(request: Request) -> Response:
        """Собрать потребности в заказ одной кнопкой."""
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        result = await service.collect_part_needs(crm, by=who(request))
        if result["added"]:
            flash(request, f"В заказ {result['order']['no']} добавлено строк: "
                           f"{result['added']}.")
        else:
            flash(request, "Новых потребностей нет: всё уже в заказе.")
        return redirect("/part-orders")

    @app.post("/part-orders/items")
    async def part_order_add_item(request: Request) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        data = await form(request)
        part = await by_id(crm.part, data.get("part_id"))
        qty = count_field(data, "qty", what="Количество", default="1", limit=9999)
        if part is None or not qty.ok:
            flash(request, qty.error or "Выберите позицию.", "err")
            return redirect("/part-orders")
        order = await crm.open_part_order()
        if order is None:
            order_id = await crm.create_part_order(supplier_id=None, note=None,
                                                   created_by=who(request))
            order = await crm.part_order(order_id)
        added = await crm.add_part_order_item(
            order["id"], part_id=part["id"], qty=qty.value,
            price=logic.to_money(part.get("cost") or 0), source="manual")
        flash(request, "Позиция добавлена в заказ." if added
              else "Эта позиция в заказе уже есть.", "ok" if added else "err")
        return redirect("/part-orders")

    @app.post("/part-orders/{order_id}/items/{item_id}/delete")
    async def part_order_delete_item(request: Request, order_id: int,
                                     item_id: int) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        if not await crm.delete_part_order_item(order_id, item_id):
            flash(request, "Строки уже нет.", "err")
        return redirect("/part-orders")

    @app.post("/part-orders/{order_id}/status")
    async def part_order_status(request: Request, order_id: int) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        order = await crm.part_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Заказ")
        data = await form(request)
        status = logic.check_choice(data.get("status"), ("ordered", "cancelled"),
                                    what="Статус заказа")
        if not status.ok:
            flash(request, status.error, "err")
            return redirect("/part-orders")
        supplier_id = logic.parse_id(data.get("supplier_id"))
        if supplier_id is None:
            supplier_id = order.get("supplier_id")
        elif await crm.supplier(supplier_id) is None:
            flash(request, "Такого поставщика нет — обновите страницу.", "err")
            return redirect("/part-orders")
        items = await crm.part_order_items(order_id)
        patch = {"status": status.value, "supplier_id": supplier_id,
                 "total": logic.order_total(items)}
        if status.value == "ordered":
            patch["ordered_at"] = datetime.now(UTC)
        else:
            patch["closed_at"] = datetime.now(UTC)
        await crm.update_part_order(order_id, **patch)
        flash(request, "Заказ отправлен поставщику." if status.value == "ordered"
              else "Заказ отменён.")
        return redirect("/part-orders")

    @app.post("/part-orders/{order_id}/receive")
    async def part_order_receive(request: Request, order_id: int) -> Response:
        if not may_edit(request, "inventory"):
            return denied(request, "inventory")
        order = await crm.part_order(order_id)
        if order is None:
            return render(request, "missing.html", status_code=404, what="Заказ")
        items = await crm.part_order_items(order_id)
        try:
            doc_id = await service.receive_part_order(crm, order, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/part-orders")
        doc = await crm.part_doc(doc_id)
        # Наряд, который стоял из-за этой запчасти, может ехать дальше -
        # и техник должен узнать об этом сейчас, а не заглянув на склад.
        await tell_parts_arrived(await service.orders_waiting_for(crm, items))
        flash(request, f"Заказ принят: приход {doc['no']} на {logic.money(doc['total'])}.")
        return redirect("/part-orders")

    # ─────────────────────── пересчёт техники ───────────────────────

    TAKE_SORTS = {"no": "no", "started": "started_at", "what": "title",
                  "expected": "expected", "found": "found", "missing": "missing",
                  "extra": "extra", "who": "created_by"}

    async def take_rows(request: Request) -> tuple[list[dict], str]:
        q = request.query_params.get("q") or ""
        rows = await crm.stock_takes(limit=2000)
        for r in rows:
            r["title"] = logic.take_title(r)
        return logic.rows_search(rows, q, ("no", "title", "note", "created_by",
                                           "location")), q

    @app.get("/stock-takes")
    async def stock_takes_page(request: Request) -> Response:
        rows, q = await take_rows(request)
        tools = list_tools(request, rows, allowed=TAKE_SORTS)
        return render(request, "stock_takes.html", rows=tools["rows"], tools=tools,
                      q=q, current=await crm.open_stock_take(),
                      places=await filter_points())

    @app.get("/stock-takes.{ext}")
    async def stock_takes_csv(request: Request, ext: str) -> Response:
        rows, _ = await take_rows(request)
        return await table(ext, "stock-takes",
                      ["№", "Дата", "Что считали", "Состояние", "Ожидалось",
                       "Найдено", "Не нашли", "Лишние", "Кто провёл", "Комментарий"],
                      [[r["no"], r.get("started_at"), r["title"],
                        logic.TAKE_STATES.get(r.get("status"), r.get("status")),
                        r.get("expected"), r.get("found"), r.get("missing"),
                        r.get("extra"), r.get("created_by"), r.get("note")]
                       for r in rows])

    @app.post("/stock-takes")
    async def stock_take_start(request: Request) -> Response:
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        data = await form(request)
        scope = logic.check_scope(data.get("scope") or "all")
        note = logic.check_note(data.get("note"))
        for check in (scope, note):
            if not check.ok:
                flash(request, check.error, "err")
                return redirect("/stock-takes")
        # Считают и закрытую точку: техника на ней никуда не делась, и
        # пересчёт - как раз способ её оттуда разобрать.
        place = logic.check_location(data.get("location"), await filter_points())
        if not place.ok:
            flash(request, place.error, "err")
            return redirect("/stock-takes")
        location = place.value
        what = logic.check_choice(data.get("what") or "all", logic.TAKE_WHAT,
                                  what="Что считаем")
        if not what.ok:
            flash(request, what.error, "err")
            return redirect("/stock-takes")
        try:
            take_id = await service.start_stock_take(
                crm, scope=scope.value, location=location, note=note.value,
                what=what.value, by=who(request))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect("/stock-takes")
        flash(request, "Пересчёт начат: отмечайте технику, которую видите.")
        return redirect(f"/stock-takes/{take_id}")

    @app.get("/stock-takes/{take_id}")
    async def stock_take_page(request: Request, take_id: int) -> Response:
        take = await crm.stock_take(take_id)
        if take is None:
            return render(request, "missing.html", status_code=404, what="Пересчёт")
        items = await crm.take_items(take_id)
        counts = logic.take_counts(items)
        return render(request, "stock_take.html", take=take, items=items,
                      counts=counts, progress=logic.take_progress(counts),
                      by_kind=logic.take_counts_by_kind(items))

    @app.post("/stock-takes/{take_id}/scan")
    async def stock_take_scan(request: Request, take_id: int) -> Response:
        """Отметка по номеру на раме: один ввод - одна единица техники."""
        take = await crm.stock_take(take_id)
        if take is None:
            return render(request, "missing.html", status_code=404, what="Пересчёт")
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        if not logic.take_is_open(take):
            flash(request, "Пересчёт закрыт.", "err")
            return redirect(f"/stock-takes/{take_id}")
        data = await form(request)
        code = logic.check_code(data.get("code"))
        if not code.ok:
            flash(request, code.error, "err")
            return redirect(f"/stock-takes/{take_id}")
        result = await service.take_add_found(crm, take, code.value)
        flash(request, result["message"], "ok" if result["state"] == "found" else "err")
        return redirect(f"/stock-takes/{take_id}")

    @app.post("/stock-takes/{take_id}/items/{item_id}")
    async def stock_take_item(request: Request, take_id: int, item_id: int) -> Response:
        take = await crm.stock_take(take_id)
        if take is None:
            return render(request, "missing.html", status_code=404, what="Пересчёт")
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        if not logic.take_is_open(take):
            flash(request, "Пересчёт закрыт.", "err")
            return redirect(f"/stock-takes/{take_id}")
        data = await form(request)
        state = logic.check_choice(data.get("state"), ("found", "expected", "missing"),
                                   what="Отметка")
        if not state.ok:
            flash(request, state.error, "err")
            return redirect(f"/stock-takes/{take_id}")
        if not await crm.set_take_item(take_id, item_id, state=state.value):
            flash(request, "Строки уже нет.", "err")
        return redirect(f"/stock-takes/{take_id}")

    @app.post("/stock-takes/{take_id}/items/{item_id}/delete")
    async def stock_take_item_delete(request: Request, take_id: int,
                                     item_id: int) -> Response:
        take = await crm.stock_take(take_id)
        if take is None:
            return render(request, "missing.html", status_code=404, what="Пересчёт")
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        if not logic.take_is_open(take):
            flash(request, "Пересчёт закрыт.", "err")
            return redirect(f"/stock-takes/{take_id}")
        item = await crm.take_item(take_id, item_id)
        # Убрать можно только лишнюю строку: снести ожидаемую - это стереть
        # недостачу, ради которой пересчёт и делают.
        if item is None or item["state"] != "extra":
            flash(request, "Убрать можно только лишнюю строку.", "err")
            return redirect(f"/stock-takes/{take_id}")
        await crm.delete_take_item(take_id, item_id)
        flash(request, "Лишняя строка убрана.")
        return redirect(f"/stock-takes/{take_id}")

    @app.post("/stock-takes/{take_id}/mark-all")
    async def stock_take_mark_all(request: Request, take_id: int) -> Response:
        take = await crm.stock_take(take_id)
        if take is None:
            return render(request, "missing.html", status_code=404, what="Пересчёт")
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        if not logic.take_is_open(take):
            flash(request, "Пересчёт закрыт.", "err")
            return redirect(f"/stock-takes/{take_id}")
        data = await form(request)
        state = "expected" if (data.get("state") or "") == "expected" else "found"
        hit = await crm.mark_take_all(take_id, state=state)
        flash(request, f"Отмечено строк: {hit}." if state == "found"
              else f"Снято отметок: {hit}.")
        return redirect(f"/stock-takes/{take_id}")

    @app.post("/stock-takes/{take_id}/close")
    async def stock_take_close(request: Request, take_id: int) -> Response:
        take = await crm.stock_take(take_id)
        if take is None:
            return render(request, "missing.html", status_code=404, what="Пересчёт")
        if not may_edit(request, "bikes"):
            return denied(request, "bikes")
        data = await form(request)
        try:
            result = await service.finish_stock_take(
                crm, take, by=who(request),
                lose_missing=bool(data.get("lose_missing")),
                return_found=bool(data.get("return_found")))
        except service.ServiceError as exc:
            flash(request, str(exc), "err")
            return redirect(f"/stock-takes/{take_id}")
        parts = [f"нашли {result['found']} из {result['total']}"]
        if result["missing"]:
            parts.append(f"не нашли {result['missing']}")
        if result["lost"]:
            parts.append(f"переведено в «Утерян»: {result['lost']}")
        if result["returned"]:
            parts.append(f"вернулось в парк: {result['returned']}")
        flash(request, "Пересчёт закрыт: " + ", ".join(parts) + ".")
        return redirect(f"/stock-takes/{take_id}")

    # ─────────────────────── импорт таблицы ───────────────────────

    # Импорт по одному: разбор xlsx на пределе распаковки - ~670 МБ, два
    # разом упираются в mem_limit панели, и OOM роняет все открытые
    # запросы вместе с недописанными импортами. Импорт - дело редкое,
    # второму подождать минуту дешевле. На app.state - чтобы видели тесты.
    app.state.import_lock = asyncio.Lock()

    @app.get("/import")
    async def import_page(request: Request) -> Response:
        return render(request, "import.html", report=None, applied=False)

    @app.post("/import")
    async def import_run(request: Request) -> Response:
        data = await request.form()
        upload = data.get("file")
        apply = data.get("apply") == "1"
        if upload is None or isinstance(upload, str) or not upload.filename:
            flash(request, "Выберите файл таблицы (.xlsx).", "err")
            return redirect("/import")
        if not upload.filename.lower().endswith(".xlsx"):
            flash(request, "Нужна таблица Excel в формате .xlsx.", "err")
            return redirect("/import")
        content = await upload.read(IMPORT_MAX_BYTES + 1)
        await upload.close()
        if len(content) > IMPORT_MAX_BYTES:
            flash(request, "Файл больше 20 МБ - это не учётная таблица.", "err")
            return redirect("/import")
        lock = app.state.import_lock
        if lock.locked():
            flash(request, "Сейчас идёт другой импорт - загрузите файл, когда он "
                           "закончится.", "err")
            return redirect("/import")
        try:
            async with lock:
                plan, done = await import_xlsx.run(crm, content, apply=apply,
                                                   by=who(request))
        except import_xlsx.ImportError_ as e:
            flash(request, str(e), "err")
            return redirect("/import")
        except Exception:                                  # noqa: BLE001
            log.exception("импорт таблицы %s не удался", upload.filename)
            flash(request, "Импорт прерван ошибкой; что успело записаться - в базе, "
                           "повторная загрузка пропустит уже добавленное. "
                           "Подробности в логе панели.", "err")
            return redirect("/import")
        if apply:
            flash(request, f"Записано: велосипедов {done['bikes']}, клиентов {done['clients']}, "
                           f"аренд {done['rentals']}.")
        return render(request, "import.html", report=import_xlsx.report_text(plan, done),
                      applied=apply, filename=upload.filename)

    return app


async def ensure_admin(crm: Any, cfg: WebConfig) -> str | None:
    """Первый администратор при пустой таблице сотрудников.

    Пароль - из секрета CRM_ADMIN_PASSWORD; если его нет, генерируется
    и возвращается вызывающему, чтобы тот показал его в логе один раз.
    """
    if await crm.staff_count() > 0:
        return None
    password = cfg.admin_password or logic.generate_password()
    owner = await crm.access_profile_by_code("owner")
    # Имя - «Владелец», как роль: «Администратор» здесь читался бы как роль
    # точки, и в меню стояло бы «Администратор · Владелец».
    await crm.create_staff(cfg.admin_login, logic.hash_password(password),
                           "Владелец", "admin", owner["id"] if owner else None)
    return None if cfg.admin_password else password

