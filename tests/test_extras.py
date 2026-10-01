"""Доп. аккумулятор за деньги и цена по заводскому названию модели.

Два правила, которые здесь стерегут. Первое: цена периода у аренды одна,
и позиции - её расшифровка, а не вторая касса; сложить их по-разному
нельзя. Второе: модель в парке названа по накладной, а цена стоит на
клиентском названии, и связывает их каталог, а не переименование парка.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

D = Decimal


def tariff(**kw):
    row = {"id": 1, "name": "Неделя", "period_days": 7, "price": D(4290),
           "kind": "bike", "model": None, "active": True}
    row.update(kw)
    return row


class TestModelAliases(unittest.TestCase):
    def setUp(self):
        self.catalogue = [
            {"title": "Городской H10", "factory_title": "Maikaolin Maikaolin H10"},
            {"title": "Компактный U5", "factory_title": "Maikaolin Maikaolin U5"},
            {"title": "Без заводского", "factory_title": None},
        ]
        self.aliases = logic.model_aliases(self.catalogue)

    def test_factory_name_leads_to_the_catalogue_name(self):
        self.assertEqual(
            logic.catalogue_model("Maikaolin Maikaolin H10", self.aliases),
            "Городской H10")

    def test_catalogue_name_stays_itself(self):
        self.assertEqual(logic.catalogue_model("Городской H10", self.aliases),
                         "Городской H10")

    def test_case_and_spaces_do_not_matter(self):
        self.assertEqual(
            logic.catalogue_model("  maikaolin maikaolin u5 ", self.aliases),
            "Компактный U5")

    def test_unknown_model_is_left_as_is(self):
        self.assertEqual(logic.catalogue_model("Чужой велосипед", self.aliases),
                         "Чужой велосипед",
                         "выдумывать модель нельзя - пусть сработает запасной тариф")

    def test_model_without_factory_name_still_works(self):
        self.assertEqual(logic.catalogue_model("Без заводского", self.aliases),
                         "Без заводского")

    def test_price_binds_through_the_factory_name(self):
        rows = [tariff(id=1, model=None, price=D(3000)),
                tariff(id=2, model="Городской H10", price=D(4290))]
        got = logic.tariffs_for_model(rows, "Maikaolin Maikaolin H10",
                                      aliases=self.aliases)
        self.assertEqual([t["id"] for t in got], [2],
                         "цена модели, а не запасной тариф")

    def test_without_aliases_the_factory_name_falls_back(self):
        rows = [tariff(id=1, model=None, price=D(3000)),
                tariff(id=2, model="Городской H10", price=D(4290))]
        got = logic.tariffs_for_model(rows, "Maikaolin Maikaolin H10")
        self.assertEqual([t["id"] for t in got], [1],
                         "без каталога остаётся запасной - но не чужая цена")

    def test_match_tariff_keeps_the_period_through_the_alias(self):
        rows = [tariff(id=1, model=None, period_days=7, price=D(3000)),
                tariff(id=2, model="Городской H10", period_days=7, price=D(4290))]
        got = logic.match_tariff(rows, rows[0], "Maikaolin Maikaolin H10",
                                 aliases=self.aliases)
        self.assertEqual(got["id"], 2)
        self.assertEqual(logic.to_money(got["price"]), D(4290))


class TestTariffKinds(unittest.TestCase):
    def test_battery_tariffs_do_not_leak_into_bikes(self):
        rows = [tariff(id=1, kind="bike", price=D(4290)),
                tariff(id=2, kind="battery", price=D(1170))]
        got = logic.tariffs_for_model(rows, "")
        self.assertEqual([t["id"] for t in got], [1])
        self.assertEqual([t["id"] for t in logic.tariffs_for_model(rows, "",
                                                                   kind="battery")],
                         [2])

    def test_old_tariff_without_kind_is_a_bike(self):
        rows = [{"id": 1, "period_days": 7, "price": D(3000), "model": None}]
        self.assertEqual(len(logic.tariffs_for_model(rows, "")), 1)

    def test_check_kind(self):
        self.assertEqual(logic.check_tariff_kind("battery").value, "battery")
        self.assertEqual(logic.check_tariff_kind("").value, "bike",
                         "пусто - это велосипед: так было до батарей")
        self.assertFalse(logic.check_tariff_kind("самокат").ok)


class TestExtras(unittest.TestCase):
    def test_period_price_is_the_sum(self):
        extras = [{"price": D(1170), "removed_at": None},
                  {"price": D(1170), "removed_at": None}]
        self.assertEqual(logic.period_price(D(4290), extras), D(6630))

    def test_removed_extra_costs_nothing(self):
        extras = [{"price": D(1170), "removed_at": None},
                  {"price": D(1170), "removed_at": "вчера"}]
        self.assertEqual(logic.extras_total(extras), D(1170))
        self.assertEqual(logic.period_price(D(4290), extras), D(5460))

    def test_no_extras_leaves_the_price_alone(self):
        self.assertEqual(logic.period_price(D(4290)), D(4290))
        self.assertEqual(logic.extras_total([]), D(0))

    def test_title_reads_like_a_line_in_the_act(self):
        self.assertEqual(logic.extra_title("battery", "70 Ач"),
                         "Доп. аккумулятор 70 Ач")
        self.assertEqual(logic.extra_title("battery", None), "Доп. аккумулятор")

    def test_battery_price_matches_the_rental_period(self):
        rows = [tariff(id=1, kind="battery", model="Аккумулятор 70 Ач",
                       period_days=7, price=D(1170)),
                tariff(id=2, kind="battery", model="Аккумулятор 70 Ач",
                       period_days=1, price=D(200))]
        battery = {"model_title": "Аккумулятор 70 Ач"}
        self.assertEqual(logic.battery_extra_price(rows, battery, 7), D(1170))
        self.assertEqual(logic.battery_extra_price(rows, battery, 1), D(200))

    def test_no_battery_tariff_is_not_a_free_battery(self):
        rows = [tariff(id=1, kind="battery", model="Аккумулятор 60 Ач",
                       period_days=7, price=D(1170))]
        self.assertIsNone(
            logic.battery_extra_price(rows, {"model_title": "Аккумулятор 70 Ач"}, 7),
            "цену должен назначить человек, а не ноль по умолчанию")
        self.assertIsNone(logic.battery_extra_price(rows, {"model_title": "х"}, 0))

    def test_spare_battery_tariff_covers_any_model(self):
        rows = [tariff(id=1, kind="battery", model=None, period_days=7,
                       price=D(1000))]
        self.assertEqual(
            logic.battery_extra_price(rows, {"model_title": "Какая угодно"}, 7),
            D(1000))

    def test_options_mark_what_has_no_price(self):
        rows = [tariff(id=1, kind="battery", model="Аккумулятор 70 Ач",
                       period_days=7, price=D(1170))]
        got = logic.battery_options(
            [{"id": 5, "code": "9510001", "model_title": "Аккумулятор 70 Ач"},
             {"id": 6, "code": "9510002", "model_title": "Аккумулятор 60 Ач"}],
            rows, 7)
        self.assertTrue(got[0]["priced"])
        self.assertEqual(got[0]["extra_price"], D(1170))
        self.assertFalse(got[1]["priced"])


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()


try:
    import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False


def _run(coro):
    import asyncio
    return asyncio.run(coro)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestExtrasOnTheRentalCard(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()
        self.model_id = _run(self.crm.create_battery_model(
            title="Аккумулятор 70 Ач", brand=None, voltage=60, capacity=D(70),
            price=D(12000), service_months=15, note=None))
        self.battery_id = _run(self.crm.create_battery(
            code="9510001", model_id=self.model_id, status="available"))
        self.rental_id = _run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id,
            tariff_id=self.tariff_id, tariff_name="Неделя", period_days=7,
            price=D(3000), billing="weekly", started_on=date(2026, 9, 1),
            contract_no="АВ-1", created_by="тест"))

    def price_of(self):
        return logic.to_money(_run(self.crm.rental(self.rental_id))["price"])

    def add(self):
        return self.client.post(f"/rentals/{self.rental_id}/extras",
                                data={"battery_id": str(self.battery_id)})

    def test_without_a_battery_tariff_nothing_is_issued(self):
        self.add()
        self.assertEqual(self.price_of(), D(3000), "цена не тронута")
        self.assertEqual(_run(self.crm.rental_extras(self.rental_id)), [])
        self.assertEqual(_run(self.crm.battery(self.battery_id))["status"],
                         "available", "батарея осталась в парке")

    def test_priced_battery_raises_the_period_price(self):
        _run(self.crm.create_tariff("АКБ · неделя", 7, D(1170), None,
                                    model="Аккумулятор 70 Ач", kind="battery"))
        self.add()
        self.assertEqual(self.price_of(), D(4170))
        extras = _run(self.crm.rental_extras(self.rental_id, live_only=True))
        self.assertEqual(len(extras), 1)
        self.assertEqual(logic.to_money(extras[0]["price"]), D(1170))
        self.assertEqual(_run(self.crm.battery(self.battery_id))["status"],
                         "rented", "батарея уехала с клиентом")

    def test_dropping_the_extra_returns_the_battery_and_the_price(self):
        _run(self.crm.create_tariff("АКБ · неделя", 7, D(1170), None,
                                    model="Аккумулятор 70 Ач", kind="battery"))
        self.add()
        extra = _run(self.crm.rental_extras(self.rental_id, live_only=True))[0]
        self.client.post(f"/rentals/{self.rental_id}/extras/{extra['id']}",
                         data={"status": "available"})
        self.assertEqual(self.price_of(), D(3000))
        self.assertEqual(_run(self.crm.battery(self.battery_id))["status"],
                         "available")

    def test_dropping_into_rented_or_new_is_refused(self):
        """«У клиента» ставит и снимает только выдача, «на сборке» - только
        ввод в эксплуатацию: подменённая форма получает отказ."""
        _run(self.crm.create_tariff("АКБ · неделя", 7, D(1170), None,
                                    model="Аккумулятор 70 Ач", kind="battery"))
        self.add()
        extra = _run(self.crm.rental_extras(self.rental_id, live_only=True))[0]
        for status in ("rented", "new", "выдумка"):
            r = self.client.post(f"/rentals/{self.rental_id}/extras/{extra['id']}",
                                 data={"status": status})
            self.assertEqual(r.status_code, 303, status)
            self.assertEqual(
                len(_run(self.crm.rental_extras(self.rental_id, live_only=True))), 1,
                f"{status}: позиция не снята")
            self.assertEqual(_run(self.crm.battery(self.battery_id))["status"],
                             "rented", status)
        self.assertIn("Недопустимый статус", self.get_ok(f"/rentals/{self.rental_id}"))

    def test_price_does_not_follow_a_later_tariff_change(self):
        """Базовая цена аренды - цена на выдаче, а не сегодняшний тариф."""
        _run(self.crm.create_tariff("АКБ · неделя", 7, D(1170), None,
                                    model="Аккумулятор 70 Ач", kind="battery"))
        self.add()
        _run(self.crm.update_tariff(self.tariff_id, price=D(9999)))
        extra = _run(self.crm.rental_extras(self.rental_id, live_only=True))[0]
        self.client.post(f"/rentals/{self.rental_id}/extras/{extra['id']}",
                         data={"status": "available"})
        self.assertEqual(self.price_of(), D(3000),
                         "аренда не переоценивается задним числом")

    def test_new_period_reprices_the_battery(self):
        """Цена позиции - за срок аренды: неделя 1 170 на месячном тарифе
        оставалась бы 1 170 в месяц."""
        _run(self.crm.create_tariff("АКБ · неделя", 7, D(1170), None,
                                    model="Аккумулятор 70 Ач", kind="battery"))
        _run(self.crm.create_tariff("АКБ · месяц", 30, D(4000), None,
                                    model="Аккумулятор 70 Ач", kind="battery"))
        self.add()
        month = _run(self.crm.create_tariff("Месяц", 30, D(11000), None))
        self.client.post(f"/rentals/{self.rental_id}/tariff",
                         data={"tariff_id": month, "billing": "auto"})
        self.assertEqual(self.price_of(), D(15000))
        extra = _run(self.crm.rental_extras(self.rental_id, live_only=True))[0]
        self.assertEqual(logic.to_money(extra["price"]), D(4000))

    def test_no_battery_price_for_the_new_period_keeps_the_tariff(self):
        """Нет цены батареи на новый срок - отказ, как на выдаче: иначе
        батарея ехала бы по цене чужого срока или бесплатно."""
        _run(self.crm.create_tariff("АКБ · неделя", 7, D(1170), None,
                                    model="Аккумулятор 70 Ач", kind="battery"))
        self.add()
        month = _run(self.crm.create_tariff("Месяц", 30, D(11000), None))
        self.client.post(f"/rentals/{self.rental_id}/tariff",
                         data={"tariff_id": month, "billing": "auto"})
        rental = _run(self.crm.rental(self.rental_id))
        self.assertEqual((rental["period_days"], self.price_of()), (7, D(4170)))
        self.assertIn("Нет тарифа на аккумулятор",
                      self.get_ok(f"/rentals/{self.rental_id}"))

    def test_more_than_the_limit_is_refused(self):
        _run(self.crm.create_tariff("АКБ · неделя", 7, D(1170), None,
                                    model="Аккумулятор 70 Ач", kind="battery"))
        ids = [self.battery_id]
        for n in range(2, 5):
            ids.append(_run(self.crm.create_battery(
                code=f"951000{n}", model_id=self.model_id, status="available")))
        for battery_id in ids:
            self.client.post(f"/rentals/{self.rental_id}/extras",
                             data={"battery_id": str(battery_id)})
        live = _run(self.crm.rental_extras(self.rental_id, live_only=True))
        self.assertEqual(len(live), logic.MAX_EXTRA_BATTERIES)

    def test_closing_the_rental_closes_the_extras(self):
        _run(self.crm.create_tariff("АКБ · неделя", 7, D(1170), None,
                                    model="Аккумулятор 70 Ач", kind="battery"))
        self.add()
        self.client.post(f"/rentals/{self.rental_id}/close",
                         data={"closed_on": "2026-09-20", "bike_status": "available"})
        self.assertEqual(_run(self.crm.rental_extras(self.rental_id, live_only=True)),
                         [])

    def test_card_shows_the_breakdown(self):
        _run(self.crm.create_tariff("АКБ · неделя", 7, D(1170), None,
                                    model="Аккумулятор 70 Ач", kind="battery"))
        self.add()
        text = self.get_ok(f"/rentals/{self.rental_id}")
        self.assertIn("Позиции аренды", text)
        self.assertIn("Доп. аккумулятор", text)
        self.assertIn("Итого за период", text)

    # ─── проверка кода: гонка выдачи и замена чужой батареи ───

    def other_rental(self, battery_status="rented"):
        """Вторая аренда - другой клиент, другой велосипед, своя батарея."""
        from app.crm import service
        client = _run(self.crm.create_client(full_name="Петров Пётр",
                                             phone="+79990000002"))
        bike = _run(self.crm.create_bike(code="B-2", model="Kugoo V3"))
        rental_id = _run(service.open_rental(
            self.crm, client=_run(self.crm.client(client)), bike=_run(self.crm.bike(bike)),
            tariff=_run(self.crm.tariff(self.tariff_id)), started_on=date.today(),
            contract_no=None, by="t"))
        battery = _run(self.crm.create_battery(code="9510077", model_id=self.model_id,
                                               status="available"))
        _run(self.crm.issue_batteries(rental_id, battery_ids=[battery], bike_id=bike,
                                      by="t"))
        return rental_id, battery

    def test_battery_taken_meanwhile_is_neither_priced_nor_issued(self):
        """Карточка показала батарею свободной, а соседний оператор успел её
        выдать: позиция и цена не встают, экран не говорит «выдан»."""
        from app.crm import service
        _run(self.crm.create_tariff("АКБ · неделя", 7, D(1170), None,
                                    model="Аккумулятор 70 Ач", kind="battery"))
        stale = _run(self.crm.battery(self.battery_id))
        _run(self.crm.update_battery(self.battery_id, status="rented", rental_id=999))
        with self.assertRaises(service.ServiceError) as err:
            _run(service.add_battery_extra(
                self.crm, _run(self.crm.rental(self.rental_id)), stale,
                tariffs=_run(self.crm.tariffs(active_only=True)), by="t"))
        self.assertIn("только что выдали", str(err.exception))
        self.assertEqual(self.price_of(), D(3000), "цена не поднялась")
        self.assertEqual(_run(self.crm.rental_extras(self.rental_id)), [])
        self.assertEqual(_run(self.crm.battery(self.battery_id))["rental_id"], 999)

    def test_issue_drops_an_extra_whose_battery_was_taken(self):
        """Выдача: занятая за секунду батарея не остаётся в цене периода и в
        первом начислении, а вызывающий узнаёт о ней из `missed`."""
        from app.crm import service
        _, taken = self.other_rental()
        client = _run(self.crm.create_client(full_name="Сидоров", phone="+79990000003"))
        bike = _run(self.crm.create_bike(code="B-3", model="Kugoo V3"))
        missed: list = []
        rental_id = _run(service.open_rental(
            self.crm, client=_run(self.crm.client(client)), bike=_run(self.crm.bike(bike)),
            tariff=_run(self.crm.tariff(self.tariff_id)), started_on=date.today(),
            contract_no=None, by="t", missed=missed,
            extras=[{"kind": "battery", "battery_id": taken, "title": "Доп. АКБ",
                     "price": D(1170)},
                    {"kind": "battery", "battery_id": self.battery_id,
                     "title": "Доп. АКБ", "price": D(1170)}]))
        self.assertEqual([m["battery_id"] for m in missed], [taken])
        rental = _run(self.crm.rental(rental_id))
        self.assertEqual(logic.to_money(rental["price"]), D(4170), "велосипед и одна батарея")
        self.assertEqual(_run(self.crm.client_balance(client)), D(-4170),
                         "первый период - без чужой батареи")
        live = _run(self.crm.rental_extras(rental_id, live_only=True))
        self.assertEqual([e["battery_id"] for e in live], [self.battery_id])
        self.assertEqual(_run(self.crm.battery(self.battery_id))["rental_id"], rental_id,
                         "своя ушла клиенту вместе с позицией")
        self.assertNotEqual(_run(self.crm.battery(taken))["rental_id"], rental_id)

    def test_swap_moves_the_extra_to_the_new_battery(self):
        """Замена платной батареи: позиция едет на новую, и её снятие
        возвращает в парк ту, что у клиента, а не снятую в ремонт."""
        _run(self.crm.create_tariff("АКБ · неделя", 7, D(1170), None,
                                    model="Аккумулятор 70 Ач", kind="battery"))
        self.add()
        fresh = _run(self.crm.create_battery(code="9510055", model_id=self.model_id,
                                             status="available"))
        r = self.client.post(f"/rentals/{self.rental_id}/battery",
                             data={"old_id": str(self.battery_id),
                                   "battery_id": str(fresh), "old_status": "repair"})
        self.assertEqual(r.status_code, 303)
        extra = _run(self.crm.rental_extras(self.rental_id, live_only=True))[0]
        self.assertEqual(extra["battery_id"], fresh)
        self.assertEqual(self.price_of(), D(4170), "цена та же: замена - не покупка")
        self.client.post(f"/rentals/{self.rental_id}/extras/{extra['id']}",
                         data={"status": "available"})
        self.assertEqual(_run(self.crm.battery(fresh))["status"], "available")
        self.assertEqual(_run(self.crm.battery(self.battery_id))["status"], "repair")

    def test_swap_cannot_take_a_battery_of_another_rental(self):
        """Номер снимаемой батареи - из формы: чужая аренда свою не теряет,
        а потерянная и «на сборке» не встают в свободные."""
        _, foreign = self.other_rental()
        lost = _run(self.crm.create_battery(code="9510066", model_id=self.model_id,
                                            status="lost"))
        for old in (foreign, lost):
            before = _run(self.crm.battery(old))
            r = self.client.post(f"/rentals/{self.rental_id}/battery",
                                 data={"old_id": str(old),
                                       "battery_id": str(self.battery_id)})
            self.assertEqual(r.status_code, 303)
            after = _run(self.crm.battery(old))
            self.assertEqual((after["status"], after["rental_id"]),
                             (before["status"], before["rental_id"]), old)
            self.assertEqual(_run(self.crm.battery(self.battery_id))["status"],
                             "available", "новая не ушла")
        self.assertIn("не у этой аренды", self.get_ok(f"/rentals/{self.rental_id}"))

    def test_return_battery_is_bound_to_its_rental(self):
        other, foreign = self.other_rental()
        self.assertFalse(_run(self.crm.return_battery(foreign, rental_id=self.rental_id,
                                                      by="t")))
        self.assertEqual(_run(self.crm.battery(foreign))["status"], "rented")
        self.assertTrue(_run(self.crm.return_battery(foreign, rental_id=other, by="t")))


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestTariffPage(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_two_groups_on_the_page(self):
        text = self.get_ok("/tariffs")
        self.assertIn("Велосипеды", text)
        self.assertIn("Аккумуляторы", text)

    def test_battery_tariff_is_created_in_its_group(self):
        self.client.post("/tariffs", data={
            "name": "АКБ · неделя", "period_days": "7", "price": "1170",
            "kind": "battery", "model": ""})
        rows = _run(self.crm.tariffs(kind="battery"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "АКБ · неделя")

    def test_same_period_for_bike_and_battery_do_not_collide(self):
        self.client.post("/tariffs", data={"name": "Неделя", "period_days": "7",
                                           "price": "3000", "kind": "bike"})
        self.client.post("/tariffs", data={"name": "АКБ · неделя", "period_days": "7",
                                           "price": "1170", "kind": "battery"})
        self.assertEqual(len(_run(self.crm.tariffs())), 2,
                         "вид разводит одинаковые сроки")
