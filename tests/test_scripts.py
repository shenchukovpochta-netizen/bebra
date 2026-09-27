"""Строки установочных скриптов, которые ломались молча: генерация пароля
панели под pipefail и чтение TZ из .env при повторном запуске install.sh.
Команды берутся из самих скриптов, а не переписываются в тесте."""

from __future__ import annotations

import asyncio
import gzip
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import asyncpg
    import pgserver
    HAVE_PG = True
except ImportError:                                    # pragma: no cover
    HAVE_PG = False


def _bash(script: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "LC_ALL": "C.UTF-8"})


@unittest.skipUnless(shutil.which("bash") and shutil.which("openssl"), "нужны bash и openssl")
class TestScripts(unittest.TestCase):
    def test_bootstrap_password_survives_pipefail(self):
        text = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        m = re.search(r"openssl rand -base64 24[^\n]*\\\n[^\n]*crm_admin_password", text)
        self.assertIsNotNone(m, "строка генерации пароля не найдена")
        cmd = m.group().replace("\\\n", " ")
        with tempfile.TemporaryDirectory() as tmp:
            r = _bash(f"set -euo pipefail; mkdir -p secrets; {cmd}; echo done", tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("done", r.stdout)
            password = (Path(tmp) / "secrets" / "crm_admin_password").read_text()
            self.assertRegex(password, r"^[A-Za-z0-9]{16}$")

    def test_install_reads_tz_without_quotes(self):
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        m = re.search(r'TZ_VALUE="\$\(sed[^\n]*\)"', text)
        self.assertIsNotNone(m, "строка чтения TZ не найдена")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text('POSTGRES_USER="mybike"\nTZ="Europe/Moscow"\n')
            r = _bash(f'{m.group()}; printf %s "$TZ_VALUE"', tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout, "Europe/Moscow")

    def test_install_keeps_crm_and_max_keys(self):
        """Повторный install.sh переписывает .env целиком: ключи панели и MAX
        должны переноситься, а не теряться."""
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        for key in ("CRM_BIND", "CRM_PORT", "CRM_ADMIN_LOGIN", "CRM_TITLE", "CRM_DOMAIN",
                    "MAX_CHANNEL_ID", "MAX_ADMIN_CHAT_ID", "MAX_ADMINS", "MAX_CONTRACT_CHAT_ID",
                    "MAX_FIX_CHAT_ID", "MAX_CONTRACT_PREFIX", "MAX_API_BASE",
                    "DEMO_DOMAIN", "DEMO_BIND", "DEMO_PORT"):
            self.assertRegex(text, rf'(?m)^{key}="\$\{{{key}:-[^}}]*\}}"$',
                             f"{key} не переносится в новый .env")

    def test_install_writes_every_key_of_env_example(self):
        """Шаблон .env в install.sh знает все ключи .env.example: ключ,
        которого там нет, повторная установка стирает. Так пропадали номер
        счёта Точки и логин StarLine."""
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        template = text[text.index("cat > .env <<EOF"):]
        template = template[:template.index("\nEOF\n")]
        for key in sorted(set(re.findall(r"(?m)^([A-Z][A-Z0-9_]*)=", example))):
            self.assertRegex(template, rf'(?m)^{key}="',
                             f"{key} из .env.example не переносится в новый .env")

    def test_install_turns_on_demo_with_its_domain(self):
        """Домен демо без профиля demo - демо не поднимается после
        обновления; и без https - Caddy, через который оно открыто."""
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        start = text.index('DEMO_DOMAIN="${DEMO_DOMAIN:-}"\n')
        end = text.index("add_profile demo; fi\n", start) + len("add_profile demo; fi\n")
        block = text[start:end]
        cases = (({"CRM_DOMAIN": "crm.x.ru", "DEMO_DOMAIN": "", "COMPOSE_PROFILES": ""},
                  "https"),
                 ({"CRM_DOMAIN": "crm.x.ru", "DEMO_DOMAIN": "demo.x.ru",
                   "COMPOSE_PROFILES": "max"}, "max,https,demo"),
                 ({"CRM_DOMAIN": "", "DEMO_DOMAIN": "demo.x.ru",
                   "COMPOSE_PROFILES": "demo,https"}, "demo,https"))
        with tempfile.TemporaryDirectory() as tmp:
            for env, want in cases:
                preset = "".join(f'{k}="{v}"; ' for k, v in env.items())
                r = _bash(f'set -euo pipefail; {preset}{block}printf %s "$COMPOSE_PROFILES"',
                          tmp)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, want, env)

    def test_bootstrap_makes_demo_secrets_always(self):
        """Секреты демо объявлены в compose на уровне файла: без файлов не
        поднялся бы и боевой стек, даже без профиля demo."""
        text = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("demo_db_password", "crm_demo_secret"):
                m = re.search(rf"if \[ ! -s secrets/{name} \]; then\n(.*?)\nfi\n", text, re.S)
                self.assertIsNotNone(m, f"нет генерации secrets/{name}")
                body = m.group().replace("say ", "echo ")
                r = _bash(f"set -euo pipefail; mkdir -p secrets; {body}", tmp)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertRegex((Path(tmp) / "secrets" / name).read_text(),
                                 r"^[0-9a-f]{64}$")

    def test_bootstrap_opens_web_ports_for_demo_domain(self):
        text = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        self.assertRegex(text, r'if \[ -n "\$\{CRM_DOMAIN:-\}" \] \|\| '
                               r'\[ -n "\$\{DEMO_DOMAIN:-\}" \]; then\n\s+ufw allow 80/tcp')

    def test_bootstrap_refuses_demo_on_the_panel_address_or_port(self):
        """Демо на домене или порту панели - это Caddy без обоих сайтов или
        панель, у которой после перезагрузки порт занят демо. bootstrap.sh
        (его зовёт и install.sh) останавливается до compose."""
        text = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        start = text.index("lower() {")
        end = text.index("\n# ─── 1. Docker")
        block = text[start:end]
        cases = (
            ({"CRM_DOMAIN": "crm.x.ru", "DEMO_DOMAIN": "demo.x.ru"}, None),
            ({"CRM_DOMAIN": "crm.x.ru", "DEMO_DOMAIN": "CRM.x.ru"}, "DEMO_DOMAIN совпадает"),
            ({"CRM_DOMAIN": "", "DEMO_DOMAIN": ""}, None),
            ({"CRM_PORT": "8080", "DEMO_PORT": "8080"}, "DEMO_PORT совпадает"),
            ({"CRM_PORT": "8080", "DEMO_PORT": "8080", "CRM_BIND": "127.0.0.1",
              "DEMO_BIND": "10.0.0.5"}, None),
            ({"CRM_PORT": "8080", "DEMO_PORT": "8080", "CRM_BIND": "0.0.0.0",
              "DEMO_BIND": "10.0.0.5"}, "DEMO_PORT совпадает"),
            ({"CRM_PORT": "8080", "DEMO_PORT": "8081"}, None),
        )
        with tempfile.TemporaryDirectory() as tmp:
            for env, error in cases:
                preset = "".join(f'{k}="{v}"; ' for k, v in env.items())
                r = _bash("set -euo pipefail; die() { echo \"$*\" >&2; exit 1; }; "
                          f"{preset}{block}\necho passed", tmp)
                if error is None:
                    self.assertEqual((r.returncode, r.stdout.strip()), (0, "passed"),
                                     (env, r.stderr))
                else:
                    self.assertEqual(r.returncode, 1, env)
                    self.assertIn(error, r.stderr, env)

    def test_scripts_parse(self):
        for name in ("bootstrap.sh", "install.sh", "update.sh"):
            r = subprocess.run(["bash", "-n", str(ROOT / name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_deploy_does_not_overwrite_server_documents(self):
        """deploy.ps1 - путь обновления установки из исходников. Поставочные
        docx несут одни подстановки, серверные - реквизиты владельца текстом:
        залитые поверх, они ушли бы клиентам с прочерками. Документ, которого
        на сервере нет, доезжает - без файла бот не поднимется."""
        text = (ROOT / "deploy.ps1").read_text(encoding="utf-8-sig")
        app_list = re.search(r"(?ms)^\$app = @\((.*?)\)$", text).group(1)
        self.assertNotIn(".docx", app_list, "docx в $app уедут прямо в app/ поверх своих")
        docs = set(re.findall(r"'(app/[\w.]+\.docx)'",
                              re.search(r"(?ms)^\$docs = @\((.*?)\)$", text).group(1)))
        self.assertEqual(docs, {p.relative_to(ROOT).as_posix()
                                for p in (ROOT / "app").glob("*.docx")})
        self.assertIn('scp $docs      "${Server}:${Path}/.docx-new/"', text)
        self.assertIn("ssh $Server \"cd '$Path' && $keepDocs\"", text)
        keep = re.search(r"(?m)^\$keepDocs = '([^']*)'$", text).group(1)
        self.assertNotIn('"', keep, "PowerShell 5 теряет двойные кавычки по пути в ssh")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for name, body in (("app/contract_template.docx", "свой договор"),
                               (".docx-new/contract_template.docx", "поставочный"),
                               (".docx-new/new_act_template.docx", "новый вид")):
                (base / name).parent.mkdir(parents=True, exist_ok=True)
                (base / name).write_text(body, encoding="utf-8")
            r = _bash(keep, tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual((base / "app/contract_template.docx").read_text(encoding="utf-8"),
                             "свой договор")
            self.assertEqual((base / "app/new_act_template.docx").read_text(encoding="utf-8"),
                             "новый вид")
            self.assertFalse((base / ".docx-new").exists(), "черновой каталог остался")


# Хвост дампа - как у pg_dump 16.10 и новее (postgres:16-alpine сегодня):
# после «dump complete» идёт «\unrestrict <ключ>», и последней строкой
# отметка уже не бывает. Старый формат - без \restrict - тоже в ходу.
DUMP_OK = ("--\n-- PostgreSQL database dump\n--\n\n\\restrict k3yK3y\n\n"
           "create table x ();\n"
           "--\n-- PostgreSQL database dump complete\n--\n\n\\unrestrict k3yK3y\n\n")
DUMP_OK_OLD = ("--\n-- PostgreSQL database dump\n--\ncreate table x ();\n"
               "--\n-- PostgreSQL database dump complete\n--\n\n")
# Заглушки команд сервера: пишут вызов в журнал CALLS и отвечают так, как
# велит окружение теста. pg_dump - по FAKE_DUMP: ok, fail (код 1) или cut
# (код 0, но без последней строки дампа).
STUBS = {
    "docker": """#!/bin/sh
echo "docker $*" >> "$CALLS"
case "$*" in
  *pg_dump*)
    case "$FAKE_DUMP" in
      fail) printf -- '-- PostgreSQL database dump\\n'; exit 1 ;;
      cut) printf -- '-- PostgreSQL database dump\\ncreate table x ();\\n'; exit 0 ;;
      *) printf '%s' "$DUMP_OK"; exit 0 ;;
    esac ;;
  *" ps"*) echo "crm   Up" ;;
esac
exit 0
""",
    "curl": '#!/bin/sh\necho "curl $*" >> "$CALLS"\nexit "${FAKE_CURL:-0}"\n',
    "id": "#!/bin/sh\necho 0\n",
    "sleep": "#!/bin/sh\nexit 0\n",
}


@unittest.skipUnless(shutil.which("bash") and shutil.which("unzip") and shutil.which("gzip"),
                     "нужны bash, unzip и gzip")
class TestUpdate(unittest.TestCase):
    """update.sh на заглушках docker и curl: сервер - временный каталог со
    своими .env, секретом и правленым договором, архив - новая версия, в
    которой лежат и подмены, не имеющие права доехать."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.server = self.base / "opt" / "mybike-bot"
        self.calls = self.base / "calls.log"
        self.calls.write_text("")
        server = {".env": 'POSTGRES_USER="mb"\nPOSTGRES_DB="mbdb"\nCRM_PORT="18080"\n',
                  "docker-compose.yml": "old\n",
                  "bootstrap.sh": 'echo old-bootstrap >> "$CALLS"\n',
                  "secrets/bot_token": "настоящий токен",
                  "app/contract_template.docx": "договор, правленый владельцем",
                  "backups/mybike-2026-09-01.sql.gz": "вчерашний бэкап"}
        for name, text in server.items():
            self.put(self.server / name, text)
        shutil.copy(ROOT / "update.sh", self.server / "update.sh")
        bin_dir = self.base / "bin"
        for name, text in STUBS.items():
            self.put(bin_dir / name, text).chmod(0o755)
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                    "CALLS": str(self.calls), "DUMP_OK": DUMP_OK, "LC_ALL": "C.UTF-8"}

    @staticmethod
    def put(path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def archive(self, **override: str | None) -> Path:
        """Архив новой версии: проект в папке mybike-bot/, как в поставке.
        None в override - файла в архиве нет."""
        import zipfile
        files = {"docker-compose.yml": "new\n", "schema.sql": "-- new\r\n",
                 "bootstrap.sh": 'echo new-bootstrap >> "$CALLS"\n', "install.sh": "true\n",
                 "update.sh": (ROOT / "update.sh").read_text(encoding="utf-8"),
                 ".env.example": "X=1\n", "app/web/app.py": "# new\n",
                 "app/contract_template.docx": "поставочный договор",
                 "app/new_act_template.docx": "новый документ",
                 ".env": "EVIL=1\n", "secrets/bot_token": "чужой токен",
                 "backups/evil.sql.gz": "чужой дамп", **override}
        path = self.base / "mybike-bot.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, text in files.items():
                if text is not None:
                    zf.writestr(f"mybike-bot/{name}", text)
        return path

    def run_update(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        # Не из каталога проекта: скрипт обязан перейти туда сам.
        return subprocess.run(["bash", str(self.server / "update.sh"), *args],
                              cwd=self.base, capture_output=True, text=True,
                              env={**self.env, **env})

    def read(self, name: str) -> str:
        return (self.server / name).read_text(encoding="utf-8")

    def test_refuses_without_the_archive(self):
        r = self.run_update()
        self.assertEqual(r.returncode, 1)
        self.assertIn("укажите архив", r.stderr)
        r = self.run_update(str(self.base / "нет.zip"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("нет файла", r.stderr)
        self.assertEqual(self.calls.read_text(), "", "без архива - ни одного вызова")

    @unittest.skipUnless(shutil.which("rsync"), "нужен rsync")
    def test_relative_archive_is_taken_from_where_it_was_run(self):
        """«bash /opt/mybike-bot/update.sh mybike-bot.zip» из /root: путь
        архива - от каталога запуска, а не от каталога проекта."""
        self.archive()
        r = self.run_update("mybike-bot.zip")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.read("docker-compose.yml"), "new\n")

    def test_failed_or_cut_dump_leaves_the_code_alone(self):
        """Без дампа нет отката, поэтому дальше дампа скрипт не идёт: код,
        бэкапы и контейнеры - как были, недописанный файл удалён."""
        for mode, words in (("fail", "дамп не снялся"), ("cut", "дамп оборван")):
            r = self.run_update(str(self.archive()), FAKE_DUMP=mode)
            self.assertEqual(r.returncode, 1, mode)
            self.assertIn(words, r.stderr)
            self.assertEqual(self.read("docker-compose.yml"), "old\n", mode)
            self.assertEqual(sorted(p.name for p in (self.server / "backups").iterdir()),
                             ["mybike-2026-09-01.sql.gz"], mode)
            self.assertNotIn("bootstrap", self.calls.read_text(), mode)

    @unittest.skipUnless(shutil.which("rsync"), "нужен rsync")
    def test_dump_of_either_pg_dump_format_is_accepted(self):
        """Отметка «dump complete» ищется в хвосте, а не в последних
        строках: с 16.10 за ней идёт \\unrestrict, и проверка по трём
        строкам отвергала каждый целый дамп - обновление не шло никогда."""
        import gzip
        for dump in (DUMP_OK, DUMP_OK_OLD):
            r = self.run_update(str(self.archive()), DUMP_OK=dump)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            self.assertEqual(self.read("docker-compose.yml"), "new\n")
            made = sorted((self.server / "backups").glob("pre-update-*"))
            dumps = [p for p in made if p.name.endswith(".sql.gz")]
            self.assertEqual(len(dumps), 1)
            self.assertEqual(gzip.decompress(dumps[0].read_bytes()).decode(), dump)
            # следующий круг - с чистого листа: имя дампа до секунды
            for p in made:
                p.unlink()
            self.put(self.server / "docker-compose.yml", "old\n")

    @unittest.skipUnless(shutil.which("rsync") and shutil.which("tar"), "нужны rsync и tar")
    def test_previous_code_is_kept_for_the_rollback(self):
        """Прежний архив к откату обычно уже перезаписан новым: код до
        обновления лежит рядом с дампом, без .env, секретов, docx и
        бэкапов, и текст отката разворачивает именно его."""
        import tarfile
        r = self.run_update(str(self.archive()))
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        codes = sorted((self.server / "backups").glob("pre-update-*-code.tar.gz"))
        self.assertEqual(len(codes), 1)
        with tarfile.open(codes[0]) as tf:
            names = {n.removeprefix("./") for n in tf.getnames()}
            self.assertEqual(tf.extractfile("./docker-compose.yml").read(), b"old\n")
        self.assertIn("bootstrap.sh", names)
        for secret in (".env", "secrets/bot_token", "app/contract_template.docx",
                       "backups/mybike-2026-09-01.sql.gz"):
            self.assertNotIn(secret, names)
        self.assertFalse(any(n.startswith("backups") for n in names), names)
        self.assertIn(f"tar -xzf backups/{codes[0].name}", r.stdout)
        self.assertNotIn("прежний архив", r.stdout)

    @unittest.skipUnless(shutil.which("rsync"), "нужен rsync")
    def test_update_keeps_settings_documents_and_backups(self):
        import gzip
        r = self.run_update(str(self.archive()))
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        dumps = sorted((self.server / "backups").glob("pre-update-*.sql.gz"))
        self.assertEqual(len(dumps), 1)
        self.assertIn("dump complete", gzip.decompress(dumps[0].read_bytes()).decode())
        # код новый, а настройки, секреты, свои документы и бэкапы - свои
        self.assertEqual(self.read("docker-compose.yml"), "new\n")
        self.assertEqual(self.read(".env").count("EVIL"), 0)
        self.assertEqual(self.read("secrets/bot_token"), "настоящий токен")
        self.assertEqual(self.read("app/contract_template.docx"), "договор, правленый владельцем")
        self.assertEqual(self.read("app/new_act_template.docx"), "новый документ",
                         "нового документа на сервере не было - он доезжает")
        self.assertFalse((self.server / "backups" / "evil.sql.gz").exists())
        self.assertEqual(self.read("schema.sql"), "-- new\n", "CRLF снят")
        calls = self.calls.read_text()
        self.assertLess(calls.index("pg_dump -U mb -d mbdb"), calls.index("new-bootstrap"))
        self.assertNotIn("old-bootstrap", calls)
        self.assertIn("docker compose ps", calls)
        self.assertIn("http://127.0.0.1:18080/healthz", calls)
        self.assertIn("drop schema if exists crm cascade", r.stdout)
        self.assertIn(dumps[0].name, r.stdout)

    @unittest.skipUnless(shutil.which("rsync"), "нужен rsync")
    def test_failure_after_the_code_prints_the_rollback(self):
        r = self.run_update(str(self.archive()), FAKE_CURL="7")
        self.assertEqual(r.returncode, 1)
        self.assertIn("панель не отвечает", r.stderr)
        self.assertIn("gunzip -c backups/pre-update-", r.stdout)
        r = self.run_update(str(self.archive(**{"bootstrap.sh": "exit 1\n"})))
        self.assertEqual(r.returncode, 1)
        self.assertIn("bootstrap.sh не прошёл", r.stderr)
        self.assertIn("drop schema if exists bot cascade", r.stdout)
        # сбой посреди переноса кода (нет install.sh ни там, ни тут) - тоже откат
        (self.server / "install.sh").unlink()
        r = self.run_update(str(self.archive(**{"install.sh": None})))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Обновление прервано", r.stderr)
        self.assertIn("gunzip -c backups/pre-update-", r.stdout)

    def test_not_an_installed_project_is_refused(self):
        (self.server / ".env").unlink()
        r = self.run_update(str(self.archive()))
        self.assertEqual(r.returncode, 1)
        self.assertIn("install.sh", r.stderr)
        self.assertEqual(self.calls.read_text(), "")


def rollback_commands() -> list[str]:
    """Команды отката так, как их печатает update.sh: функция rollback из
    самого скрипта, переменные - как у сервера из TestUpdate. Строки с
    переносом «\\» склеены - одна команда на элемент."""
    text = (ROOT / "update.sh").read_text(encoding="utf-8")
    func = re.search(r"(?ms)^rollback\(\) \{\n.*?^\}\n", text).group()
    r = subprocess.run(["bash", "-c", f"{func}\nrollback"], capture_output=True, text=True,
                       env={**os.environ, "LC_ALL": "C.UTF-8", "DB_USER": "mb",
                            "DB_NAME": "mbdb", "DUMP": "backups/pre-update.sql.gz",
                            "CODE": "backups/pre-update-code.tar.gz"})
    lines = [x.strip() for x in r.stdout.replace("\\\n", " ").splitlines()
             if x.startswith("      ")]
    return [" ".join(x.split()) for x in lines]


@unittest.skipUnless(HAVE_PG and shutil.which("bash") and shutil.which("gzip"),
                     "нужны pgserver, asyncpg, bash и gzip")
class TestRollbackRestore(unittest.TestCase):
    """Откат из текста update.sh - на настоящей базе со схемой проекта.
    Сервер: дамп «до обновления», поверх - «новая версия». Команда
    заливки идёт как напечатана, docker - заглушка, которая исполняет
    «compose exec -T postgres …» прямо против тестового Postgres."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.pg = pgserver.get_server(str(base / "pg"))
        cls.host = re.search(r"host=([^&]+)", cls.pg.get_uri()).group(1)
        # psql и pg_dump - системные, если есть (так на сервере: 16.10+ с
        # \restrict в дампе), иначе из комплекта pgserver.
        from pgserver._commands import POSTGRES_BIN_PATH
        cls.bin = {name: shutil.which(name) or str(POSTGRES_BIN_PATH / name)
                   for name in ("psql", "pg_dump")}
        cls.stub = base / "bin"
        cls.stub.mkdir()
        (cls.stub / "docker").write_text(
            '#!/bin/sh\n'
            'if [ "$1 $2 $3 $4" = "compose exec -T postgres" ]; then\n'
            '  shift 4; cmd="$1"; shift; exec "$PSQL_DIR/$cmd" "$@"\n'
            'fi\n'
            'echo "docker $*" >> "$CALLS"\n', encoding="utf-8")
        (cls.stub / "docker").chmod(0o755)
        link = base / "pgbin"
        link.mkdir()
        (link / "psql").symlink_to(cls.bin["psql"])
        cls.env = {**os.environ, "LC_ALL": "C.UTF-8", "PGHOST": cls.host,
                   "PATH": f"{cls.stub}:{os.environ.get('PATH', '')}",
                   "PSQL_DIR": str(link), "CALLS": str(base / "calls.log")}
        asyncio.run(cls._prepare())
        cls.commands = rollback_commands()

    @classmethod
    async def _prepare(cls):
        """Роль и база как у сервера (.env TestUpdate), схема проекта."""
        admin = await asyncpg.connect(cls.pg.get_uri())
        try:
            await admin.execute("create role mb superuser login")
            await admin.execute("create database mbdb owner mb")
        finally:
            await admin.close()
        from app.db import Database, _init_connection
        pool = await asyncpg.create_pool(host=cls.host, user="mb", database="mbdb",
                                         min_size=1, max_size=1, init=_init_connection)
        try:
            await Database(pool).apply_schema(ROOT / "schema.sql")
        finally:
            await pool.close()

    @classmethod
    def tearDownClass(cls):
        cls.pg.cleanup()
        cls.tmp.cleanup()

    def sql(self, query: str):
        async def go():
            conn = await asyncpg.connect(host=self.host, user="mb", database="mbdb")
            try:
                return await conn.fetchval(query)
            finally:
                await conn.close()
        return asyncio.run(go())

    def command(self, word: str) -> str:
        found = [c for c in self.commands if word in c]
        self.assertEqual(len(found), 1, self.commands)
        return found[0]

    def restore(self, dump: str) -> subprocess.CompletedProcess:
        """Сервер «после обновления»: дамп прежней базы в backups/, новая
        схема и новые данные поверх. Затем - напечатанная команда заливки."""
        server = Path(self.tmp.name) / "server"
        (server / "backups").mkdir(parents=True, exist_ok=True)
        (server / "backups" / "pre-update.sql.gz").write_bytes(gzip.compress(dump.encode()))
        self.sql("insert into crm.settings (key, value) values ('probe', 'новая версия') "
                 "on conflict (key) do update set value = excluded.value")
        self.sql("create table if not exists crm.new_version_only (id int)")
        # Все шаги отката, что идут в базу, подряд и до первого сбоя - как
        # их прошёл бы человек: снос схем отдельной командой тоже в счёт.
        steps = [c for c in self.commands if "exec -T postgres" in c]
        self.assertTrue(steps, self.commands)
        return subprocess.run(["bash", "-o", "pipefail", "-c", " && ".join(steps)],
                              cwd=server, capture_output=True, text=True, env=self.env)

    def dump(self) -> str:
        """Дамп «до обновления» - настоящим pg_dump, как в update.sh."""
        self.sql("insert into crm.settings (key, value) values ('probe', 'до обновления') "
                 "on conflict (key) do update set value = excluded.value")
        self.sql("drop table if exists crm.new_version_only")
        r = subprocess.run([self.bin["pg_dump"], "-h", self.host, "-U", "mb", "-d", "mbdb"],
                           capture_output=True, text=True, check=True)
        self.assertIn("PostgreSQL database dump complete", r.stdout)
        return r.stdout

    def test_every_writer_is_stopped(self):
        """MAX-бот держит мост в основную базу и пишет обращения: живой, он
        вставлял бы строки между заливкой таблиц и их ключами."""
        stop = self.command(" stop ")
        self.assertIn("--profile max", stop, "без профиля bot-max не остановить")
        services = set(stop.split(" stop ", 1)[1].split())
        self.assertLessEqual({"bot", "crm", "bot-max"}, services)
        self.assertNotIn("postgres", services)
        order = [i for i, c in enumerate(self.commands)
                 for word in (" stop ", "gunzip -c", "tar -xzf", "bootstrap.sh") if word in c]
        self.assertEqual(order, sorted(order), "порядок отката")

    def test_rollback_restores_the_dump(self):
        dump = self.dump()
        r = self.restore(dump)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertEqual(self.sql("select value from crm.settings where key = 'probe'"),
                         "до обновления")
        self.assertIsNone(self.sql("select to_regclass('crm.new_version_only')"))
        # Ключи и триггеры на месте: заливка дошла до конца.
        self.assertTrue(self.sql("select exists (select 1 from pg_trigger "
                                 "where tgname = 'bikes_status_log' "
                                 "and tgrelid = 'crm.bikes'::regclass)"))
        self.assertTrue(self.sql("select exists (select 1 from pg_constraint "
                                 "where conrelid = 'crm.ledger'::regclass "
                                 "and contype = 'p')"))

    def test_failed_restore_leaves_the_database_as_it_was(self):
        """Сбой посреди заливки - после таблиц с данными, до ключей: всё
        откатывается, включая снос схем. Раньше снос шёл отдельной
        командой, и база оставалась наполовину: таблицы без ключей и
        триггеров, а прежний код поднимался на ней как ни в чём не бывало."""
        dump = self.dump()
        # Первый ключ: всё до него - таблицы и COPY с данными.
        cut = dump.rindex("ALTER TABLE ONLY", 0, dump.index("ADD CONSTRAINT"))
        self.assertLess(dump.index("COPY crm."), cut)
        r = self.restore(dump[:cut] + "SELECT 1/0;\n" + dump[cut:])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("division by zero", r.stderr)
        self.assertEqual(self.sql("select value from crm.settings where key = 'probe'"),
                         "новая версия")
        self.assertIsNotNone(self.sql("select to_regclass('crm.new_version_only')"))
        self.assertTrue(self.sql("select exists (select 1 from pg_constraint "
                                 "where conrelid = 'crm.ledger'::regclass "
                                 "and contype = 'p')"))


class TestComposeLimits(unittest.TestCase):
    """Предел памяти - у панели: она смотрит в интернет. База и бот без
    предела: OOM посреди записи в журнал хуже тесноты."""

    def block(self, name: str) -> str:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        found = re.search(rf"(?m)^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|^[a-z]|\Z)",
                          compose, re.S)
        self.assertIsNotNone(found, name)
        return found.group(1)

    def test_panel_has_a_limit_above_the_measured_peak(self):
        m = re.search(r"(?m)^    mem_limit: (\d+)([mg])$", self.block("crm"))
        self.assertIsNotNone(m, "у crm нет mem_limit")
        limit = int(m.group(1)) * (2 ** 30 if m.group(2) == "g" else 2 ** 20)
        # Замер: импорт xlsx на пределе распаковки поднимает панель до ~670 МБ.
        self.assertGreaterEqual(limit, 900 * 2 ** 20)
        for name in ("postgres", "bot", "backup"):
            self.assertNotIn("mem_limit", self.block(name), name)


def caddy_script() -> str:
    """Команда сервиса caddy из docker-compose.yml как её выполнит sh."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    block = compose[compose.index("\n  caddy:\n"):]
    start = block.index("    command:\n      - |\n") + len("    command:\n      - |\n")
    end = block.index("\n", block.index("exec caddy run", start)) + 1
    return "\n".join(line[8:] for line in block[start:end].splitlines()).replace("$$", "$")


@unittest.skipUnless(shutil.which("sh"), "нужен sh")
class TestCaddyfile(unittest.TestCase):
    """Caddyfile собирается при старте контейнера. Ошибка в адресе демо не
    должна уносить панель: демо дописывается, только если Caddy принял
    файл целиком. Вместо caddy - заглушка: adapt отвечает кодом из
    FAKE_ADAPT, run печатает итоговый файл."""

    def build(self, crm: str, demo: str, *, adapt: int = 0) -> tuple[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "caddy"
            fake.write_text('#!/bin/sh\nif [ "$1" = adapt ]; then exit "$FAKE_ADAPT"; fi\n'
                            'cat /tmp/Caddyfile\n', encoding="utf-8")
            fake.chmod(0o755)
            script = caddy_script().replace("/tmp/Caddyfile", f"{tmp}/Caddyfile")
            fake.write_text(fake.read_text().replace("/tmp/Caddyfile", f"{tmp}/Caddyfile"))
            r = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                               env={"PATH": f"{tmp}:{os.environ.get('PATH', '')}",
                                    "CRM_DOMAIN": crm, "DEMO_DOMAIN": demo,
                                    "FAKE_ADAPT": str(adapt), "LC_ALL": "C.UTF-8"})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout, r.stderr

    def sites(self, caddyfile: str) -> list[str]:
        return re.findall(r"(?m)^(\S+) \{$", caddyfile)

    def test_panel_and_demo(self):
        out, _ = self.build("crm.x.ru", "demo.x.ru")
        self.assertEqual(self.sites(out), ["crm.x.ru", "demo.x.ru"])
        self.assertRegex(out, r"crm\.x\.ru \{\n\trequest_body \{\n\t\tmax_size 22MB\n"
                              r"\t\}\n\treverse_proxy crm:8080\n")
        self.assertRegex(out, r"max_size 1MB\n\t\}\n\treverse_proxy crm-demo:8080\n")
        # Недокачанную загрузку нельзя держать открытой: таймауты чтения.
        self.assertIn("read_body 120s", out)
        self.assertIn("read_header 10s", out)

    def test_demo_on_the_panel_domain_keeps_the_panel(self):
        out, err = self.build("crm.x.ru", "CRM.x.ru")
        self.assertEqual(self.sites(out), ["crm.x.ru"])
        self.assertIn("совпадает с CRM_DOMAIN", err)

    def test_broken_demo_domain_keeps_the_panel(self):
        out, err = self.build("crm.x.ru", "demo.x.ru,", adapt=1)
        self.assertEqual(self.sites(out), ["crm.x.ru"])
        self.assertIn("не принял", err)

    def test_one_site_each(self):
        self.assertEqual(self.sites(self.build("", "demo.x.ru")[0]), ["demo.x.ru"])
        self.assertEqual(self.sites(self.build("crm.x.ru", "")[0]), ["crm.x.ru"])
        self.assertEqual(self.sites(self.build("", "")[0]), [])

    def test_body_limits_match_the_panel(self):
        """Предел Caddy не ниже предела панели для законной загрузки и не
        выше её предела вообще: иначе импорт на 20 МБ упрётся в Caddy, а
        лишнее прочитает панель."""
        from app.web import app as web_app
        out, _ = self.build("crm.x.ru", "demo.x.ru")
        crm, demo = (int(x) * 1000 * 1000 for x in re.findall(r"max_size (\d+)MB", out))
        self.assertGreater(crm, web_app.IMPORT_MAX_BYTES + 64 * 1024)
        self.assertLessEqual(crm, web_app.BODY_MAX)
        self.assertLessEqual(demo, web_app.DEMO_BODY_MAX)


class TestConsistencyDemoGuard(unittest.TestCase):
    """consistency.py не пускает в блок демо боевые секреты и тома: логин
    демо публичен, а сброс сносит схемы своей базы."""

    COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    def check(self, compose: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "docker-compose.yml").write_text(compose, encoding="utf-8")
            return subprocess.run([sys.executable, str(ROOT / "consistency.py"), tmp],
                                  capture_output=True, text=True)

    def demo_lines(self, compose: str) -> list[str]:
        out = self.check(compose).stdout
        return [line for line in out.splitlines() if "демо" in line or "crm-demo" in line]

    def test_current_compose_is_clean(self):
        self.assertEqual(self.demo_lines(self.COMPOSE), [])

    def inject(self, old: str, new: str) -> str:
        self.assertEqual(self.COMPOSE.count(old), 1, old)
        return self.COMPOSE.replace(old, new)

    def test_forbidden_secret_fails(self):
        bad = self.inject("    secrets: [demo_db_password, crm_demo_secret]\n",
                          "    secrets: [demo_db_password, crm_demo_secret, bot_token]\n")
        r = self.check(bad)
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stdout, r"crm-demo.*bot_token")

    def test_prod_secret_file_fails(self):
        bad = self.inject("      CRM_SECRET_FILE: /run/secrets/crm_demo_secret\n",
                          "      CRM_SECRET_FILE: /run/secrets/crm_secret\n")
        self.assertRegex(self.check(bad).stdout, r"crm-demo.*crm_secret")

    def test_prod_volume_fails(self):
        bad = self.inject("    command: [\"python\", \"-m\", \"app.demo\"]\n",
                          "    command: [\"python\", \"-m\", \"app.demo\"]\n"
                          "    volumes:\n      - kycfiles:/files:ro\n")
        self.assertRegex(self.check(bad).stdout, r"crm-demo.*kycfiles")
        bad = self.inject("      - pgdata_demo:/var/lib/postgresql/data\n",
                          "      - pgdata:/var/lib/postgresql/data\n")
        self.assertRegex(self.check(bad).stdout, r"postgres-demo.*pgdata")

    def test_prod_database_host_fails(self):
        bad = self.inject("      POSTGRES_HOST: postgres-demo\n",
                          "      POSTGRES_HOST: postgres\n")
        self.assertRegex(self.check(bad).stdout, r"crm-demo ходит не в postgres-demo")

    def test_missing_demo_service_is_loud(self):
        bad = self.inject("  crm-demo:\n", "  crm-demo-renamed:\n")
        self.assertRegex(self.check(bad).stdout, r"нет сервиса crm-demo")

    COMMAND = '    command: ["python", "-m", "app.demo"]\n'

    def add_to_crm_demo(self, lines: str) -> str:
        return self.inject(self.COMMAND, self.COMMAND + lines)

    def test_keys_that_pull_in_another_service_fail(self):
        """extends, слияние YAML, volumes_from и env_file имён боевых
        секретов в блоке не содержат, а боевое приносят целиком."""
        for lines, key in (("    extends: crm\n", "extends"),
                           ("    <<: *crm\n", "<<"),
                           ("    volumes_from: [crm]\n", "volumes_from"),
                           ("    env_file: .env\n", "env_file"),
                           ("    network_mode: host\n", "network_mode"),
                           ("    privileged: true\n", "privileged")):
            out = self.check(self.add_to_crm_demo(lines)).stdout
            self.assertRegex(out, rf"crm-demo\): ключи \['{re.escape(key)}'\]", key)

    def test_bind_mounts_fail(self):
        for source in ("./backups", "/var/run/docker.sock", "~/secrets"):
            bad = self.add_to_crm_demo(f"    volumes:\n      - {source}:/x\n")
            self.assertIn(f"том «{source}:/x»", self.check(bad).stdout, source)
        bad = self.add_to_crm_demo("    volumes:\n      - type: bind\n"
                                   "        source: ./backups\n        target: /x\n")
        self.assertIn("том «./backups»", self.check(bad).stdout)

    def test_demo_secret_pointing_at_a_prod_file_fails(self):
        """Имя в блоке демо верное, а файл в общем разделе secrets: -
        боевой: самая вероятная ошибка копипасты."""
        for name, prod in (("demo_db_password", "db_password"),
                           ("crm_demo_secret", "crm_secret")):
            bad = self.inject(f"  {name}:\n    file: ./secrets/{name}\n",
                              f"  {name}:\n    file: ./secrets/{prod}\n")
            self.assertIn(f"секрет {name} в разделе secrets:", self.check(bad).stdout)

    def test_demo_network_is_its_own(self):
        bad = self.inject("    networks: [demo]\n    secrets: [demo_db_password]\n",
                          "    networks: [default, demo]\n    secrets: [demo_db_password]\n")
        self.assertRegex(self.check(bad).stdout, r"postgres-demo\): сети")
        bad = self.inject("    networks: [demo]\n    # Демо открыто", "    # Демо открыто")
        self.assertRegex(self.check(bad).stdout, r"crm-demo\): сети None")
        bad = self.inject("    networks: [default, demo]\n", "    networks: [demo]\n")
        self.assertRegex(self.check(bad).stdout, r"caddy в сетях")

    def test_any_spelling_of_the_database_host_passes(self):
        for line in ('      POSTGRES_HOST: "postgres-demo"\n',
                     "      POSTGRES_HOST: 'postgres-demo'\n",
                     "      - POSTGRES_HOST=postgres-demo\n",
                     '      - "POSTGRES_HOST=postgres-demo"\n'):
            good = self.inject("      POSTGRES_HOST: postgres-demo\n", line)
            self.assertEqual(self.demo_lines(good), [], line)
        bad = self.inject("      POSTGRES_HOST: postgres-demo\n",
                          '      POSTGRES_HOST: "postgres"\n')
        self.assertRegex(self.check(bad).stdout, r"crm-demo ходит не в postgres-demo")


if __name__ == "__main__":
    unittest.main()
