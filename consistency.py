"""Сверка файлов проекта между собой.

Ищет расхождения, которые не видит ни один тест: переменная объявлена в одном
месте и забыта в другом, файл есть в проекте но не заливается на сервер,
колонка используется в коде но отсутствует в схеме.
"""
import ast
import importlib.util
import pathlib
import re
import string
import sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
problems = []


def read(name):
    p = ROOT / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


compose = read("docker-compose.yml")
env_example = read(".env.example")
# Схем две: bot.* и fleet.*. Для сверки колонок они равноправны - колонка,
# объявленная в fleet_schema.sql, ничем не хуже колонки из schema.sql.
schema = read("schema.sql") + "\n" + read("fleet_schema.sql")
deploy = read("deploy.ps1")
config = read("app/config.py")

# ── 1. переменные: compose ↔ .env.example ────────────────────────────────
compose_vars = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", compose))
env_vars = set(re.findall(r"^([A-Z_][A-Z0-9_]*)=", env_example, re.M))
missing_in_env = compose_vars - env_vars
if missing_in_env:
    problems.append(
        f"compose подставляет ${{}} для переменных, которых нет в .env.example: "
        f"{sorted(missing_in_env)} -> контейнер получит пустую строку")

unused_in_compose = env_vars - compose_vars - {"POSTGRES_PASSWORD"}
if unused_in_compose:
    problems.append(
        f".env.example объявляет переменные, которые compose не передаёт в контейнер: "
        f"{sorted(unused_in_compose)} -> заполнение ничего не изменит")

# ── 2. переменные, которые читает config.py ──────────────────────────────
config_vars = set(re.findall(r'_env\(\s*"([A-Z_][A-Z0-9_]*)"', config))
config_vars |= set(re.findall(r'_int\(\s*"([A-Z_][A-Z0-9_]*)"', config))
config_secrets = set(re.findall(r'_secret\(\s*"([A-Z_][A-Z0-9_]*)"', config))
# Переменные, которые compose задаёт литералом, а не через ${}: подставлять
# их из .env незачем, значение фиксировано устройством контейнера.
LITERAL_IN_COMPOSE = {"POSTGRES_HOST", "POSTGRES_PORT", "STORAGE_DIR",
                      "CONTRACT_TEMPLATE", "ACT_IN_TEMPLATE", "ACT_OUT_TEMPLATE",
                      "SOGLASIE_TEMPLATE", "PDN_POLICY_FILE",
                      "AUTO_APPROVE", "RATE_SOFT", "RATE_HARD"}
not_passed = config_vars - compose_vars - LITERAL_IN_COMPOSE
if not_passed:
    problems.append(
        f"config.py читает переменные, которых compose не передаёт: {sorted(not_passed)}")

for secret in config_secrets:
    if f"{secret}_FILE" not in compose:
        problems.append(f"секрет {secret} не пробрасывается через {secret}_FILE в compose")

# ── 3. состав пакета ↔ список заливки в deploy.ps1 ───────────────────────
# Служебные каталоги в состав пакета не входят. Без исключения .git проверка
# требовала заливать на сервер всю историю репозитория и выдавала полсотни
# «файлов», которых в проекте нет. .claude - локальные настройки редактора,
# они в .gitignore и на сервере не нужны.
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", ".ruff_cache",
             ".pytest_cache", ".claude"}
shipped = {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*")
           if p.is_file() and not SKIP_DIRS & set(p.parts)}
# Берём только то, что похоже на имя файла в проекте: с точкой или Dockerfile
# (у него нет расширения). Абсолютные пути - это каталог развёртывания, не файл.
listed = {m for m in re.findall(r"'([\w./-]+)'", deploy)
          if not m.startswith("/") and ("." in m or m == "Dockerfile")}
listed |= {".env.example", ".gitignore"}
not_uploaded = {f for f in shipped - listed
                if not f.endswith((".zip",)) and f != "deploy.ps1"}
if not_uploaded:
    problems.append(f"файлы есть в проекте, но deploy.ps1 их не заливает: {sorted(not_uploaded)}")

ghost = {f for f in listed - shipped if f not in {".env.example", ".gitignore"}}
if ghost:
    problems.append(f"deploy.ps1 ждёт файлы, которых нет: {sorted(ghost)} -> заливка упадёт")

# ── 4. колонки, используемые в коде, против схемы ────────────────────────
code = "\n".join(read(p.relative_to(ROOT).as_posix())
                 for p in (ROOT / "app").rglob("*.py"))
schema_cols = set(re.findall(r"^\s{2,}(\w+)\s+(?:bigint|text|jsonb|numeric|timestamptz"
                             r"|integer|bigserial|serial|date|boolean)",
                             schema, re.M))
schema_cols |= set(re.findall(r"add column if not exists\s+(\w+)", schema))
used = set(re.findall(r'"(\w+)"\s*:', "")) | set(re.findall(r"bot\.users\s+set\s+(\w+)", code))
for col in re.findall(r"(\w+)\s*=\s*(?:null|\$\d+)", code):
    used.add(col)
unknown_cols = {c for c in used if c not in schema_cols and c.islower() and "_" in c}
if unknown_cols:
    problems.append(f"в коде встречаются колонки, которых нет в schema.sql: {sorted(unknown_cols)}")

# ── 5. значения-заглушки, которые проверяет bootstrap ────────────────────
boot = read("bootstrap.sh")
# Значения в .env.example взяты в кавычки - иначе пробел внутри значения
# ломает чтение файла. Для сравнения кавычки снимаем.
env_plain = re.sub(r'^([A-Z_][A-Z0-9_]*)="(.*)"$', r"\1=\2", env_example, flags=re.M)
for var in re.findall(r'if \[ "\$(\w+)" = "([^"]+)" \]', boot):
    name, placeholder = var
    if f"{name}={placeholder}" not in env_plain:
        problems.append(
            f"bootstrap.sh считает заглушкой {name}={placeholder}, "
            f"но в .env.example другое значение -> проверка не сработает")

# ── 6. поля шаблонов texts.py против того, что подставляют вызовы ────────
# Шаблон один, а ботов два: добавленное в texts.py поле легко подставить
# в Telegram-боте и забыть в MAX - тот падает с KeyError уже в проде,
# на живом клиенте. Проверка статическая: разбираем каждый вызов
# texts.ИМЯ.format(...) и сверяем именованные аргументы с {полями} шаблона.
texts_path = ROOT / "app" / "texts.py"
if texts_path.exists():
    spec = importlib.util.spec_from_file_location("_texts_check", texts_path)
    texts_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(texts_mod)
    for py in sorted((ROOT / "app").rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "format"
                    and isinstance(node.func.value, ast.Attribute)
                    and isinstance(node.func.value.value, ast.Name)
                    and node.func.value.value.id == "texts"):
                continue
            # Позиционные аргументы и **kwargs статически не разобрать.
            if node.args or any(kw.arg is None for kw in node.keywords):
                continue
            name = node.func.value.attr
            template = getattr(texts_mod, name, None)
            if not isinstance(template, str):
                problems.append(f"{py.name}: texts.{name} - такого шаблона нет")
                continue
            need = {field for _, field, _, _ in string.Formatter().parse(template)
                    if field}
            got = {kw.arg for kw in node.keywords}
            where = f"{py.relative_to(ROOT).as_posix()}:{node.lineno}"
            if need - got:
                problems.append(
                    f"{where}: texts.{name}.format() не передаёт "
                    f"{sorted(need - got)} -> KeyError при отправке")
            if got - need:
                problems.append(
                    f"{where}: texts.{name}.format() передаёт лишние "
                    f"{sorted(got - need)} -> подстановка молча пропадёт")

# ── итог ─────────────────────────────────────────────────────────────────
if problems:
    print(f"НАЙДЕНО РАСХОЖДЕНИЙ: {len(problems)}\n")
    for i, p in enumerate(problems, 1):
        print(f"{i}. {p}\n")
    sys.exit(1)
print("расхождений между файлами не найдено")
