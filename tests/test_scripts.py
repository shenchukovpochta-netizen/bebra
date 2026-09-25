"""Строки установочных скриптов, которые ломались молча: генерация пароля
панели под pipefail и чтение TZ из .env при повторном запуске install.sh.
Команды берутся из самих скриптов, а не переписываются в тесте."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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
        for name in ("bootstrap.sh", "install.sh"):
            r = subprocess.run(["bash", "-n", str(ROOT / name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


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
