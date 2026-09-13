"""Строки установочных скриптов, которые ломались молча: генерация пароля
панели под pipefail и чтение TZ из .env при повторном запуске install.sh.
Команды берутся из самих скриптов, а не переписываются в тесте."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
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
                    "MAX_FIX_CHAT_ID", "MAX_CONTRACT_PREFIX", "MAX_API_BASE"):
            self.assertRegex(text, rf'(?m)^{key}="\$\{{{key}:-[^}}]*\}}"$',
                             f"{key} не переносится в новый .env")

    def test_scripts_parse(self):
        for name in ("bootstrap.sh", "install.sh"):
            r = subprocess.run(["bash", "-n", str(ROOT / name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
