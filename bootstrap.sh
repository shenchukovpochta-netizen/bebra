#!/usr/bin/env bash
# Разворачивание на чистой Ubuntu (Beget VPS). От root, из каталога проекта:
#   bash bootstrap.sh
# Идемпотентен: повторный запуск не перегенерирует секреты.
set -euo pipefail

cd "$(dirname "$0")"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mОШИБКА: %s\033[0m\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then die "запускать от root"; fi
if [ ! -f .env ]; then die "нет .env. Сначала: cp .env.example .env и заполнить"; fi

# uid процесса внутри контейнера. Читается из Dockerfile, а не пишется числом
# в двух местах: разъехавшись, эти два числа дают отказ в доступе к секретам,
# который виден только в трейсбеке при старте.
BOT_UID="$( { awk '/--uid/{ for (i=1;i<NF;i++) if ($i=="--uid") { print $(i+1); exit } }' Dockerfile 2>/dev/null || true; } )"
: "${BOT_UID:=10001}"

set -a; . ./.env; set +a
: "${CHANNEL_ID:?не задан в .env}" "${ADMIN_CHAT_ID:?не задан в .env}" "${ADMINS:?не задан в .env}"
if [ "$CHANNEL_ID" = "-1001234567890" ]; then die "CHANNEL_ID в .env остался примером"; fi
if [ "$ADMINS" = "111111111" ]; then die "ADMINS в .env остался примером"; fi
# Демо-стенд не должен отнимать у панели ни адрес, ни порт. Одинаковый
# домен Caddy не примет вовсе (демо он пропустит сам, но это ошибка в
# .env), одинаковый порт на одном адресе получит тот контейнер, что
# поднимется первым, - после перезагрузки это может быть демо, а не
# панель. install.sh зовёт этот скрипт, так что проверка общая.
lower() { printf %s "$1" | tr '[:upper:]' '[:lower:]'; }
if [ -n "${DEMO_DOMAIN:-}" ] && [ "$(lower "$DEMO_DOMAIN")" = "$(lower "${CRM_DOMAIN:-}")" ]; then
  die "DEMO_DOMAIN совпадает с CRM_DOMAIN ($CRM_DOMAIN): демо нужен свой поддомен, например demo.${CRM_DOMAIN#*.}"
fi
if [ "${DEMO_PORT:-8081}" = "${CRM_PORT:-8080}" ]; then
  demo_bind="${DEMO_BIND:-127.0.0.1}" crm_bind="${CRM_BIND:-127.0.0.1}"
  if [ "${demo_bind}" = "${crm_bind}" ] || [ "${demo_bind}" = 0.0.0.0 ] \
      || [ "${crm_bind}" = 0.0.0.0 ]; then
    die "DEMO_PORT совпадает с CRM_PORT (${CRM_PORT:-8080}): панель и демо не поднимутся на одном порту - поставьте DEMO_PORT=8081"
  fi
fi

# ─── 1. Docker ───────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  say "ставлю Docker из официального репозитория"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  say "Docker уже установлен: $(docker --version)"
fi

# ─── 2. Файрвол ──────────────────────────────────────────────────────────────
# Боту не нужен ни один открытый входящий порт: long polling ходит только
# наружу. Поэтому открыт SSH, а 80/443 - только когда задан домен панели.
if command -v ufw >/dev/null 2>&1; then
  say "настраиваю ufw (наружу открыт только SSH)"
  # Порт берётся из конфигурации самого sshd, а не из профиля «OpenSSH»:
  # профиль открывает ровно 22, и на сервере с перенесённым портом включение
  # ufw отрезает доступ намертво - лечится потом только консолью хостера.
  SSH_PORTS="$( { sshd -T 2>/dev/null || true; } | awk '/^port /{print $2}' )"
  if [ -z "$SSH_PORTS" ]; then
    SSH_PORTS="$( { grep -E '^[[:space:]]*Port[[:space:]]+[0-9]+' /etc/ssh/sshd_config 2>/dev/null || true; } | awk '{print $2}' )"
  fi
  [ -n "$SSH_PORTS" ] || SSH_PORTS=22
  for p in $SSH_PORTS; do
    ufw allow "$p/tcp" >/dev/null
    printf '    открыт %s/tcp — по нему вы сейчас и подключены\n' "$p"
  done
  # Домен панели или демо-стенда задан - Caddy нужны 80 (выпуск и
  # продление сертификата Let's Encrypt) и 443 (сами сайты). Без домена
  # наружу по-прежнему только SSH.
  if [ -n "${CRM_DOMAIN:-}" ] || [ -n "${DEMO_DOMAIN:-}" ]; then
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null
    printf '    открыты 80 и 443/tcp\n'
    [ -z "${CRM_DOMAIN:-}" ] || printf '    панель: https://%s\n' "$CRM_DOMAIN"
    [ -z "${DEMO_DOMAIN:-}" ] || printf '    демо-стенд: https://%s\n' "$DEMO_DOMAIN"
  fi
  ufw --force enable >/dev/null
  ufw status | sed 's/^/    /'
fi

# ─── 3. Секреты ──────────────────────────────────────────────────────────────
mkdir -p secrets && chmod 700 secrets
if [ ! -s secrets/db_password ]; then
  # hex, а не base64: в base64 попадаются / + =, которые приходится
  # экранировать в каждом месте, где пароль куда-нибудь подставляется.
  openssl rand -hex 32 | tr -d '\n' > secrets/db_password
  say "сгенерирован secrets/db_password"
fi
if [ ! -s secrets/pdn_key ]; then
  # Ключ шифрования анкеты. Генерируется один раз: смена ключа делает
  # незаконченные анкеты нечитаемыми, и люди проходят их заново.
  openssl rand -base64 32 | tr -d '\n' > secrets/pdn_key
  say "сгенерирован secrets/pdn_key — положите его в бэкап отдельно от базы"
fi
if [ ! -s secrets/bot_token ]; then
  die "нет secrets/bot_token. Создайте: printf '%s' '<токен>' > secrets/bot_token"
fi
# Веб-панель CRM: ключ подписи cookie и пароль первого администратора.
# Пароль нужен один раз - при пустой таблице сотрудников; дальше живёт
# в базе, и файл можно удалить после первого входа.
if [ ! -s secrets/crm_secret ]; then
  openssl rand -hex 32 | tr -d '\n' > secrets/crm_secret
  say "сгенерирован secrets/crm_secret"
fi
if [ ! -s secrets/crm_admin_password ]; then
  # Не «tr </dev/urandom | head»: head закрывает трубу, tr падает по
  # SIGPIPE, и под pipefail установка молча обрывалась на этой строке.
  openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | cut -c1-16 | tr -d '\n' \
    > secrets/crm_admin_password
  say "сгенерирован пароль панели CRM: secrets/crm_admin_password"
fi
# Демо-стенд: пароль его базы и ключ его cookie - свои, не боевые (логин
# демо публичен). Генерируются и без DEMO_DOMAIN: compose объявляет
# секреты на уровне файла, и без файлов не поднялся бы вовсе.
if [ ! -s secrets/demo_db_password ]; then
  openssl rand -hex 32 | tr -d '\n' > secrets/demo_db_password
  say "сгенерирован secrets/demo_db_password"
fi
if [ ! -s secrets/crm_demo_secret ]; then
  openssl rand -hex 32 | tr -d '\n' > secrets/crm_demo_secret
  say "сгенерирован secrets/crm_demo_secret"
fi
# Пустой файл-заглушка: compose объявляет секрет max_bot_token на уровне
# файла, и отсутствие файла ломало бы запуск даже тем, кто MAX не включал.
# Реальный токен кладётся сюда только при включении профиля max.
[ -f secrets/max_bot_token ] || : > secrets/max_bot_token
# Трекеры StarLine необязательны: пустые файлы нужны только чтобы compose
# поднялся. Заполните их - и опрос заведётся сам при следующем рестарте.
[ -f secrets/starline_app_secret ] || : > secrets/starline_app_secret
[ -f secrets/starline_password ] || : > secrets/starline_password
# Счёт в Точке - тоже необязателен.
[ -f secrets/tochka_token ] || : > secrets/tochka_token
# Авито - тоже: пустой секрет значит «интеграция выключена».
[ -f secrets/avito_client_secret ] || : > secrets/avito_client_secret
# «Входящие»: ключ переписки генерируется сразу - без него обращения
# пишутся без текста. Смена ключа делает старую переписку нечитаемой.
if [ ! -s secrets/inbox_key ]; then
  openssl rand -base64 32 | tr -d '\n' > secrets/inbox_key
  say "сгенерирован secrets/inbox_key — положите его в бэкап отдельно от базы"
fi
# Токен хука для шлюза WhatsApp и n8n: пустой - хук выключен. Включить:
#   openssl rand -hex 32 | tr -d '\n' > secrets/inbox_hook_token
[ -f secrets/inbox_hook_token ] || : > secrets/inbox_hook_token
chmod 600 secrets/* .env
# Владелец - uid 10001, под которым работает процесс в контейнере (см. Dockerfile).
# Вне swarm docker compose не копирует файл секрета, а подключает хостовый как
# есть, вместе с владельцем и правами. root:root 600 контейнер прочитать
# не может и падает на старте с PermissionError: /run/secrets/bot_token.
# Postgres это не задевало: его entrypoint читает пароль ещё под root.
chown "$BOT_UID:$BOT_UID" secrets/*

# ─── 4. Запуск ───────────────────────────────────────────────────────────────
say "собираю образ и поднимаю контейнеры"
docker compose up -d --build

say "жду Postgres"
for i in $(seq 1 30); do
  if docker compose exec -T postgres pg_isready -U "${POSTGRES_USER:-mybike}" -d "${POSTGRES_DB:-mybike}" >/dev/null 2>&1; then
    break
  fi
  if [ "$i" -eq 30 ]; then die "Postgres не поднялся: docker compose logs postgres"; fi
  sleep 2
done
# Схему бот применяет сам при старте - отдельный шаг не нужен.

# ─── 5. Проверки ─────────────────────────────────────────────────────────────
say "проверяю доступность Telegram с сервера"
if curl -sS -m 10 "https://api.telegram.org/bot$(cat secrets/bot_token)/getMe" | grep -q '"ok":true'; then
  printf '    Telegram отвечает\n'
else
  printf '\033[31m    Telegram НЕ отвечает. Без прокси бот работать не будет.\033[0m\n'
fi

say "готово"
cat <<EOF
    Логи:      docker compose logs -f bot
    Рестарт:   docker compose restart bot
    Обновить:  docker compose up -d --build

    В логе при успешном старте: «схема применена» и «бот @<имя> готов».
    Дальше отправьте боту /start.
EOF
