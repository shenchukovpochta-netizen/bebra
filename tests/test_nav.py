"""Меню панели (app/web/nav.py): каждый пункт живой, горит нужный,
права режут то же, что страж маршрутов, счётчики не выдают чужого."""

from __future__ import annotations

import re
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_web as tw

    from app.web import nav
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

ROOT = Path(__file__).resolve().parent.parent


def staff(**sections) -> dict:
    return {"perms": {"sections": sections, "actions": {}}}


OWNER = {"perms": logic.BUILT_IN_PROFILES[0][2], "profile_code": "owner"}
MANAGER = {"perms": logic.BUILT_IN_PROFILES[1][2]}
TECH = {"perms": logic.BUILT_IN_PROFILES[2][2]}


def labels(menu: list[dict]) -> list[str]:
    out = []
    for g in menu:
        for it in g["items"]:
            out.append(it["label"])
            out.extend(c["label"] for c in it["children"])
    return out


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestTree(unittest.TestCase):
    def test_every_item_is_a_live_route_of_its_section(self):
        app_src = (ROOT / "app/web/app.py").read_text(encoding="utf-8")
        routes = set(re.findall(r'@app\.get\("([^"{]+)"\)', app_src))
        for item in nav.all_items():
            self.assertIn(item.href, routes, item.label)
            if item.children:
                continue
            code = logic.section_for(item.href)
            if item.section and code:
                self.assertEqual(code, item.section, f"{item.label}: права пункта и адреса")

    def test_icons_exist(self):
        svg = (ROOT / "app/web/static/icons.svg").read_text(encoding="utf-8")
        ids = set(re.findall(r'id="i-([a-z-]+)"', svg))
        self.assertTrue({i.icon for i in nav.all_items()} <= ids)
        for name in ("search", "chevron", "menu", "graduation"):
            self.assertIn(name, ids)

    def test_the_longest_address_wins(self):
        def on(path, who=OWNER):
            item = nav.active_item(path, who)
            return item.label if item else None
        self.assertEqual(on("/"), "Сводка")
        self.assertEqual(on("/parts"), "Остатки")
        self.assertEqual(on("/parts/12"), "Остатки")
        self.assertEqual(on("/parts/receipts"), "Приходы")
        self.assertEqual(on("/parts.xlsx"), "Остатки")
        self.assertEqual(on("/service"), "Рабочий стол")
        self.assertEqual(on("/orders/5"), "Наряды")
        self.assertEqual(on("/reports"), "Отчёты")
        self.assertEqual(on("/reports/techs"), "Отчёты")
        self.assertEqual(nav.trail("/reports/techs", OWNER)[0]["label"], "Сервис")
        self.assertEqual(on("/reports/spend"), "Расход")
        self.assertEqual(on("/reports/points"), "Отчёты")
        self.assertEqual(on("/bank"), "Касса")
        self.assertEqual(on("/bookings"), "Входящие")
        self.assertEqual(on("/trackers/3"), "Карта")
        self.assertEqual(on("/setup"), "Готовность")
        self.assertIsNone(on("/me"))
        self.assertIsNone(on("/partsx"), "префикс не цепляет чужой адрес")

    def test_rights_cut_the_menu(self):
        tech = labels(nav.menu(TECH, "/"))
        for label in ("Наряды", "Остатки", "Приходы", "Велосипеды", "Мои задачи"):
            self.assertIn(label, tech)
        for label in ("Клиенты", "Выдача", "Финансы", "Касса", "Сотрудники", "Роли",
                      "Настройки", "Входящие", "Франчайзи"):
            self.assertNotIn(label, tech, label)
        manager = labels(nav.menu(MANAGER, "/"))
        self.assertIn("Входящие", manager, "заявки и «Я оплатил» - его")
        self.assertIn("Расход", manager, "склад и отчёты он смотрит - расход тоже")
        only_reports = labels(nav.menu(staff(reports="view"), "/"))
        self.assertNotIn("Расход", only_reports, "без права на склад")
        self.assertNotIn("Склад", only_reports)
        self.assertEqual(nav.menu({"perms": {}}, "/"),
                         [{"title": "Главное", "items": [
                             {"label": "Мои задачи", "href": "/my", "icon": "square-check",
                              "on": False, "open": False, "children": [], "badge": 0}]}])

    def test_parent_opens_on_its_child(self):
        menu = nav.menu(OWNER, "/parts/write-offs")
        store = next(it for g in menu for it in g["items"] if it["label"] == "Склад")
        self.assertTrue(store["open"])
        self.assertFalse(store["on"], "родитель раскрыт, горит подпункт")
        self.assertEqual([c["label"] for c in store["children"] if c["on"]], ["Списания"])
        service = next(it for g in menu for it in g["items"] if it["label"] == "Сервис")
        self.assertFalse(service["open"])

    def test_badges_show_only_what_the_role_sees(self):
        counts = {"incoming": {"message": 5, "booking": 2, "claim": 1}, "alerts": 3,
                  "parts": 4, "orders": 2}
        def badge(who, label):
            for g in nav.menu(who, "/", counts):
                for it in g["items"]:
                    if it["label"] == label:
                        return it["badge"]
                    for c in it["children"]:
                        if c["label"] == label:
                            return c["badge"]
            return None
        self.assertEqual(badge(OWNER, "Входящие"), 8)
        self.assertEqual(badge(MANAGER, "Входящие"), 3, "переписка - только владельцу")
        self.assertEqual(badge(OWNER, "Тревоги"), 3)
        self.assertEqual(badge(OWNER, "Заказ запчастей"), 4)
        self.assertEqual(badge(OWNER, "Склад"), 4, "свёрнутая группа - сумма подпунктов")
        self.assertEqual(badge(TECH, "Наряды"), 2)
        self.assertEqual(badge(OWNER, "Клиенты"), 0)
        self.assertEqual(nav.badge_value(nav.NAV[2].items[4], {"alerts": "мусор"}, OWNER), 0)

    def test_trail(self):
        self.assertEqual(nav.trail("/parts/receipts", OWNER),
                         [{"label": "Склад", "href": ""},
                          {"label": "Приходы", "href": "/parts/receipts"}])
        self.assertEqual(nav.trail("/documents", OWNER),
                         [{"label": "Система", "href": ""}, {"label": "Настройки", "href": ""},
                          {"label": "Документы", "href": "/documents"}])
        self.assertEqual(nav.trail("/bikes/7", OWNER),
                         [{"label": "Парк", "href": ""},
                          {"label": "Велосипеды", "href": "/bikes"}])
        self.assertEqual(nav.trail("/clients/7", OWNER),
                         [{"label": "Клиенты", "href": "/clients"}])
        self.assertEqual(nav.trail("/me", OWNER), [])

    def test_is_page(self):
        for path in ("/", "/parts", "/bikes/12", "/reports/techs"):
            self.assertTrue(nav.is_page(path), path)
        for path in ("/static/style.css", "/bikes.xlsx", "/healthz", "/hook/inbox/x",
                     "/sign/abc", "/manifest.webmanifest", "/bikes/3/photo/frame.jpg"):
            self.assertFalse(nav.is_page(path), path)

    def test_counts_cache(self):
        now = [100.0]
        cache = nav.CountsCache(ttl=30, clock=lambda: now[0])
        self.assertIsNone(cache.fresh())
        cache.put({"alerts": 1})
        now[0] += 29
        self.assertEqual(cache.fresh(), {"alerts": 1})
        now[0] += 2
        self.assertIsNone(cache.fresh(), "устарело")
        cache.put({"alerts": 2})
        cache.drop()
        self.assertIsNone(cache.fresh(), "запись в панели сбрасывает кэш")
        # Подсчёт начался до записи и закончился после - в кэш не ложится.
        started = cache.generation
        cache.drop()
        self.assertEqual(cache.put({"alerts": 3}, generation=started), {"alerts": 3})
        self.assertIsNone(cache.fresh(), "старые числа не выдают себя за свежие")
        cache.put({"alerts": 4}, generation=cache.generation)
        self.assertEqual(cache.fresh(), {"alerts": 4})


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestMenuInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def test_menu_crumbs_and_role_under_the_name(self):
        page = self.get_ok("/parts/receipts")
        self.assertIn("Приходы", self.menu_labels(page))
        self.assertRegex(page, r'<a class="nav-link on" href="/parts/receipts"')
        self.assertIn('<details class="nav-sub" open>', page)
        self.assertIn('<nav class="crumbs" aria-label="Вы здесь"><span>Склад</span>', page)
        self.assertIn("<small>Владелец</small>", page)
        self.assertIn('action="/bikes" role="search"', page, "поиск велосипеда в меню")

    def test_parts_badge_counts_low_stock(self):
        """Счётчик «Заказ запчастей» - по остаткам, как у склада: сырые
        строки позиций пометок «ниже»/«на пределе» не несут."""
        tw.run(self.crm.create_part(title="Колодки", node="brakes", unit="шт",
                                    cost=0, price=0, min_stock=5, model=None,
                                    note=None))
        counts = tw.run(nav.gather_counts(self.crm, today=date.today()))
        self.assertEqual(counts["parts"], 1)
        page = self.get_ok("/")
        self.assertRegex(page, r'<span>Склад</span><em class="badge">1</em>')

    def test_counts_reach_the_menu_and_a_write_refreshes_them(self):
        page = self.get_ok("/")
        self.assertIn('class="nav-group"', page)
        # счётчики собираются, и страница не падает, даже если база их не дала
        broken = self.crm.tracker_alerts

        async def boom(*a, **k):
            raise RuntimeError("нет базы")
        self.crm.tracker_alerts = boom
        try:
            self.client.post("/logout")                 # запись сбрасывает кэш
            self.login()
            page = self.get_ok("/staff")
            self.assertIn("Сотрудники", self.menu_labels(page), "меню без чисел, но есть")
        finally:
            self.crm.tracker_alerts = broken


if __name__ == "__main__":
    unittest.main()
