"""Обход панели на засеянном демо: каждая страница открывается.

Покупатель франшизы входит под демо-логином и нажимает всё подряд, поэтому
проверка - не выборка страниц, а обход: от всех GET-маршрутов панели и
карточки каждого вида по ссылкам, которые страницы показывают сами. Всё,
что ответило не 200, - провал с адресом и трассировкой. GET-маршрут панели,
который обход не встретил, - тоже провал: новый раздел без демо-данных
должен ронять этот тест, а не показ покупателю.

Панель собрана так же, как в python -m app.demo: runtime.demo_config (без
бота, банка и хука), bot=None, настоящий Database для bot.users, ключ
«Входящих» от секрета панели. Нужен pgserver, как в test_crm_pg; без него
набор пропускается. Пояс - Europe/Moscow на время набора (test_audit).
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import os
import re
import sys
import tempfile
import time
import traceback
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import asyncpg
    import pgserver
    from httpx import ASGITransport, AsyncClient

    from app.crm import logic
    from app.crm.db import CrmDB
    from app.db import Database, _init_connection
    from app.demo import runtime
    from app.demo.world import MSK
    from app.web.app import create_app
    from app.web.config import WebConfig
    HAVE_PG = True
except ImportError:                                    # pragma: no cover
    HAVE_PG = False

SECRET = "demo-crawl-secret"
HREF = re.compile(r'\bhref="([/?][^"#]*)"')
# Строка «пусто» в таблице: так шаблоны панели говорят, что строк нет.
EMPTY_ROW = re.compile(r'<tr>\s*<td colspan="\d+" class="muted"[^>]*>\s*([^<]+?)\s*</td>\s*</tr>')
# Следы данных, которых шаблон не ждал: None из Python, непрочитанная
# переписка, неподставленный шаблон.
ODD = re.compile(r"\bNone\b|\bnan\b|\[текст зашифрован|(?<!<code>)\{\{|\bundefined\b")

# Файлы, которых в демо нет по устройству стенда: договор пишет бот (его
# у демо нет), снимки сверки и свои шаблоны демо не сохраняет, пакет на
# подпись собран без файлов. Эти маршруты отвечают честным 404 «не
# найден» - обход проверяет именно его, а не 500.
FILES_404 = {
    "/clients/{client_id}/contract": "договор - файл бота, бота у демо нет",
    "/bikes/{bike_id}/photo/{field}": "снимки сверки в демо не сохраняются",
    "/batteries/{battery_id}/photo/{field}": "снимки сверки в демо не сохраняются",
    "/signings/{request_id}/doc/{index}": "пакет на подпись в демо без файлов",
    "/sign/{token}/doc/{index}": "пакет на подпись в демо без файлов",
    "/documents/mine/{template_id}": "своих шаблонов в демо нет: загрузка закрыта",
    "/documents/marks/{kind}": "подписи и печати в демо нет: загрузка закрыта",
    "/documents/ours/{kind}": "наши шаблоны - с реквизитами настоящего ИП",
}
# Отчёты «с начала месяца»: первого числа их период - один день, и пусты
# они честно. Раздел проверяется за 30 дней, иначе тест зависел бы от
# календаря и падал бы каждое первое число.
MONTH_TO_DATE = ("/reports/techs", "/reports/model-parts", "/reports/spend",
                 "/reports/referrals")
# Маршруты, которые обход по ссылкам не открывает, - у каждого своя
# проверка ниже: /issue/docs пишет заявку на подпись прямо на GET, /sign/
# пишет «открыто» в протокол подписи и открыт без входа.
OWN_CHECK = ("/issue/docs", "/sign/")
# Параметры, у которых значение - свободный ввод или дата: для обхода
# все их значения одинаковы, иначе «прошлый месяц» уводил бы в 2001 год.
FREE = {"q", "dir", "next", "since", "until", "month", "started_on", "phone", "promo",
        "range"}

# Карточка каждого вида: адрес и запрос, дающий по одному id на каждую
# разновидность строки. Пустой ответ - провал: вид в демо не засеян.
DETAILS: tuple[tuple[str, str, str], ...] = (
    ("клиент по статусу", "/clients/{}",
     "select distinct on (status) id from crm.clients order by status, id"),
    ("клиент в розыске", "/clients/{}",
     "select client_id from crm.rentals where status = 'active' "
     "and search_at is not null order by id limit 1"),
    ("должник", "/clients/{}",
     "select client_id from crm.ledger group by client_id having sum(amount) < 0 "
     "order by client_id limit 1"),
    ("долг без аренды", "/clients/{}",
     "select l.client_id from crm.ledger l group by l.client_id "
     "having sum(l.amount) < 0 and not exists (select 1 from crm.rentals r "
     "where r.client_id = l.client_id and r.status = 'active') "
     "order by l.client_id limit 1"),
    ("сторонний ремонт", "/clients/{}",
     "select client_id from crm.work_orders where bike_id is null "
     "and client_id is not null order by id limit 1"),
    ("агент приглашений", "/clients/{}",
     "select agent_id from crm.referrals where status = 'paid' order by id limit 1"),
    ("открытая заявка на аренду", "/clients/{}",
     "select client_id from crm.bookings where status = 'new' order by id limit 1"),
    ("карта для автосписания", "/clients/{}",
     "select client_id from crm.card_tokens order by id limit 1"),
    ("подписи по статусу", "/clients/{}",
     "select distinct on (status) client_id from crm.sign_requests order by status, id"),
    ("клиент без аренд", "/clients/{}",
     "select c.id from crm.clients c where not exists (select 1 from crm.rentals r "
     "where r.client_id = c.id) and not exists (select 1 from crm.work_orders o "
     "where o.client_id = c.id) order by c.id desc limit 1"),
    ("аренда по виду", "/rentals/{}",
     "select distinct on (status, search_at is null, intent) id from crm.rentals "
     "order by status, search_at is null, intent, id"),
    ("аренда с заменой", "/rentals/{}",
     "select rental_id from crm.rental_bikes where reason <> 'Выдача' "
     "order by id limit 2"),
    ("аренда с доп. аккумулятором", "/rentals/{}",
     "select distinct on (removed_at is null) rental_id from crm.rental_extras "
     "order by removed_at is null, id"),
    ("потерянный велосипед", "/rentals/{}",
     "select r.id from crm.rentals r join crm.bikes b on b.id = r.bike_id "
     "where b.status = 'lost' and r.status = 'closed' order by r.id limit 1"),
    ("аренда с акцией", "/rentals/{}",
     "select rental_id from crm.bonuses where kind = 'promo' order by id limit 1"),
    ("аренда в группе точек", "/rentals/{}",
     "select rental_id from crm.ops_reports where rental_id is not null "
     "order by id limit 1"),
    ("аренда с подписью", "/rentals/{}",
     "select rental_id from crm.sign_requests order by id limit 1"),
    ("велосипед по статусу", "/bikes/{}",
     "select distinct on (status) id from crm.bikes order by status, id"),
    ("велосипед с нарядом", "/bikes/{}",
     "select distinct on (status) bike_id from crm.work_orders "
     "where bike_id is not null order by status, id"),
    ("подменный", "/bikes/{}", "select id from crm.bikes where spare order by id limit 1"),
    ("велосипед с блокировкой", "/bikes/{}",
     "select bike_id from crm.trackers where blocked order by id limit 1"),
    ("батарея по статусу", "/batteries/{}",
     "select distinct on (status) id from crm.batteries order by status, id"),
    ("трекер по тревоге", "/trackers/{}",
     "select distinct on (state, handled_at is null) tracker_id from crm.tracker_alerts "
     "order by state, handled_at is null, id"),
    ("трекер по виду", "/trackers/{}",
     "select distinct on (active, blocked) id from crm.trackers "
     "order by active, blocked, id"),
    ("трек за вчера", "/trackers/{}?range=yesterday",
     "select id from crm.trackers where active order by id limit 1"),
    ("трек за неделю", "/trackers/{}?range=week",
     "select id from crm.trackers where active order by id limit 1"),
    ("наряд по виду", "/orders/{}",
     "select distinct on (status, payer, bike_id is null) id from crm.work_orders "
     "order by status, payer, bike_id is null, id"),
    ("смета", "/orders/{}",
     "select distinct on (approved_at is null, declined_at is null) id "
     "from crm.work_orders where estimate_sent_at is not null "
     "order by approved_at is null, declined_at is null, id"),
    ("наряд со счётом", "/orders/{}",
     "select work_order_id from crm.pay_orders where work_order_id is not null"),
    ("наряды велосипеда", "/orders?bike={}",
     "select bike_id from crm.work_orders where bike_id is not null "
     "and status not in ('done', 'cancelled') order by id limit 1"),
    ("новый наряд на велосипед", "/orders/new?bike={}",
     "select id from crm.bikes where status = 'available' order by id limit 1"),
    ("запчасть", "/parts/{}", "select id from crm.parts order by id"),
    ("смена по точке", "/cash/{}",
     "select distinct on (location, status) id from crm.cash_shifts "
     "order by location, status, id desc"),
    ("смена с расхождением", "/cash/{}",
     "select id from crm.cash_shifts where diff <> 0 order by id limit 1"),
    ("счёт по статусу", "/payments/{}",
     "select distinct on (status, kind) id from crm.pay_orders "
     "order by status, kind, id"),
    ("рассылка", "/mailing/{}", "select id from crm.campaigns order by id"),
    ("акция", "/promos/{}", "select id from crm.promos order by id"),
    ("подпись по статусу", "/signings/{}",
     "select distinct on (status) id from crm.sign_requests order by status, id"),
    ("пересчёт", "/stock-takes/{}", "select id from crm.stock_takes order by id"),
    ("точка", "/reports/points/{}", "select id from crm.locations order by id"),
    ("обращение", "/inbox/{}", "select id from crm.inbox_threads order by id"),
    ("профиль доступа", "/profiles/{}", "select id from crm.access_profiles order by id"),
    ("выдача: клиент с арендой", "/issue?client={}",
     "select client_id from crm.rentals where status = 'active' order by id limit 1"),
    ("выдача: с велосипеда", "/issue?bike={}",
     "select id from crm.bikes where status = 'available' order by id limit 1"),
    ("аренда вручную", "/rentals/new?client={}",
     "select c.id from crm.clients c where c.status = 'active' and not exists "
     "(select 1 from crm.rentals r where r.client_id = c.id and r.status = 'active') "
     "order by c.id limit 1"),
)


def _route_regex(path: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(path)) + "$")


def signature(url: str) -> tuple:
    """Вид адреса для обхода: id в пути и числа в параметрах - одно и то
    же, сортировка и страницы - по одному разу на список."""
    parts = urlsplit(url)
    path = re.sub(r"/\d+(?=/|$)", "/{id}", parts.path)
    params = parse_qsl(parts.query, keep_blank_values=True)
    keys = {k for k, _ in params}
    for special in ("sort", "page", "rows"):
        if special in keys:
            value = dict(params)[special] if special == "sort" else "#"
            return path, ((special, value),)
    norm = set()
    for key, value in params:
        if key in FREE:
            value = "*"
        elif key == "location" and value not in ("", "none"):
            value = "*"
        else:
            value = re.sub(r"\d+", "#", value)
        norm.add((key, value))
    return path, tuple(sorted(norm))


@dataclasses.dataclass
class Crawl:
    """Итог обхода: ответ на каждый адрес, провалы и строки «пусто»."""
    status: dict[str, int] = dataclasses.field(default_factory=dict)
    failures: list[str] = dataclasses.field(default_factory=list)
    empty: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    odd: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    seconds: float = 0.0


async def crawl(client: AsyncClient, start: list[str], *, follow: bool = True,
                expect: dict[str, int] | None = None, limit: int = 3000) -> Crawl:
    """Открыть start целиком и дальше по ссылкам страниц - по одному адресу
    каждого вида. Ответ не 200 (или не то, что ждали по expect) и
    исключение - провал с адресом и хвостом трассировки."""
    expect = expect or {}
    result = Crawl()
    seen_kind: set[tuple] = set()
    queue: list[tuple[str, bool]] = [(url, True) for url in start]
    started = time.monotonic()
    while queue and len(result.status) < limit:
        url, forced = queue.pop(0)
        if url in result.status:
            continue
        kind = signature(url)
        if not forced and kind in seen_kind:
            continue
        seen_kind.add(kind)
        try:
            r = await client.get(url)
        except Exception:                               # noqa: BLE001
            tail = "".join(traceback.format_exc().splitlines(keepends=True)[-6:])
            result.status[url] = 500
            result.failures.append(f"{url}: исключение\n{tail}")
            continue
        result.status[url] = r.status_code
        wanted = expect.get(url, 200)
        if r.status_code != wanted:
            result.failures.append(f"{url}: {r.status_code}, ждали {wanted} "
                                   f"{r.headers.get('location') or ''}")
            continue
        if not r.headers.get("content-type", "").startswith("text/html"):
            continue
        text = r.text
        if rows := EMPTY_ROW.findall(text):
            result.empty[url] = [row.strip() for row in rows]
        for m in ODD.finditer(text):
            result.odd.setdefault(url, []).append(text[max(0, m.start() - 80):m.end() + 40])
        if not follow:
            continue
        base = urlsplit(url).path
        for href in HREF.findall(text):
            href = href.replace("&amp;", "&")
            if href.startswith("?"):
                href = base + href
            if href.startswith(("/static", "/logout", *OWN_CHECK)) or "://" in href:
                continue
            if href not in result.status:
                queue.append((href, False))
    result.seconds = time.monotonic() - started
    return result


@unittest.skipUnless(HAVE_PG, "pgserver, asyncpg или httpx не установлены")
class TestDemoCrawl(unittest.IsolatedAsyncioTestCase):
    """Один сброс на набор. Обход пишет в базу только там, где это делает
    сама панель на GET (/issue/docs, /sign/), и это проверено отдельно."""

    @classmethod
    def setUpClass(cls):
        cls.tz_before = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Moscow"
        time.tzset()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.tmp.name)
        files = Path(cls.tmp.name) / "files"
        # Конфиг - тот же, что у python -m app.demo; каталоги - во временном,
        # чтобы /issue/docs не писал договоры в /tmp машины тестов.
        cls.cfg = dataclasses.replace(
            runtime.demo_config(WebConfig(pg={}, secret=SECRET, admin_login="admin",
                                          admin_password="", bot_token="",
                                          storage_dir=files, port=8080,
                                          remind_before_days=2)),
            storage_dir=files / "kyc", bike_photo_dir=files / "bikes",
            doc_dir=files / "doctemplates")
        cls.now = datetime.now(MSK).replace(microsecond=0)
        cls.today = cls.now.date()
        started = time.monotonic()
        cls.summary = asyncio.run(cls._reset())
        cls.seconds = time.monotonic() - started

    @classmethod
    async def _reset(cls):
        """Сброс тем же путём, что у процесса демо: runtime.reset передаёт
        сиду секрет панели, и «Входящие» читаются ключом из конфига."""
        pool = await asyncpg.create_pool(cls.pg.get_uri(), min_size=1, max_size=2,
                                         init=_init_connection)
        try:
            return await runtime.reset(pool, cls.cfg, now=cls.now)
        finally:
            await pool.close()

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()
        cls.tmp.cleanup()
        if cls.tz_before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = cls.tz_before
        time.tzset()

    async def asyncSetUp(self):
        # Отладочный цикл набора ругается на каждую страницу дольше 0,1 с;
        # тяжёлые страницы (трекеры, отчёты) - норма, а не зависание.
        asyncio.get_running_loop().slow_callback_duration = 5
        self.pool = await asyncpg.create_pool(self.pg.get_uri(), min_size=1, max_size=4,
                                              init=_init_connection)
        self.crm = CrmDB(self.pool)
        # bot=None и настоящий Database для bot.users - как в app.demo.
        self.app = create_app(crm=self.crm, db=Database(self.pool), cfg=self.cfg,
                              bot=None)
        # Обход - сотни запросов в секунду с одного адреса: ровно то, от
        # чего предел демо защищает. Сам предел проверен в test_demo_mode.
        self.assertIsNotNone(self.app.state.demo_limits)
        self.app.state.demo_limits = None

    async def asyncTearDown(self):
        await self.pool.close()

    def client(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test",
                           follow_redirects=False)

    async def login(self, client: AsyncClient, login: str) -> str:
        r = await client.post("/login", data={"login": login, "password": "demo"})
        self.assertEqual(r.status_code, 303, f"вход {login}: {r.status_code}")
        return r.headers["location"]

    def routes(self) -> list[str]:
        return sorted({r.path for r in self.app.routes
                       if "GET" in (getattr(r, "methods", None) or ())})

    async def details(self) -> list[str]:
        pages: list[str] = []
        empty: list[str] = []
        for what, template, sql in DETAILS:
            ids = [r[0] for r in await self.pool.fetch(sql) if r[0] is not None]
            if not ids:
                empty.append(what)
            pages += [template.format(i) for i in ids]
        self.assertEqual(empty, [], "в демо не засеяно")
        return pages

    async def variants(self) -> list[str]:
        """Параметры, которые меняют ветку страницы, а ссылкой не везде
        достижимы (формы фильтров): по одному значению каждого."""
        prev = (self.today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        since = (self.today - timedelta(days=60)).isoformat()
        until = self.today.isoformat()
        span = urlencode({"since": since, "until": until})
        pages = [f"/?month={prev}", "/?chart=cumulative", f"/service?month={prev}",
                 f"/reports/points?month={prev}", f"/reports/points?{span}",
                 f"/reports/points/none?month={prev}", f"/finance?{span}",
                 f"/reports?{span}", f"/reports/payback?{span}",
                 f"/reports/referrals?{span}", f"/reports/techs?{span}",
                 f"/reports/spend?{span}", "/alerts?view=all", "/ops?bad=1",
                 "/batteries?view=search", "/bikes?location=none",
                 "/rentals?status=all", "/rentals?status=closed", "/inbox?tab=all",
                 "/orders?location=none", "/orders?payer=client", "/bank?status=all",
                 "/clients?status=blacklist", "/map?q=МБ-1"]
        pages += [f"/rentals?view={v}" for v in ("debt", "overdue", "search", "repair",
                                                "nobike")]
        pages += [f"/bikes?status={s}" for s in logic.BIKE_STATUSES]
        pages += [f"/orders?status={s}" for s in ("new", "in_work", "approve", "waiting",
                                                  "done", "cancelled")]
        pages += [f"/inbox?tab={t}" for t in ("new", "work", "done", "spam")]
        pages += [f"/inbox?tab=all&channel={c}" for c in logic.INBOX_CHANNELS]
        pages += [f"/assets?tab={t}" for t in ("all", "worn", "written_off", "live")]
        pages += [f"/bank?status={s}" for s in ("new", "matched", "ignored")]
        pages += [f"/payments?status={s}" for s in ("new", "sent", "paid", "failed",
                                                    "cancelled")]
        pages += [f"/parts/moves?kind={k}" for k in ("receipt", "order", "issue",
                                                     "write_off", "count")]
        pages += [f"/ops?kind={k}" for k in ("fix", "swap", "return", "daily")]
        pages += [f"/promos/new?kind={k}" for k in logic.PROMO_KINDS]
        pages += [f"{v['section']}?{v['query']}" for v in await self.pool.fetch(
            "select section, query from crm.saved_views order by id")]
        pages += await self.issue_steps()
        return pages

    async def issue_steps(self) -> list[str]:
        """Мастер выдачи до последнего шага: свободный клиент, тариф
        модели свободного велосипеда и сам велосипед."""
        client = await self.pool.fetchval(
            "select c.id from crm.clients c where c.status = 'active' and not exists "
            "(select 1 from crm.rentals r where r.client_id = c.id "
            "and r.status = 'active') order by c.id limit 1")
        bike = await self.pool.fetchrow(
            "select id, model from crm.bikes where status = 'available' "
            "order by id limit 1")
        tariff = await self.pool.fetchval(
            "select id from crm.tariffs where active and kind = 'bike' and model = $1 "
            "order by period_days limit 1", bike["model"])
        booking = await self.pool.fetchrow(
            "select id, client_id from crm.bookings where status = 'new' "
            "order by id limit 1")
        code = await self.pool.fetchval(
            "select code from crm.promos where code is not null order by id limit 1")
        model = urlencode({"model": bike["model"]})
        base = f"/issue?client={client}&tariff={tariff}&{model}"
        step4 = f"{base}&bike={bike['id']}"
        # Промокод - настоящий и чужой: у каждого своя ветка предпросмотра.
        return [f"/issue?client={client}", base, step4,
                f"{step4}&{urlencode({'promo': code or 'ДЕМО'})}", f"{step4}&promo=NOPE",
                f"/issue?client={booking['client_id']}&booking={booking['id']}"]

    async def file_pages(self) -> dict[str, int]:
        """По адресу на каждый маршрут FILES_404 и выгрузка с чужим
        расширением: всё это в демо - честный 404, а не 500."""
        one = self.pool.fetchval
        client = await one("select id from crm.clients where tg_id is not null "
                           "order by id limit 1")
        bike = await one("select id from crm.bikes order by id limit 1")
        battery = await one("select id from crm.batteries order by id limit 1")
        signing = await one("select id from crm.sign_requests where status = 'signed' "
                            "order by id limit 1")
        token = await one("select token from crm.sign_requests where status = 'signed' "
                          "order by id limit 1")
        return {f"/clients/{client}/contract": 404, f"/bikes/{bike}/photo/frame_no": 404,
                f"/batteries/{battery}/photo/code": 404,
                f"/signings/{signing}/doc/0": 404, f"/sign/{token}/doc/0": 404,
                "/documents/mine/1": 404, "/documents/marks/stamp": 404,
                "/documents/ours/contract": 404, "/clients.pdf": 404}

    # ─────────────────────────── владелец ───────────────────────────

    async def test_owner_opens_every_page(self):
        """Все GET-маршруты, карточка каждого вида, выгрузки и всё, куда
        ведут ссылки, - под владельцем демо. Только 200 и честные 404."""
        routes = self.routes()
        static = [r for r in routes if "{" not in r and r != "/login"
                  and not r.startswith(OWN_CHECK)]
        exports = [r.replace("{ext}", ext) for r in routes if r.endswith(".{ext}")
                   for ext in ("xlsx", "csv")]
        files = await self.file_pages()
        start = [*static, *exports, *await self.details(), *await self.variants(),
                 *files]
        async with self.client() as client:
            self.assertEqual(await self.login(client, "demo"), "/")
            r = await client.get("/login")
            self.assertEqual((r.status_code, r.headers.get("location")), (303, "/"),
                             "вошедшего вход уводит на сводку")
            # Шаблон акции без вида - честный редирект на выбор шаблона.
            result = await crawl(client, start, expect={**files, "/promos/new": 303})
        self.assertEqual(result.failures, [], "\n".join(result.failures))
        self.assertEqual(result.odd, {}, "следы непредусмотренных данных")

        # Каждый GET-маршрут встретился: иначе обход о нём просто не знает.
        visited = [urlsplit(u).path for u in result.status]
        missed = [r for r in routes if r != "/login" and not r.startswith(OWN_CHECK)
                  and not any(_route_regex(r).match(p) for p in visited)]
        self.assertEqual(missed, [], "маршруты панели без обхода")
        # Список файловых маршрутов живой: каждый есть в панели и открыт
        # ровно одним адресом из file_pages.
        self.assertEqual(set(FILES_404) - set(routes), set(), "маршрута больше нет")
        self.assertEqual(sorted(t for t in FILES_404 for url in files
                                if _route_regex(t).match(urlsplit(url).path)),
                         sorted(FILES_404))
        self.assertLess(result.seconds, 60, f"обход владельца: {len(result.status)} "
                                            f"адресов за {result.seconds:.0f} с")

    async def test_sections_are_not_empty(self):
        """Пустых разделов в демо нет: ни одна главная страница раздела не
        показывает строку «пусто» - ни в списке, ни в его блоках (заявки на
        зачисление, кандидаты в розыск, потребности склада...)."""
        span = urlencode({"since": (self.today - timedelta(days=30)).isoformat(),
                          "until": self.today.isoformat()})
        pages = sorted({f"{r}?{span}" if r in MONTH_TO_DATE else r
                        for r in self.routes() if "{" not in r
                        and r not in ("/login", "/healthz", "/me", "/promos/new")
                        and not r.startswith(OWN_CHECK)})
        async with self.client() as client:
            await self.login(client, "demo")
            result = await crawl(client, pages, follow=False)
        self.assertEqual(result.failures, [], "\n".join(result.failures))
        self.assertEqual(result.empty, {}, "разделы демо без строк")

    async def test_exports_are_marked(self):
        """Каждая выгрузка демо помечена: имя файла с demo- и строка
        «Демо-версия» над шапкой - и в xlsx, и в csv."""
        from openpyxl import load_workbook
        exports = [r.replace("{ext}", ext) for r in self.routes() if r.endswith(".{ext}")
                   for ext in ("xlsx", "csv")]
        self.assertGreater(len(exports), 20)
        async with self.client() as client:
            await self.login(client, "demo")
            for url in exports:
                r = await client.get(url)
                self.assertEqual(r.status_code, 200, url)
                self.assertRegex(r.headers["content-disposition"], r'filename="demo-', url)
                if url.endswith(".xlsx"):
                    first = load_workbook(io.BytesIO(r.content)).active["A1"].value
                else:
                    first = r.content.decode("utf-8-sig").splitlines()[0]
                self.assertTrue(str(first).startswith("Демо-версия"), url)

    # ─────────────────────────── роли ───────────────────────────

    async def test_operator_and_mechanic_open_their_pages(self):
        """Оператор и механик: вход ведёт на их главную, и всё, куда ведут
        их ссылки, открывается - никаких «нет доступа» и 500."""
        for login in ("operator", "mechanic"):
            async with self.client() as client:
                home = await self.login(client, login)
                staff = await self.crm.staff_by_login(login)
                self.assertEqual(home, logic.home_for(staff), login)
                result = await crawl(client, [home, "/me"], limit=1200)
            self.assertEqual(result.failures, [], f"{login}:\n" + "\n".join(
                result.failures))
            self.assertEqual(result.odd, {}, login)
            self.assertGreater(len(result.status), 50, login)

    # ─────────────────────────── без входа ───────────────────────────

    async def test_public_pages(self):
        async with self.client() as guest:
            r = await guest.get("/login")
            self.assertEqual(r.status_code, 200)
            self.assertIn("Демо-версия:", r.text)
            for login in ("demo", "operator", "mechanic"):
                self.assertIn(login, r.text)
            r = await guest.get("/robots.txt")
            self.assertEqual((r.status_code, r.text), (200, "User-agent: *\nDisallow: /\n"))
            self.assertEqual((await guest.get("/healthz")).status_code, 200)
            r = await guest.get("/rentals")
            self.assertEqual(r.status_code, 303)
            self.assertTrue(r.headers["location"].startswith("/login?next="))
            # Ссылка клиента на подпись: открывается у каждой заявки, текст
            # соглашения - у живых. Живы подписанная (пакет остаётся
            # клиенту) и ждущая кода - она обязана дожить до ночного сброса;
            # «new» в демо - намеренно истёкшая, отменённая закрыта.
            for row in await self.pool.fetch(
                    "select distinct on (status) token, status "
                    "from crm.sign_requests order by status, id"):
                r = await guest.get(f"/sign/{row['token']}")
                self.assertEqual(r.status_code, 200, row["status"])
                r = await guest.get(f"/sign/{row['token']}/agreement")
                self.assertEqual(r.status_code,
                                 200 if row["status"] in ("signed", "code") else 404,
                                 row["status"])

    async def test_issue_docs_step(self):
        """Шаг «документы» мастера выдачи: у аренды с заявкой - та же
        заявка, без второй; у аренды без неё - новая, и страница 200."""
        count = self.pool.fetchval
        signed = await count("select rental_id from crm.sign_requests "
                             "where status = 'signed' order by id limit 1")
        fresh = await count(
            "select r.id from crm.rentals r where r.status = 'active' and not exists "
            "(select 1 from crm.sign_requests s where s.rental_id = r.id) "
            "order by r.id desc limit 1")
        before = await count("select count(*) from crm.sign_requests")
        async with self.client() as client:
            await self.login(client, "demo")
            r = await client.get(f"/issue/docs?rental={signed}")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(await count("select count(*) from crm.sign_requests"), before)
            r = await client.get(f"/issue/docs?rental={fresh}")
            self.assertEqual(r.status_code, 200)
        self.assertEqual(await count("select count(*) from crm.sign_requests "
                                     "where rental_id = $1", fresh), 1)

    def test_seed_is_quick_and_clean(self):
        self.assertLess(self.seconds, 60)
        self.assertEqual(set(self.summary["integrity"]), {"debt_without_rental"})

if __name__ == "__main__":
    unittest.main()
