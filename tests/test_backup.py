"""Сервис backup (backup.sh): дамп, копия в облако, проверка восстановления.

Сценарий гоняется настоящим sh, а pg_dump, psql, createdb, dropdb и rclone
подменены заглушками: они пишут, чем их позвали, и отвечают тем, что
задал тест. Живой прогон с Postgres и S3 - в INSTALL.md («проверить
сейчас»); здесь - то, что ломается молча: отчёт в базу, «последний
удачный» после сбоя, ключ не в аргументах, недельная копия, сроки.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crm import logic  # noqa: E402

try:
    import pgserver
    from pgserver._commands import POSTGRES_BIN_PATH as PG_BIN
except ImportError:                                    # pragma: no cover
    pgserver = None

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "backup.sh"
MSK = ZoneInfo("Europe/Moscow")
KEY = "ключ-Key+/=42"

# Одна заглушка на все программы: кем её позвали, решает имя ссылки.
FAKE = r'''#!/usr/bin/env python3
import gzip, json, os, shutil, sys, time
from pathlib import Path

fake = Path(os.environ["FAKE"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(fake / "calls.jsonl", "a") as f:
    f.write(json.dumps([name] + args) + "\n")
fail = os.environ.get("FAKE_FAIL", "").split(",")


def remote(path):
    assert path.startswith("offsite:"), path
    return fake / "remote" / path[len("offsite:"):]


if name == "pg_dump":
    if "dump" in fail:
        sys.stderr.write(os.environ.get("FAKE_DUMP_ERROR", "pg_dump: error: refused") + "\n")
        sys.exit(1)
    out = args[args.index("-f") + 1]
    # Строки таблиц - блоками COPY, как у настоящего pg_dump: по ним
    # сценарий судит, пуст ли дамп.
    counts = json.loads((fake / "counts.json").read_text())
    with gzip.open(out, "wt") as g:
        g.write("create table t ();\n")
        for table, n in counts.get(args[args.index("-d") + 1], {}).items():
            g.write(f"COPY {table} (id) FROM stdin;\n")
            g.writelines(f"{i}\n" for i in range(1, n + 1))
            g.write("\\.\n\n")
elif name == "df":
    print("Filesystem 1024-blocks Used Available Capacity Mounted on")
    print(f"/dev/vda 20000000 1 {os.environ.get('FAKE_DF_KB', '19000000')} 5% /")
elif name == "psql":
    opts = {}
    it = iter(args)
    for a in it:
        if a in ("-v", "-d", "-c", "-o"):
            opts.setdefault(a, []).append(next(it))
    data = sys.stdin.read() if "-c" not in opts else ""
    variables = dict(v.split("=", 1) for v in opts.get("-v", []))
    if "status" in variables:
        assert ":'status'" in data, data
        if "push" in fail:
            sys.stderr.write('ERROR:  relation "crm.settings" does not exist\n')
            sys.exit(1)
        (fake / "status.json").write_text(variables["status"])
    elif "-c" in opts and "pg_database_size" in opts["-c"][0]:
        print(os.environ.get("FAKE_DB_BYTES", str(100 * 1024 ** 2)))
    elif "-c" in opts:
        db = opts["-d"][0]
        table = opts["-c"][0].rsplit(" ", 1)[-1]
        counts = json.loads((fake / "counts.json").read_text())
        if table not in counts.get(db, {}):
            sys.stderr.write("relation does not exist\n")
            sys.exit(1)
        print(counts[db][table])
    else:
        if "restore" in fail:
            sys.stderr.write('ERROR:  syntax error at or near "мусор"\n')
            sys.exit(3)
        (fake / "restored.sql").write_text(data)
elif name in ("createdb", "dropdb"):
    if name in fail:
        sys.stderr.write(name + ": error: permission denied\n")
        sys.exit(1)
elif name == "rclone":
    with open(fake / "rclone_env.jsonl", "a") as f:
        f.write(json.dumps({k: v for k, v in os.environ.items()
                            if k.startswith("RCLONE_")}) + "\n")
    flags, pos = {}, []
    it = iter(args)
    for a in it:
        if a in ("--max-age", "--min-age"):
            flags[a] = next(it)
        elif a.startswith("-") and a != "-":
            flags[a] = True
        else:
            pos.append(a)
    cmd = pos[0]
    if cmd in fail:
        sys.stderr.write("2026/09/27 03:00:01 ERROR : " + cmd + ": AccessDenied\n")
        sys.exit(1)
    if cmd == "obscure":
        (fake / "key_seen").write_text(sys.stdin.read())
        print("obscured")
    elif cmd == "copyto":
        src, dst = pos[1], pos[2]
        src = remote(src) if src.startswith("offsite:") else Path(src)
        dst = remote(dst) if dst.startswith("offsite:") else Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    elif cmd in ("lsf", "lsl"):
        d = remote(pos[1])
        if not d.is_dir():
            sys.exit(3)
        for p in sorted(d.iterdir()):
            age = (time.time() - p.stat().st_mtime) / 86400
            if "--max-age" in flags and age > float(flags["--max-age"].rstrip("d")):
                continue
            print(p.name)
    elif cmd == "delete":
        d = remote(pos[1])
        limit = float(flags["--min-age"].rstrip("d"))
        for p in list(d.iterdir()) if d.is_dir() else []:
            if (time.time() - p.stat().st_mtime) / 86400 > limit:
                p.unlink()
'''

# Счёт строк: имя базы → таблица → число. Живая и развёрнутая совпадают.
COUNTS = {"crm.clients": 120, "crm.bikes": 190, "crm.rentals": 800,
          "crm.ledger": 12000, "crm.bike_status_log": 5000}


@unittest.skipUnless(shutil.which("sh") and shutil.which("gunzip"), "нужны sh и gunzip")
class BackupCase(unittest.TestCase):
    STUBS = ("pg_dump", "psql", "createdb", "dropdb", "rclone", "df")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fake = self.tmp / "fake"
        self.bin = self.tmp / "bin"
        self.dir = self.tmp / "backups"
        for d in (self.fake, self.bin, self.dir, self.tmp / "secrets"):
            d.mkdir()
        stub = self.bin / "stub.py"
        stub.write_text(FAKE)
        stub.chmod(0o755)
        for name in self.STUBS:
            (self.bin / name).symlink_to(stub)
        self.key = self.tmp / "secrets" / "backup_key"
        self.key.write_text(KEY)
        self.s3 = self.tmp / "secrets" / "backup_s3_secret"
        self.s3.write_text("s3-secret-value")
        (self.tmp / "secrets" / "db_password").write_text("pw")
        self.counts(COUNTS, COUNTS)
        self.env = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "FAKE": str(self.fake), "LC_ALL": "C.UTF-8", "TZ": "Europe/Moscow",
            "BACKUP_DIR": str(self.dir), "POSTGRES_DB": "mybike", "POSTGRES_USER": "mybike",
            "BACKUP_KEY_FILE": str(self.key), "BACKUP_S3_SECRET_FILE": str(self.s3),
            "BACKUP_PG_PASSWORD_FILE": str(self.tmp / "secrets" / "db_password"),
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cloud(self, **extra):
        self.env.update({"BACKUP_S3_ENDPOINT": "https://storage.yandexcloud.net",
                         "BACKUP_S3_REGION": "ru-central1", "BACKUP_S3_BUCKET": "mybike-bk",
                         "BACKUP_S3_PREFIX": "kzn", "BACKUP_S3_ACCESS_KEY": "YCAJE",
                         **extra})

    def old_copy_in_the_cloud(self):
        old = self.fake / "remote" / "daily" / "mybike-2026-09-20.sql.gz"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"real")
        return old

    def counts(self, live, restored):
        (self.fake / "counts.json").write_text(json.dumps(
            {"mybike": live, "mybike_restore_check": restored}))

    def run_sh(self, *args, fail="", **env):
        r = subprocess.run(["sh", str(SCRIPT), *args], capture_output=True, text=True,
                           env={**self.env, "FAKE_FAIL": fail, **env}, timeout=60)
        return r

    def calls(self, name=None):
        path = self.fake / "calls.jsonl"
        rows = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
        return [r for r in rows if name is None or r[0] == name]

    def rclone(self, cmd):
        return [r[1:] for r in self.calls("rclone") if cmd in r[1:]]

    def state(self, part):
        return json.loads((self.dir / ".state" / f"{part}.json").read_text())

    def pushed(self):
        return json.loads((self.fake / "status.json").read_text())

    def today_dump(self):
        # «Сегодня» сценария - московское (TZ в окружении), а не часов
        # процесса тестов: с 21:00 UTC это уже завтра.
        return self.dir / f"mybike-{datetime.now(MSK).strftime('%Y-%m-%d')}.sql.gz"


class TestScript(unittest.TestCase):
    def test_parses_as_posix_sh(self):
        r = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_image_fixes_windows_line_ends(self):
        """Архив, собранный на Windows: с CRLF sh не читает даже первую
        строку, и сервис не делает ни одного дампа. Образ чинит концы
        строк сам, чем бы файл ни приехал."""
        docker = (ROOT / "backup.Dockerfile").read_text(encoding="utf-8")
        fix = re.search(r"(?m)^RUN (.*backup\.sh.*)$", docker).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "backup.sh"
            copy.write_bytes(SCRIPT.read_bytes().replace(b"\n", b"\r\n"))
            broken = subprocess.run(["sh", str(copy), "справка"], capture_output=True, text=True)
            subprocess.run(["sh", "-c", fix.replace("/usr/local/bin/backup.sh", str(copy))],
                           check=True)
            r = subprocess.run(["sh", str(copy), "справка"], capture_output=True, text=True)
        self.assertNotIn("backup.sh dump", broken.stderr, "без починки сценарий не читается")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("backup.sh dump", r.stderr, "справка - сценарий прочитан целиком")

    def test_near_tolerance(self):
        """Свежая копия почти равна базе: 5 % или 10 строк - норма, пустая
        таблица в копии при непустой в базе - нет."""
        text = SCRIPT.read_text(encoding="utf-8")
        body = re.search(r"(?ms)^near\(\) \{.*?^\}\n", text).group()
        cases = (("1000", "960", 0), ("1000", "940", 1), ("5", "0", 1), ("3", "2", 0),
                 ("8", "18", 0), ("0", "0", 0), ("-1", "-1", 0), ("10", "-1", 1),
                 ("-1", "10", 1), ("20000", "20900", 0), ("20000", "21100", 1),
                 # дамп снят до schema.sql, а в базе таблица уже есть и пуста
                 ("0", "-1", 0))
        for live, got, rc in cases:
            r = subprocess.run(["sh", "-c", f'{body}near {live} {got}'], capture_output=True)
            self.assertEqual(r.returncode, rc, (live, got))


class TestDump(BackupCase):
    def test_dump_is_reported_to_the_database(self):
        r = self.run_sh("dump")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.today_dump().exists())
        status = self.pushed()
        self.assertTrue(status["dump"]["ok"])
        self.assertEqual(status["dump"]["file"], self.today_dump().name)
        self.assertEqual(status["dump"]["size"], self.today_dump().stat().st_size)
        self.assertIsNone(status["restore"], "проверки ещё не было")
        parsed = logic.parse_backup_status(json.dumps(status))
        self.assertNotIn("dump", logic.backup_problems(parsed, datetime.now(UTC)))

    def test_failed_dump_keeps_last_success_and_says_why(self):
        self.run_sh("dump")
        first = self.state("dump")["last_ok"]
        self.today_dump().unlink()
        r = self.run_sh("dump", fail="dump",
                        FAKE_DUMP_ERROR='pg_dump: ошибка: "postgres" \\ refused\n'
                                        'Connection refused')
        self.assertEqual(r.returncode, 1)
        dump = self.pushed()["dump"]
        self.assertFalse(dump["ok"])
        self.assertEqual(dump["last_ok"], first, "последний удачный не теряется")
        self.assertIn("Connection refused", dump["error"])
        self.assertNotIn('"', dump["error"])
        self.assertEqual(list(self.dir.glob("*.tmp")), [], "оборванный дамп не остаётся")
        self.assertFalse(self.today_dump().exists())
        parsed = logic.parse_backup_status(json.dumps(self.pushed()))
        problems = logic.backup_problems(parsed, datetime.now(UTC))
        self.assertIn("не сделался", problems["dump"]["text"])

    def test_report_waits_for_the_schema(self):
        """На первом старте crm.settings ещё нет: отчёт не теряется, а уходит
        следующим запуском."""
        self.run_sh("dump", fail="push")
        self.assertFalse((self.fake / "status.json").exists())
        self.assertFalse((self.dir / ".state" / "pushed").exists())
        self.run_sh("dump")
        self.assertTrue(self.pushed()["dump"]["ok"])


class TestOffsite(BackupCase):
    def test_upload_is_encrypted_daily_and_weekly(self):
        self.cloud()
        self.run_sh("dump")
        r = self.run_sh("upload")
        self.assertEqual(r.returncode, 0, r.stderr)
        name = self.today_dump().name
        remote = self.fake / "remote"
        self.assertTrue((remote / "daily" / name).exists())
        self.assertTrue((remote / "weekly" / name).exists(), "первая копия недели")
        env = json.loads((self.fake / "rclone_env.jsonl").read_text().splitlines()[-1])
        self.assertEqual(env["RCLONE_CONFIG_OFFSITE_TYPE"], "crypt")
        self.assertEqual(env["RCLONE_CONFIG_OFFSITE_REMOTE"], "s3:mybike-bk/kzn")
        self.assertEqual(env["RCLONE_CONFIG_OFFSITE_PASSWORD"], "obscured")
        self.assertEqual(env["RCLONE_CONFIG_S3_ENDPOINT"], "https://storage.yandexcloud.net")
        self.assertEqual(env["RCLONE_CONFIG_S3_REGION"], "ru-central1")
        self.assertEqual((self.fake / "key_seen").read_text(), KEY, "ключ - через stdin")
        argv = (self.fake / "calls.jsonl").read_text()
        self.assertNotIn("Key+/=42", argv, "ключа нет в аргументах процессов")
        self.assertNotIn("s3-secret-value", argv)
        off = self.pushed()["offsite"]
        self.assertTrue(off["ok"] and off["enabled"])
        self.assertEqual(off["target"], "storage.yandexcloud.net/mybike-bk/kzn")
        self.assertIsNone(off["prune_error"])

    def test_weekly_copy_once_a_week_and_old_copies_go(self):
        self.cloud(BACKUP_S3_KEEP_DAYS="14", BACKUP_S3_KEEP_WEEKS="8")
        self.run_sh("dump")
        self.run_sh("upload")
        self.run_sh("upload")
        weekly = [c for c in self.rclone("copyto") if c[-1].startswith("offsite:weekly/")]
        self.assertEqual(len(weekly), 1, "недельная - одна за шесть дней")
        deletes = self.rclone("delete")
        self.assertIn(["delete", "-q", "--use-server-modtime", "--min-age", "14d",
                       "offsite:daily/"], deletes)
        self.assertIn(["delete", "-q", "--use-server-modtime", "--min-age", "56d",
                       "offsite:weekly/"], deletes)

    def test_keep_zero_never_deletes(self):
        """Ключ без права удаления: срок держит правило бакета."""
        self.cloud(BACKUP_S3_KEEP_DAYS="0")
        self.run_sh("dump")
        self.assertEqual(self.run_sh("upload").returncode, 0)
        self.assertEqual(self.rclone("delete"), [])

    def test_prune_failure_does_not_undo_the_upload(self):
        self.cloud()
        self.run_sh("dump")
        self.assertEqual(self.run_sh("upload", fail="delete").returncode, 0)
        off = self.pushed()["offsite"]
        self.assertTrue(off["ok"])
        self.assertIn("AccessDenied", off["prune_error"])
        problems = logic.backup_problems(logic.parse_backup_status(json.dumps(self.pushed())),
                                         datetime.now(UTC))
        self.assertIn("не удаляются", problems["offsite"]["text"])

    def test_no_key_no_upload(self):
        """Открытая копия базы у чужого провайдера хуже, чем её отсутствие."""
        self.cloud()
        self.key.write_text("")
        self.run_sh("dump")
        self.assertEqual(self.run_sh("upload").returncode, 1)
        self.assertEqual(self.rclone("copyto"), [])
        off = self.pushed()["offsite"]
        self.assertFalse(off["ok"])
        self.assertIn("secrets/backup_key", off["error"])

    def test_failed_upload_is_reported(self):
        self.cloud()
        self.run_sh("dump")
        self.assertEqual(self.run_sh("upload", fail="copyto").returncode, 1)
        off = self.pushed()["offsite"]
        self.assertFalse(off["ok"])
        self.assertEqual(off["error"], "выгрузка: copyto: AccessDenied",
                         "отметка времени rclone и уровень срезаны")

    def test_empty_dump_does_not_push_out_real_copies(self):
        """Новый сервер при восстановлении: его пустой дамп не должен стать
        «последней копией» в облаке - её бы и скачали. Судится файл, а не
        живая база: базу уже восстановили, а на диске последний - дамп,
        снятый до этого."""
        self.cloud()
        old = self.old_copy_in_the_cloud()
        empty = dict.fromkeys(COUNTS, 0)
        self.counts(empty, empty)
        self.run_sh("dump")
        self.assertEqual(self.run_sh("upload").returncode, 1)
        self.assertIn("снят с пустой базы", self.pushed()["offsite"]["error"])
        self.counts(COUNTS, COUNTS)                     # базу восстановили
        self.assertEqual(self.run_sh("upload").returncode, 1,
                         "дамп до восстановления так и остался пустым")
        self.assertEqual(sorted(p.name for p in old.parent.iterdir()), [old.name])
        self.run_sh("dump")                             # шаг INSTALL.md
        self.assertEqual(self.run_sh("upload").returncode, 0)
        self.assertTrue((old.parent / self.today_dump().name).exists())

    def test_first_install_uploads_even_an_empty_dump(self):
        """Облако пустое - вытеснять нечего: первая копия уходит как есть."""
        self.cloud()
        empty = dict.fromkeys(COUNTS, 0)
        self.counts(empty, empty)
        self.run_sh("dump")
        self.assertEqual(self.run_sh("upload").returncode, 0)

    def test_fleet_without_clients_goes_to_the_cloud(self):
        """Новая точка завела парк, клиентов ещё нет: копия нужна, а не
        ежедневное «восстановите базу из облака»."""
        self.cloud()
        self.old_copy_in_the_cloud()
        only_bikes = {**dict.fromkeys(COUNTS, 0), "crm.bikes": 3}
        self.counts(only_bikes, only_bikes)
        self.run_sh("dump")
        self.assertEqual(self.run_sh("upload").returncode, 0)
        self.assertTrue(self.pushed()["offsite"]["ok"])

    def test_without_bucket_the_cloud_is_off(self):
        r = self.run_sh("upload")
        self.assertEqual(r.returncode, 1)
        self.assertIn("BACKUP_S3_BUCKET", r.stderr)
        self.assertEqual(self.calls("rclone"), [])


class TestRestoreCheck(BackupCase):
    def test_cloud_copy_is_restored_and_compared(self):
        self.cloud()
        self.run_sh("dump")
        self.run_sh("upload")
        r = self.run_sh("check")
        self.assertEqual(r.returncode, 0, r.stderr)
        rest = self.pushed()["restore"]
        self.assertTrue(rest["ok"])
        self.assertEqual(rest["source"], "offsite")
        self.assertEqual(rest["tables"]["crm.ledger"], [12000, 12000])
        pulled = [c for c in self.rclone("copyto") if c[-2].startswith("offsite:daily/")]
        self.assertEqual(len(pulled), 1, "копия скачана из облака, а не взята с диска")
        self.assertIn("create table", (self.fake / "restored.sql").read_text())
        self.assertIn(["createdb", "-T", "template0", "mybike_restore_check"], self.calls())
        self.assertEqual(self.calls()[-2][:2], ["dropdb", "--if-exists"],
                         "одноразовая база удалена")
        self.assertFalse((self.dir / ".state" / "check.sql.gz").exists())
        nxt = int((self.dir / ".state" / "restore_next").read_text())
        self.assertGreater(nxt - time.time(), 6 * 86400)

    def test_without_cloud_the_local_dump_is_checked(self):
        self.run_sh("dump")
        self.assertEqual(self.run_sh("check").returncode, 0)
        self.assertEqual(self.pushed()["restore"]["source"], "local")
        self.assertEqual(self.calls("rclone"), [])

    def test_counts_that_do_not_match_fail(self):
        self.run_sh("dump")
        self.counts(COUNTS, {**COUNTS, "crm.ledger": 0})
        self.assertEqual(self.run_sh("check").returncode, 1)
        rest = self.pushed()["restore"]
        self.assertFalse(rest["ok"])
        self.assertIn("crm.ledger: в копии 0, в базе 12000", rest["error"])
        nxt = int((self.dir / ".state" / "restore_next").read_text())
        self.assertLess(nxt - time.time(), 86400, "неудачная - повтор через сутки")

    def test_broken_restore_is_reported_and_cleaned(self):
        self.run_sh("dump")
        self.assertEqual(self.run_sh("check", fail="restore").returncode, 1)
        rest = self.pushed()["restore"]
        self.assertIn("psql: syntax error at or near", rest["error"])
        self.assertEqual(self.calls()[-2][:2], ["dropdb", "--if-exists"])

    def test_no_room_no_restore(self):
        """Копия разворачивается в кластер боевой базы: на почти полном
        диске проверка уронила бы Postgres, а с ним бота и панель. Такую
        ночь она пропускает и говорит об этом владельцу."""
        self.cloud()
        self.run_sh("dump")
        self.run_sh("upload")
        r = self.run_sh("check", FAKE_DB_BYTES=str(2 * 1024 ** 3),
                        FAKE_DF_KB=str(3 * 1024 ** 2))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.calls("createdb"), [], "в кластер ничего не легло")
        self.assertEqual([c for c in self.rclone("copyto") if c[-2].startswith("offsite:")],
                         [], "и не скачивалось")
        rest = self.pushed()["restore"]
        self.assertFalse(rest["ok"])
        self.assertIn("свободно 3072 МБ", rest["error"])
        self.assertIn("около 4608 МБ", rest["error"], "две базы и запас")
        nxt = int((self.dir / ".state" / "restore_next").read_text())
        self.assertLess(nxt - time.time(), 86400, "освободят место - проверка следующей ночью")
        # Места хватает - та же база проверяется.
        self.assertEqual(self.run_sh("check", FAKE_DB_BYTES=str(2 * 1024 ** 3),
                                     FAKE_DF_KB=str(5 * 1024 ** 2)).returncode, 0)

    def test_wrong_key_shows_up_as_failed_check(self):
        self.cloud()
        self.run_sh("dump")
        self.run_sh("upload")
        self.run_sh("check")
        ok_at = self.pushed()["restore"]["last_ok"]
        self.assertEqual(self.run_sh("check", fail="copyto").returncode, 1)
        rest = self.pushed()["restore"]
        self.assertFalse(rest["ok"])
        self.assertEqual(rest["last_ok"], ok_at)
        parsed = logic.parse_backup_status(json.dumps(self.pushed()))
        text = logic.backup_problems(parsed, datetime.now(UTC))["restore"]["text"]
        self.assertIn("из облака не восстановилась", text)


class TestLoop(BackupCase):
    def loop(self, seconds=3.0):
        p = subprocess.Popen(["sh", str(SCRIPT)], env={**self.env, "BACKUP_TICK": "1"},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(seconds)
        started = time.monotonic()
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=10)
        self.assertLess(time.monotonic() - started, 5, "SIGTERM не ждёт круга")
        self.assertEqual(p.returncode, 0, err)
        return out

    def test_one_dump_one_upload_one_check_a_day(self):
        self.cloud()
        self.loop()
        self.assertEqual(len(self.calls("pg_dump")), 1)
        daily = [c for c in self.rclone("copyto") if c[-1].startswith("offsite:daily/")]
        self.assertEqual(len(daily), 1)
        self.assertEqual(len([c for c in self.calls("createdb")]), 1,
                         "первая проверка - сразу, следующая - через неделю")
        status = self.pushed()
        self.assertTrue(status["dump"]["ok"] and status["offsite"]["ok"]
                        and status["restore"]["ok"])
        pushes = [c for c in self.calls("psql") if any(a.startswith("status=") for a in c)]
        self.assertEqual(len(pushes), 1, "тот же отчёт в базу второй раз не пишется")

    def test_only_its_own_unfinished_dump_is_cleaned(self):
        """В ./backups пишет и update.sh: его недописанный дамп перед
        обновлением круг сервиса не трогает, свой оборванный - убирает."""
        theirs = self.dir / "pre-update-20260927-0300.sql.gz.tmp"
        mine = self.dir / "mybike-2026-09-20.sql.gz.tmp"
        theirs.write_bytes(b"half")
        mine.write_bytes(b"half")
        self.loop(2.5)
        self.assertTrue(theirs.exists(), "дамп update.sh на месте")
        self.assertFalse(mine.exists())

    def test_upgrade_from_the_old_service_adopts_todays_dump(self):
        """Прежний сервис отчётов не писал: сегодняшний дамп на диске - это
        удачный бэкап, а не повод делать второй."""
        with gzip.open(self.today_dump(), "wb") as g:
            g.write(b"create table t ();\n")
        out = self.loop(2.0)
        self.assertEqual(self.calls("pg_dump"), [], out)
        status = self.pushed()
        self.assertTrue(status["dump"]["ok"])
        self.assertEqual(status["dump"]["file"], self.today_dump().name)
        self.assertEqual(status["offsite"], {"enabled": False})
        self.assertEqual(status["restore"]["source"], "local")


@unittest.skipUnless(pgserver is not None, "нужен pgserver")
class TestOnPostgres(BackupCase):
    """Настоящие pg_dump, psql, createdb и dropdb на встроенном Postgres,
    облако и df - заглушки. Здесь то, чего заглушка не поймает: формат
    дампа, по которому сценарий судит «пусто», и первый круг нового
    сервера, пока бот применяет schema.sql."""
    STUBS = ("rclone", "df")
    SCHEMA = ROOT / "schema.sql"

    @classmethod
    def setUpClass(cls):
        cls.pgdir = tempfile.TemporaryDirectory()
        cls.pg = pgserver.get_server(cls.pgdir.name)
        cls.host = parse_qs(urlparse(cls.pg.get_uri()).query)["host"][0]
        # Схема один раз - в шаблон, каждая проверка берёт копию.
        cls.pg_run("createdb", "mybike_schema")
        cls.pg_run("psql", "-X", "-q", "-1", "-v", "ON_ERROR_STOP=1", "-d", "mybike_schema",
                   "-f", str(cls.SCHEMA))

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()
        cls.pgdir.cleanup()

    @classmethod
    def pg_run(cls, prog, *args):
        return subprocess.run([f"{PG_BIN}/{prog}", *args], capture_output=True, text=True,
                              check=True, env={"PGHOST": cls.host, "PGUSER": "postgres",
                                               "LC_ALL": "C.UTF-8"}).stdout

    def setUp(self):
        super().setUp()
        self.env.update({"PATH": f"{self.bin}:{PG_BIN}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                         "PGHOST": self.host, "POSTGRES_USER": "postgres"})
        for db in ("mybike", "mybike_restore_check"):
            self.pg_run("dropdb", "--if-exists", db)

    def sql(self, query):
        return self.pg_run("psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1",
                           "-d", "mybike", "-c", query).strip()

    def status(self):
        return json.loads(self.sql("select value from crm.settings where key = 'backup_status'"))

    FLEET = ("insert into crm.bikes (code, model) values "
             "('B-1', 'Kugoo V3'), ('B-2', 'Kugoo V3'), ('B-3', 'Kugoo V3')")

    def test_restored_server_keeps_its_empty_dump_off_the_cloud(self):
        """INSTALL.md «Восстановить на новом сервере»: первый круг снял дамп
        пустой базы со схемой, потом базу восстановили. Тот дамп в облако
        не уходит, хотя живая база уже полна, а снятый заново - уходит."""
        self.cloud()
        cloud = self.old_copy_in_the_cloud().parent
        self.pg_run("createdb", "-T", "mybike_schema", "mybike")
        self.assertEqual(self.run_sh("dump").returncode, 0)
        self.sql(self.FLEET)
        self.sql("insert into crm.clients (full_name, phone) "
                 "values ('Иванов Иван', '+79990000001')")
        r = self.run_sh("upload")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("снят с пустой базы", self.status()["offsite"]["error"])
        self.assertEqual([p.name for p in cloud.iterdir()], ["mybike-2026-09-20.sql.gz"])
        self.assertEqual(self.run_sh("dump").returncode, 0)
        self.assertEqual(self.run_sh("upload").returncode, 0)
        sent = gzip.decompress((cloud / self.today_dump().name).read_bytes()).decode()
        self.assertRegex(sent, r"COPY crm\.clients \(.*\) FROM stdin;\n\d+\t")

    def test_new_point_with_a_fleet_and_no_clients(self):
        self.cloud()
        self.old_copy_in_the_cloud()
        self.pg_run("createdb", "-T", "mybike_schema", "mybike")
        self.sql(self.FLEET)
        self.assertEqual(self.run_sh("dump").returncode, 0)
        r = self.run_sh("upload")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.status()["offsite"]["ok"])

    def test_first_check_while_the_bot_applies_the_schema(self):
        """Первый круг нового сервера: дамп снят до schema.sql, к сверке бот
        её уже применил - в копии таблиц нет, в базе они пусты. Терять было
        нечего: проверка проходит, а не краснеет в день установки."""
        self.pg_run("createdb", "mybike")
        self.assertEqual(self.run_sh("dump").returncode, 0)
        self.pg_run("psql", "-X", "-q", "-1", "-v", "ON_ERROR_STOP=1", "-d", "mybike",
                    "-f", str(self.SCHEMA))
        r = self.run_sh("check")
        self.assertEqual(r.returncode, 0, r.stderr)
        restore = self.status()["restore"]
        self.assertTrue(restore["ok"], restore)
        self.assertEqual(restore["tables"]["crm.clients"], [-1, 0])
        self.assertEqual(self.pg_run("psql", "-X", "-At", "-d", "postgres", "-c",
                                     "select count(*) from pg_database "
                                     "where datname = 'mybike_restore_check'").strip(), "0")


class TestParse(unittest.TestCase):
    NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)

    def status(self, **parts):
        base = {"dump": {"at": "2026-09-27T00:05:00Z", "ok": True, "error": None,
                         "last_ok": "2026-09-27T00:05:00Z", "size": 5 * 1024 ** 2},
                "offsite": {"enabled": False},
                "restore": {"at": "2026-09-25T00:07:00Z", "ok": True,
                            "last_ok": "2026-09-25T00:07:00Z", "source": "local"}}
        base.update(parts)
        return logic.parse_backup_status(json.dumps(base))

    def test_garbage_is_none(self):
        for raw in (None, "", "не json", "[1, 2]", "42"):
            self.assertIsNone(logic.parse_backup_status(raw))

    def test_fields_are_typed(self):
        s = logic.parse_backup_status(json.dumps({
            "dump": {"at": "2026-09-27T00:05:00Z", "ok": "yes", "size": "12",
                     "tables": {"crm.x": [1, 2], "crm.y": ["1", 2], "crm.z": [1]}},
            "offsite": {"enabled": "true"}, "restore": "мусор"}))
        self.assertEqual(s["dump"]["at"], datetime(2026, 9, 27, 0, 5, tzinfo=UTC))
        self.assertIsNone(s["dump"]["ok"], "только настоящий bool")
        self.assertIsNone(s["dump"]["size"])
        self.assertEqual(s["dump"]["tables"], {"crm.x": (1, 2)})
        self.assertFalse(s["offsite"]["enabled"], "облако включено только явным true")
        self.assertEqual(s["restore"]["tables"], {})

    def test_healthy_backup_has_no_problems(self):
        self.assertEqual(logic.backup_problems(self.status(), self.NOW), {})

    def test_no_report_at_all(self):
        problems = logic.backup_problems(None, self.NOW)
        self.assertIn("ни разу не отчитался", problems["dump"]["text"])

    def test_stale_dump(self):
        s = self.status(dump={"at": "2026-09-26T00:05:00Z", "ok": True,
                              "last_ok": "2026-09-25T09:00:00Z"})
        self.assertIn("не делался с 25.09 12:00",
                      logic.backup_problems(s, self.NOW)["dump"]["text"])

    def test_cloud_enabled_but_not_tried_yet_is_quiet(self):
        s = self.status(offsite={"enabled": True, "target": "x/y"})
        self.assertEqual(logic.backup_problems(s, self.NOW), {})

    def test_stale_cloud_copy(self):
        s = self.status(offsite={"enabled": True, "at": "2026-09-25T00:06:00Z", "ok": True,
                                 "last_ok": "2026-09-25T00:06:00Z"})
        self.assertIn("не обновлялась", logic.backup_problems(s, self.NOW)["offsite"]["text"])

    def test_restore_never_ran_or_is_old(self):
        s = self.status(restore=None)
        self.assertIn("ни разу", logic.backup_problems(s, self.NOW)["restore"]["text"])
        s = self.status(restore={"at": "2026-09-18T00:07:00Z", "ok": True,
                                 "last_ok": "2026-09-18T00:07:00Z"})
        self.assertIn("не проходила с 18.09",
                      logic.backup_problems(s, self.NOW)["restore"]["text"])


class TestComposeWiring(unittest.TestCase):
    COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    def block(self, name):
        found = re.search(rf"(?ms)^  {name}:\n(.*?)(?=^  [a-z-]+:\n|^[a-z]|\Z)", self.COMPOSE)
        return found.group(1)

    def test_backup_service_builds_its_image_with_its_secrets(self):
        block = self.block("backup")
        self.assertIn("dockerfile: backup.Dockerfile", block)
        self.assertRegex(block, r"secrets: \[db_password, backup_key, backup_s3_secret\]")
        for var in ("BACKUP_S3_ENDPOINT", "BACKUP_S3_REGION", "BACKUP_S3_BUCKET",
                    "BACKUP_S3_PREFIX", "BACKUP_S3_ACCESS_KEY", "BACKUP_S3_KEEP_DAYS",
                    "BACKUP_S3_KEEP_WEEKS"):
            self.assertRegex(block, rf"\n      {var}: \$\{{{var}:-")
        docker = (ROOT / "backup.Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(docker, r"FROM rclone/rclone:\d")
        self.assertIn("FROM postgres:16-alpine", docker,
                      "pg_dump той же версии, что сервер")
        self.assertIn("COPY backup.sh", docker)

    def test_bot_knows_the_domains_and_the_panel(self):
        block = self.block("bot")
        self.assertIn("CRM_DOMAIN: ${CRM_DOMAIN:-}", block)
        self.assertIn("DEMO_DOMAIN: ${DEMO_DOMAIN:-}", block)
        self.assertIn("HEALTH_PANEL_URL: http://crm:8080/healthz", block)

    def test_consistency_catches_a_forgotten_backup_secret(self):
        """Забытый секрет выключил бы облако молча - до дня, когда сервер умер."""
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("backup.sh", "backup.Dockerfile", ".env.example"):
                shutil.copy(ROOT / name, Path(tmp) / name)
            compose = self.COMPOSE.replace(
                "secrets: [db_password, backup_key, backup_s3_secret]",
                "secrets: [db_password, backup_key]").replace(
                "      BACKUP_S3_BUCKET: ${BACKUP_S3_BUCKET:-}\n", "")
            (Path(tmp) / "docker-compose.yml").write_text(compose, encoding="utf-8")
            r = subprocess.run([sys.executable, str(ROOT / "consistency.py"), tmp],
                               capture_output=True, text=True)
        self.assertIn("backup.sh читает секрет backup_s3_secret", r.stdout)
        self.assertIn("backup.sh читает BACKUP_S3_BUCKET", r.stdout)


if __name__ == "__main__":
    unittest.main()
