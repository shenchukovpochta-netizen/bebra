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
schema = read("schema.sql")
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

# COMPOSE_PROFILES читает сам docker compose из .env, в контейнеры он не идёт.
unused_in_compose = env_vars - compose_vars - {"POSTGRES_PASSWORD", "COMPOSE_PROFILES"}
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
                      "BUYOUT_TEMPLATE",
                      "SOGLASIE_TEMPLATE", "PDN_POLICY_FILE",
                      # Каталоги томов: значение задано устройством
                      # контейнера, подставлять его из .env незачем.
                      "DOC_TEMPLATE_DIR", "BIKE_PHOTO_DIR",
                      "AUTO_APPROVE", "RATE_SOFT", "RATE_HARD"}
not_passed = config_vars - compose_vars - LITERAL_IN_COMPOSE
if not_passed:
    problems.append(
        f"config.py читает переменные, которых compose не передаёт: {sorted(not_passed)}")

# Секреты читают три процесса: бот, панель и MAX-бот. Проверка - по блоку
# СВОЕГО сервиса: секрет, проброшенный боту, но забытый у панели, молча
# выключает её часть (хук «Входящих» без токена), а общий поиск по файлу
# этого не видел.
def service_block(name: str) -> str:
    found = re.search(rf"(?m)^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|^[A-Za-z]|\Z)",
                      compose, re.S)
    return found.group(1) if found else ""


for source, service in (("app/config.py", "bot"), ("app/web/config.py", "crm"),
                        ("app/max_main.py", "bot-max")):
    block = service_block(service)
    listed = re.search(r"secrets:\s*\[([^\]]*)\]", block, re.S)
    mounted = set(re.findall(r"[a-z0-9_]+", listed.group(1))) if listed else set()
    for secret in sorted(set(re.findall(r'_secret\(\s*"([A-Z_][A-Z0-9_]*)"', read(source)))):
        path = re.search(rf"(?<![A-Z0-9_]){secret}_FILE:\s*/run/secrets/([a-z0-9_]+)", block)
        if path is None:
            problems.append(f"секрет {secret} ({source}) не пробрасывается сервису "
                            f"{service} через {secret}_FILE")
        elif path.group(1) not in mounted:
            problems.append(f"секрет {path.group(1)} не смонтирован сервису {service} "
                            f"(нет в его secrets:)")

# ── 2а. демо-стенд изолирован от боевых данных ───────────────────────────
# Логин демо публичен, а его сброс сносит схемы crm и bot целиком. Боевой
# секрет или том, скопированный в блок демо вместе с соседним сервисом,
# отдал бы всем посетителям настоящих клиентов (152-ФЗ) или бота, а
# база postgres вместо postgres-demo стёрлась бы первым же сбросом.
# Проверка - белым списком, а не поиском запрещённых имён: `extends: crm`,
# `<<: *crm`, `volumes_from`, `env_file` или файл секрета, переписанный на
# боевой в общем разделе secrets:, имён боевых секретов в блоке демо не
# содержат, а боевое приносят.
DEMO_FORBIDDEN = ("db_password", "bot_token", "crm_secret", "crm_admin_password",
                  "tochka_token", "inbox_key", "inbox_hook_token", "pdn_key",
                  "avito_client_secret", "max_bot_token", r"starline_\w+",
                  # тома боевой панели и базы
                  "kycfiles", "bikefiles", "doctemplates", "pgdata")
DEMO_SECRETS = {"demo_db_password": "./secrets/demo_db_password",
                "crm_demo_secret": "./secrets/crm_demo_secret"}
DEMO_VOLUMES = {"pgdata_demo"}
# Ключи, из которых собран блок демо. Всё прочее - повод посмотреть
# глазами: extends, volumes_from, env_file, network_mode, privileged,
# cap_add, devices, pid, ipc и слияние YAML (<<) тянут чужое целиком.
DEMO_KEYS = {"image", "build", "profiles", "restart", "command", "depends_on",
             "networks", "secrets", "environment", "healthcheck", "ports", "volumes",
             "logging", "mem_limit", "pids_limit", "tmpfs", "stop_grace_period"}


def service_items(block: str, key: str) -> list[str] | None:
    """Значения ключа сервиса: `[a, b]` в строку или «- a» столбиком
    (длинная запись тома - по его `source:`). None - ключа нет."""
    found = re.search(rf"(?m)^    {re.escape(key)}:[ \t]*(.*)$", block)
    if found is None:
        return None
    inline = found.group(1).strip()
    if inline.startswith("["):
        return [x.strip().strip("'\"") for x in inline.strip("[]").split(",") if x.strip()]
    if inline:
        return [inline.strip("'\"")]
    items = []
    for line in block[found.end():].split("\n")[1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= 4:
            break
        item = re.match(r"\s+(?:-\s*)?source:\s*(\S+)", line) \
            or re.match(r"\s+-\s*(?!type:)(\S.*?)\s*$", line)
        if item:
            items.append(item.group(1).strip("'\""))
    return items


def top_section(name: str) -> str:
    found = re.search(rf"(?m)^{name}:\n(.*?)(?=^\S|\Z)", compose, re.S)
    return found.group(1) if found else ""


def uncommented(text: str) -> str:
    # Комментарии - не конфигурация: «не монтировать kycfiles» в пояснении
    # проверку ронять не должно.
    return re.sub(r"(?m)(^|\s)#.*$", r"\1", text)


for service in ("crm-demo", "postgres-demo"):
    block = uncommented(service_block(service))
    if not block.strip():
        problems.append(f"в docker-compose.yml нет сервиса {service}: проверка "
                        f"изоляции демо-стенда не может его найти")
        continue
    where = f"демо-стенд ({service})"
    leaked = sorted({m.group() for name in DEMO_FORBIDDEN
                     for m in re.finditer(rf"(?<![\w-]){name}(?![\w-])", block)})
    if "./secrets" in block:
        leaked.append("./secrets")
    if leaked:
        problems.append(f"{where} получает боевое: {leaked} -> логин демо "
                        f"публичен, это отдало бы их всем. Демо - только "
                        f"demo_db_password, crm_demo_secret и свой том")
    keys = re.findall(r"(?m)^    (<<|[A-Za-z_][\w-]*)\s*:", block)
    odd = sorted(set(keys) - DEMO_KEYS)
    if odd:
        problems.append(f"{where}: ключи {odd} вне белого списка -> extends, <<, "
                        f"volumes_from, env_file и им подобные приносят чужие секреты "
                        f"и тома целиком. Разрешены: {sorted(DEMO_KEYS)}")
    stranger = sorted((set(service_items(block, "secrets") or [])
                       | set(re.findall(r"/run/secrets/([\w.-]+)", block)))
                      - set(DEMO_SECRETS))
    if stranger:
        problems.append(f"{where}: секреты {stranger} не демо -> разрешены только "
                        f"{sorted(DEMO_SECRETS)}")
    for item in service_items(block, "volumes") or []:
        source = item.split(":", 1)[0].strip()
        if source.startswith((".", "/", "~", "$")) or source not in DEMO_VOLUMES:
            problems.append(f"{where}: том «{item}» -> каталог сервера или чужой том "
                            f"(бэкапы, docker.sock) достался бы посетителям; "
                            f"можно только {sorted(DEMO_VOLUMES)}")
    nets = service_items(block, "networks")
    if not nets or set(nets) != {"demo"}:
        problems.append(f"{where}: сети {nets} -> демо живёт только в сети demo, "
                        f"иначе из него видны postgres и crm боевого стека")
crm_demo = uncommented(service_block("crm-demo"))
host = re.search(r"""(?m)^\s+-?\s*["']?POSTGRES_HOST["']?\s*[:=]\s*["']?([^"'\s]+)["']?\s*$""",
                 crm_demo)
if crm_demo.strip() and (host is None or host.group(1) != "postgres-demo"):
    problems.append("crm-demo ходит не в postgres-demo -> ночной сброс демо снёс бы "
                    "схемы crm и bot чужой базы")
# Файлы секретов демо - свои: имя в блоке демо верное, а путь в общем
# разделе secrets:, переписанный на боевой, отдал бы демо боевой пароль.
declared = uncommented(top_section("secrets"))
for name, path in DEMO_SECRETS.items():
    found = re.search(rf"(?m)^  {name}:\n((?:^    .*\n?)*)", declared)
    body = found.group(1) if found else ""
    source = re.search(r"(?m)^    file:\s*[\"']?([^\"'\s]+)", body)
    if source is None or source.group(1) != path or re.search(r"(?m)^    (?!file:)", body):
        problems.append(f"секрет {name} в разделе secrets: должен быть ровно "
                        f"file: {path} -> иначе демо получает чужой файл")
caddy_nets = service_items(uncommented(service_block("caddy")), "networks") or []
if service_block("crm-demo") and not {"default", "demo"} <= set(caddy_nets):
    problems.append(f"caddy в сетях {caddy_nets} -> ему нужны обе, default и demo: "
                    f"иначе он не достанет до crm или до crm-demo")

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
schema_cols = set(re.findall(r"^\s{2,}(\w+)\s+(?:bigint|text|jsonb|numeric|timestamptz|integer"
                             r"|bigserial|date|boolean)",
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
