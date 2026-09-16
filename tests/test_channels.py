"""Откуда приходят клиенты: канал привлечения и отчёт по каналам.

Отчёт отвечает на вопрос «куда давать рекламу». Незаполненный канал
показывается честным пробелом, а не размазывается по известным.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

TODAY = date(2026, 9, 16)


def client(month: int, channel: str | None = None, day: int = 5) -> dict:
    return {"channel": channel,
            "created_at": datetime(2026, month, day, 12, tzinfo=UTC)}


class TestChannelLogic(unittest.TestCase):
    def test_columns_hold_only_used_channels(self):
        data = logic.channel_rows([client(9, "avito"), client(9, "avito"),
                                   client(8, "2gis")], months=3, today=TODAY)
        self.assertEqual(data["columns"], ["avito", "2gis"])
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["totals"]["avito"], 2)

    def test_unknown_channel_is_an_honest_gap(self):
        data = logic.channel_rows([client(9), client(9, "лунапарк")],
                                  months=2, today=TODAY)
        self.assertEqual(data["columns"], [""])
        self.assertEqual(data["totals"][""], 2)
        self.assertEqual(logic.channel_label(""), "не спросили")

    def test_months_go_in_order_and_cover_the_window(self):
        data = logic.channel_rows([], months=12, today=TODAY)
        self.assertEqual(len(data["rows"]), 12)
        self.assertEqual(data["rows"][-1]["month"], date(2026, 9, 1))
        self.assertEqual(data["rows"][0]["month"], date(2025, 10, 1))

    def test_clients_outside_the_window_are_ignored(self):
        old = {"channel": "avito", "created_at": datetime(2024, 1, 1, tzinfo=UTC)}
        data = logic.channel_rows([old, client(9, "avito")], months=3, today=TODAY)
        self.assertEqual(data["total"], 1)

    def test_channel_is_checked(self):
        self.assertTrue(logic.check_channel("avito").ok)
        self.assertIsNone(logic.check_channel("").value)
        self.assertFalse(logic.check_channel("лунапарк").ok)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestChannelsInPanel(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def test_channel_is_saved_from_the_card(self):
        r = self.client.post(f"/clients/{self.client_id}/edit", data={
            "full_name": "Иванов Иван", "phone": "+79990000000",
            "status": "active", "channel": "avito", "note": ""})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(tw.run(self.crm.client(self.client_id))["channel"], "avito")

    def test_bad_channel_is_refused(self):
        r = self.client.post(f"/clients/{self.client_id}/edit", data={
            "full_name": "Иванов Иван", "phone": "+79990000000",
            "status": "active", "channel": "лунапарк", "note": ""})
        self.assertIn("Канал привлечения", self.get_ok(r.headers["location"]))
        self.assertIsNone(tw.run(self.crm.client(self.client_id))["channel"])

    def test_new_client_keeps_the_channel(self):
        r = self.client.post("/clients", data={
            "full_name": "Петров Пётр", "phone": "+79995554433",
            "status": "active", "channel": "2gis", "note": ""})
        self.assertEqual(r.status_code, 303)
        client_id = int(r.headers["location"].rsplit("/", 1)[1])
        self.assertEqual(tw.run(self.crm.client(client_id))["channel"], "2gis")

    def test_invited_friend_gets_the_referral_channel(self):
        """Клиента привёл друг - канал известен без вопросов."""
        agent_id = tw.run(self.crm.create_client(full_name="Агент", phone="+79991112233",
                                                 tg_id=4242))
        tw.run(self.crm.add_referral(agent_id=agent_id, tg_id=5001))
        tw.run(tw.service.ref_signed(self.crm, tw.run(self.crm.client(self.client_id))))
        self.assertEqual(tw.run(self.crm.client(self.client_id))["channel"], "referral")

    def test_report_and_csv(self):
        self.client.post(f"/clients/{self.client_id}/edit", data={
            "full_name": "Иванов Иван", "phone": "+79990000000",
            "status": "active", "channel": "avito", "note": ""})
        page = self.get_ok("/reports/channels")
        self.assertIn("Авито", page)
        self.assertIn("ИТОГО", page)
        r = self.client.get("/reports/channels.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Авито", r.text)

    def test_report_needs_access_to_clients(self):
        """Это разрез клиентской базы: механику она ни к чему."""
        tech = tw.run(self.crm.access_profile_by_code("tech"))
        tw.run(self.crm.create_staff("petr", logic.hash_password("password-1"),
                                     "Пётр", "manager", tech["id"]))
        self.client.post("/logout")
        self.login("petr", "password-1")
        self.assertEqual(self.client.get("/reports/channels").status_code, 403)
        self.assertEqual(self.client.get("/reports/channels.csv").status_code, 403)

        manager = tw.run(self.crm.access_profile_by_code("manager"))
        tw.run(self.crm.create_staff("ivan", logic.hash_password("password-1"),
                                     "Иван", "manager", manager["id"]))
        self.client.post("/logout")
        self.login("ivan", "password-1")
        self.assertEqual(self.client.get("/reports/channels").status_code, 200)


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
