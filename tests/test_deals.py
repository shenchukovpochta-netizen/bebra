"""Воронка «Входящих» (app/crm/deals.py): сделки по этапам от заявки до
сдачи. Правила сверки - чистые функции, доска и перенос - через панель."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import deals

try:
    from tests.test_web import HAVE_WEB, WebCase, run
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False
    WebCase = unittest.TestCase                        # type: ignore[misc,assignment]

NOW = datetime(2026, 10, 5, 9, tzinfo=UTC)


def at(hours_ago):
    return NOW - timedelta(hours=hours_ago)


class TestRules(unittest.TestCase):
    def test_manual_moves_only_before_rental(self):
        self.assertIsNone(deals.can_move({"stage": "new"}, "touch"))
        self.assertIsNone(deals.can_move({"stage": "touch"}, "lost"))
        self.assertIsNone(deals.can_move({"stage": "deferred"}, "contract"))
        self.assertIn("ставит сама аренда", deals.can_move({"stage": "new"}, "rented"))
        self.assertIn("ставит сама аренда", deals.can_move({"stage": "touch"}, "returned"))
        self.assertIn("ставит аренда", deals.can_move({"stage": "rented"}, "lost"))
        self.assertEqual(deals.can_move({"stage": "new"}, "zzz"), "Такого этапа нет.")

    def test_renewed_is_second_period_from_issue(self):
        rental = {"started_on": date(2026, 9, 1), "billed_until": date(2026, 9, 8),
                  "period_days": 7, "issue_period_days": 7}
        self.assertIsNone(deals.renewed(rental))
        rental["billed_until"] = date(2026, 9, 15)
        self.assertEqual(deals.renewed(rental), date(2026, 9, 8))
        # смена тарифа на месяц после выдачи на неделю: срок - при выдаче
        rental.update(period_days=30, billed_until=date(2026, 10, 8))
        self.assertEqual(deals.renewed(rental), date(2026, 9, 8))

    def test_rental_without_deal_starts_at_rented(self):
        ops = deals.plan_rentals([], [{"id": 7, "client_id": 3, "bike_model": "Kugoo",
                                       "location": "Павлюхина", "created_at": at(5),
                                       "started_on": date(2026, 10, 5),
                                       "billed_until": date(2026, 10, 12),
                                       "period_days": 7}], {})
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["op"], "create")
        self.assertEqual(ops[0]["stage"], "rented")
        self.assertEqual(ops[0]["at"], at(5), "время этапа - выдача, а не сверка")
        self.assertEqual(ops[0]["fields"]["rental_id"], 7)

    def test_early_deal_moves_to_rented_and_returned(self):
        early = {"id": 1, "stage": "touch", "client_id": 3, "closed_at": None}
        rental = {"id": 7, "client_id": 3, "created_at": at(2),
                  "started_on": date(2026, 10, 5), "billed_until": date(2026, 10, 12),
                  "period_days": 7, "location": "Павлюхина"}
        ops = deals.plan_rentals([early], [rental], {})
        self.assertEqual([(o["op"], o["stage"]) for o in ops], [("move", "rented")])
        self.assertEqual(ops[0]["set"], {"rental_id": 7, "location": "Павлюхина"})
        renting = {**early, "stage": "rented", "rental_id": 7}
        self.assertEqual(deals.plan_rentals([renting], [rental], {}), [], "повтор - тишина")
        closed = {7: {**rental, "status": "closed", "closed_at": at(1)}}
        ops = deals.plan_rentals([renting], [], closed)
        self.assertEqual([(o["stage"], o["close"]) for o in ops], [("returned", True)])
        self.assertEqual(ops[0]["at"], at(1))

    def test_renewal_moves_rented_deal(self):
        deal = {"id": 1, "stage": "rented", "client_id": 3, "rental_id": 7,
                "closed_at": None}
        rental = {"id": 7, "client_id": 3, "started_on": date(2026, 9, 1),
                  "billed_until": date(2026, 9, 15), "period_days": 7}
        ops = deals.plan_rentals([deal], [rental], {})
        self.assertEqual([(o["op"], o["stage"]) for o in ops], [("move", "renewed")])

    def test_booking_new_and_cancelled(self):
        booking = {"id": 4, "client_id": 3, "model": "Kugoo", "tariff_name": "Неделя",
                   "location_name": "Адоратского", "created_at": at(3)}
        ops = deals.plan_bookings([], [booking], {})
        self.assertEqual(ops[0]["stage"], "new")
        self.assertEqual(ops[0]["fields"]["title"], "Kugoo · Неделя")
        # у клиента уже идёт ранняя сделка - заявка к ней, а не вторая
        early = {"id": 1, "stage": "touch", "client_id": 3, "closed_at": None}
        ops = deals.plan_bookings([early], [booking], {})
        self.assertEqual(ops, [{"op": "link", "id": 1, "set": {
            "booking_id": 4, "title": "Kugoo · Неделя", "location": "Адоратского"}}])
        linked = {**early, "booking_id": 4}
        ops = deals.plan_bookings([linked], [], {4: {"status": "cancelled",
                                                     "handled_at": at(1)}})
        self.assertEqual([(o["stage"], o["close"]) for o in ops], [("lost", True)])
        # выданная заявка (done) - не «Не взял»: её переведёт аренда
        self.assertEqual(deals.plan_bookings([linked], [], {4: {"status": "done"}}), [])

    def test_threads(self):
        fresh = {"id": 9, "status": "new", "created_at": at(1), "name": "Азиз",
                 "phone": "+79990000009", "channel": "wa", "subject": None}
        ops = deals.plan_threads([], [fresh], since=at(48))
        self.assertEqual((ops[0]["stage"], ops[0]["fields"]["source"]), ("new", "wa"))
        # ответ из панели или из чата - «1-е касание»
        deal = {"id": 1, "stage": "new", "thread_id": 9, "closed_at": None}
        ops = deals.plan_threads([deal], [{**fresh, "status": "work",
                                           "last_out_at": at(0)}], since=at(48))
        self.assertEqual([(o["op"], o["stage"]) for o in ops], [("move", "touch")])
        # спам - «Не взял»
        ops = deals.plan_threads([deal], [{**fresh, "status": "spam"}], since=at(48))
        self.assertEqual([(o["stage"], o["close"]) for o in ops], [("lost", True)])
        # закрытое обращение до первой сверки сделку не заводит
        old = {**fresh, "id": 10, "status": "done", "created_at": at(100)}
        self.assertEqual(deals.plan_threads([], [old], since=at(48)), [])
        # пишет арендатор - обращение к его сделке аренды, а не новая заявка
        renting = {"id": 2, "stage": "rented", "client_id": 3, "closed_at": None}
        ops = deals.plan_threads([renting], [{**fresh, "client_id": 3}], since=at(48))
        self.assertEqual(ops, [{"op": "link", "id": 2, "set": {"thread_id": 9}}])

    def test_contract_signed_after_deal(self):
        deal = {"id": 1, "stage": "touch", "client_id": 3, "created_at": at(5),
                "closed_at": None}
        self.assertEqual([o["stage"] for o in deals.plan_contracts([deal], {3: at(1)})],
                         ["contract"])
        self.assertEqual(deals.plan_contracts([deal], {3: at(50)}), [],
                         "договор, подписанный до сделки, - старый клиент, а не этап")

    def test_board_and_filters(self):
        rows = [{"id": 1, "stage": "new", "stage_at": at(1), "name": "Азиз",
                 "phone": "+79990000009", "source": "wa", "thread_id": 9,
                 "responsible_id": 5, "location": "Павлюхина"},
                {"id": 2, "stage": "returned", "stage_at": at(24 * 40),
                 "closed_at": at(24 * 40), "client_name": "Старый", "source": "rental"},
                {"id": 3, "stage": "new", "stage_at": at(0), "client_name": "Иван",
                 "client_id": 3, "source": "manual"}]
        cols = {c["code"]: c for c in deals.board(rows, now=NOW)}
        self.assertEqual([d["id"] for d in cols["new"]["cards"]], [3, 1], "свежие сверху")
        self.assertEqual(cols["returned"]["count"], 0, "сданные давнее срока не на доске")
        self.assertTrue(cols["rented"]["auto"])
        hidden = deals.dress(rows[0], inbox_ok=False, now=NOW)
        self.assertEqual((hidden["who"], hidden["phone_shown"]), ("Обращение", None),
                         "имя из канала - только тому, кому открыта переписка")
        shown = deals.dress(rows[0], inbox_ok=True, now=NOW)
        self.assertEqual(shown["who"], "Азиз")
        self.assertTrue(deals.matches(shown, q="0009"))
        self.assertTrue(deals.matches(shown, who="5", location="Павлюхина"))
        self.assertFalse(deals.matches(shown, who="me", me=6))
        self.assertFalse(deals.matches(shown, who="none"))
        self.assertFalse(deals.matches(shown, location="none"))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestBoardPages(WebCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def as_(self, login, code):
        from app.crm import logic
        profile = run(self.crm.access_profile_by_code(code))
        run(self.crm.create_staff(login, logic.hash_password("password-1"), login,
                                  "manager", profile["id"]))
        self.client.post("/logout")
        self.login(login, "password-1")

    def rent(self, client_id, bike_id, days_ago=1):
        run(self.crm.create_rental(client_id=client_id, bike_id=bike_id,
                                   tariff_id=self.tariff_id, tariff_name="Неделя",
                                   period_days=7, price=D(3000), billing="auto",
                                   started_on=date.today() - timedelta(days=days_ago),
                                   contract_no=None, created_by="t"))
        return max(self.crm.rentals_)

    def test_sync_follows_rentals_and_is_idempotent(self):
        self.login()
        rid = self.rent(self.client_id, self.bike_id)
        self.assertIn("Иванов Иван", self.get_ok("/incoming"))
        self.get_ok("/incoming")
        self.assertEqual(len(self.crm.deals_), 1, "повторная сверка не задваивает")
        deal = next(iter(self.crm.deals_.values()))
        self.assertEqual((deal["stage"], deal["rental_id"]), ("rented", rid))
        run(self.crm.close_rental(rid, closed_on=date.today(), note=None))
        self.get_ok("/incoming")
        self.assertEqual(deal["stage"], "returned")
        self.assertIsNotNone(deal["closed_at"])
        stages = [x["to_stage"] for x in run(self.crm.deal_log(deal["id"]))]
        self.assertEqual(stages, ["rented", "returned"])

    def test_quick_add_move_and_issue(self):
        self.login()
        r = self.client.post("/deals", data={"name": "Пётр", "phone": "8 999 111-22-33",
                                             "title": "Kugoo на месяц"})
        self.assertEqual(r.status_code, 303)
        deal_id = int(r.headers["location"].rsplit("/", 1)[1])
        deal = self.crm.deals_[deal_id]
        self.assertEqual((deal["stage"], deal["phone"], deal["name"]),
                         ("new", "+79991112233", "Пётр"))
        self.assertEqual(deal["responsible_id"], 1, "ответственный - кто завёл")
        card = self.get_ok(f"/deals/{deal_id}")
        self.assertIn("Kugoo на месяц", card)
        self.assertIn("/issue?phone=%2B79991112233", card, "выдача прямо из сделки")
        # перетаскивание: ранний этап - да, «В аренде» - только выдачей
        ok = self.client.post(f"/deals/{deal_id}/stage", data={"stage": "touch"},
                              headers={"Accept": "application/json"})
        self.assertEqual(ok.json(), {"ok": True, "error": None})
        refused = self.client.post(f"/deals/{deal_id}/stage", data={"stage": "rented"},
                                   headers={"Accept": "application/json"})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("ставит сама аренда", refused.json()["error"])
        self.assertEqual(deal["stage"], "touch")
        # тот же телефон второй раз - та же сделка, а не дубль
        again = self.client.post("/deals", data={"phone": "+79991112233"})
        self.assertEqual(again.headers["location"], f"/deals/{deal_id}")
        # известный клиент по телефону - сделка на его карточку
        r = self.client.post("/deals", data={"phone": "+79990000000"})
        known = self.crm.deals_[int(r.headers["location"].rsplit("/", 1)[1])]
        self.assertEqual((known["client_id"], known["name"], known["phone"]),
                         (self.client_id, None, None))
        # выдача клиенту переводит его сделку в «В аренде»
        self.rent(self.client_id, self.bike_id)
        self.get_ok("/incoming")
        self.assertEqual(known["stage"], "rented")

    def test_booking_deal_and_rights(self):
        run(self.crm.create_booking(client_id=self.client_id, model="Kugoo V3",
                                    tariff_id=self.tariff_id, location_id=None,
                                    wanted_on=date.today()))
        thread = run(self.crm.inbox_record(channel="wa", origin="hook",
                                           ext_id="+79990000009", direction="in",
                                           name="Азиз Курьеров", phone="+79990000009"))
        self.login()
        board = self.get_ok("/incoming")
        self.assertIn("Азиз Курьеров", board, "владельцу переписка открыта")
        self.assertIn("Kugoo V3", board)
        tdeal = next(d for d in self.crm.deals_.values()
                     if d["thread_id"] == thread["thread_id"])
        self.as_("oper", "manager")
        board = self.get_ok("/incoming")
        self.assertNotIn("Азиз Курьеров", board, "оператору имя из канала закрыто")
        self.assertNotIn("+79990000009", self.get_ok(f"/deals/{tdeal['id']}"))
        self.assertIn("Быстрое добавление", board)
        self.as_("mech", "tech")
        self.assertEqual(self.client.get("/incoming").status_code, 403)
        r = self.client.post(f"/deals/{tdeal['id']}/stage", data={"stage": "lost"},
                             headers={"Accept": "application/json"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(tdeal["stage"], "new")


if __name__ == "__main__":
    unittest.main()
