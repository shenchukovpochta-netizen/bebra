"""Фото при сдаче: форма закрытия в панели, ответ оператора в боте, срок.

Что стережём: снимков не больше шести на аренду и только jpg/png/webp до
8 МБ - и проверка идёт до закрытия, чтобы негодный файл не оставил аренду
закрытой без снимков; файл лежит на томе снимков техники под нашим
именем, и путь из базы не уводит чтение и удаление за пределы каталога;
отдаёт снимок только панель вошедшему с правом на аренды; бот прикладывает
фото к идущей аренде или закрытой не раньше часа назад; дневной проход
удаляет старше срока вместе с файлами; в демо файлы не пишутся.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import tempfile
import types
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.crm import logic  # noqa: E402

try:
    import test_web as tw

    from app.crm import billing, photos, service
    from app.web import app as web_app
    from tests.fake_crm import FakeCrm
    HAVE_WEB = tw.HAVE_WEB
except ImportError:                                    # pragma: no cover
    HAVE_WEB = False

try:
    import test_flow as tf
    HAVE_AIOGRAM = tf.HAVE_AIOGRAM
except ImportError:                                    # pragma: no cover
    HAVE_AIOGRAM = False

try:
    import asyncpg
    import pgserver

    from app.crm.db import CrmDB
    from app.db import Database, _init_connection
    HAVE_PG = True
except ImportError:                                    # pragma: no cover
    HAVE_PG = False

SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"

D = Decimal
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 512
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


class TestPhotoLogic(unittest.TestCase):
    def test_suffix_by_name_or_type(self):
        self.assertEqual(logic.return_photo_suffix("IMG_1.JPEG"), ".jpg")
        self.assertEqual(logic.return_photo_suffix("x.webp"), ".webp")
        self.assertEqual(logic.return_photo_suffix(None, "image/png"), ".png")
        self.assertIsNone(logic.return_photo_suffix("скан.pdf"))
        self.assertIsNone(logic.return_photo_suffix("x.jpg.exe"))
        self.assertIsNone(logic.return_photo_suffix(None, "text/html"))

    def test_path_is_ours_and_checked(self):
        path = logic.return_photo_path(12, ".jpeg", "0123456789ab")
        self.assertEqual(path, "returns/ret-12-0123456789ab.jpg")
        self.assertTrue(logic.is_return_photo_path(path))
        for bad in ("../../etc/passwd", "returns/../secrets/pdn_key",
                    "/bikes/returns/ret-1-0123456789ab.jpg",
                    "returns/ret-1-0123456789ab.jpg/../x", "ret-1-0123456789ab.jpg",
                    "returns/ret-1-XYZ.jpg", "", None):
            self.assertFalse(logic.is_return_photo_path(bad), bad)
        with self.assertRaises(ValueError):
            logic.return_photo_path(1, ".exe", "0123456789ab")
        with self.assertRaises(ValueError):
            logic.return_photo_path(1, ".jpg", "../x")

    def test_retention_setting(self):
        self.assertEqual(logic.return_photo_days({}), 180)
        self.assertEqual(logic.return_photo_days({"return_photo_days": "30"}), 30)
        for junk in ("мусор", "0", "-5", "99999"):
            self.assertEqual(logic.return_photo_days({"return_photo_days": junk}), 180,
                             junk)

    def test_which_rental_gets_the_bot_photo(self):
        active = {"id": 1, "status": "active"}
        fresh = {"id": 2, "status": "closed", "closed_at": NOW - timedelta(minutes=30)}
        old = {"id": 3, "status": "closed", "closed_at": NOW - timedelta(minutes=61)}
        unknown = {"id": 4, "status": "closed", "closed_at": None}
        self.assertIs(logic.return_photo_rental(active, fresh, now=NOW), active)
        self.assertIs(logic.return_photo_rental(None, fresh, now=NOW), fresh)
        self.assertIsNone(logic.return_photo_rental(None, old, now=NOW))
        self.assertIsNone(logic.return_photo_rental(None, unknown, now=NOW),
                          "момент закрытия неизвестен - старая аренда")
        self.assertIsNone(logic.return_photo_rental(None, None, now=NOW))


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class PhotoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "bikes"
        self.crm = FakeCrm()
        self.client_id = run(self.crm.create_client(full_name="Иванов Иван",
                                                    phone="+79990000000", tg_id=5001))
        self.bike_id = run(self.crm.create_bike(code="МБ-7", model="Kugoo V3"))
        self.rid = run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=None,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="manual",
            started_on=date.today(), contract_no=None, created_by="t"))
        self.rental = run(self.crm.rental(self.rid))

    def files(self) -> list[Path]:
        return sorted(p for p in self.folder.rglob("*") if p.is_file())


class TestSaveAndPurge(PhotoCase):
    def test_saved_under_our_name_with_a_row(self):
        pid = run(photos.save(self.crm, self.folder, self.rental, JPEG, ".jpg",
                              by="staff:t"))
        row = run(self.crm.return_photo(pid))
        self.assertTrue(logic.is_return_photo_path(row["path"]))
        self.assertEqual((row["rental_id"], row["bike_id"], row["created_by"]),
                         (self.rid, self.bike_id, "staff:t"))
        self.assertEqual(photos.path_of(self.folder, row["path"]).read_bytes(), JPEG)

    def test_six_per_rental_and_no_orphan_file(self):
        for _ in range(logic.RETURN_PHOTOS_MAX):
            run(photos.save(self.crm, self.folder, self.rental, JPEG, ".png", by="t"))
        with self.assertRaises(service.ServiceError) as err:
            run(photos.save(self.crm, self.folder, self.rental, JPEG, ".png", by="t"))
        self.assertIn("уже 6", str(err.exception))
        self.assertEqual(len(self.files()), logic.RETURN_PHOTOS_MAX,
                         "седьмой файл не остался на диске без строки")

    def test_bad_file_is_refused(self):
        for raw, suffix in ((JPEG, ".gif"), (b"", ".jpg"),
                            (b"0" * (logic.RETURN_PHOTO_MAX_BYTES + 1), ".jpg")):
            with self.assertRaises(service.ServiceError):
                run(photos.save(self.crm, self.folder, self.rental, raw, suffix, by="t"))
        self.assertEqual(self.files(), [])

    def test_purge_by_age_with_files(self):
        old = run(photos.save(self.crm, self.folder, self.rental, JPEG, ".jpg", by="t"))
        gone = run(photos.save(self.crm, self.folder, self.rental, JPEG, ".jpg", by="t"))
        fresh = run(photos.save(self.crm, self.folder, self.rental, JPEG, ".jpg", by="t"))
        for pid in (old, gone):
            self.crm.return_photos_[pid]["created_at"] -= timedelta(days=200)
        # Файл уже пропал руками - строка всё равно уходит.
        photos.path_of(self.folder, self.crm.return_photos_[gone]["path"]).unlink()
        self.assertEqual(run(photos.purge(self.crm, self.folder, 180)), 2)
        self.assertEqual([p["id"] for p in run(self.crm.return_photos(rental_id=self.rid))],
                         [fresh])
        self.assertEqual(len(self.files()), 1)

    def test_purge_keeps_the_row_when_the_file_stays(self):
        pid = run(photos.save(self.crm, self.folder, self.rental, JPEG, ".jpg", by="t"))
        path = photos.path_of(self.folder, self.crm.return_photos_[pid]["path"])
        path.unlink()
        path.mkdir()                        # unlink каталога - OSError
        self.crm.return_photos_[pid]["created_at"] -= timedelta(days=200)
        self.assertEqual(run(photos.purge(self.crm, self.folder, 180)), 0)
        self.assertIsNotNone(run(self.crm.return_photo(pid)))

    def test_purge_never_touches_a_foreign_path(self):
        victim = Path(self.tmp.name) / "victim.txt"
        victim.write_text("не трогать")
        pid = run(self.crm.add_return_photo(self.rid, bike_id=None,
                                            path="../victim.txt", created_by="t",
                                            limit=6))
        self.crm.return_photos_[pid]["created_at"] -= timedelta(days=200)
        self.assertEqual(run(photos.purge(self.crm, self.folder, 180)), 1)
        self.assertTrue(victim.exists())
        self.assertIsNone(run(self.crm.return_photo(pid)))

    def test_daily_pass_uses_the_owner_setting(self):
        pid = run(photos.save(self.crm, self.folder, self.rental, JPEG, ".jpg", by="t"))
        self.crm.return_photos_[pid]["created_at"] -= timedelta(days=40)
        cfg = types.SimpleNamespace(contract_chat_id=-1, remind_before_days=2,
                                    bike_photo_dir=self.folder)
        bot = types.SimpleNamespace(send_message=_noop)
        db = types.SimpleNamespace(get_user=_none)
        run(billing.run_daily(bot, db, self.crm, cfg, today=date.today(),
                              now=datetime.now(), done={}))
        self.assertIsNotNone(run(self.crm.return_photo(pid)), "180 дней по умолчанию")
        run(self.crm.set_setting("return_photo_days", "30", by="t"))
        run(billing.run_daily(bot, db, self.crm, cfg, today=date.today(),
                              now=datetime.now(), done={}))
        self.assertIsNone(run(self.crm.return_photo(pid)))
        self.assertEqual(self.files(), [])


async def _noop(*_a, **_k):
    return None


async def _none(*_a, **_k):
    return None


# ─────────────────────────── панель ───────────────────────────

@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class PanelCase(tw.WebCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = dataclasses.replace(self.cfg, bike_photo_dir=Path(self.tmp.name) / "bikes")
        self.app = web_app.create_app(crm=self.crm, db=self.db, cfg=self.cfg, bot=self.bot)
        self.client = tw.TestClient(self.app, follow_redirects=False)
        self.login()
        self.seed()
        self.rid = run(self.crm.create_rental(
            client_id=self.client_id, bike_id=self.bike_id, tariff_id=self.tariff_id,
            tariff_name="Неделя", period_days=7, price=D(3000), billing="manual",
            started_on=date.today(), contract_no=None, created_by="t"))

    def close(self, shots):
        return self.client.post(
            f"/rentals/{self.rid}/close",
            data={"closed_on": date.today().isoformat(), "bike_status": "available"},
            files=[("photos", shot) for shot in shots])


class TestPanelClose(PanelCase):
    def test_close_form_takes_photos(self):
        page = self.get_ok(f"/rentals/{self.rid}")
        self.assertIn('enctype="multipart/form-data"', page)
        self.assertIn('name="photos"', page)
        r = self.close([("перед.jpg", JPEG, "image/jpeg"),
                        ("бок.webp", b"RIFF0000WEBP", "image/webp")])
        self.assertEqual(r.status_code, 303)
        self.assertEqual(run(self.crm.rental(self.rid))["status"], "closed")
        rows = run(self.crm.return_photos(rental_id=self.rid))
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["created_by"] for r in rows}, {"staff:admin"})
        page = self.get_ok(f"/rentals/{self.rid}")
        self.assertIn("Фото при сдаче: 2.", page)
        self.assertIn(f"/rentals/{self.rid}/photos/{rows[0]['id']}", page)
        r = self.client.get(f"/rentals/{self.rid}/photos/{rows[0]['id']}")
        self.assertEqual((r.status_code, r.content), (200, JPEG))
        bike = self.get_ok(f"/bikes/{self.bike_id}")
        self.assertIn(f"/rentals/{self.rid}/photos/{rows[1]['id']}", bike,
                      "карточка велосипеда показывает его фото при сдаче")

    def test_seventh_photo_keeps_the_rental_open(self):
        r = self.close([(f"{i}.jpg", JPEG, "image/jpeg") for i in range(7)])
        self.assertEqual(r.status_code, 303)
        self.assertEqual(run(self.crm.rental(self.rid))["status"], "active",
                         "негодные фото - повод поправить форму, а не закрыть аренду")
        self.assertEqual(run(self.crm.return_photos(rental_id=self.rid)), [])
        self.assertIn("не больше 6", self.get_ok(f"/rentals/{self.rid}"))

    def test_wrong_type_and_size_are_refused_before_closing(self):
        for shot in (("акт.pdf", b"%PDF-1.4", "application/pdf"),
                     ("big.jpg", b"0" * (logic.RETURN_PHOTO_MAX_BYTES + 1), "image/jpeg")):
            r = self.close([shot])
            self.assertEqual(r.status_code, 303)
            self.assertEqual(run(self.crm.rental(self.rid))["status"], "active", shot[0])
        self.assertFalse(self.cfg.bike_photo_dir.exists())

    def test_close_without_photos_still_works(self):
        r = self.close([])
        self.assertEqual(r.status_code, 303)
        self.assertEqual(run(self.crm.rental(self.rid))["status"], "closed")

    def test_photo_of_another_rental_or_a_foreign_path_is_404(self):
        self.close([("перед.jpg", JPEG, "image/jpeg")])
        pid = run(self.crm.return_photos(rental_id=self.rid))[0]["id"]
        self.assertEqual(self.client.get(f"/rentals/{self.rid + 1}/photos/{pid}")
                         .status_code, 404)
        bad = run(self.crm.add_return_photo(self.rid, bike_id=None, path="../../etc/hosts",
                                            created_by="t", limit=99))
        self.assertEqual(self.client.get(f"/rentals/{self.rid}/photos/{bad}").status_code,
                         404)
        self.assertEqual(self.client.get(f"/rentals/{self.rid}/photos/99999").status_code,
                         404)

    def test_photos_need_the_rentals_right(self):
        self.close([("перед.jpg", JPEG, "image/jpeg")])
        pid = run(self.crm.return_photos(rental_id=self.rid))[0]["id"]
        url = f"/rentals/{self.rid}/photos/{pid}"
        profile = run(self.crm.create_access_profile(
            "Только парк", {"sections": {"bikes": "edit"}, "actions": {}}))
        run(self.crm.create_staff("petr", logic.hash_password("password-1"), "Пётр",
                                  "manager", profile))
        self.client.post("/logout")
        r = self.client.get(url)
        self.assertEqual(r.status_code, 303, "без входа - на страницу входа")
        self.login("petr", "password-1")
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertNotIn(url, self.get_ok(f"/bikes/{self.bike_id}"),
                         "без права на аренды ссылок на фото нет")

    def test_retention_is_set_on_the_intake_page(self):
        self.assertIn('name="return_photo_days" inputmode="numeric" value="180"',
                      self.get_ok("/intake"))
        self.client.post("/intake", data={"required": "on", "return_photo_days": "30"})
        self.assertEqual(run(self.crm.settings())["return_photo_days"], "30")
        self.client.post("/intake", data={"required": "on", "return_photo_days": "0"})
        self.assertEqual(run(self.crm.settings())["return_photo_days"], "30",
                         "ноль стёр бы вчерашние снимки")

    def test_body_limit_fits_six_photos(self):
        self.assertGreaterEqual(web_app.BODY_MAX,
                                logic.RETURN_PHOTOS_MAX * logic.RETURN_PHOTO_MAX_BYTES)


@unittest.skipUnless(HAVE_WEB, "fastapi не установлен")
class TestDemoIgnoresPhotos(PanelCase):
    def setUp(self):
        super().setUp()
        self.cfg = dataclasses.replace(self.cfg, demo=True)
        self.app = web_app.create_app(crm=self.crm, db=self.db, cfg=self.cfg, bot=None)
        self.client = tw.TestClient(self.app, follow_redirects=False)
        self.login()

    def test_close_passes_but_nothing_is_written(self):
        self.assertNotIn('name="photos"', self.get_ok(f"/rentals/{self.rid}"),
                         "поля в демо нет")
        r = self.close([("перед.jpg", JPEG, "image/jpeg")])
        self.assertEqual(r.status_code, 303)
        self.assertEqual(run(self.crm.rental(self.rid))["status"], "closed")
        self.assertEqual(run(self.crm.return_photos(rental_id=self.rid)), [])
        self.assertFalse(self.cfg.bike_photo_dir.exists())
        self.assertIn(web_app.DEMO_PHOTO_TEXT, self.get_ok(f"/rentals/{self.rid}"))


# ─────────────────────────── бот ───────────────────────────

@unittest.skipUnless(HAVE_AIOGRAM, "aiogram не установлен")
class TestBotReturnPhotos(unittest.IsolatedAsyncioTestCase):
    """Настоящий Dispatcher: оператор отвечает фото на карточку сдачи."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "bikes"
        self.crm = FakeCrm()
        (self.dp, self.bot, self.db, self.session, self.cfg,
         self.vault) = tf.build(tf.make_config(bike_photo_dir=self.folder), crm=self.crm)
        files = tf.files
        self._orig = (files.download, files.store, files.remove)
        files.download = lambda bot, file_id, max_bytes: tf._async(JPEG)
        files.store = lambda d, tg, slot, data: (
            Path(f"/tmp/{tg}-{slot}.{files.SLOT_EXT[slot]}"), "hash")
        files.remove = lambda path: True

    async def asyncTearDown(self):
        tf.files.download, tf.files.store, tf.files.remove = self._orig
        await self.bot.session.close()

    # путь клиента - те же шаги, что в test_flow
    feed = tf.TestFlow.feed
    fill_anketa = tf.TestFlow.fill_anketa
    register_up_to_confirm = tf.TestFlow.register_up_to_confirm
    submit = tf.TestFlow.submit
    approve = tf.TestFlow.approve
    ISSUE_FORM = tf.TestFlow.ISSUE_FORM
    provide_issue = tf.TestFlow.provide_issue
    approve_fully = tf.TestFlow.approve_fully
    confirm_pay = tf.TestFlow.confirm_pay
    register_fully = tf.TestFlow.register_fully
    CLOSE_FORM = tf.TestFlow.CLOSE_FORM
    provide_return = tf.TestFlow.provide_return
    request_close = tf.TestFlow.request_close

    async def photo_reply(self, caption=None):
        card = self.db.users[tf.USER_ID]["return_message_id"]
        update = tf.msg(caption, chat_id=tf.ADMIN_CHAT, user_id=tf.ADMIN_ID,
                        chat_type="supergroup", reply_to=card, photo=True)
        if caption:
            update = update.model_copy(update={"message": update.message.model_copy(
                update={"caption": caption, "text": None})})
        await self.feed(update)

    async def rental(self):
        client = await self.crm.client_by_tg(tf.USER_ID)
        return (await self.crm.client_rentals(client["id"]))[0]

    def replies(self):
        return [m.text or "" for m in self.session.sent_to(tf.ADMIN_CHAT)
                if isinstance(m, tf.SendMessage)]

    async def test_photo_goes_to_the_running_rental(self):
        await self.register_fully()
        await self.request_close()
        await self.photo_reply()
        rental = await self.rental()
        self.assertEqual(rental["status"], "active")
        shots = await self.crm.return_photos(rental_id=rental["id"])
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0]["created_by"], f"tg:{tf.ADMIN_ID}")
        self.assertEqual(photos.path_of(self.folder, shots[0]["path"]).read_bytes(), JPEG)
        self.assertEqual(self.db.users[tf.USER_ID]["state"], tf.logic.APPROVED,
                         "фото без формы - не форма закрытия")

    async def test_photo_with_the_close_form_does_both(self):
        await self.register_fully()
        await self.request_close()
        await self.photo_reply(self.CLOSE_FORM)
        rental = await self.rental()
        self.assertEqual(len(await self.crm.return_photos(rental_id=rental["id"])), 1)
        self.assertEqual(self.db.users[tf.USER_ID]["state"], tf.logic.WAIT_RETURN_SIGN)

    async def test_broken_form_in_the_caption_is_named(self):
        """Форма без «принял:» в подписи - не заметка: иначе фото получало
        👍, ошибки никто не видел, а аренда оставалась открытой и копила
        долг. Фото при этом приложено."""
        await self.register_fully()
        await self.request_close()
        before = len(self.replies())
        broken = "\n".join(line for line in self.CLOSE_FORM.splitlines()
                           if not line.startswith("принял"))
        await self.photo_reply(broken)
        rental = await self.rental()
        self.assertEqual(len(await self.crm.return_photos(rental_id=rental["id"])), 1)
        self.assertEqual(self.replies()[before:], ["Не хватает: принял."])
        self.assertEqual(self.db.users[tf.USER_ID]["state"], tf.logic.APPROVED)
        # опечатка в ключе - тоже форма, а не заметка
        await self.photo_reply(self.CLOSE_FORM.replace("принял:", "прниял:"))
        self.assertIn("Не понял строки: прниял", self.replies()[-1])

    async def test_note_in_the_caption_is_just_a_photo(self):
        await self.register_fully()
        await self.request_close()
        before = len(self.replies())
        await self.photo_reply("царапина на левом крыле")
        rental = await self.rental()
        self.assertEqual(len(await self.crm.return_photos(rental_id=rental["id"])), 1)
        self.assertEqual(self.replies()[before:], [], "заметка - не форма, ответа нет")
        self.assertEqual(self.db.users[tf.USER_ID]["state"], tf.logic.APPROVED)

    async def test_after_the_act_within_an_hour_and_not_later(self):
        await self.register_fully()
        await self.request_close()
        await self.provide_return()
        await self.feed(tf.cb("return_sign"))
        rental = await self.rental()
        self.assertEqual(rental["status"], "closed")
        self.assertIsNotNone(await self.crm.feedback_of_rental(rental["id"]),
                             "акт подписан в боте - вопрос об оценке в очереди")
        await self.photo_reply()
        self.assertEqual(len(await self.crm.return_photos(rental_id=rental["id"])), 1)
        self.crm.rentals_[rental["id"]]["closed_at"] -= timedelta(
            minutes=logic.RETURN_PHOTO_MINUTES + 1)
        await self.photo_reply()
        self.assertEqual(len(await self.crm.return_photos(rental_id=rental["id"])), 1)
        self.assertTrue(any("Фото не к чему приложить" in t for t in self.replies()))


@unittest.skipUnless(HAVE_PG and HAVE_WEB, "pgserver или asyncpg не установлены")
class TestPhotosOnPostgres(unittest.IsolatedAsyncioTestCase):
    """Предел снимков под замком строки аренды и чистка по возрасту - на
    настоящей базе: два альбома одновременно не кладут седьмой снимок."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(self.pg.get_uri(), min_size=1, max_size=6,
                                              init=_init_connection)
        await self.pool.execute("drop schema if exists crm cascade; "
                                "drop schema if exists bot cascade")
        db = Database(self.pool)
        await db.apply_schema(SCHEMA)
        await db.apply_schema(SCHEMA)
        self.crm = CrmDB(self.pool)
        client = await self.crm.create_client(full_name="Иванов Иван",
                                              phone="+79990000000", tg_id=5001)
        self.bike_id = await self.crm.create_bike(code="МБ-7", model="Kugoo V3")
        self.rid = await self.crm.create_rental(
            client_id=client, bike_id=self.bike_id, tariff_id=None, tariff_name="Неделя",
            period_days=7, price=D(3000), billing="manual", started_on=date.today(),
            contract_no=None, created_by="t")

    async def asyncTearDown(self):
        await self.pool.close()

    async def test_limit_holds_under_concurrency(self):
        async def one(n):
            return await self.crm.add_return_photo(
                self.rid, bike_id=self.bike_id,
                path=logic.return_photo_path(self.rid, ".jpg", f"{n:012x}"),
                created_by="t", limit=logic.RETURN_PHOTOS_MAX)
        got = await asyncio.gather(*(one(n) for n in range(10)))
        self.assertEqual(sum(1 for g in got if g is not None), logic.RETURN_PHOTOS_MAX)
        rows = await self.crm.return_photos(rental_id=self.rid)
        self.assertEqual(len(rows), logic.RETURN_PHOTOS_MAX)
        by_bike = await self.crm.return_photos(bike_id=self.bike_id, limit=3)
        self.assertEqual(len(by_bike), 3)
        self.assertIn("closed_on", by_bike[0])

    async def test_old_rows_are_found_and_dropped(self):
        ids = [await self.crm.add_return_photo(
            self.rid, bike_id=None, path=logic.return_photo_path(self.rid, ".png", f"{n:012x}"),
            created_by="t", limit=6) for n in range(3)]
        await self.pool.execute("update crm.return_photos set created_at = now() - "
                                "interval '200 days' where id = any($1::bigint[])", ids[:2])
        old = await self.crm.old_return_photos(180)
        self.assertEqual([r["id"] for r in old], ids[:2])
        self.assertEqual(await self.crm.drop_return_photos(ids[:2]), 2)
        self.assertEqual([r["id"] for r in await self.crm.return_photos(rental_id=self.rid)],
                         ids[2:])
        self.assertEqual(await self.crm.drop_return_photos([]), 0)


if __name__ == "__main__":                             # pragma: no cover
    unittest.main()
