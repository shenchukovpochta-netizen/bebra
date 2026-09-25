"""Точки из справочника в ответах бота: конвейер Telegram и процесс MAX.

Точки заводит владелец в панели, а называет их клиенту бот - другой
процесс. Чистые ответы проверяет test_faq; здесь - весь путь через
настоящий Dispatcher: конвейер обновляет снимок, ответ «где вы» и вопрос
в поддержку перечисляют каждую открытую точку, новая точка доезжает после
срока жизни снимка, закрытая пропадает, а бот без CRM отвечает прежним
зашитым текстом.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from aiogram.methods import SendMessage

    from app import logic
    from app.crm import points
    from tests.fake_crm import FakeCrm
    from tests.test_flow import USER_ID, FakeDB, build, cb, make_config, msg, settle
    HAVE_AIOGRAM = True
except ImportError:                                    # pragma: no cover
    HAVE_AIOGRAM = False

# Третья точка заведена владельцем в панели - её адреса нет нигде в коде.
THREE = (
    ("Павлюхина", "г. Казань, ул. Павлюхина, 97А", "пн-вс: 10:00-19:00"),
    ("Адоратского", "г. Казань, ул. Адоратского, 11А", "пн-вс: 10:00-19:00"),
    ("Восстания", "г. Казань, ул. Восстания, 100", "пн-пт: 09:00-21:00"),
)
ADDRESSES = tuple(address for _name, address, _hours in THREE)


async def directory(crm) -> dict[str, int]:
    """Справочник из трёх точек, как после «Добавить точку» в панели."""
    ids = {}
    for sort, (name, address, hours) in enumerate(THREE, 1):
        ids[name] = await crm.create_location(
            name=name, city="Казань", address=address, note="ключ у охраны",
            public_title=f"Май Байк — {name}", hours=hours,
            phone="+7 (904) 676-49-26", sort=sort * 10)
    return ids


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestPointsInTelegram(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Снимок общий на процесс: чужой, оставшийся от прошлого теста,
        # подменил бы справочник этого.
        points.reset()
        self.crm = FakeCrm()
        self.ids = await directory(self.crm)
        (self.dp, self.bot, self.db, self.session,
         self.cfg, _vault) = build(make_config(), crm=self.crm)

    async def asyncTearDown(self):
        points.reset()
        await self.bot.session.close()

    async def feed(self, update):
        await self.dp.feed_update(self.bot, update)
        await settle()

    def approved_user(self, lang="ru"):
        self.db.users[USER_ID] = {
            "tg_id": USER_ID, "username": "ivan", "state": logic.APPROVED,
            "status": logic.ST_APPROVED, "rl_count": 0, "full_name": "Иванов Иван",
            "phone": "+79990000000", "lang": lang, "contract_no": None,
            "contract_status": logic.CT_NONE, "contract_path": None,
            "issue_data": None, "rent_until": None, "extend_until": None,
            "act_in_signed_at": None, "act_out_signed_at": None, "anketa_enc": None,
        }

    def texts_to_user(self) -> list[str]:
        return [m.text or "" for m in self.session.sent_to(USER_ID)
                if isinstance(m, SendMessage)]

    async def ask_address(self) -> str:
        await self.feed(cb("faq:ADDR"))
        return self.texts_to_user()[-1]

    async def test_pipeline_loads_points_and_the_answer_lists_each(self):
        self.approved_user()
        text = await self.ask_address()
        for address in ADDRESSES:
            self.assertIn(address, text)
        self.assertIn("пн-пт: 09:00-21:00", text)
        self.assertNotIn("ГСК «Сокол»", text, "зашитый текст не подмешан")
        self.assertNotIn("ключ у охраны", text, "описание точки - для своих")
        self.assertEqual([p["address"] for p in points.snapshot()], list(ADDRESSES))

    async def test_answer_in_the_clients_language_keeps_addresses(self):
        self.approved_user(lang="en")
        text = await self.ask_address()
        self.assertIn("Our points", text)
        for address in ADDRESSES:
            self.assertIn(address, text, "адрес - данные, не переводится")

    async def test_support_question_is_answered_from_the_directory(self):
        """Набранный вопрос «где вы» отвечается тем же снимком, что и кнопка."""
        self.approved_user()
        await self.feed(msg("🆘 Поддержка"))
        self.assertEqual(self.db.users[USER_ID]["state"], logic.WAIT_SUPPORT)
        await self.feed(msg("где вы находитесь?"))
        answered = [t for t in self.texts_to_user() if ADDRESSES[2] in t]
        self.assertTrue(answered, "третья точка не названа")
        for address in ADDRESSES:
            self.assertIn(address, answered[-1])

    async def test_new_point_arrives_after_ttl_and_closed_one_leaves(self):
        self.approved_user()
        await self.ask_address()
        await self.crm.create_location(
            name="Баумана", city="Казань", address="г. Казань, ул. Баумана, 1",
            note=None, sort=40)
        await self.crm.update_location(self.ids["Адоратского"], active=False)
        # Пока снимок свежий, в базу за точками не ходят: запрос на каждый
        # апдейт - лишний, а минуты задержки никого не задевают.
        text = await self.ask_address()
        self.assertNotIn("Баумана", text)
        self.assertIn(ADDRESSES[1], text)
        # Срок жизни вышел - следующий апдейт перечитывает справочник.
        with mock.patch.object(points, "is_fresh", return_value=False):
            text = await self.ask_address()
        self.assertIn("г. Казань, ул. Баумана, 1", text)
        self.assertNotIn(ADDRESSES[1], text, "на закрытую точку не зовут")

    async def test_bot_without_crm_answers_with_the_hardcoded_points(self):
        await self.bot.session.close()
        # Снимок от прошлой жизни процесса: бот без CRM обязан его забыть.
        points.set_snapshot([{"name": n, "address": a} for n, a, _h in THREE])
        (self.dp, self.bot, self.db, self.session,
         self.cfg, _vault) = build(make_config())
        self.approved_user()
        text = await self.ask_address()
        self.assertIn("11А", text)
        self.assertIn("97А", text)
        self.assertNotIn(ADDRESSES[2], text)
        self.assertEqual(points.snapshot(), [])


class _Max:
    """Клиент MAX без сети: подписки нет, сообщения копятся."""

    def __init__(self):
        self.sent = []

    async def is_member(self, channel_id, user_id):
        return False

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return {"message": {"body": {"mid": f"mid.{len(self.sent)}"}}}

    async def answer_callback(self, cid, notification=None):
        self.sent.append({"callback": cid, "text": notification})


@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestPointsInMax(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        points.reset()

    async def asyncTearDown(self):
        points.reset()

    async def test_dialog_update_refreshes_the_snapshot(self):
        """Процесс MAX читает тот же справочник: рядом с реквизитами."""
        from app.max import runner
        from app.max.handlers import Ctx
        from app.services.crypto import Vault
        crm = FakeCrm()
        await directory(crm)
        cfg = make_config()
        cl = _Max()
        ctx = Ctx(cl, FakeDB(), cfg, Vault.from_raw(cfg.pdn_key), crm=crm)
        await runner._dispatch_dialog(ctx, {}, {"kind": "message", "user_id": 42,
                                                "username": "u", "text": "привет"})
        self.assertTrue(cl.sent, "апдейт дошёл до гейта подписки")
        self.assertEqual([p["address"] for p in points.snapshot()], list(ADDRESSES))


if __name__ == "__main__":
    unittest.main(verbosity=2)
