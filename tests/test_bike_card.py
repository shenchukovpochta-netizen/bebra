"""Карточка велосипеда: трекер по привязке, история нарядов, бренд из
каталога. Панель через TestClient (обвязка из test_web.py).
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_web as tw
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

D = Decimal
_run = tw.run if HAVE_WEB else None


class TestCatalogueEntry(unittest.TestCase):
    MODELS = [{"id": 1, "title": "Городской H10", "factory_title": "Maikaolin H10",
               "brand": "Maikaolin"},
              {"id": 2, "title": "Truck+", "factory_title": None, "brand": "Wolt"}]

    def test_matches_client_or_factory_name_case_insensitively(self):
        self.assertEqual(logic.catalogue_entry(self.MODELS, "maikaolin h10")["id"], 1)
        self.assertEqual(logic.catalogue_entry(self.MODELS, "Городской H10")["id"], 1)
        self.assertEqual(logic.catalogue_entry(self.MODELS, " truck+ ")["id"], 2)
        self.assertIsNone(logic.catalogue_entry(self.MODELS, "Kugoo V3"))
        self.assertIsNone(logic.catalogue_entry(self.MODELS, ""))


@unittest.skipUnless(HAVE_WEB, "нет fastapi/httpx")
class TestBikeCard(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.seed()

    def order(self, bike_id, **over):
        fields = dict(bike_id=bike_id, payer="own", client_id=None, complaint="стук",
                      object_note=None, tech_id=None, estimate=D(0), created_by="т")
        fields.update(over)
        return _run(self.crm.create_work_order(**fields))

    def test_tracker_block_follows_the_binding_not_the_checkbox(self):
        page = self.get_ok(f"/bikes/{self.bike_id}")
        self.assertIn("Привязанного трекера нет", page)
        _run(self.crm.create_tracker(device_id="1001", alias="Метка 1",
                                     bike_id=self.bike_id, last_seen=datetime.now(UTC),
                                     lat=55.79, lon=49.12, speed=D(0), voltage=D("12.6")))
        page = self.get_ok(f"/bikes/{self.bike_id}")
        self.assertIn("Метка 1", page)
        self.assertIn("трек за период", page)
        self.assertIn("12.6 В", page)
        self.assertIn("Галочка «трекер установлен» в карточке не стоит", page,
                      "расхождение галочки и привязки подсвечено")

    def test_orders_history_and_the_filtered_list(self):
        first = self.order(self.bike_id)
        _run(self.crm.update_work_order(first, status="done", total=D(1500),
                                        closed_at=datetime.now(UTC)))
        self.order(self.bike_id, complaint="снова стук")
        other = _run(self.crm.create_bike(code="B-2", model="Truck+"))
        self.order(other, complaint="чужой")
        page = self.get_ok(f"/bikes/{self.bike_id}")
        self.assertIn(f"/orders?bike={self.bike_id}", page)
        self.assertIn(logic.ORDER_STATUSES["done"], page, "закрытый наряд в истории есть")
        self.assertIn("1 500 ₽", page)
        listing = self.get_ok(f"/orders?bike={self.bike_id}")
        self.assertIn("Наряды велосипеда", listing)
        self.assertIn("№ B-1", listing)
        self.assertIn("Итого 2", listing)
        self.assertNotIn("Truck+", listing, "чужой наряд отфильтрован")
        self.assertEqual(self.client.get("/orders?bike=abc").status_code, 200,
                         "мусор в фильтре не роняет список")

    def test_brand_and_client_name_come_from_the_catalogue(self):
        page = self.get_ok(f"/bikes/{self.bike_id}")
        self.assertNotIn("Городской", page)
        _run(self.crm.create_bike_model(title="Городской V3", brand="Kugoo",
                                        factory_title="Kugoo V3", battery_slots=2,
                                        note=None))
        page = self.get_ok(f"/bikes/{self.bike_id}")
        self.assertIn("Kugoo Городской V3", page)
        self.assertIn("Kugoo V3", page, "модель по накладной остаётся: это история парка")


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
