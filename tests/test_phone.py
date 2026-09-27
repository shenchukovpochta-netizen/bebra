"""Панель с телефона: списки карточками и значок на главный экран.

На 390 пикселях таблица в десять колонок - это прокрутка вбок, в которой
номер велосипеда уезжает от его статуса. Списки остаются таблицами, а на
узком экране строка становится карточкой: у каждой ячейки data-label с
именем колонки, подпись рисует CSS. Здесь стерегут три вещи: подпись есть
у каждой ячейки этих списков и совпадает со своей колонкой; правила
карточек живут только в телефонном @media - компьютер не меняется;
манифест и значки на месте, открыты без входа, а service worker нет.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "app" / "web" / "templates"
STATIC = ROOT / "app" / "web" / "static"

try:
    import test_web as tw

    from app.crm import service
    from app.web import app as web_app
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

try:
    from app.demo import runtime
    HAVE_DEMO = HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_DEMO = False

try:
    from PIL import Image

    from app.web import icons
    HAVE_PIL = True
except ImportError:                                    # pragma: no cover
    HAVE_PIL = False

D = Decimal
# Шаблон - сколько в нём таблиц-карточек. «По точкам» - две: сами точки
# и три числа по месяцам.
CARD_TABLES = {"rentals.html": 1, "bikes.html": 1, "clients.html": 1, "orders.html": 1,
               "points.html": 2, "cash.html": 1, "finance.html": 1, "inbox.html": 1,
               "batteries.html": 1, "parts.html": 1}
TABLE = re.compile(r'<table class="cards">(.*?)</table>', re.S)
HEAD = re.compile(r"""\{\{\s*list\.th\('([^']*)'|<th\b[^>]*>(.*?)</th>""", re.S)
LABEL = re.compile(r'data-label="([^"]*)"')
# Открывающий тег ячейки: «>» внутри {% if a > b %} тег не закрывает.
CELL = re.compile(r"<td\b(?:\{%.*?%\}|\{\{.*?\}\}|[^>])*>", re.S)
ICON_SIZES = {"icon-192.png": 192, "icon-512.png": 512, "icon-maskable-512.png": 512,
              "apple-touch-icon.png": 180}


def plain(html: str) -> str:
    """Текст заголовка колонки: без Jinja и тегов, пробелы схлопнуты -
    «Парк<br>сейчас» в шапке и «Парк сейчас» в подписи одно и то же, а
    мягкий перенос («Выпол&shy;нено») - подсказка браузеру, не текст."""
    text = re.sub(r"\{[%#].*?[%#]\}", "", html, flags=re.S)
    text = text.replace("&shy;", "").replace("\u00ad", "")
    return " ".join(re.sub(r"<[^>]+>", " ", text).split())


class TestCardTemplates(unittest.TestCase):
    """Разбор исходников: каждая ячейка списка подписана своей колонкой."""

    def tables(self, name: str) -> list[str]:
        return TABLE.findall((TEMPLATES / name).read_text(encoding="utf-8"))

    def test_lists_are_marked_as_cards(self):
        for name, count in CARD_TABLES.items():
            with self.subTest(name):
                self.assertEqual(len(self.tables(name)), count)

    def test_every_cell_has_a_label(self):
        """Ячейка без подписи на телефоне - число без смысла. Без подписи
        только ячейки на всю ширину (colspan): «Пусто.» и итоговые строки."""
        for name in CARD_TABLES:
            for body in self.tables(name):
                for cell in CELL.findall(body):
                    with self.subTest(name, cell=cell):
                        self.assertRegex(cell, r'data-label="[^"]+"|colspan=')

    def test_labels_follow_the_header(self):
        """Подпись - имя своей колонки и в том же порядке: переставленная
        колонка без правки подписи назвала бы баланс просрочкой. Пустая
        шапка (колонка кнопок) подпись берёт свою."""
        for name in CARD_TABLES:
            for body in self.tables(name):
                if "rowspan" in body:
                    continue           # матрица по месяцам: подпись - точка и число
                head_row = re.search(r"<tr>(.*?)</tr>", body, re.S).group(1)
                head = [plain(a or b) for a, b in HEAD.findall(head_row)]
                labels = LABEL.findall(body[len(head_row):])
                with self.subTest(name):
                    self.assertGreaterEqual(len(head), 5)
                    self.assertGreaterEqual(len(labels), len(head))
                    for column, label in zip(head, labels, strict=False):
                        if column:
                            self.assertEqual(label, column)
                    named = {c for c in head if c}
                    extra = [x for x in labels[len(head):] if x not in named]
                    self.assertEqual(extra, [], "подпись итоговой строки - из шапки")

    def test_month_matrix_names_the_point(self):
        """В матрице «три числа по месяцам» колонка - это точка и число:
        карточка месяца без имени точки была бы столбиком процентов."""
        matrix = self.tables("points.html")[1]
        self.assertIn('data-label="Месяц"', matrix)
        self.assertIn('data-label="{{ point }} · простой"', matrix)
        self.assertIn('data-label="{{ point }} · чек"', matrix)


class TestCardStyles(unittest.TestCase):
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    def blocks(self) -> list[tuple[str, str]]:
        """Верхние блоки таблицы стилей: (заголовок, тело)."""
        out, depth, start, head = [], 0, 0, ""
        text = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    head, start = text[start:i].strip(), i + 1
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    out.append((head, text[start:i]))
                    start = i + 1
        return out

    def test_cards_only_on_phones(self):
        """Карточки - только в телефонном @media: на компьютере список
        остаётся таблицей, и ни одна его строка не меняет вид."""
        found = [head for head, body in self.blocks()
                 if "table.cards" in head or "table.cards" in body]
        self.assertTrue(found)
        for head in found:
            self.assertRegex(head, r"@media \(max-width:640px\)", head)

    def test_label_comes_from_the_cell(self):
        self.assertIn("content:attr(data-label)", self.css)

    def test_sorting_survives_on_the_phone(self):
        """Шапка со ссылками сортировки не прячется целиком: без неё на
        телефоне сортировать нечем."""
        self.assertIn("table.cards th:not(.opt):has(a){display:block", self.css)

    def test_no_scroll_hint_over_cards(self):
        """«Прокрутите вбок» под карточками - неправда."""
        self.assertIn(".table-wrap:has(>table.cards)~.scroll-hint{display:none}", self.css)


class CardParser(HTMLParser):
    """Строки таблиц-карточек отданной страницы: [(тег, атрибуты)]."""

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[tuple[str, dict]]]] = []
        self.depth = 0          # вложенность таблиц
        self.inside = 0         # на какой глубине открыта таблица-карточки

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.depth += 1
            if "cards" in (attrs.get("class") or "").split():
                self.tables.append([])
                self.inside = self.depth
            return
        if not self.tables or self.inside != self.depth:
            return
        if tag == "tr":
            self.tables[-1].append([])
        elif tag in ("td", "th") and self.tables[-1]:
            self.tables[-1][-1].append((tag, attrs))

    def handle_endtag(self, tag):
        if tag == "table":
            if self.inside == self.depth:
                self.inside = 0
            self.depth -= 1


@unittest.skipUnless(HAVE_WEB, "нет fastapi/starlette")
class TestCardPages(tw.WebCase if HAVE_WEB else unittest.TestCase):
    """Отданные страницы с данными: в каждой строке столько подписанных
    ячеек, сколько колонок в шапке, - включая колонки с деньгами, которые
    видны не всем."""

    def setUp(self):
        super().setUp()
        self.seed()
        self.login()
        crm = self.crm
        tw.run(service.open_rental(
            crm, client=tw.run(crm.client(self.client_id)),
            bike=tw.run(crm.bike(self.bike_id)), tariff=tw.run(crm.tariff(self.tariff_id)),
            started_on=date.today(), contract_no="АВ-1", by="test"))
        tw.run(crm.add_ledger(client_id=self.client_id, kind="payment", amount=D(3000),
                              method="cash", note="неделя", created_by="test"))
        tw.run(crm.create_shift(location=None, opening=D(0), note=None, by="test"))
        tw.run(crm.create_battery(by="test", code="AKB-1", status="available"))
        tw.run(crm.create_part(title="Колодки", node=None, unit="шт", cost=D(100),
                               price=D(300), min_stock=2, model=None, note=None))
        tw.run(crm.create_work_order(bike_id=None, payer="client", client_id=self.client_id,
                                     complaint="скрипит", object_note="самокат",
                                     tech_id=None, estimate=None, created_by="test"))
        tw.run(crm.inbox_record(channel="wa", origin="hook", ext_id="79990000001",
                                direction="in", name="Гость"))

    def rows_of(self, path: str) -> list[list[list[tuple[str, dict]]]]:
        parser = CardParser()
        parser.feed(self.get_ok(path))
        return parser.tables

    def test_rows_carry_one_label_per_column(self):
        for path in ("/rentals", "/bikes", "/clients", "/orders", "/cash", "/finance",
                     "/inbox", "/batteries", "/parts", "/reports/points"):
            tables = self.rows_of(path)
            with self.subTest(path):
                self.assertTrue(tables, "список не помечен как карточки")
                rows = tables[0]
                head = [a for tag, a in rows[0] if tag == "th"]
                body = [r for r in rows[1:] if r and not any("colspan" in a for _, a in r)]
                self.assertTrue(body, "в списке нет ни одной строки")
                for row in body:
                    self.assertEqual(len(row), len(head))
                    self.assertTrue(all(a.get("data-label") for _, a in row), row)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/starlette")
class TestManifest(tw.WebCase if HAVE_WEB else unittest.TestCase):
    """«На экран Домой»: манифест открыт без входа, значки на месте."""

    def manifest(self, client=None) -> dict:
        r = (client or self.client).get("/manifest.webmanifest")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("application/manifest+json"))
        return json.loads(r.text)

    def test_manifest_is_public_and_valid(self):
        data = self.manifest()                   # без входа: браузер шлёт его без cookie
        self.assertEqual(data["name"], "МАЙБАЙК CRM")
        self.assertEqual(data["short_name"], "МАЙБАЙК")
        self.assertEqual((data["start_url"], data["scope"], data["display"]),
                         ("/", "/", "standalone"))
        self.assertEqual(data["lang"], "ru")

    def test_colors_are_the_panel_header(self):
        """Полоса браузера и заставка - цвета тёмной шапки панели."""
        data = self.manifest()
        ink = re.search(r"--ink:(#[0-9A-Fa-f]{6})",
                        (STATIC / "style.css").read_text(encoding="utf-8")).group(1)
        self.assertEqual(data["theme_color"].upper(), ink.upper())
        self.assertEqual(data["background_color"].upper(), ink.upper())

    def test_icons_are_served_with_their_sizes(self):
        data = self.manifest()
        purposes = {(i["sizes"], i["purpose"]) for i in data["icons"]}
        self.assertLessEqual({("192x192", "any"), ("512x512", "any"),
                              ("512x512", "maskable")}, purposes)
        for icon in data["icons"]:
            self.assertRegex(icon["src"], r"^/static/[\w.-]+\.png\?v=[0-9a-f]{8}$")
            r = self.client.get(icon["src"])     # тоже без входа
            self.assertEqual(r.status_code, 200, icon["src"])
            self.assertEqual(r.headers["content-type"], "image/png")
            if HAVE_PIL:
                import io
                side = int(icon["sizes"].split("x")[0])
                self.assertEqual(Image.open(io.BytesIO(r.content)).size, (side, side))

    def test_page_head_links_the_app(self):
        page = self.get_ok("/login")
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', page)
        self.assertRegex(page, r'<link rel="apple-touch-icon" '
                               r'href="/static/apple-touch-icon\.png\?v=[0-9a-f]{8}">')
        for scheme in ("light", "dark"):
            self.assertRegex(page, rf'<meta name="theme-color" content="#[0-9A-Fa-f]{{6}}" '
                                   rf'media="\(prefers-color-scheme: {scheme}\)">')
        self.assertIn('<meta name="apple-mobile-web-app-capable" content="yes">', page)
        self.assertIn('<meta name="apple-mobile-web-app-title" content="МАЙБАЙК">', page)
        self.login()
        self.assertIn('<link rel="manifest"', self.get_ok("/rentals"))

    def test_no_service_worker(self):
        """Страницы с телефонами и долгами клиентов не должны оседать в
        кэше телефона: service worker панели не заводится вовсе."""
        files = [*TEMPLATES.glob("*.html"), *STATIC.glob("*.css"), *STATIC.glob("*.js"),
                 ROOT / "app" / "web" / "app.py"]
        for path in files:
            self.assertNotIn("serviceWorker", path.read_text(encoding="utf-8"), path.name)
        self.assertNotIn("serviceworker", json.dumps(self.manifest()).lower())

    def test_names(self):
        self.assertEqual(web_app.app_names("МАЙБАЙК"), ("МАЙБАЙК CRM", "МАЙБАЙК"))
        self.assertEqual(web_app.app_names("МАЙБАЙК · демо"),
                         ("МАЙБАЙК CRM · демо", "МАЙБАЙК демо"))
        # Подпись под значком телефон режет после ~12 знаков.
        self.assertLessEqual(len(web_app.app_names("МАЙБАЙК · демо")[1]), 12)


@unittest.skipUnless(HAVE_DEMO, "нет пакета демо")
class TestDemoManifest(tw.WebCase if HAVE_DEMO else unittest.TestCase):
    """Демо на главном экране рядом с боевой панелью не должно выглядеть
    как она: в имени - «демо»; предел запросов манифест не тратит."""

    def setUp(self):
        super().setUp()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        tmp = Path(holder.name)
        self.cfg = dataclasses.replace(runtime.demo_config(self.cfg),
                                       storage_dir=tmp / "kyc", bike_photo_dir=tmp / "bikes",
                                       doc_dir=tmp / "doctemplates")
        self.app = tw.create_app(crm=self.crm, db=self.db, cfg=self.cfg, bot=None)
        self.client = tw.TestClient(self.app, follow_redirects=False)

    def test_demo_name(self):
        data = json.loads(self.client.get("/manifest.webmanifest").text)
        self.assertEqual(data["name"], "МАЙБАЙК CRM · демо")
        self.assertEqual(data["short_name"], "МАЙБАЙК демо")
        page = self.client.get("/login").text
        self.assertIn('<meta name="apple-mobile-web-app-title" content="МАЙБАЙК демо">', page)
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', page)

    def test_manifest_spares_the_demo_limit(self):
        self.app.state.demo_limits = web_app.DemoLimits(rate=0.0, burst=1)
        self.assertEqual(self.client.get("/login").status_code, 200)
        self.assertEqual(self.client.get("/login").status_code, 429)
        self.assertEqual(self.client.get("/manifest.webmanifest").status_code, 200)
        self.assertEqual(self.client.get("/static/icon-192.png").status_code, 200)


@unittest.skipUnless(HAVE_PIL, "нет Pillow")
class TestIcons(unittest.TestCase):
    def test_files_and_sizes(self):
        for name, side in ICON_SIZES.items():
            with self.subTest(name), Image.open(STATIC / name) as image:
                self.assertEqual(image.size, (side, side))

    def test_full_bleed_icons_are_opaque(self):
        """maskable и значок iPhone - во весь край: прозрачное iOS залила бы
        чёрным, а Android вырезал бы из него дырявую форму."""
        for name in ("icon-maskable-512.png", "apple-touch-icon.png"):
            with Image.open(STATIC / name) as image:
                self.assertEqual(image.convert("RGBA").getextrema()[3], (255, 255), name)

    def test_maskable_bolt_inside_the_safe_zone(self):
        """Android режет maskable по кругу в 80 % стороны: молния вся
        внутри, иначе на круглом значке у неё срезаны концы."""
        with Image.open(STATIC / "icon-maskable-512.png") as source:
            image = source.convert("RGB")
        side = image.size[0]
        centre, radius = side / 2, side * 0.4
        pixels = image.load()
        orange = [(x, y) for x in range(0, side, 2) for y in range(0, side, 2)
                  if pixels[x, y][0] > 200 and pixels[x, y][2] < 100]
        self.assertTrue(orange)
        far = max(((x - centre) ** 2 + (y - centre) ** 2) ** 0.5 for x, y in orange)
        self.assertLess(far, radius)

    def test_files_match_the_generator(self):
        """Файлы в static нарисованы app/web/icons.py из молнии брендбука:
        поправили молнию или цвета - перерисуйте значки."""
        with tempfile.TemporaryDirectory() as tmp:
            for fresh in icons.render(Path(tmp)):
                with Image.open(STATIC / fresh.name) as a, Image.open(fresh) as b:
                    kept, new = a.convert("RGBA").tobytes(), b.convert("RGBA").tobytes()
                    self.assertEqual(a.size, b.size)
                # Средняя разница канала меньше двух из 255: сглаживание
                # краёв у разных версий Pillow чуть разное, фигура - та же.
                diff = sum(abs(x - y) for x, y in zip(kept, new, strict=True))
                self.assertLess(diff / len(kept), 2, fresh.name)


if __name__ == "__main__":
    unittest.main()
