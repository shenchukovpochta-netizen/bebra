"""Скидка на простаивающие: подсказка и акция с ограничением моделью и точкой.

Модель на точке стоит свободной дольше порога в среднем - сводка и страница
точки предлагают сезонную акцию на неё. Стерегут: кто считается
простаивающим (подменный и выданный - нет), что форма только заполнена, а
не заведена, что ограниченная акция ложится лишь на аренды этой модели на
этой точке - и в предпросмотре выдачи, и в начислении, - что заготовка
дарит скидку только новым арендам, а не продлениям идущих, что простой
считается и от переезда (привезённый переброской не «простаивает» там, куда
его привезли), что ошибка в форме не стирает ограничение, и что заведённая
акция встаёт на место подсказки.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import logic  # noqa: E402

try:
    from app.crm import service
    from tests import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
PAV, ADO = "Павлюхина", "Адоратского"
MT, KG = "Monster Truck", "Kugoo"


def bike(bid, *, place=ADO, model=MT, status="available", spare=False):
    return {"id": bid, "code": f"B-{bid}", "model": model, "location": place,
            "status": status, "spare": spare}


def standing(days: dict[int, int | None]) -> dict[int, datetime | None]:
    """С какого момента велосипед в текущем статусе: bike_id -> дней назад."""
    return {bid: (None if d is None else NOW - timedelta(days=d, hours=1))
            for bid, d in days.items()}


def scoped(**over) -> dict:
    base = {"id": 7, "kind": "season", "title": f"{MT} на {ADO}", "percent": 15,
            "amount": None, "code": None, "params": {"model": MT, "location": ADO},
            "starts_on": None, "ends_on": None, "max_uses": None,
            "once_per_client": True, "active": True, "uses": 0}
    base.update(over)
    return base


class TestIdleModels(unittest.TestCase):
    def test_average_over_free_bikes_of_the_model(self):
        fleet = [bike(1), bike(2), bike(3), bike(4, model=KG), bike(5, place=PAV)]
        rows = logic.idle_models(fleet, since=standing({1: 12, 2: 9, 3: 3, 4: 2, 5: 30}),
                                 now=NOW, days=7)
        self.assertEqual([(r["location"], r["model"], r["count"], r["days"], r["max"])
                          for r in rows],
                         [(PAV, MT, 1, 30, 30), (ADO, MT, 3, 8, 12)],
                         "самые долгие - первыми; Kugoo стоит 2 дня - не простой")
        self.assertEqual([b["code"] for b in rows[1]["bikes"]], ["B-1", "B-2", "B-3"])

    def test_one_old_bike_among_moving_ones_is_not_model_idle(self):
        fleet = [bike(1), bike(2), bike(3)]
        rows = logic.idle_models(fleet, since=standing({1: 20, 2: 0, 3: 0}), now=NOW,
                                 days=7)
        self.assertEqual(rows, [])

    def test_what_is_not_free_is_not_idle(self):
        """Подменный держат нарочно, выданный и ремонт - не свободны, у
        «не на точке» акцию не ограничить, чужая точка - вне списка."""
        fleet = [bike(1, spare=True), bike(2, status="rented"), bike(3, status="repair"),
                 bike(4, place=None), bike(5, place="Склад"), bike(6, status="reserved")]
        since = standing(dict.fromkeys(range(1, 7), 40))
        self.assertEqual(logic.idle_models(fleet, since=since, now=NOW, days=7,
                                           points=[PAV, ADO]), [])

    def test_transfer_restarts_the_count_at_the_new_point(self):
        """Переброска статус не меняет: без журнала мест привезённый вчера
        велосипед «простаивал» бы на новой точке все 20 дней старой."""
        fleet = [bike(1, place=PAV), bike(2, place=PAV)]
        since = standing({1: 20, 2: 20})
        self.assertEqual(len(logic.idle_models(fleet, since=since, now=NOW, days=7)), 1)
        rows = logic.idle_models(fleet, since=since, now=NOW, days=7,
                                 moved=standing({1: 1, 2: 1}))
        self.assertEqual(rows, [], "на Павлюхина они со вчера")
        # переезд раньше смены статуса ничего не меняет: считаем от позднего
        rows = logic.idle_models(fleet, since=since, now=NOW, days=7,
                                 moved=standing({1: 60, 2: 60}))
        self.assertEqual([(r["days"], r["max"]) for r in rows], [(20, 20)])
        # без журнала статусов - ноль, как и раньше, что бы ни было в журнале мест
        rows = logic.idle_models(fleet, since={}, now=NOW, days=1,
                                 moved=standing({1: 30, 2: 30}))
        self.assertEqual(rows, [])

    def test_no_journal_counts_as_zero_days(self):
        rows = logic.idle_models([bike(1), bike(2)], since=standing({1: 16, 2: None}),
                                 now=NOW, days=8)
        self.assertEqual([(r["days"], r["max"]) for r in rows], [(8, 16)])

    def test_model_goes_through_the_catalogue(self):
        aliases = logic.model_aliases([{"title": "Городской H10",
                                        "factory_title": "Maikaolin H10"}])
        rows = logic.idle_models([bike(1, model="Maikaolin H10")],
                                 since=standing({1: 10}), now=NOW, days=7,
                                 aliases=aliases)
        self.assertEqual(rows[0]["model"], "Городской H10")

    def test_settings(self):
        self.assertEqual(logic.idle_promo_settings({}),
                         {"days": logic.IDLE_PROMO_DAYS,
                          "percent": logic.IDLE_PROMO_PERCENT})
        self.assertEqual(logic.idle_promo_settings({"idle_promo_days": "10",
                                                    "idle_promo_percent": "20"}),
                         {"days": 10, "percent": 20})
        for junk in ("0", "101", "²", "", "-5"):
            got = logic.idle_promo_settings({"idle_promo_days": junk,
                                             "idle_promo_percent": junk})
            self.assertEqual(got["percent"], logic.IDLE_PROMO_PERCENT, junk)
        self.assertEqual(logic.idle_promo_settings({"idle_promo_days": "400"})["days"],
                         logic.IDLE_PROMO_DAYS)


class TestIdleOffers(unittest.TestCase):
    ROW = {"location": ADO, "model": MT, "count": 3, "days": 9, "max": 12,
           "bikes": [{"code": "B-1"}, {"code": "B-2"}, {"code": "B-3"}]}

    def test_running_scoped_promo_takes_the_place_of_the_offer(self):
        cases = (
            ("своя модель и точка", scoped(), True),
            ("только модель", scoped(params={"model": MT}), True),
            ("только точка", scoped(params={"location": ADO}), True),
            ("другая точка", scoped(params={"model": MT, "location": PAV}), False),
            ("другая модель", scoped(params={"model": KG}), False),
            ("общая без ограничения", scoped(params={}), False),
            ("выключена", scoped(active=False), False),
            ("срок вышел", scoped(ends_on=TODAY - timedelta(days=1)), False),
            ("не тот шаблон", scoped(kind="first"), False),
        )
        for title, promo, found in cases:
            with self.subTest(title):
                got = logic.idle_promo_rows([self.ROW], [promo], today=TODAY)[0]
                self.assertEqual(got["promo"] is not None, found)

    def test_offer_url_carries_model_point_and_days(self):
        got = logic.idle_promo_rows([self.ROW], [], today=TODAY)[0]
        self.assertEqual(got["url"], f"/promos/new?kind=season&model={quote(MT)}"
                                     f"&location={quote(ADO)}&idle=9")

    def test_tasks_are_offers_or_running_promos(self):
        offer = logic.idle_promo_rows([self.ROW], [], today=TODAY)
        task = logic.today_tasks(idle=offer, today=TODAY)[0]
        self.assertEqual((task["code"], task["count"], task["level"], task["edit"]),
                         ("idle_promo", 3, "info", True))
        self.assertEqual(task["title"],
                         f"{MT} на {ADO} простаивает 9 дн. — предложить скидку?")
        self.assertEqual(task["url"], offer[0]["url"])
        self.assertEqual(task["names"], ["B-1", "B-2", "B-3"])
        running = logic.idle_promo_rows([self.ROW], [scoped()], today=TODAY)
        task = logic.today_tasks(idle=running, today=TODAY)[0]
        self.assertEqual((task["url"], task["edit"]), ("/promos/7", False))
        self.assertIn(f"идёт акция «{MT} на {ADO}»", task["title"])
        many = logic.today_tasks(idle=offer * 5, today=TODAY)
        self.assertEqual(len(many), logic.IDLE_PROMO_TASKS, "на сводке - самые долгие")


class TestIdlePromoForm(unittest.TestCase):
    CHOICES = {"models": [KG, MT], "places": [PAV, ADO]}

    def test_prefilled_but_not_created(self):
        promo = logic.idle_promo_form({"model": MT, "location": ADO, "idle": "9"},
                                      today=TODAY, percent=20, **self.CHOICES)
        self.assertEqual((promo["kind"], promo["title"], promo["percent"]),
                         ("season", f"{MT} на {ADO}", 20))
        self.assertEqual((promo["starts_on"], promo["ends_on"]),
                         (TODAY, TODAY + timedelta(days=logic.IDLE_PROMO_LENGTH - 1)))
        self.assertTrue(promo["once_per_client"], "скидка зовёт нового, а не дарит")
        self.assertEqual(logic.promo_scope(promo), {"model": MT, "location": ADO})
        self.assertTrue(logic.promo_new_only(promo),
                        "продление идущей аренды простой не снимает")
        self.assertIn("9 дн.", promo["note"])

    def test_foreign_values_are_not_prefilled(self):
        promo = logic.idle_promo_form({"model": "Луноход", "location": "<script>"},
                                      today=TODAY, percent=20, **self.CHOICES)
        self.assertEqual(promo, logic.promo_form_defaults("season"))
        half = logic.idle_promo_form({"model": MT, "location": "Луна", "idle": "²"},
                                     today=TODAY, percent=20, **self.CHOICES)
        self.assertEqual(logic.promo_scope(half), {"model": MT, "location": None})
        self.assertEqual(half["title"], MT)
        self.assertNotIn("²", half["note"])


class TestPromoScope(unittest.TestCase):
    def ctx(self, **over):
        base = {"period_index": 1, "today": TODAY, "code": "", "previous_rentals": 0,
                "last_closed_on": None, "client_uses": {}, "model": MT, "location": ADO}
        base.update(over)
        return base

    def test_scope_limits_every_kind(self):
        self.assertTrue(logic.promo_fits(scoped(), self.ctx()))
        self.assertTrue(logic.promo_fits(scoped(), self.ctx(model="monster truck")),
                        "регистр модели не важен")
        self.assertFalse(logic.promo_fits(scoped(), self.ctx(location=PAV)))
        self.assertFalse(logic.promo_fits(scoped(), self.ctx(model=KG)))
        self.assertFalse(logic.promo_fits(scoped(), self.ctx(model="", location="")),
                         "аренда без велосипеда и точки под ограничение не подходит")
        self.assertTrue(logic.promo_fits(scoped(params={}), self.ctx(model="",
                                                                     location="")))
        # записанное ограничение действует и у шаблона без поля в форме
        first = scoped(kind="first", params={"location": PAV})
        self.assertFalse(logic.promo_fits(first, self.ctx()))

    def test_scope_reads_json_and_junk(self):
        self.assertEqual(logic.promo_scope({"params": '{"model": "MT"}'}),
                         {"model": "MT", "location": None})
        for junk in ("{", None, [], "[]"):
            self.assertEqual(logic.promo_scope({"params": junk}),
                             {"model": None, "location": None}, junk)
        self.assertEqual(logic.promo_scope_label(scoped()), f"{MT} · {ADO}")
        self.assertEqual(logic.promo_scope_label(scoped(params={})), "")

    def test_form_takes_scope_only_from_the_lists(self):
        data = {"title": "Простой", "percent": "15", "model": MT, "location": ADO}
        got = logic.check_promo_form(data, kind="season", models=[MT], places=[ADO])
        self.assertEqual(got.value["params"], {"model": MT, "location": ADO})
        bad = logic.check_promo_form({**data, "model": "Луноход"}, kind="season",
                                     models=[MT], places=[ADO])
        self.assertEqual(bad.error, "Модель: недопустимое значение.")
        bad = logic.check_promo_form({**data, "location": "Луна"}, kind="season",
                                     models=[MT], places=[ADO])
        self.assertEqual(bad.error, "Точка: недопустимое значение.")
        empty = logic.check_promo_form({**data, "model": " ", "location": ""},
                                       kind="season", models=[MT], places=[ADO])
        self.assertEqual(empty.value["params"], {})
        # у шаблона без ограничения поле отбрасывается, как код у сезонной
        first = logic.check_promo_form(data, kind="first", models=[MT], places=[ADO])
        self.assertEqual(first.value["params"], {})
        self.assertTrue(logic.PROMO_KINDS["season"]["scope"])

    def test_new_only_skips_renewals(self):
        """«Только новые аренды»: первый период - да, продление - нет. Без
        флага сезонная по-прежнему ложится на каждый период окна."""
        new_only = scoped(params={"model": MT, "location": ADO, "new_only": True})
        self.assertTrue(logic.promo_fits(new_only, self.ctx(period_index=1)))
        for index in (2, 3, 5):
            self.assertFalse(logic.promo_fits(new_only, self.ctx(period_index=index)), index)
            self.assertTrue(logic.promo_fits(scoped(), self.ctx(period_index=index)), index)
        self.assertTrue(logic.promo_new_only({"params": '{"new_only": true}'}))
        for junk in ({"new_only": "yes"}, {"new_only": 1}, "{", None, {}):
            self.assertFalse(logic.promo_new_only({"params": junk}), junk)

    def test_form_takes_new_only_only_for_scoped_kinds(self):
        data = {"title": "Простой", "percent": "15", "model": MT, "new_only": "1"}
        got = logic.check_promo_form(data, kind="season", models=[MT], places=[ADO])
        self.assertEqual(got.value["params"], {"model": MT, "new_only": True})
        off = logic.check_promo_form({**data, "new_only": ""}, kind="season",
                                     models=[MT], places=[ADO])
        self.assertEqual(off.value["params"], {"model": MT}, "снятая галочка не пишется")
        first = logic.check_promo_form(data, kind="first", models=[MT], places=[ADO])
        self.assertEqual(first.value["params"], {})

    def test_echo_keeps_what_was_typed(self):
        """Форма с ошибкой возвращает введённое, а не заготовку шаблона."""
        data = {"kind": "season", "title": f"{MT} на {ADO}", "percent": "150",
                "model": MT, "location": ADO, "new_only": "1", "once_per_client": "1",
                "starts_on": "2026-09-23", "ends_on": "2026-10-06", "note": "x",
                "max_uses": "", "text": ""}
        promo = logic.promo_form_echo(data, "season")
        self.assertEqual((promo["title"], promo["percent"], promo["starts_on"],
                          promo["ends_on"], promo["once_per_client"], promo["note"]),
                         (f"{MT} на {ADO}", "150", TODAY, date(2026, 10, 6), True, "x"))
        self.assertEqual(logic.promo_scope(promo), {"model": MT, "location": ADO})
        self.assertTrue(logic.promo_new_only(promo))
        junk = logic.promo_form_echo({"starts_on": "вчера", "every": "²"}, "loyalty")
        self.assertIsNone(junk["starts_on"])
        self.assertEqual(logic.promo_params(junk), {"every": 4}, "мусор - умолчание")
        self.assertFalse(junk["once_per_client"])

    def test_stamp_names_promo_and_discount(self):
        self.assertEqual(logic.promo_stamp(None, D(0)), "-")
        self.assertEqual(logic.promo_stamp(scoped(), D("450")), "7:450.00")
        self.assertNotEqual(logic.promo_stamp(scoped(), D("450")),
                            logic.promo_stamp(scoped(id=8), D("450")))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestIdlePromoFlow(tw.WebCase if HAVE_WEB else unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.login()
        for name in (PAV, ADO):
            tw.run(self.crm.create_location(name=name, city="Казань", address=None,
                                            note=None))
        self.point = next(p for p in tw.run(self.crm.locations()) if p["name"] == ADO)
        self.tariff_id = tw.run(self.crm.create_tariff("Неделя", 7, D(3000), None))
        self.idle_ids = [tw.run(self.crm.create_bike(code=f"MT-{i}", model=MT,
                                                     location=ADO)) for i in (1, 2)]
        self.kugoo = tw.run(self.crm.create_bike(code="KG-1", model=KG, location=ADO))
        self.pav = tw.run(self.crm.create_bike(code="MT-9", model=MT, location=PAV))
        for bid in self.idle_ids:
            self.age(bid, 10)
        self.client_id = tw.run(self.crm.create_client(full_name="Курьер",
                                                       phone="+79990000000", tg_id=5001))

    def age(self, bike_id, days):
        """Велосипед заведён `days` дней назад: как в базе, первая строка
        журнала мест пишется тем же INSERT, что и строка статуса."""
        for row in (*self.crm.status_log_, *self.crm.location_log_):
            if row["bike_id"] == bike_id:
                row["changed_at"] -= timedelta(days=days)

    def offer_url(self):
        return (f"/promos/new?kind=season&model={quote(MT)}&location={quote(ADO)}"
                f"&idle=10")

    def create_scoped(self, **over):
        """Поля, которые отправляет заготовка из подсказки о простое."""
        data = {"kind": "season", "title": f"{MT} на {ADO}", "percent": "15",
                "model": MT, "location": ADO, "once_per_client": "1", "new_only": "1",
                "starts_on": date.today().isoformat(),
                "ends_on": (date.today() + timedelta(days=13)).isoformat()}
        data.update(over)
        r = self.client.post("/promos", data=data)
        self.assertEqual(r.status_code, 303)
        return int(r.headers["location"].rsplit("/", 1)[1])

    def issue(self, bike_id, *, location=None):
        applied = []
        tw.run(service.open_rental(
            self.crm, client=tw.run(self.crm.client(self.client_id)),
            bike=tw.run(self.crm.bike(bike_id)), tariff=tw.run(self.crm.tariff(self.tariff_id)),
            started_on=date.today(), contract_no=None, by="t", applied=applied,
            location=location))
        return applied

    def test_dashboard_and_point_page_offer_the_form(self):
        dash = self.get_ok("/")
        self.assertIn(f"{MT} на {ADO} простаивает 10 дн. — предложить скидку?", dash)
        self.assertIn(self.offer_url().replace("&", "&amp;"), dash)
        self.assertNotIn(f"{KG} на {ADO} простаивает", dash)
        page = self.get_ok(f"/reports/points/{self.point['id']}")
        self.assertIn("Простаивают модели", page)
        self.assertIn(f"{MT} простаивает 10 дн.", page)
        self.assertIn(f'href="{self.offer_url().replace("&", "&amp;")}"', page)
        self.assertIn("Предложить скидку", page)
        self.assertEqual(tw.run(self.crm.promos()), [], "подсказка ничего не заводит")

    def test_form_is_prefilled_and_creating_shows_the_running_promo(self):
        form = self.get_ok(self.offer_url())
        self.assertIn("Заготовка из подсказки о простое", form)
        self.assertIn(f'value="{MT} на {ADO}"', form)
        self.assertIn(f'<option value="{MT}" selected>', form)
        self.assertIn(f'<option value="{ADO}" selected>', form)
        self.assertIn(f'value="{logic.IDLE_PROMO_PERCENT}"', form)
        self.assertIn(f'value="{date.today().isoformat()}"', form)
        self.assertIn('name="new_only" value="1" checked', form)
        promo_id = self.create_scoped()
        promo = tw.run(self.crm.promo(promo_id))
        self.assertEqual(logic.promo_scope(promo), {"model": MT, "location": ADO})
        self.assertTrue(logic.promo_new_only(promo))
        dash = self.get_ok("/")
        self.assertIn(f"идёт акция «{MT} на {ADO}»", dash)
        self.assertIn(f'href="/promos/{promo_id}"', dash)
        self.assertNotIn("предложить скидку?", dash)
        page = self.get_ok(f"/reports/points/{self.point['id']}")
        self.assertIn(f'<a href="/promos/{promo_id}">«{MT} на {ADO}»</a>', page)
        listing = self.get_ok("/promos")
        self.assertIn(f"только {MT} · {ADO} · только новые аренды", listing)
        card = self.get_ok(f"/promos/{promo_id}")
        self.assertIn(f'<option value="{ADO}" selected>', card)

    def test_scoped_promo_lands_only_on_its_model_and_point(self):
        """Начисление: скидка - только аренде этой модели на этой точке."""
        self.create_scoped(once_per_client="")
        self.assertEqual(self.issue(self.kugoo), [], "другая модель на той же точке")
        rental = tw.run(self.crm.active_rental_of(self.client_id))
        tw.run(service.close_rental(self.crm, rental, closed_on=date.today(), note=None,
                                    by="t"))
        self.assertEqual(self.issue(self.pav), [], "та же модель на другой точке")
        rental = tw.run(self.crm.active_rental_of(self.client_id))
        tw.run(service.close_rental(self.crm, rental, closed_on=date.today(), note=None,
                                    by="t"))
        got = self.issue(self.idle_ids[0])
        self.assertEqual([g["amount"] for g in got], [D(450)])
        # выдача на другую точку увозит аренду из-под ограничения
        rental = tw.run(self.crm.active_rental_of(self.client_id))
        tw.run(service.close_rental(self.crm, rental, closed_on=date.today(), note=None,
                                    by="t"))
        self.assertEqual(self.issue(self.idle_ids[1], location=PAV), [])

    def test_issue_preview_sees_model_and_point(self):
        self.create_scoped()
        base = f"/issue?client={self.client_id}&tariff={self.tariff_id}"
        page = self.get_ok(f"{base}&bike={self.idle_ids[0]}")
        self.assertIn(f"Акция «{MT} на {ADO}»", page)
        self.assertIn('value="2550"', page, "к оплате со скидкой 15 %")
        page = self.get_ok(f"{base}&bike={self.kugoo}")
        self.assertNotIn(f"Акция «{MT} на {ADO}»", page)

    def test_manager_sees_running_promo_not_the_offer(self):
        manager = tw.run(self.crm.access_profile_by_code("manager"))
        tw.run(self.crm.create_staff("ivan", logic.hash_password("password-1"), "Иван",
                                     "manager", manager["id"]))
        self.client.post("/logout")
        self.login("ivan", "password-1")
        dash = self.get_ok("/")
        self.assertNotIn("предложить скидку?", dash, "форма ему закрыта")
        page = self.get_ok(f"/reports/points/{self.point['id']}")
        self.assertNotIn("Предложить скидку", page)
        self.assertIn("можно предложить скидку", page)
        self.assertEqual(self.client.post("/promos/settings",
                                          data={"idle_promo_days": "3"}).status_code, 403)

    def test_settings_move_the_threshold_and_percent(self):
        r = self.client.post("/promos/settings", data={"idle_promo_days": "11",
                                                       "idle_promo_percent": "25"})
        self.assertEqual(r.status_code, 303)
        self.assertNotIn("простаивает", self.get_ok("/"), "10 дней меньше порога 11")
        self.assertIn('value="25"', self.get_ok(self.offer_url()))
        self.client.post("/promos/settings", data={"idle_promo_days": "0",
                                                   "idle_promo_percent": "25"})
        self.assertEqual(tw.run(self.crm.settings())["idle_promo_days"], "11")
        self.assertIn("Простаивает дольше: целое число от 1", self.get_ok("/promos"))

    def test_bad_scope_is_refused(self):
        r = self.client.post("/promos", data={"kind": "season", "title": "Х",
                                              "percent": "10", "model": "Луноход"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Модель: недопустимое значение.", r.text)
        self.assertEqual(tw.run(self.crm.promos()), [])

    def test_error_in_prefilled_form_keeps_the_scope(self):
        """Ошибка в одном поле не стирает модель, точку, срок и галочки:
        иначе исправленная форма заводила бы бессрочную скидку на всю сеть."""
        today = date.today()
        r = self.client.post("/promos", data={
            "kind": "season", "title": f"{MT} на {ADO}", "percent": "150", "model": MT,
            "location": ADO, "once_per_client": "1", "new_only": "1",
            "starts_on": today.isoformat(),
            "ends_on": (today + timedelta(days=13)).isoformat()})
        self.assertEqual(r.status_code, 400)
        page = r.text
        self.assertIn("Процент скидки: целое от 1 до 100.", page)
        self.assertIn(f'<option value="{MT}" selected>', page)
        self.assertIn(f'<option value="{ADO}" selected>', page)
        self.assertIn(f'value="{MT} на {ADO}"', page)
        self.assertIn('value="150"', page)
        self.assertIn(f'value="{(today + timedelta(days=13)).isoformat()}"', page)
        self.assertIn('name="once_per_client" value="1" checked', page)
        self.assertIn('name="new_only" value="1" checked', page)
        self.assertIn('action="/promos"', page, "та же форма новой акции")
        self.assertEqual(tw.run(self.crm.promos()), [])
        # чужой шаблон - не форма, а список акций
        r = self.client.post("/promos", data={"kind": "nope", "title": "Х"})
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/promos"))

    def test_prefilled_promo_skips_current_renters(self):
        """Заготовка из подсказки - только новые аренды: курьер, который уже
        катается на этой модели с этой точки, на продлении скидки не
        получает (он продлил бы и так), а новый курьер на простаивающем
        велосипеде - получает."""
        renter_bike = tw.run(self.crm.create_bike(code="MT-3", model=MT, location=ADO))
        self.issue(renter_bike)                                 # акции ещё нет
        self.create_scoped()
        applied = []
        tw.run(service.charge_all(self.crm, today=date.today() + timedelta(days=7),
                                  applied=applied))
        self.assertEqual(applied, [], "продление идущей аренды - без скидки")
        newcomer = tw.run(self.crm.create_client(full_name="Новый", phone="+79990000001"))
        got = []
        tw.run(service.open_rental(
            self.crm, client=tw.run(self.crm.client(newcomer)),
            bike=tw.run(self.crm.bike(self.idle_ids[0])),
            tariff=tw.run(self.crm.tariff(self.tariff_id)), started_on=date.today(),
            contract_no=None, by="t", applied=got))
        self.assertEqual([g["amount"] for g in got], [D(450)])

    def test_transfer_does_not_make_the_target_idle(self):
        """Переброска статус не меняет, но на новой точке велосипед стоит с
        приезда: подсказка не зовёт скидку туда, куда его повезли под спрос."""
        tw.run(self.crm.update_bike(self.pav, status="repair", by="t"))
        r = self.client.post("/bikes/transfer", data={
            "source": ADO, "target": PAV, "bike_ids": [str(i) for i in self.idle_ids]})
        self.assertEqual(r.status_code, 303)
        self.assertEqual({tw.run(self.crm.bike(i))["location"] for i in self.idle_ids},
                         {PAV})
        dash = self.get_ok("/")
        self.assertNotIn(f"{MT} на {PAV} простаивает", dash)
        point = next(p for p in tw.run(self.crm.locations()) if p["name"] == PAV)
        self.assertNotIn("Простаивают модели", self.get_ok(f"/reports/points/{point['id']}"))

    def test_issue_checks_the_discount_it_showed(self):
        """Точку выдачи на шаге 4 меняют без перезагрузки, а скидка от неё
        зависит: оформление с другой суммой возвращает на шаг, а не
        оставляет клиента с долгом или с необъявленной скидкой."""
        promo_id = self.create_scoped()
        step = (f"/issue?client={self.client_id}&tariff={self.tariff_id}"
                f"&bike={self.idle_ids[0]}")
        page = self.get_ok(step)
        self.assertIn(f'name="promo_seen" value="{promo_id}:450.00"', page)
        base = {"client_id": self.client_id, "tariff_id": self.tariff_id,
                "bike_id": self.idle_ids[0], "started_on": date.today().isoformat(),
                "pay_amount": "2550", "pay_method": "cash", "mileage": "0"}
        r = self.client.post("/issue", data={**base, "location": PAV,
                                             "promo_seen": f"{promo_id}:450.00"})
        self.assertEqual(r.status_code, 303)
        self.assertIn(f"location={quote(PAV)}", r.headers["location"])
        self.assertIsNone(tw.run(self.crm.active_rental_of(self.client_id)),
                          "ничего не выдано и не начислено")
        self.assertEqual(tw.run(self.crm.client_balance(self.client_id)), D(0))
        back = self.get_ok(r.headers["location"])
        self.assertIn("Скидка по акции для этой выдачи другая", back)
        self.assertIn('name="promo_seen" value="-"', back)
        self.assertIn('value="3000"', back, "на Павлюхина - полная цена")
        # обратно: без скидки на экране, с ней в журнале - тоже возврат
        r = self.client.post("/issue", data={**base, "location": ADO, "pay_amount": "3000",
                                             "promo_seen": "-"})
        self.assertIsNone(tw.run(self.crm.active_rental_of(self.client_id)))
        # что показали, то и начислили
        r = self.client.post("/issue", data={**base, "location": ADO,
                                             "promo_seen": f"{promo_id}:450.00"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("/issue/docs", r.headers["location"])
        self.assertEqual(tw.run(self.crm.client_balance(self.client_id)), D(0),
                         "2550 деньгами и 450 баллами закрыли период")

    def test_rename_carries_the_promo_scope(self):
        promo_id = self.create_scoped()
        self.assertEqual(tw.run(self.crm.rename_location(self.point["id"],
                                                         "Адоратского 5")), "ok")
        promo = tw.run(self.crm.promo(promo_id))
        self.assertEqual(logic.promo_scope(promo), {"model": MT, "location": "Адоратского 5"})


if __name__ == "__main__":
    unittest.main()
