"""Акции: шаблоны в коде, параметры в строке, скидка баллами.

Красная линия та же, что у баллов вообще: скидка меняет баланс, но
платежом не считается — средний чек парка считается по платежам. Здесь
проверяется, что акция срабатывает один раз на период, выбирается
выгоднейшая, промокод проверяется до денег, а выключенная акция новых
скидок не даёт.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_web as tw

    from app.crm import billing, service
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal
TODAY = date(2026, 9, 23)


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def promo(**over) -> dict:
    base = {"id": 1, "kind": "season", "title": "Осень", "percent": 10,
            "amount": None, "code": None, "params": {}, "starts_on": None,
            "ends_on": None, "max_uses": None, "once_per_client": False,
            "active": True, "uses": 0}
    base.update(over)
    return base


def ctx(**over) -> dict:
    base = {"period_index": 1, "today": TODAY, "code": "", "previous_rentals": 0,
            "last_closed_on": None, "client_uses": {}}
    base.update(over)
    return base


class TestPromoLogic(unittest.TestCase):
    def test_catalogue_has_six_templates_with_texts(self):
        self.assertEqual(set(logic.PROMO_KINDS),
                         {"first", "comeback", "promocode", "season", "renewal",
                          "loyalty"})
        for code, spec in logic.PROMO_KINDS.items():
            self.assertTrue(spec["text"], code)
            self.assertEqual(logic.check_promo_text(spec["text"]).ok, True, code)
        self.assertIn("promo", logic.BONUS_KINDS)
        self.assertIn("promos", logic.SECTIONS)
        self.assertEqual(logic.section_for("/promos/3/toggle"), "promos")
        self.assertIn("promo_applied", logic.NOTICES)

    def test_discount_is_percent_or_capped_amount(self):
        self.assertEqual(logic.promo_discount(promo(percent=10), D(3000)), D(300))
        self.assertEqual(logic.promo_discount(promo(percent=None, amount=D(500)),
                                              D(3000)), D(500))
        self.assertEqual(logic.promo_discount(promo(percent=None, amount=D(5000)),
                                              D(3000)), D(3000), "не больше цены")
        self.assertEqual(logic.promo_discount(promo(percent=None, amount=None),
                                              D(3000)), D(0))
        self.assertEqual(logic.promo_discount(promo(percent=10), D(0)), D(0))
        self.assertEqual(logic.promo_discount_label(promo(percent=15)), "15 %")

    def test_alive_checks_dates_switch_and_limit(self):
        self.assertTrue(logic.promo_alive(promo(), today=TODAY))
        self.assertFalse(logic.promo_alive(promo(active=False), today=TODAY))
        self.assertFalse(logic.promo_alive(promo(starts_on=TODAY + timedelta(days=1)),
                                           today=TODAY))
        self.assertFalse(logic.promo_alive(promo(ends_on=TODAY - timedelta(days=1)),
                                           today=TODAY))
        self.assertTrue(logic.promo_alive(promo(starts_on=TODAY, ends_on=TODAY),
                                          today=TODAY))
        self.assertFalse(logic.promo_alive(promo(max_uses=3, uses=3), today=TODAY))
        self.assertTrue(logic.promo_alive(promo(max_uses=3, uses=2), today=TODAY))

    def test_first_period_kinds_only_fit_the_first_period(self):
        first = promo(kind="first", once_per_client=True)
        self.assertTrue(logic.promo_fits(first, ctx()))
        self.assertFalse(logic.promo_fits(first, ctx(period_index=2)))
        self.assertFalse(logic.promo_fits(first, ctx(previous_rentals=1)),
                         "вторая аренда - уже не первая")
        self.assertFalse(logic.promo_fits(first, ctx(client_uses={1: 1})),
                         "один раз на клиента")

    def test_comeback_needs_a_real_break(self):
        back = promo(kind="comeback", params={"after_days": 30})
        self.assertFalse(logic.promo_fits(back, ctx()), "новичок - не вернувшийся")
        self.assertFalse(logic.promo_fits(back, ctx(
            previous_rentals=1, last_closed_on=TODAY - timedelta(days=10))))
        self.assertTrue(logic.promo_fits(back, ctx(
            previous_rentals=1, last_closed_on=TODAY - timedelta(days=45))))

    def test_promocode_matches_ignoring_case_and_spaces(self):
        code = promo(kind="promocode", code="ВЕСНА")
        self.assertTrue(logic.promo_fits(code, ctx(code="весна")))
        self.assertTrue(logic.promo_fits(code, ctx(code=" ВЕС НА ")))
        self.assertFalse(logic.promo_fits(code, ctx(code="ЛЕТО")))
        self.assertFalse(logic.promo_fits(code, ctx(code="")))
        self.assertEqual(logic.clean_promo_code(" ве сна "), "ВЕСНА")

    def test_renewal_and_loyalty_count_periods(self):
        renewal = promo(kind="renewal", params={"from_period": 4})
        self.assertFalse(logic.promo_fits(renewal, ctx(period_index=3)))
        self.assertTrue(logic.promo_fits(renewal, ctx(period_index=4)))
        self.assertTrue(logic.promo_fits(renewal, ctx(period_index=9)))
        loyalty = promo(kind="loyalty", params={"every": 4})
        self.assertFalse(logic.promo_fits(loyalty, ctx(period_index=3)))
        self.assertTrue(logic.promo_fits(loyalty, ctx(period_index=4)))
        self.assertFalse(logic.promo_fits(loyalty, ctx(period_index=5)))
        self.assertTrue(logic.promo_fits(loyalty, ctx(period_index=8)))

    def test_params_fall_back_to_defaults_on_junk(self):
        self.assertEqual(logic.promo_params(promo(kind="loyalty", params={"every": "x"})),
                         {"every": 4})
        self.assertEqual(logic.promo_params(promo(kind="loyalty", params='{"every": 6}')),
                         {"every": 6})
        self.assertEqual(logic.promo_params(promo(kind="comeback",
                                                  params={"after_days": 9999})),
                         {"after_days": 30}, "за пределами диапазона - умолчание")

    def test_pick_takes_the_best_for_the_client(self):
        small = promo(id=1, percent=5)
        big = promo(id=2, percent=None, amount=D(400))
        same = promo(id=3, percent=10)
        off = promo(id=4, percent=90, active=False)
        picked = logic.pick_promo([small, big, same, off], ctx(), D(3000))
        self.assertEqual(picked[0]["id"], 2)
        self.assertEqual(picked[1], D(400))
        picked = logic.pick_promo([same, promo(id=5, percent=10)], ctx(), D(3000))
        self.assertEqual(picked[0]["id"], 3, "при равной скидке - заведённая раньше")
        self.assertIsNone(logic.pick_promo([off], ctx(), D(3000)))

    def test_rental_history_skips_the_current_rental(self):
        rentals = [{"id": 7, "closed_on": None},
                   {"id": 5, "closed_on": date(2026, 8, 1)},
                   {"id": 2, "closed_on": date(2026, 6, 1)}]
        got = logic.rental_history(rentals, 7)
        self.assertEqual(got["previous_rentals"], 2)
        self.assertEqual(got["last_closed_on"], date(2026, 8, 1))
        self.assertEqual(logic.rental_history([], None),
                         {"previous_rentals": 0, "last_closed_on": None})

    def test_text_substitutions(self):
        p = promo(kind="promocode", code="ВЕСНА", text="Код {code}: {discount} для {name}")
        self.assertEqual(logic.promo_text(p, discount=D(300), name="Иван"),
                         "Код ВЕСНА: 300 ₽ для Иван")
        self.assertIn("10 %", logic.promo_mailing_body(promo(percent=10, text="{discount}")))
        self.assertEqual(logic.promo_mailing_body(promo(text="{name}")), "{name}",
                         "имя подставит рассылка")
        self.assertFalse(logic.check_promo_text("{unknown}").ok)
        self.assertIsNone(logic.check_promo_text("  ").value)

    def test_form_check(self):
        good = logic.check_promo_form({"kind": "promocode", "title": "Весна",
                                       "percent": "10", "code": "весна",
                                       "max_uses": "50", "once_per_client": "1"})
        self.assertTrue(good.ok, good.error)
        self.assertEqual(good.value["code"], "ВЕСНА")
        self.assertEqual(good.value["max_uses"], 50)
        self.assertTrue(good.value["once_per_client"])
        self.assertFalse(logic.check_promo_form({"kind": "x", "title": "a"}).ok)
        self.assertFalse(logic.check_promo_form({"kind": "season", "title": "a"}).ok,
                         "без скидки")
        self.assertFalse(logic.check_promo_form({"kind": "season", "title": "a",
                                                 "percent": "10", "amount": "5"}).ok,
                         "процент и сумма сразу")
        self.assertFalse(logic.check_promo_form({"kind": "season", "title": "a",
                                                 "percent": "150"}).ok)
        self.assertFalse(logic.check_promo_form({"kind": "promocode", "title": "a",
                                                 "percent": "10", "code": "!"}).ok)
        self.assertFalse(logic.check_promo_form({"kind": "loyalty", "title": "a",
                                                 "percent": "10", "every": "1"}).ok)
        self.assertFalse(logic.check_promo_form({"kind": "season", "title": "a",
                                                 "percent": "10", "starts_on": "2026-09-10",
                                                 "ends_on": "2026-09-01"}).ok)
        season = logic.check_promo_form({"kind": "season", "title": "a", "amount": "300",
                                         "code": "МУСОР", "starts_on": "01.09.2026"})
        self.assertTrue(season.ok)
        self.assertIsNone(season.value["code"], "код есть только у промокода")
        self.assertEqual(season.value["amount"], D(300))
        self.assertEqual(season.value["starts_on"], date(2026, 9, 1))
        defaults = logic.promo_form_defaults("loyalty")
        self.assertEqual(defaults["percent"], 50)
        self.assertEqual(defaults["params"], {"every": 4})
        self.assertFalse(defaults["once_per_client"])

    def test_totals(self):
        got = logic.promo_totals([promo(uses=2, total=D(600)),
                                  promo(id=2, active=False, uses=1, total=D(100))])
        self.assertEqual(got, {"active": 1, "count": 2, "uses": 3, "total": D(700)})


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestPromoFlow(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def make(self, kind="first", **over):
        fields = {"title": logic.PROMO_KINDS[kind]["title"], "percent": 10,
                  "amount": None, "code": None, "params": {}, "starts_on": None,
                  "ends_on": None, "max_uses": None,
                  "once_per_client": kind in logic.PROMO_FIRST_KINDS, "text": None,
                  "note": None}
        fields.update(over)
        return _run(self.crm.create_promo(kind=kind, by="t", **fields))

    def open_rental(self, code=None, started=None, applied=None):
        return _run(service.open_rental(
            self.crm, client=_run(self.crm.client(self.client_id)),
            bike=_run(self.crm.bike(self.bike_id)),
            tariff=_run(self.crm.tariff(self.tariff_id)),
            started_on=started or date.today(), contract_no="АВ-1", by="t",
            promo_code=code, applied=applied))

    def test_first_rental_discount_is_points_not_payment(self):
        self.make("first", percent=10)
        applied = []
        rid = self.open_rental(applied=applied)
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["amount"], D(300))
        self.assertEqual(applied[0]["rental_id"], rid)
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(-2700),
                         "начислено 3000, скидка 300 баллами")
        totals = _run(self.crm.ledger_totals(since=date(2000, 1, 1)))
        self.assertEqual(totals.get("payment", D(0)), D(0), "платежа нет - чек цел")
        self.assertEqual(totals.get("bonus"), D(300))
        grant = _run(self.crm.bonuses(kind="promo"))[0]
        self.assertEqual(grant["rental_id"], rid)
        self.assertEqual(grant["period_from"], date.today())
        self.assertEqual(_run(self.crm.promo(applied[0]["promo"]["id"]))["uses"], 1)

    def test_second_charge_pass_does_not_double_the_discount(self):
        self.make("season", percent=10)
        rid = self.open_rental()
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(-2700))
        rental = _run(self.crm.rental(rid))
        applied = []
        self.assertEqual(_run(service.charge_due(self.crm, rental=rental,
                                                 today=date.today(), applied=applied)), 0)
        self.assertEqual(applied, [], "период не начислился второй раз - и скидка тоже")
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(-2700))
        # начисление и скидка - одна запись за другой, с периодом у обеих
        rows = [x for x in _run(self.crm.ledger_of(self.client_id)) if x["rental_id"] == rid]
        self.assertEqual(sorted(x["kind"] for x in rows), ["bonus", "charge"])
        bonus = next(x for x in rows if x["kind"] == "bonus")
        self.assertEqual(bonus["period_from"], date.today())
        self.assertEqual(bonus["period_to"], date.today() + timedelta(days=7))
        self.assertIn(logic.period_label(date.today(), date.today() + timedelta(days=7)),
                      bonus["note"])

    def test_discount_is_taken_from_the_bike_price_not_the_extras(self):
        """Доп. аккумулятор - отдельная позиция: скидка считается от цены
        велосипеда, ровно той, что оператор видел на шаге выдачи."""
        self.make("season", percent=10)
        applied = []
        _run(service.open_rental(
            self.crm, client=_run(self.crm.client(self.client_id)),
            bike=_run(self.crm.bike(self.bike_id)),
            tariff=_run(self.crm.tariff(self.tariff_id)),
            started_on=date.today(), contract_no=None, by="t",
            extras=[{"kind": "battery", "title": "Доп. АКБ", "price": D(500)}],
            applied=applied))
        self.assertEqual(applied[0]["amount"], D(300), "10 % от 3000, а не от 3500")
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(-3200))

    def test_comeback_break_is_measured_to_the_start_date(self):
        self.make("comeback", percent=15, params={"after_days": 30})
        self.open_rental(started=date.today() - timedelta(days=60))
        rental = _run(self.crm.active_rental_of(self.client_id))
        _run(service.close_rental(self.crm, rental,
                                  closed_on=date.today() - timedelta(days=32), note=None,
                                  by="t"))
        # перерыв до начала аренды 22 дня, до сегодня 32: акция не положена
        applied = []
        self.open_rental(started=date.today() - timedelta(days=10), applied=applied)
        self.assertEqual(applied, [])
        rental = _run(self.crm.active_rental_of(self.client_id))
        _run(service.close_rental(self.crm, rental, closed_on=date.today(), note=None,
                                  by="t"))
        self.assertEqual(logic.rental_history(_run(self.crm.client_rentals(self.client_id)),
                                              None)["last_closed_on"], date.today())

    def test_season_applies_to_every_period_in_window(self):
        self.make("season", percent=10, ends_on=date.today() + timedelta(days=30))
        rid = self.open_rental(started=date.today() - timedelta(days=8))
        # два периода: сегодня в окне, скидка на каждый
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(-5400))
        self.assertEqual(len(_run(self.crm.bonuses(kind="promo"))), 2)
        self.assertEqual(_run(self.crm.rental_charge_count(rid)), 2)

    def test_loyalty_every_fourth_period(self):
        self.make("loyalty", percent=50, params={"every": 4})
        self.open_rental(started=date.today() - timedelta(days=7 * 3 + 1))
        # четыре периода начислены, скидка только на четвёртый
        grants = _run(self.crm.bonuses(kind="promo"))
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["amount"], D(1500))
        self.assertEqual(_run(self.crm.client_balance(self.client_id)),
                         D(-12000 + 1500))

    def test_best_of_two_wins_and_only_one_applies(self):
        self.make("first", percent=10)
        self.make("season", percent=None, amount=D(500))
        applied = []
        self.open_rental(applied=applied)
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["promo"]["kind"], "season")
        self.assertEqual(applied[0]["amount"], D(500))

    def test_promocode_is_checked_before_money(self):
        pid = self.make("promocode", code="ВЕСНА", percent=20, max_uses=1)
        with self.assertRaises(service.ServiceError):
            _run(service.check_promo_code(self.crm, "ЛЕТО", today=date.today()))
        self.assertIsNone(_run(service.check_promo_code(self.crm, "", today=date.today())))
        # код действует сегодня, но к дате начала аренды выйдет срок
        _run(self.crm.update_promo(pid, ends_on=date.today() + timedelta(days=2)))
        with self.assertRaises(service.ServiceError):
            _run(service.check_promo_code(self.crm, "ВЕСНА", today=date.today(),
                                          started_on=date.today() + timedelta(days=5)))
        applied = []
        self.open_rental(code="весна", applied=applied)
        self.assertEqual(applied[0]["amount"], D(600))
        rental = _run(self.crm.rental(applied[0]["rental_id"]))
        self.assertEqual(rental["promo_code"], "ВЕСНА", "код остался на аренде")
        # предел выбран: код больше не принимается
        self.assertEqual(_run(self.crm.promo(pid))["uses"], 1)
        with self.assertRaises(service.ServiceError):
            _run(service.check_promo_code(self.crm, "ВЕСНА", today=date.today()))

    def test_future_rental_keeps_the_code_until_the_first_charge(self):
        self.make("promocode", code="ЗАВТРА", percent=10)
        rid = self.open_rental(code="ЗАВТРА", started=date.today() + timedelta(days=2))
        self.assertEqual(_run(self.crm.bonuses(kind="promo")), [], "ещё не начислено")
        applied = []
        _run(service.charge_all(self.crm, today=date.today() + timedelta(days=2),
                                applied=applied))
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["rental_id"], rid)

    def test_once_per_client_and_switch_off(self):
        pid = self.make("first", percent=10)
        self.open_rental()
        rental = _run(self.crm.active_rental_of(self.client_id))
        _run(service.close_rental(self.crm, rental, closed_on=date.today(), note=None,
                                  by="t"))
        # вторая аренда - уже не первая, и акция один раз на клиента
        self.make("promocode", code="ЕЩЁ", percent=10)
        applied = []
        self.open_rental(code="ЕЩЁ", applied=applied)
        self.assertEqual(applied[0]["promo"]["kind"], "promocode")
        rental = _run(self.crm.active_rental_of(self.client_id))
        _run(service.close_rental(self.crm, rental, closed_on=date.today(), note=None,
                                  by="t"))
        _run(self.crm.update_promo(pid, active=False))
        applied = []
        self.open_rental(code="ЕЩЁ", applied=applied)
        self.assertEqual(applied, [], "промокод один раз на клиента, первая выключена")

    def test_billing_run_button_tells_the_client_too(self):
        """«Начислить» в панели - те же скидки, что и дневной проход,
        и клиенту о них говорит тот, кто начислил."""
        self.make("season", percent=10)
        self.open_rental(started=date.today() - timedelta(days=8))
        self.bot.sent.clear()
        # следующий период наступит через 6 дней: сдвигаем его сегодня
        rental = _run(self.crm.active_rental_of(self.client_id))
        self.crm.rentals_[rental["id"]]["billed_until"] = date.today()
        r = self.client.post("/billing/run")
        self.assertEqual(r.status_code, 303)
        self.assertTrue(any(chat == 5001 and "300 ₽" in t for chat, t in self.bot.sent),
                        self.bot.sent)
        self.assertIn("Скидок по акциям: 1", self.get_ok("/"))

    def test_limit_is_enforced_at_write_time(self):
        """Снимок «применений 0» у двух выдач в одну секунду не должен
        раздать на одну скидку больше: предел проверяет запись."""
        pid = self.make("promocode", code="ОДИН", percent=10, max_uses=1)
        self.open_rental(code="ОДИН")
        other = _run(self.crm.create_client(full_name="Петров Пётр",
                                            phone="+79990000002", tg_id=5002))
        bike2 = _run(self.crm.create_bike(code="B-2", model="Kugoo V3"))
        rid = _run(self.crm.create_rental(
            client_id=other, bike_id=bike2, tariff_id=self.tariff_id, tariff_name="Неделя",
            period_days=7, price=D(3000), billing="auto", started_on=date.today(),
            contract_no=None, created_by="t", promo_code="ОДИН"))
        bonus = {"promo_id": pid, "amount": D(300), "note": "снимок", "by": "t"}
        ok = _run(self.crm.charge_period(rid, other, period_from=date.today(),
                                         period_to=date.today() + timedelta(days=7),
                                         amount=D(-3000), note="период", bonus=bonus))
        self.assertTrue(ok, "начисление прошло")
        self.assertFalse(bonus["granted"], "скидка - нет: предел выбран")
        self.assertEqual(_run(self.crm.client_balance(other)), D(-3000))
        self.assertEqual(_run(self.crm.promo(pid))["uses"], 1)

    def test_one_broken_rental_does_not_stop_the_pass(self):
        self.make("season", percent=10)
        self.open_rental(started=date.today() - timedelta(days=8))
        other = _run(self.crm.create_client(full_name="Петров Пётр",
                                            phone="+79990000002", tg_id=5002))
        bike2 = _run(self.crm.create_bike(code="B-2", model="Kugoo V3"))
        rid2 = _run(service.open_rental(
            self.crm, client=_run(self.crm.client(other)), bike=_run(self.crm.bike(bike2)),
            tariff=_run(self.crm.tariff(self.tariff_id)),
            started_on=date.today() - timedelta(days=8), contract_no=None, by="t"))
        first = _run(self.crm.active_rental_of(self.client_id))["id"]
        real = self.crm.rental_charge_count

        async def broken(rental_id):
            if rental_id == first:
                raise RuntimeError("база моргнула")
            return await real(rental_id)
        self.crm.rental_charge_count = broken
        later = date.today() + timedelta(days=6)
        applied = []
        with self.assertRaises(service.ChargeError) as caught:
            _run(service.charge_all(self.crm, today=later, applied=applied))
        self.assertEqual(caught.exception.done, 1, "вторая аренда начислена, первая - в лог")
        self.assertEqual(caught.exception.failed, [first])
        self.assertEqual([a["rental_id"] for a in applied], [rid2])
        self.crm.rental_charge_count = real
        # первая догоняет следующим проходом, скидка вместе с ней
        applied = []
        self.assertEqual(_run(service.charge_all(self.crm, today=later, applied=applied)), 1)
        self.assertEqual([a["rental_id"] for a in applied], [first])

    def test_daily_pass_tells_the_client(self):
        self.make("season", percent=10)
        self.open_rental(started=date.today() - timedelta(days=8))
        # аренда начислена по сегодня; следующий период - через неделю
        later = date.today() + timedelta(days=6)
        self.bot.sent.clear()
        _run(billing.run_daily(self.bot, self.db, self.crm, self.cfg, today=later))
        texts_sent = [t for chat, t in self.bot.sent if chat == 5001]
        self.assertTrue(any("Сезонная" in t and "300 ₽" in t for t in texts_sent),
                        texts_sent)
        log = _run(self.crm.notice_log(limit=50))
        self.assertTrue(any(r["code"] == "promo_applied" and r["status"] == "sent"
                            for r in log))

    def test_switched_off_notice_keeps_the_points(self):
        self.make("first", percent=10)
        _run(self.crm.set_notice("promo_applied", enabled=False, at_hour=None, by="t"))
        r = self.client.post("/issue", data={"client_id": self.client_id,
                                             "tariff_id": self.tariff_id,
                                             "bike_id": self.bike_id,
                                             "pay_amount": "2700", "pay_method": "cash",
                                             "mileage": "10"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(0),
                         "2700 деньгами + 300 баллами закрыли период")
        self.assertFalse(any(chat == 5001 and "Акция" in t for chat, t in self.bot.sent))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestPromoPages(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def test_menu_has_the_section_in_blocks(self):
        page = self.get_ok("/promos")
        self.assertIn('href="/promos"', page)
        self.assertIn('class="blk"', page, "группы меню - блоками")
        self.assertEqual(page.count('class="blk"'), 7)

    def test_templates_become_promos(self):
        page = self.get_ok("/promos")
        for spec in logic.PROMO_KINDS.values():
            self.assertIn(spec["title"], page)
        self.assertIn("Акций ещё нет", page)
        form = self.get_ok("/promos/new?kind=loyalty")
        self.assertIn('name="every"', form)
        self.assertIn('value="50"', form, "умолчание шаблона подставлено")
        r = self.client.post("/promos", data={"kind": "loyalty", "title": "Четвёртая неделя",
                                              "percent": "50", "every": "4"})
        self.assertEqual(r.status_code, 303)
        promo_id = int(r.headers["location"].rsplit("/", 1)[1])
        card = self.get_ok(f"/promos/{promo_id}")
        self.assertIn("Четвёртая неделя", card)
        self.assertIn("действует", card)
        self.assertIn("Пока никому", card)
        listing = self.get_ok("/promos")
        self.assertIn("Каждый N-й период: 4", listing)
        self.assertEqual(self.client.get("/promos/new?kind=nope").status_code, 303)
        self.assertEqual(self.client.get("/promos/999").status_code, 404)

    def test_bad_form_is_explained(self):
        r = self.client.post("/promos", data={"kind": "promocode", "title": "Код",
                                              "percent": "10", "code": "!"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("Промокод", self.get_ok("/promos/new?kind=promocode"))

    def test_duplicate_active_code_is_refused(self):
        r = self.client.post("/promos", data={"kind": "promocode", "title": "А",
                                              "percent": "10", "code": "ВЕСНА"})
        first = int(r.headers["location"].rsplit("/", 1)[1])
        r = self.client.post("/promos", data={"kind": "promocode", "title": "Б",
                                              "percent": "10", "code": "весна"})
        self.assertEqual(r.headers["location"], "/promos/new?kind=promocode")
        self.assertEqual(len(_run(self.crm.promos())), 1)
        # выключили первую - слово освободилось
        self.client.post(f"/promos/{first}/toggle")
        r = self.client.post("/promos", data={"kind": "promocode", "title": "Б",
                                              "percent": "10", "code": "весна"})
        self.assertEqual(len(_run(self.crm.promos())), 2)
        # включить первую обратно нельзя, пока живёт вторая
        self.client.post(f"/promos/{first}/toggle")
        self.assertFalse(_run(self.crm.promo(first))["active"])

    def test_edit_keeps_kind_and_toggle_stops_new_discounts(self):
        r = self.client.post("/promos", data={"kind": "first", "title": "Новичок",
                                              "percent": "10", "once_per_client": "1"})
        promo_id = int(r.headers["location"].rsplit("/", 1)[1])
        r = self.client.post(f"/promos/{promo_id}", data={"kind": "season", "title": "Новичок 2",
                                                          "amount": "500"})
        self.assertEqual(r.status_code, 303)
        got = _run(self.crm.promo(promo_id))
        self.assertEqual(got["kind"], "first", "шаблон не меняется")
        self.assertEqual(got["title"], "Новичок 2")
        self.assertEqual(got["amount"], D(500))
        self.assertIsNone(got["percent"])
        self.client.post(f"/promos/{promo_id}/toggle")
        self.assertFalse(_run(self.crm.promo(promo_id))["active"])
        applied = []
        _run(service.open_rental(
            self.crm, client=_run(self.crm.client(self.client_id)),
            bike=_run(self.crm.bike(self.bike_id)),
            tariff=_run(self.crm.tariff(self.tariff_id)),
            started_on=date.today(), contract_no=None, by="t", applied=applied))
        self.assertEqual(applied, [])

    def test_future_issue_keeps_the_code_without_a_false_error(self):
        _run(self.crm.create_promo(
            kind="promocode", title="Завтра", percent=10, amount=None, code="ЗАВТРА",
            params={}, starts_on=None, ends_on=None, max_uses=None,
            once_per_client=True, text=None, note=None, by="t"))
        start = date.today() + timedelta(days=2)
        r = self.client.post("/issue", data={"client_id": self.client_id,
                                             "tariff_id": self.tariff_id,
                                             "bike_id": self.bike_id,
                                             "started_on": start.isoformat(),
                                             "pay_amount": "2700", "pay_method": "cash",
                                             "mileage": "10", "promo_code": "завтра"})
        self.assertEqual(r.status_code, 303)
        page = self.get_ok(r.headers["location"])
        self.assertNotIn("не подошёл", page)
        self.assertIn("сохранён на аренде", page)
        rental = _run(self.crm.active_rental_of(self.client_id))
        self.assertEqual(rental["promo_code"], "ЗАВТРА")

    def test_bad_form_keeps_code_and_date_on_the_way_back(self):
        _run(self.crm.create_promo(
            kind="promocode", title="Весна", percent=20, amount=None, code="ВЕСНА",
            params={}, starts_on=None, ends_on=None, max_uses=None,
            once_per_client=True, text=None, note=None, by="t"))
        start = date.today() + timedelta(days=1)
        r = self.client.post("/issue", data={"client_id": self.client_id,
                                             "tariff_id": self.tariff_id,
                                             "bike_id": self.bike_id,
                                             "started_on": start.isoformat(),
                                             "pay_amount": "много", "pay_method": "cash",
                                             "mileage": "10", "promo_code": "весна"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("promo=%D0%92%D0%95%D0%A1%D0%9D%D0%90", r.headers["location"])
        self.assertIn(f"started_on={start.isoformat()}", r.headers["location"])
        page = self.get_ok(r.headers["location"])
        self.assertIn("Акция «Весна»", page)

    def test_bigger_promo_wins_without_a_false_error(self):
        _run(self.crm.create_promo(
            kind="promocode", title="Весна", percent=10, amount=None, code="ВЕСНА",
            params={}, starts_on=None, ends_on=None, max_uses=None,
            once_per_client=True, text=None, note=None, by="t"))
        _run(self.crm.create_promo(
            kind="first", title="Новичок", percent=15, amount=None, code=None,
            params={}, starts_on=None, ends_on=None, max_uses=None,
            once_per_client=True, text=None, note=None, by="t"))
        base = f"/issue?client={self.client_id}&tariff={self.tariff_id}&bike={self.bike_id}"
        page = self.get_ok(base + "&promo=ВЕСНА")
        self.assertNotIn("не подходит", page)
        self.assertIn("Акция «Новичок»", page)
        self.assertIn("выгоднее", page)
        self.assertIn('value="2550"', page)

    def test_future_issue_collects_full_price_and_credits_later(self):
        _run(self.crm.create_promo(
            kind="first", title="Новичок", percent=10, amount=None, code=None,
            params={}, starts_on=None, ends_on=None, max_uses=None,
            once_per_client=True, text=None, note=None, by="t"))
        start = date.today() + timedelta(days=2)
        base = f"/issue?client={self.client_id}&tariff={self.tariff_id}&bike={self.bike_id}"
        page = self.get_ok(base + f"&started_on={start.isoformat()}")
        self.assertIn("в день начала", page)
        self.assertIn('value="3000"', page, "скидку с оплаты сейчас не снимаем")
        r = self.client.post("/issue", data={"client_id": self.client_id,
                                             "tariff_id": self.tariff_id,
                                             "bike_id": self.bike_id,
                                             "started_on": start.isoformat(),
                                             "pay_amount": "3000", "pay_method": "cash",
                                             "mileage": "10"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(3000))
        _run(service.charge_all(self.crm, today=start))
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(300),
                         "период списан, скидка легла баллами в плюс")

    def test_code_that_does_not_fit_is_refused_before_money(self):
        _run(self.crm.create_promo(
            kind="promocode", title="Весна", percent=20, amount=None, code="ВЕСНА",
            params={}, starts_on=None, ends_on=None, max_uses=None,
            once_per_client=True, text=None, note=None, by="t"))
        _run(service.open_rental(
            self.crm, client=_run(self.crm.client(self.client_id)),
            bike=_run(self.crm.bike(self.bike_id)),
            tariff=_run(self.crm.tariff(self.tariff_id)),
            started_on=date.today(), contract_no=None, by="t", promo_code="ВЕСНА"))
        rental = _run(self.crm.active_rental_of(self.client_id))
        _run(service.close_rental(self.crm, rental, closed_on=date.today(), note=None,
                                  by="t"))
        r = self.client.post("/issue", data={"client_id": self.client_id,
                                             "tariff_id": self.tariff_id,
                                             "bike_id": self.bike_id,
                                             "pay_amount": "2400", "pay_method": "cash",
                                             "mileage": "10", "promo_code": "ВЕСНА"})
        self.assertEqual(r.status_code, 303)
        self.assertIsNone(_run(self.crm.active_rental_of(self.client_id)),
                          "код не положен - отказ до денег")
        self.assertIn("не подходит", self.get_ok(r.headers["location"]))

    def test_check_says_when_the_code_does_not_fit_this_client(self):
        _run(self.crm.create_promo(
            kind="promocode", title="Весна", percent=20, amount=None, code="ВЕСНА",
            params={}, starts_on=None, ends_on=None, max_uses=None,
            once_per_client=True, text=None, note=None, by="t"))
        # клиент уже получал эту акцию
        _run(service.open_rental(
            self.crm, client=_run(self.crm.client(self.client_id)),
            bike=_run(self.crm.bike(self.bike_id)),
            tariff=_run(self.crm.tariff(self.tariff_id)),
            started_on=date.today(), contract_no=None, by="t", promo_code="ВЕСНА"))
        rental = _run(self.crm.active_rental_of(self.client_id))
        _run(service.close_rental(self.crm, rental, closed_on=date.today(), note=None,
                                  by="t"))
        base = f"/issue?client={self.client_id}&tariff={self.tariff_id}&bike={self.bike_id}"
        page = self.get_ok(base + "&promo=ВЕСНА")
        self.assertIn("этому клиенту не подходит", page)
        self.assertNotIn("Акция «Весна»", page)

    def test_issue_step_shows_discount_and_checks_code(self):
        _run(self.crm.create_promo(
            kind="promocode", title="Весна", percent=20, amount=None, code="ВЕСНА",
            params={}, starts_on=None, ends_on=None, max_uses=None,
            once_per_client=True, text=None, note=None, by="t"))
        base = f"/issue?client={self.client_id}&tariff={self.tariff_id}&bike={self.bike_id}"
        page = self.get_ok(base)
        self.assertIn('name="promo_code"', page)
        self.assertNotIn("Акция «Весна»", page)
        page = self.get_ok(base + "&promo=весна")
        self.assertIn("Акция «Весна»", page)
        self.assertIn("600 ₽", page)
        self.assertIn('value="2400"', page, "к оплате со скидкой")
        page = self.get_ok(base + "&promo=ЛЕТО")
        self.assertIn("не найден", page)
        r = self.client.post("/issue", data={"client_id": self.client_id,
                                             "tariff_id": self.tariff_id,
                                             "bike_id": self.bike_id,
                                             "pay_amount": "2400", "pay_method": "cash",
                                             "mileage": "10", "promo_code": "ЛЕТО"})
        self.assertEqual(r.status_code, 303)
        self.assertIsNone(_run(self.crm.active_rental_of(self.client_id)),
                          "неверный код остановил выдачу до денег")
        r = self.client.post("/issue", data={"client_id": self.client_id,
                                             "tariff_id": self.tariff_id,
                                             "bike_id": self.bike_id,
                                             "pay_amount": "2400", "pay_method": "cash",
                                             "mileage": "10", "promo_code": "весна"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(_run(self.crm.client_balance(self.client_id)), D(0))
        self.assertTrue(any(chat == 5001 and "Весна" in t and "600 ₽" in t
                            for chat, t in self.bot.sent), self.bot.sent)
        rental = _run(self.crm.active_rental_of(self.client_id))
        self.assertIn("ВЕСНА", self.get_ok(f"/rentals/{rental['id']}"))
        self.assertIn("По акции", self.get_ok(f"/clients/{self.client_id}"))

    def test_mailing_bridge_makes_one_template(self):
        r = self.client.post("/promos", data={"kind": "promocode", "title": "Весна",
                                              "percent": "20", "code": "ВЕСНА",
                                              "text": "Код {code} даёт {discount}, {name}!"})
        promo_id = int(r.headers["location"].rsplit("/", 1)[1])
        r = self.client.post(f"/promos/{promo_id}/mailing")
        self.assertEqual(r.headers["location"], "/mailing")
        templates = _run(self.crm.templates())
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]["code"], f"promo_{promo_id}")
        self.assertEqual(templates[0]["body"], "Код ВЕСНА даёт 20 %, {name}!")
        self.client.post(f"/promos/{promo_id}", data={"title": "Весна", "percent": "25",
                                                      "code": "ВЕСНА",
                                                      "text": "Код {code} даёт {discount}"})
        self.client.post(f"/promos/{promo_id}/mailing")
        templates = _run(self.crm.templates())
        self.assertEqual(len(templates), 1, "второй раз - тот же шаблон")
        self.assertEqual(templates[0]["body"], "Код ВЕСНА даёт 25 %")
        self.assertIsNone(templates[0]["body_max"], "MAX берёт основной текст")

    def test_manager_sees_but_does_not_edit(self):
        manager = _run(self.crm.access_profile_by_code("manager"))
        _run(self.crm.create_staff("olga", logic.hash_password("password-1"), "Ольга",
                                   "manager", profile_id=manager["id"]))
        self.client.post("/logout")
        self.assertEqual(self.login("olga", "password-1").status_code, 303)
        page = self.get_ok("/promos")
        self.assertNotIn("Завести по шаблону", page)
        self.assertEqual(self.client.get("/promos/new?kind=first").status_code, 403)
        self.assertEqual(self.client.post("/promos", data={"kind": "first"}).status_code,
                         403)


if __name__ == "__main__":
    unittest.main()
