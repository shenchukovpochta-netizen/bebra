"""Остальное демо (seed_extras) на настоящем Postgres после полного reset().

Модуль пишет SQL в обход кнопок панели, поэтому проверки - те, что держат
сама панель и бот: тревоги трекеров ровно те, что поднял бы опрос, строка
выписки и оплаченный счёт ссылаются на свой платёж, у каждого балла есть
запись журнала, переписка читается ключом демо, ничего не стоит в очереди
бота, история уведомлений - в пределах 30 дней и каталога. Страницы -
тем же ASGI-клиентом, что test_web_pg, под демо-логином. Нужен pgserver.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import asyncpg
    import pgserver
    from httpx import ASGITransport, AsyncClient

    from app.crm import logic, service
    from app.crm.db import CrmDB
    from app.db import _init_connection
    from app.demo import seed, seed_extras
    from app.demo.world import MSK
    from app.web.app import create_app
    from app.web.config import WebConfig
    from tests.test_web import FakeBot, FakeBotDB
    HAVE_PG = True
except ImportError:                                    # pragma: no cover
    HAVE_PG = False

D = Decimal


@unittest.skipUnless(HAVE_PG, "pgserver, asyncpg или httpx не установлены")
class TestDemoExtras(unittest.IsolatedAsyncioTestCase):
    """Один сброс на набор: проверки только читают (кроме детерминизма,
    который сбрасывает теми же зерном и «сейчас» - база та же)."""

    @classmethod
    def setUpClass(cls):
        cls.tz_before = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Moscow"
        time.tzset()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.tmp.name)
        cls.now = datetime.now(MSK).replace(microsecond=0)
        cls.today = cls.now.date()
        cls.summary = asyncio.run(cls._reset())

    @classmethod
    async def _reset(cls):
        pool = await asyncpg.create_pool(cls.pg.get_uri(), min_size=1, max_size=2,
                                         init=_init_connection)
        try:
            return await seed.reset(pool, today=cls.today, now=cls.now)
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
        self.pool = await asyncpg.create_pool(self.pg.get_uri(), min_size=1, max_size=3,
                                              init=_init_connection)
        self.crm = CrmDB(self.pool)

    async def asyncTearDown(self):
        await self.pool.close()

    async def rows(self, sql: str, *args) -> list[dict]:
        return [dict(r) for r in await self.pool.fetch(sql, *args)]

    # ─────────────────────────── время ───────────────────────────

    async def test_nothing_in_the_future(self):
        checks = {
            "trackers": ("last_seen", "moved_at", "blocked_at", "created_at"),
            "tracker_positions": ("recorded_at",),
            "tracker_alerts": ("created_at", "handled_at", "taken_at"),
            "tracker_commands": ("requested_at", "sent_at"),
            "bank_txns": ("booked_at", "handled_at", "created_at"),
            "pay_orders": ("created_at", "sent_at", "paid_at", "checked_at"),
            "card_tokens": ("created_at",), "referrals": ("created_at", "paid_at"),
            "bonuses": ("created_at",), "ledger": ("created_at",),
            "bookings": ("created_at", "handled_at"),
            "sign_requests": ("created_at", "signed_at", "code_at"),
            "sign_events": ("at",),
            "campaigns": ("created_at", "started_at", "finished_at"),
            "campaign_sends": ("sent_at",),
            "inbox_threads": ("created_at", "announced_at", "last_in_at", "last_out_at",
                              "handled_at", "waiting_since"),
            "inbox_messages": ("created_at", "sent_at"),
            "ops_reports": ("created_at",), "notice_log": ("created_at",),
            "saved_views": ("created_at",), "clients": ("created_at",),
            "rentals": ("review_asked_at", "service_invited_at"),
        }
        for table, columns in checks.items():
            for column in columns:
                latest = await self.pool.fetchval(f"select max({column}) from crm.{table}")
                if latest is not None:
                    self.assertLessEqual(latest, self.now, f"{table}.{column}")

    async def test_nothing_before_the_client_card(self):
        """Заявка, подпись, переписка, баллы и рассылка - не раньше, чем
        клиент появился в базе."""
        for table, column in (("bookings", "created_at"), ("sign_requests", "created_at"),
                              ("inbox_threads", "created_at"), ("bonuses", "created_at"),
                              ("campaign_sends", "sent_at"), ("notice_log", "created_at"),
                              ("card_tokens", "created_at"), ("pay_orders", "created_at"),
                              ("bank_txns", "handled_at")):
            early = await self.pool.fetchval(
                f"select count(*) from crm.{table} x join crm.clients c "
                f"on c.id = x.client_id where x.{column} < c.created_at")
            self.assertEqual(early, 0, table)
        self.assertEqual(await self.pool.fetchval(
            "select count(*) from crm.referrals r join crm.clients c on c.id = r.client_id "
            "where r.signed_at < c.created_at or r.created_at > c.created_at"), 0)

    async def test_nothing_waits_for_the_bot(self):
        """Бот к демо не подключается - и забирать ему нечего."""
        one = self.pool.fetchval
        self.assertEqual(await one("select count(*) from crm.tracker_commands "
                                   "where sent_at is null"), 0)
        self.assertEqual(await one("select count(*) from crm.inbox_messages "
                                   "where status in ('queued', 'sending')"), 0)
        self.assertEqual(await one("select count(*) from crm.inbox_threads "
                                   "where announced_at is null"), 0)
        self.assertEqual(await one("select count(*) from crm.campaigns "
                                   "where status = 'sending'"), 0)
        self.assertEqual(await one("select count(*) from crm.card_tokens t join "
                                   "crm.settings s on s.key = 'autocharge' "
                                   "where s.value = '1'"), 0, "автосписание выключено")

    # ─────────────────────────── трекеры ───────────────────────────

    async def test_trackers_raise_exactly_what_the_poll_would(self):
        settings = await self.crm.settings()
        watched = logic.tracker_rows(await self.crm.trackers(active_only=True),
                                     now=self.now, settings=settings)
        self.assertTrue(110 <= len(watched) <= 150, len(watched))
        wanted = {(row["id"], a["kind"]) for row in watched
                  for a in logic.detect_alerts(row, settings=settings)}
        alerts = await self.rows("select * from crm.tracker_alerts")
        opened = {(a["tracker_id"], a["kind"]) for a in alerts if a["handled_at"] is None}
        self.assertEqual(opened, wanted)
        self.assertTrue(3 <= len(alerts) <= 6, alerts)
        self.assertIn("normal", {a["state"] for a in alerts}, "«это норма» в демо есть")
        self.assertTrue(any(a["handled_at"] and a["level"] == "urgent" for a in alerts))
        for a in alerts:
            self.assertEqual(a["level"], logic.alert_level(a["kind"]))
        online = [r for r in watched if not r["offline"]]
        self.assertGreaterEqual(len(online), len(watched) - 3)
        for row in online:
            self.assertLess(self.now - row["last_seen"], timedelta(minutes=15), row["id"])
            self.assertTrue(logic.has_fix(row["lat"], row["lon"]), row["id"])

    async def test_one_tracker_per_bike_and_the_motor_block(self):
        trackers = await self.rows("select t.*, b.status as bike_status, b.tracker_ok "
                                   "from crm.trackers t join crm.bikes b on b.id = t.bike_id")
        self.assertEqual(len({t["bike_id"] for t in trackers}), len(trackers))
        self.assertTrue(all(t["tracker_ok"] for t in trackers))
        self.assertEqual(await self.pool.fetchval(
            "select count(*) from crm.bikes where tracker_ok"), len(trackers))
        for t in trackers:
            self.assertEqual(t["active"], t["bike_status"] != "lost", t["id"])
        commands = await self.rows("select * from crm.tracker_commands")
        self.assertEqual([(c["command"], c["ok"]) for c in commands], [("block", True)])
        blocked = [t for t in trackers if t["blocked"]]
        self.assertEqual([t["id"] for t in blocked], [commands[0]["tracker_id"]])
        self.assertEqual(blocked[0]["blocked_by"], commands[0]["requested_by"])

    async def test_tracks_today_are_in_kazan(self):
        """Сегодняшние точки - вокруг Казани; свободные стоят у своей точки.

        «Сегодня» - с полуночи, но не короче 15 минут: у живого трекера
        последняя точка не старше 9 минут, а в первые минуты суток она
        ещё вчерашняя, и счёт «с полуночи» ронял тест с 00:00 до 00:09."""
        places = {p["name"]: p for p in await self.crm.locations()}
        since = min(datetime.combine(self.today, datetime.min.time(), MSK),
                    self.now - timedelta(minutes=15))
        rows = await self.rows(
            """
            select t.id, b.status, b.location, t.lat, t.lon,
                   (select count(*) from crm.tracker_positions p
                     where p.tracker_id = t.id and p.recorded_at >= $1) as today
              from crm.trackers t join crm.bikes b on b.id = t.bike_id where t.active
            """, since)
        with_today = [r for r in rows if r["today"]]
        self.assertGreaterEqual(len(with_today), len(rows) * 0.9)
        for r in rows:
            if r["lat"] is None:
                continue
            self.assertLess(logic.distance_km(r["lat"], r["lon"], 55.79, 49.12), 25, r)
            if r["status"] == "available" and r["location"] in places:
                place = places[r["location"]]
                self.assertLess(logic.distance_km(r["lat"], r["lon"], float(place["lat"]),
                                                  float(place["lon"])), 0.2, r)
        oldest = await self.pool.fetchval("select min(recorded_at) from crm.tracker_positions")
        self.assertGreaterEqual(oldest, self.now - timedelta(days=30, minutes=1))

    # ─────────────────────────── банк, счета ───────────────────────────

    async def test_bank_rows_point_at_their_payments(self):
        txns = await self.rows("select * from crm.bank_txns")
        by_status = Counter((t["direction"], t["status"]) for t in txns)
        self.assertTrue(3 <= by_status[("credit", "new")] <= 5, by_status)
        self.assertGreater(by_status[("credit", "matched")], 30, by_status)
        for t in txns:
            self.assertGreaterEqual(t["booked_at"], self.now - timedelta(days=30, minutes=1))
            if t["status"] != "matched":
                self.assertIsNone(t["ledger_id"], t["id"])
                continue
            entry = await self.pool.fetchrow("select * from crm.ledger where id = $1",
                                             t["ledger_id"])
            self.assertEqual((entry["kind"], entry["method"], entry["client_id"],
                              entry["amount"]),
                             ("payment", "transfer", t["client_id"], t["amount"]))
            self.assertEqual(entry["note"], f"Выписка банка: {t['purpose']}")
            self.assertEqual(entry["created_at"], t["handled_at"])
        self.assertEqual(len({t["txn_id"] for t in txns}), len(txns))

    async def test_pay_orders_and_cards(self):
        orders = await self.rows("select * from crm.pay_orders order by created_at, id")
        self.assertEqual([o["no"] for o in orders],
                         [logic.pay_no(n) for n in range(1, len(orders) + 1)],
                         "номера СЧТ - по времени")
        for o in orders:
            if o["status"] != "paid" or o["work_order_id"] is not None:
                self.assertIsNone(o["ledger_id"], o["no"])
                continue
            entry = await self.pool.fetchrow("select * from crm.ledger where id = $1",
                                             o["ledger_id"])
            self.assertEqual((entry["kind"], entry["method"], entry["amount"],
                              entry["rental_id"], entry["note"], entry["created_by"]),
                             ("payment", "card", o["amount"], o["rental_id"],
                              f"Счёт {o['no']}", "эквайринг"), o["no"])
            self.assertEqual(entry["created_at"], o["paid_at"])
        waiting = [o for o in orders if o["status"] in ("new", "sent")]
        self.assertEqual(len(waiting), 1)
        self.assertLess(self.now - waiting[0]["created_at"], timedelta(hours=24))
        failed = [o for o in orders if o["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0]["error"])
        cards = await self.rows("select * from crm.card_tokens")
        self.assertTrue(1 <= len(cards) <= 8)
        self.assertEqual(len({c["client_id"] for c in cards}), len(cards))

    # ─────────────────────────── баллы ───────────────────────────

    async def test_every_bonus_has_its_ledger_entry(self):
        bonuses = await self.rows(
            "select b.*, l.kind as l_kind, l.amount as l_amount, l.client_id as l_client, "
            "l.period_from as l_from from crm.bonuses b join crm.ledger l "
            "on l.id = b.ledger_id")
        self.assertEqual(len(bonuses),
                         await self.pool.fetchval("select count(*) from crm.bonuses"))
        self.assertEqual(len(bonuses), await self.pool.fetchval(
            "select count(*) from crm.ledger where kind = 'bonus'"))
        kinds = Counter(b["kind"] for b in bonuses)
        self.assertEqual(set(kinds), {"promo", "referral", "review"})
        for b in bonuses:
            self.assertEqual((b["l_kind"], b["l_amount"], b["l_client"]),
                             ("bonus", b["amount"], b["client_id"]), b["id"])
            if b["kind"] == "promo":
                # Скидка ложится с начислением периода той же аренды.
                charge = await self.pool.fetchrow(
                    "select * from crm.ledger where kind = 'charge' and rental_id = $1 "
                    "and period_from = $2", b["rental_id"], b["period_from"])
                self.assertIsNotNone(charge, b["id"])
                self.assertEqual(charge["created_at"], b["created_at"])
                self.assertEqual(b["l_from"], b["period_from"])
        reviews = [b["client_id"] for b in bonuses if b["kind"] == "review"]
        self.assertEqual(len(reviews), len(set(reviews)), "отзыв - раз на клиента")
        promos = await self.rows("select * from crm.promos")
        self.assertEqual(len(promos), 2)
        self.assertTrue(all(p["active"] for p in promos))
        for p in promos:
            uses = sum(1 for b in bonuses if b["promo_id"] == p["id"])
            self.assertGreater(uses, 0, p["title"])
            if p["max_uses"] is not None:
                self.assertLessEqual(uses, p["max_uses"])
            if p["code"]:
                self.assertEqual(uses, await self.pool.fetchval(
                    "select count(*) from crm.rentals where promo_code = $1", p["code"]))

    async def test_referral_funnel(self):
        refs = await self.rows("select * from crm.referrals")
        friends = [r for r in refs if r["client_id"] is not None]
        self.assertTrue(10 <= len(friends) <= 20, len(friends))
        statuses = Counter(r["status"] for r in refs)
        self.assertGreater(statuses["click"], 0)
        self.assertGreater(statuses["signed"], 0)
        self.assertGreater(statuses["paid"], 0)
        invited = {r["id"]: r for r in await self.rows(
            "select id, invited_by, tg_id from crm.clients where invited_by is not null")}
        self.assertEqual(set(invited), {r["client_id"] for r in friends},
                         "агент у клиента - только через переход по ссылке")
        for r in friends:
            client = invited[r["client_id"]]
            self.assertEqual((client["invited_by"], client["tg_id"]),
                             (r["agent_id"], r["tg_id"]))
            if r["status"] == "paid":
                entry = await self.pool.fetchrow("select * from crm.ledger where id = $1",
                                                 r["ledger_id"])
                self.assertEqual((entry["kind"], entry["client_id"], entry["amount"]),
                                 ("bonus", r["agent_id"], r["bonus"]))

    # ─────────────────────────── заявки, ПЭП ───────────────────────────

    async def test_open_bookings(self):
        rows = await self.rows(
            """
            select k.*, exists (select 1 from crm.rentals r where r.client_id = k.client_id
                                 and r.status = 'active') as renting
              from crm.bookings k
            """)
        opened = [r for r in rows if r["status"] == "new"]
        self.assertTrue(3 <= len(opened) <= 4, opened)
        self.assertEqual(len({r["client_id"] for r in opened}), len(opened))
        self.assertFalse(any(r["renting"] for r in opened), "заявка - у тех, кто без аренды")
        places = {p["id"] for p in await self.crm.locations()}
        self.assertTrue(all(r["location_id"] in places for r in rows))
        self.assertTrue(all(r["rental_id"] for r in rows if r["status"] == "done"))

    async def test_signed_requests_are_protocols(self):
        requests = await self.rows("select * from crm.sign_requests order by created_at")
        self.assertEqual([r["no"] for r in requests],
                         [logic.sign_no(n) for n in range(1, len(requests) + 1)])
        signed = [r for r in requests if r["status"] == "signed"]
        self.assertGreaterEqual(len(signed), 5)
        # Ждущая кода - самая свежая выдача: ссылка переживает ночной сброс,
        # а не истекает к вечеру того же дня.
        waiting = [r for r in requests if r["status"] == "code"]
        self.assertEqual(len(waiting), 1)
        self.assertGreater(waiting[0]["expires_at"], self.now + timedelta(days=1))
        for r in requests:
            self.assertEqual(len(r["token"]), 32)
            events = await self.crm.sign_events(r["id"])
            kinds = [e["kind"] for e in sorted(events, key=lambda e: (e["at"], e["id"]))]
            self.assertEqual(kinds[0], "created", r["no"])
            docs = r["docs"]
            self.assertEqual(docs[0]["sha256"], seed_extras.esign.sha256_text(r["agreement"]))
            if r["status"] == "signed":
                self.assertIsNone(r["code_hash"])
                self.assertEqual(kinds[-1], "signed")
                last = max(events, key=lambda e: (e["at"], e["id"]))
                self.assertEqual(last["note"], f"хэш пакета {logic.sign_docs_digest(docs)}")

    # ─────────────────────────── входящие, рассылки ───────────────────────────

    async def test_inbox_reads_with_the_demo_key(self):
        threads = await self.rows("select * from crm.inbox_threads")
        self.assertTrue(12 <= len(threads) <= 18, len(threads))
        self.assertEqual({t["channel"] for t in threads}, {"avito", "tg", "max", "wa"})
        self.assertTrue({"new", "work", "done"} <= {t["status"] for t in threads})
        vault = service.inbox_vault(seed_extras.inbox_key_text(seed_extras.DEMO_SECRET))
        messages = await self.rows("select * from crm.inbox_messages order by id")
        texts = [service.inbox_open(vault, m["body_enc"]) for m in messages
                 if m["body_enc"]]
        self.assertGreater(len(texts), 20)
        self.assertFalse([t for t in texts if t.startswith("[")], "всё расшифровано")
        # Ключ - производный от секрета: чужой секрет текста не откроет.
        stranger = service.inbox_vault(seed_extras.inbox_key_text("чужой секрет"))
        sealed = next(m["body_enc"] for m in messages if m["body_enc"])
        with self.assertLogs("app.services.crypto", level="ERROR"):
            self.assertEqual(service.inbox_open(stranger, sealed), "[не расшифровано]")
        last = {}
        for m in messages:
            last[m["thread_id"]] = m
        for t in threads:
            if t["status"] in ("done", "spam"):
                self.assertIsNone(t["waiting_since"], t["id"])
            elif last[t["id"]]["direction"] == "in":
                self.assertIsNotNone(t["waiting_since"], t["id"])
            if t["channel"] == "wa":
                self.assertEqual(t["ext_id"], t["phone"])

    async def test_mailing(self):
        campaigns = await self.rows("select * from crm.campaigns order by id")
        self.assertEqual(Counter(c["status"] for c in campaigns), {"done": 2, "draft": 1})
        for c in campaigns:
            sends = await self.crm.campaign_sends(c["id"])
            self.assertTrue(sends, c["no"])
            statuses = {s["status"] for s in sends}
            if c["status"] == "done":
                self.assertNotIn("queued", statuses, c["no"])
                self.assertIn("sent", statuses)
            else:
                self.assertEqual(statuses, {"queued"})

    # ─────────────────────────── журналы ───────────────────────────

    async def test_notices_follow_the_catalog(self):
        rows = await self.rows("select * from crm.notice_log")
        codes = Counter(r["code"] for r in rows)
        for code in ("rent_soon", "rent_due", "pay_credited", "pay_paid", "review_ask",
                     "inbox_new", "booking_new", "daily_digest"):
            self.assertGreater(codes[code], 0, code)
        for r in rows:
            self.assertIn(r["code"], logic.NOTICES)
            self.assertEqual(r["target"], logic.NOTICES[r["code"]]["target"])
            self.assertGreaterEqual(r["created_at"], self.now - timedelta(days=30))
            if r["status"] == "skipped":
                self.assertEqual(r["target"], "client")
        # Напоминание - одно в день на аренду, как у remind_once.
        per_day: dict[tuple, int] = defaultdict(int)
        for r in rows:
            if r["code"] in ("rent_soon", "rent_due", "rent_overdue"):
                per_day[(r["client_id"], r["created_at"].astimezone(MSK).date())] += 1
        self.assertLessEqual(max(per_day.values()), 1)

    async def test_ops_reports(self):
        rows = await self.rows("select * from crm.ops_reports order by created_at")
        kinds = Counter((r["kind"], r["ok"]) for r in rows)
        self.assertGreater(kinds[("fix", True)], 0)
        self.assertEqual(kinds[("fix", False)], 1)
        self.assertEqual(kinds[("return", False)], 1)
        self.assertGreater(kinds[("daily", True)], 0)
        self.assertEqual(len({(r["chat_id"], r["message_id"]) for r in rows}), len(rows))
        self.assertEqual([r["message_id"] for r in rows], sorted(r["message_id"] for r in rows))
        for r in rows:
            data = r["data"]
            self.assertNotIn("phones", data)
            if r["kind"] == "fix" and r["ok"]:
                bike = await self.crm.bike(r["bike_id"])
                self.assertEqual(logic.vin_key(data["vin_frame"]),
                                 logic.vin_key(bike["frame_no"]))
            if r["kind"] == "return":
                self.assertIn("debt_paid_sum", data)

    async def test_claims_wait_for_the_operator(self):
        """Две заявки «Я оплатил(а)»: из кабинета бота (Telegram есть),
        у должника с идущей арендой, в прошлом и без записи в журнале."""
        claims = await self.rows(
            "select p.*, c.tg_id, r.id as rental_id from crm.payment_claims p "
            "join crm.clients c on c.id = p.client_id left join crm.rentals r "
            "on r.client_id = p.client_id and r.status = 'active'")
        self.assertEqual(len(claims), 2)
        for claim in claims:
            self.assertEqual(claim["status"], "pending")
            self.assertIsNotNone(claim["tg_id"])
            self.assertIsNotNone(claim["rental_id"])
            self.assertIsNone(claim["ledger_id"])
            self.assertLessEqual(claim["created_at"], self.now)
            self.assertLess(await self.crm.client_balance(claim["client_id"]), 0)

    async def test_integrity_still_clean(self):
        issues = logic.integrity_issues(
            await self.crm.bikes(limit=10000), await self.crm.active_rentals(),
            await self.crm.open_orders_by_bike(), await self.crm.debtors(200),
            batteries=await self.crm.batteries(limit=10000))
        kinds = Counter(i["kind"] for i in issues)
        self.assertIn(kinds.pop("debt_without_rental", 0), (1, 2))
        self.assertEqual(dict(kinds), {})

    # ─────────────────────────── страницы ───────────────────────────

    async def test_pages_open_for_the_demo_owner(self):
        cfg = WebConfig(pg={}, secret="test-secret", admin_login="admin",
                        admin_password="admin-pass-123", bot_token="",
                        storage_dir=Path("/tmp/kyc"), port=8080, remind_before_days=2,
                        inbox_key=seed_extras.inbox_key_text(seed_extras.DEMO_SECRET))
        app = create_app(crm=self.crm, db=FakeBotDB(), cfg=cfg, bot=FakeBot())
        one = self.pool.fetchval
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               follow_redirects=False) as client:
            r = await client.post("/login", data={"login": "demo", "password": "demo"})
            self.assertEqual(r.status_code, 303)
            thread = await one("select t.id from crm.inbox_threads t join "
                               "crm.inbox_messages m on m.thread_id = t.id "
                               "where m.body_enc is not null order by t.id limit 1")
            text = service.inbox_open(
                service.inbox_vault(cfg.inbox_key),
                await one("select body_enc from crm.inbox_messages where thread_id = $1 "
                          "and body_enc is not null order by id limit 1", thread))
            page = await client.get(f"/inbox/{thread}")
            self.assertEqual(page.status_code, 200)
            self.assertIn(text.split()[0], page.text, "переписка читается в панели")
            pages = ["/", "/inbox", "/inbox?tab=all", "/bookings", "/ops", "/signings",
                     "/mailing", "/promos", "/bank", "/bank?status=all", "/payments",
                     "/notices", "/map", "/alerts", "/trackers", "/reports/referrals",
                     "/reports/channels", "/rentals?status=active&view=debt"]
            pages += [f"/trackers/{i}" for i in (
                await one("select id from crm.trackers where blocked"),
                await one("select tracker_id from crm.tracker_alerts "
                          "where state = 'normal'"),
                await one("select id from crm.trackers where active order by id limit 1"))]
            pages += [f"/trackers/{await one('select id from crm.trackers limit 1')}"
                      "?period=week"]
            pages += [f"/inbox/{r['id']}" for r in await self.rows(
                "select id from crm.inbox_threads")]
            pages += [f"/mailing/{r['id']}" for r in await self.rows(
                "select id from crm.campaigns")]
            pages += [f"/promos/{r['id']}" for r in await self.rows(
                "select id from crm.promos")]
            pages += [f"/signings/{r['id']}" for r in await self.rows(
                "select distinct on (status) id from crm.sign_requests")]
            pages += [f"/payments/{r['id']}" for r in await self.rows(
                "select distinct on (status) id from crm.pay_orders")]
            pages += [f"/clients/{r['agent_id']}" for r in await self.rows(
                "select agent_id from crm.referrals where status = 'paid' limit 2")]
            promo_rental = await one("select rental_id from crm.bonuses "
                                     "where kind = 'promo' limit 1")
            pages.append(f"/rentals/{promo_rental}")
            pages += [v["section"] + "?" + v["query"] for v in await self.rows(
                "select section, query from crm.saved_views")]
            for path in pages:
                r = await client.get(path)
                self.assertEqual(r.status_code, 200, f"{path}: {r.status_code}")
            token = await one("select token from crm.sign_requests where status = 'code'")
            async with AsyncClient(transport=ASGITransport(app=app),
                                   base_url="http://test") as guest:
                r = await guest.get(f"/sign/{token}")
                self.assertEqual(r.status_code, 200, "ссылка клиента на подпись открыта")

    # ─────────────────────────── повтор ───────────────────────────

    async def test_same_seed_same_extras(self):
        """Шифротекст переписки случаен (nonce AES-GCM), текст - нет."""
        async def digest() -> list:
            vault = service.inbox_vault(seed_extras.inbox_key_text(seed_extras.DEMO_SECRET))
            texts = [service.inbox_open(vault, r["body_enc"]) for r in await self.pool.fetch(
                "select body_enc from crm.inbox_messages order by id")]
            row = await self.pool.fetchrow(
                """
                select (select md5(string_agg(concat_ws('|', id, device_id, bike_id,
                                                        last_seen, lat, lon, moved_at),
                                              ',' order by id)) from crm.trackers),
                       (select count(*) from crm.tracker_positions),
                       (select md5(string_agg(concat_ws('|', id, kind, state, created_at),
                                              ',' order by id)) from crm.tracker_alerts),
                       (select md5(string_agg(concat_ws('|', id, txn_id, amount, status,
                                                        ledger_id), ',' order by id))
                          from crm.bank_txns),
                       (select md5(string_agg(concat_ws('|', id, no, status, ledger_id),
                                              ',' order by id)) from crm.pay_orders),
                       (select md5(string_agg(concat_ws('|', id, client_id, kind, amount,
                                                        created_at), ',' order by id))
                          from crm.bonuses),
                       (select md5(string_agg(concat_ws('|', id, code, client_id, status,
                                                        created_at), ',' order by id))
                          from crm.notice_log),
                       (select md5(string_agg(concat_ws('|', id, kind, data::text),
                                              ',' order by id)) from crm.ops_reports)
                """)
            return [*row, texts]

        before = await digest()
        again = await seed.reset(self.pool, today=self.today, now=self.now)
        self.assertEqual(again, self.summary)
        self.assertEqual(await digest(), before)


if __name__ == "__main__":
    unittest.main()
