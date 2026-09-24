"""Карточка клиента: запасные телефоны, компания и стаж. Панель через
TestClient (обвязка из test_web.py) и чистые проверки полей.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

_run = tw.run if HAVE_WEB else None


class TestClientChoices(unittest.TestCase):
    def test_employer_and_experience_are_optional_but_from_the_lists(self):
        self.assertIsNone(logic.check_employer("").value)
        self.assertTrue(logic.check_employer("samokat").ok)
        self.assertFalse(logic.check_employer("uber").ok)
        self.assertIsNone(logic.check_experience(None).value)
        self.assertEqual(logic.check_experience("over_3").value, "over_3")
        self.assertFalse(logic.check_experience("много").ok)


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestClientCard(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def edit(self, **over):
        data = {"full_name": "Иванов Иван", "phone": "+79990000000",
                "contract_no": "", "status": "active", "channel": "", "max_id": "",
                "note": "", "phone2": "", "phone3": "", "employer": "", "experience": ""}
        data.update(over)
        return self.client.post(f"/clients/{self.client_id}/edit", data=data)

    def test_spare_phones_and_work_are_saved_and_shown(self):
        r = self.edit(phone2="8 (917) 111-22-33", employer="samokat", experience="under_year")
        self.assertEqual(r.status_code, 303)
        client = _run(self.crm.client(self.client_id))
        self.assertEqual(client["phone2"], "+79171112233", "запасной номер нормализован")
        self.assertIsNone(client["phone3"])
        self.assertEqual(client["employer"], "samokat")
        self.assertEqual(client["experience"], "under_year")
        page = self.get_ok(f"/clients/{self.client_id}")
        self.assertIn("запасные +79171112233", page)
        self.assertIn("Самокат", page)
        self.assertIn("стаж: До года", page)

    def test_junk_spare_phone_is_refused_and_nothing_changes(self):
        self.edit(phone2="позвонить маме")
        client = _run(self.crm.client(self.client_id))
        self.assertIsNone(client["phone2"])
        self.assertIn("не похоже на номер", self.get_ok(f"/clients/{self.client_id}"))

    def test_search_finds_the_client_by_a_spare_phone(self):
        self.edit(phone2="+79171112233")
        self.assertIn("Иванов", self.get_ok("/clients?q=917111"))
        self.assertIn("Иванов", self.get_ok("/clients?q=8 917 111-22-33"))
        self.assertNotIn("Иванов", self.get_ok("/clients?q=905555"))

    def test_clearing_a_spare_phone_removes_it(self):
        self.edit(phone2="+79171112233")
        self.edit(phone2="")
        self.assertIsNone(_run(self.crm.client(self.client_id))["phone2"])


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestPaymentFormOnce(tw.WebCase):
    def test_double_click_records_one_payment(self):
        """Двойной клик по «Принять» - одна запись в журнале, а не две."""
        import re
        self.login()
        self.seed()
        page = self.get_ok(f"/clients/{self.client_id}")
        key = re.search(r'name="once" value="([^"]+)"', page).group(1)
        data = {"kind": "payment", "amount": "1500", "method": "cash", "note": "",
                "once": key}
        self.client.post(f"/clients/{self.client_id}/ledger", data=data)
        self.client.post(f"/clients/{self.client_id}/ledger", data=data)
        from decimal import Decimal
        self.assertEqual(tw.run(self.crm.client_balance(self.client_id)), Decimal("1500"))
        self.assertIn("повторное нажатие пропущено", self.get_ok(f"/clients/{self.client_id}"))
