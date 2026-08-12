#!/usr/bin/env bash
# Быстрая установка на чистую Ubuntu (Beget VPS). От root, из каталога проекта:
#
#   bash install.sh
#
# Спрашивает всё, что нужно, проверяет каждый ответ через Telegram API,
# генерирует секреты и запускает бота. Редактировать .env руками не требуется.
#
# Отличие от bootstrap.sh: тот берёт готовый .env и молча разворачивает,
# этот .env составляет. Механическую часть install.sh не дублирует, а вызывает
# bootstrap.sh - иначе два скрипта неизбежно разъезжаются в мелочах.
#
# Повторный запуск безопасен: секреты не перегенерируются, а уже заполненные
# ответы предлагаются значениями по умолчанию - достаточно жать Enter.

set -euo pipefail
cd "$(dirname "$0")"

bold()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()    { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '    \033[33m!\033[0m %s\n' "$*"; }
die()   { printf '\n\033[31mОШИБКА: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "запускать от root: sudo bash install.sh"
[ -f docker-compose.yml ] || die "запускать из каталога проекта (тут нет docker-compose.yml)"

# ─── 0. Утилиты, без которых нечем спрашивать и проверять ────────────────────
if ! command -v curl >/dev/null 2>&1 || ! command -v openssl >/dev/null 2>&1; then
  bold "Ставлю curl и openssl"
  apt-get update -qq
  apt-get install -y -qq curl openssl ca-certificates
fi

# Значения из прошлого запуска - чтобы при повторе просто жать Enter.
# TZ берётся строкой из файла, а не из окружения: переменная часто приходит
# по SSH с машины администратора, а от неё зависит дата в шапке договора.
#
# Чтение обёрнуто в проверку файла не для красоты: при set -o pipefail sed
# на несуществующем .env возвращает 2, из-за чего присваивание считается
# упавшим и set -e молча обрывает скрипт. На первой установке .env как раз
# и нет - то есть падало ровно там, где важнее всего.
TZ_VALUE=""
if [ -f .env ]; then
  TZ_VALUE="$(sed -n 's/^TZ=//p' .env | head -1)"
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi
: "${TZ_VALUE:=Europe/Moscow}"

OLD_TOKEN=""
if [ -s secrets/bot_token ]; then OLD_TOKEN="$(cat secrets/bot_token)"; fi

# ─── вспомогательное: спросить с проверкой ──────────────────────────────────
# ask ПЕРЕМЕННАЯ "вопрос" "регулярка" "подсказка при ошибке" [можно_пусто]
ask() {
  local var="$1" prompt="$2" pattern="$3" hint="$4" allow_empty="${5:-no}"
  local current="${!var-}" value
  while :; do
    if [ -n "$current" ]; then
      printf '%s\n    [Enter = %s]: ' "$prompt" "$current"
    else
      printf '%s\n    ' "$prompt"
    fi
    read -r value || die "ввод прерван"
    [ -z "$value" ] && value="$current"
    if [ -z "$value" ] && [ "$allow_empty" = "yes" ]; then
      printf -v "$var" '%s' ""
      return 0
    fi
    if printf '%s' "$value" | grep -Eq "$pattern"; then
      printf -v "$var" '%s' "$value"
      return 0
    fi
    warn "$hint"
  done
}

# Спросить у Telegram: отвечает ли API и что это за чат.
tg() {
  curl -sS -m 15 "https://api.telegram.org/bot${BOT_TOKEN}/$1" 2>/dev/null || true
}

tg_ok()    { printf '%s' "$1" | grep -q '"ok":true'; }
tg_title() { printf '%s' "$1" | sed -n 's/.*"title":"\([^"]*\)".*/\1/p' | head -1; }
tg_error() { printf '%s' "$1" | sed -n 's/.*"description":"\([^"]*\)".*/\1/p' | head -1; }

cat <<'EOF'

  ┌──────────────────────────────────────────────────────────┐
  │  Установка бота регистрации                              │
  │                                                          │
  │  Сейчас будет 8 вопросов. Каждый ответ проверяется       │
  │  сразу — неверный ID не доедет до боевого запуска.       │
  │                                                          │
  │  Что держать под рукой:                                  │
  │    • токен от @BotFather                                 │
  │    • ID канала и чата модерации (бот уже добавлен туда)  │
  │    • свой tg_id (@userinfobot)                           │
  │    • ссылку на правила проката (необязательно)           │
  └──────────────────────────────────────────────────────────┘
EOF

# ─── 1. Токен ───────────────────────────────────────────────────────────────
bold "1/8. Токен бота"
BOT_TOKEN="$OLD_TOKEN"
while :; do
  ask BOT_TOKEN "Вставьте токен от @BotFather:" \
      '^[0-9]{6,}:[A-Za-z0-9_-]{30,}$' "формат: 123456789:AAH… — скопируйте строку целиком"
  ME="$(tg getMe)"
  if tg_ok "$ME"; then
    ok "бот @$(printf '%s' "$ME" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p' | head -1)"
    break
  fi
  if [ -z "$ME" ]; then
    warn "api.telegram.org не отвечает с этого сервера. Проверьте сеть и повторите."
  else
    warn "Telegram отверг токен: $(tg_error "$ME")"
  fi
  BOT_TOKEN=""
done

# ─── 2. Канал ───────────────────────────────────────────────────────────────
bold "2/8. Канал, подписку на который проверяет бот"
while :; do
  ask CHANNEL_ID "Числовой ID канала (начинается с -100):" \
      '^-100[0-9]{6,}$' "ID канала выглядит как -1001234567890"
  CHAT="$(tg "getChat?chat_id=${CHANNEL_ID}")"
  if tg_ok "$CHAT"; then
    ok "канал «$(tg_title "$CHAT")»"
    ADMINS_CHECK="$(tg "getChatAdministrators?chat_id=${CHANNEL_ID}")"
    if tg_ok "$ADMINS_CHECK" && printf '%s' "$ADMINS_CHECK" | grep -q '"is_bot":true'; then
      ok "бот в администраторах"
    else
      # Без прав администратора getChatMember не отдаст статус подписки,
      # и гейт развернёт вообще всех: «вы не подписаны» будет висеть вечно.
      warn "бот НЕ администратор канала — гейт подписки не заработает."
      warn "Добавьте его в администраторы и запустите install.sh заново."
    fi
    break
  fi
  warn "Telegram: $(tg_error "$CHAT"). Бот добавлен в канал?"
  CHANNEL_ID=""
done
ask CHANNEL_URL "Публичная ссылка на канал:" '^https://t\.me/.+' "нужна ссылка вида https://t.me/имя"

# ─── 3. Чат модерации ───────────────────────────────────────────────────────
bold "3/8. Чат модерации — куда падают заявки"
while :; do
  ask ADMIN_CHAT_ID "Числовой ID группы модерации:" \
      '^-[0-9]{6,}$' "ID группы отрицательный, вида -1009876543210"
  CHAT="$(tg "getChat?chat_id=${ADMIN_CHAT_ID}")"
  if tg_ok "$CHAT"; then ok "чат «$(tg_title "$CHAT")»"; break; fi
  warn "Telegram: $(tg_error "$CHAT"). Бот добавлен в группу?"
  ADMIN_CHAT_ID=""
done

# ─── 4. Кто утверждает ──────────────────────────────────────────────────────
bold "4/8. Кому разрешено жать «Одобрить»"
ask ADMINS "Ваш tg_id (несколько — через пробел):" \
    '^[0-9]+([ ,]+[0-9]+)*$' "только числа, например: 111111111 222222222"

# ─── 5. Личка для договоров ─────────────────────────────────────────────────
bold "5/8. Кому приходит договор на утверждение"
cat <<'EOF'
    Это личка @arenda_velo_kazan. Telegram не даёт боту написать первым,
    поэтому владелец аккаунта обязан один раз отправить боту /start.
    Пусто = договоры пойдут в чат модерации из пункта 3 (так тоже работает).
EOF
while :; do
  ask CONTRACT_CHAT_ID "Числовой id аккаунта (или Enter, чтобы пропустить):" \
      '^[0-9]+$' "нужен числовой id, а не @username" yes
  [ -z "$CONTRACT_CHAT_ID" ] && { warn "договоры пойдут в чат модерации"; break; }
  CHAT="$(tg "getChat?chat_id=${CONTRACT_CHAT_ID}")"
  if tg_ok "$CHAT"; then
    ok "аккаунт найден, диалог с ботом открыт"
    break
  fi
  # Именно этот случай ловится тут: пока человек не нажал /start,
  # getChat отвечает «chat not found», и отправка договора упала бы уже
  # после того, как пользователю сказано «заявка одобрена».
  warn "Telegram: $(tg_error "$CHAT")"
  warn "Скорее всего владелец ещё не отправил боту /start."
  CONTRACT_CHAT_ID=""
done

# ─── 6. Чат фиксации ────────────────────────────────────────────────────────
bold "6/8. Куда уходит подписанный договор"
cat <<'EOF'
    Обычно группа с темами. Пусто = в чат модерации из пункта 3.
EOF
while :; do
  ask FIX_CHAT_ID "Числовой ID группы фиксации (или Enter):" \
      '^-[0-9]{6,}$' "ID группы отрицательный" yes
  [ -z "$FIX_CHAT_ID" ] && break
  CHAT="$(tg "getChat?chat_id=${FIX_CHAT_ID}")"
  if tg_ok "$CHAT"; then ok "чат «$(tg_title "$CHAT")»"; break; fi
  warn "Telegram: $(tg_error "$CHAT"). Бот добавлен в группу?"
  FIX_CHAT_ID=""
done
ask FIX_TOPIC_ID "Номер темы «Фиксация сдачи» (Enter = общая лента):" \
    '^[1-9][0-9]*$' "номер темы — положительное число, ноль не подходит" yes

# ─── 7. Документы ───────────────────────────────────────────────────────────
# Оферты в боте больше нет - согласие на обработку ПДн он показывает сам.
# Ссылка на правила проката необязательна: если есть, на экране согласия
# появится кнопка.
bold "7/8. Документы и оплата"
ask OFERTA_URL "Ссылка на правила проката (Enter = пропустить):" \
    '^https?://.+' "нужна ссылка http(s)://… или пустая строка" yes
ask OFERTA_VERSION "Редакция согласия на обработку ПДн (дата):" \
    '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' "формат ГГГГ-ММ-ДД, например 2026-01-15"
# Ссылка на оплату по расчётному счёту. Значение по умолчанию - действующий
# счёт проката; при смене счёта достаточно переспросить этот вопрос.
: "${PAY_URL:=https://qr.nspk.ru/BS1A0050UAJCK8MS89CR3TLN80KF48DJ?type=01&bank=100000000284&crc=3DC0}"
ask PAY_URL "Ссылка на оплату по расчётному счёту (СБП QR):" \
    '^https?://.+' "нужна ссылка http(s)://…"

# ─── 8. Сроки хранения ──────────────────────────────────────────────────────
bold "8/8. Сроки хранения сканов"
cat <<'EOF'
    Эти числа печатаются в договоре и по ним реально удаляются файлы.
    Они обязаны совпадать с тем, что написано в вашей оферте.
EOF
ask PURGE_APPROVED_DAYS "Хранить после одобрения, дней:" '^[0-9]{1,4}$' "нужно число"
ask PURGE_REJECTED_DAYS "Хранить после отказа, дней:" '^[0-9]{1,4}$' "нужно число"

# ─── Запись .env ────────────────────────────────────────────────────────────
bold "Записываю .env"
umask 077
# Значения пишутся в кавычках. Это не украшение: bootstrap.sh читает .env
# через `. ./.env`, и строка ADMINS=111 222 без кавычек означает для shell
# «присвоить ADMINS=111, затем выполнить команду 222». Результат - «222:
# command not found», обрыв установки и потерянный второй администратор.
# Docker compose кавычки понимает и снимает их сам.
cat > .env <<EOF
# Создан install.sh $(date '+%Y-%m-%d %H:%M'). Секреты лежат в ./secrets/.
# Значения в кавычках: пробел внутри значения иначе ломает чтение файла.
POSTGRES_USER="mybike"
POSTGRES_DB="mybike"
TZ="${TZ_VALUE}"

CHANNEL_ID="${CHANNEL_ID}"
CHANNEL_URL="${CHANNEL_URL}"
ADMIN_CHAT_ID="${ADMIN_CHAT_ID}"
ADMINS="${ADMINS}"

CONTRACT_CHAT_ID="${CONTRACT_CHAT_ID}"
FIX_CHAT_ID="${FIX_CHAT_ID}"
FIX_TOPIC_ID="${FIX_TOPIC_ID}"

OFERTA_URL="${OFERTA_URL}"
OFERTA_VERSION="${OFERTA_VERSION}"
PDN_URL="${PDN_URL:-}"
PDN_VERSION="${PDN_VERSION:-}"
VIDEO_URL="${VIDEO_URL:-https://youtu.be/CyZzskq8o0o}"
PAY_URL="${PAY_URL}"

PURGE_APPROVED_DAYS="${PURGE_APPROVED_DAYS}"
PURGE_REJECTED_DAYS="${PURGE_REJECTED_DAYS}"
UPDATES_LOG_DAYS="${UPDATES_LOG_DAYS:-7}"
REMIND_BEFORE_DAYS="${REMIND_BEFORE_DAYS:-2}"
REMIND_HOUR_UTC="${REMIND_HOUR_UTC:-7}"
EOF
chmod 600 .env
ok ".env готов"

mkdir -p secrets && chmod 700 secrets
printf '%s' "$BOT_TOKEN" > secrets/bot_token
chmod 600 secrets/bot_token
ok "токен положен в secrets/bot_token"

# ─── Разворачивание ─────────────────────────────────────────────────────────
# Docker, ufw, генерация пароля БД и ключа анкеты, сборка и запуск - всё это
# уже умеет bootstrap.sh. Дублировать его здесь значило бы получить два места,
# которые надо править одинаково.
bold "Разворачиваю (Docker, файрвол, сборка образа) — 3–5 минут"
bash bootstrap.sh

# ─── Проверка ───────────────────────────────────────────────────────────────
bold "Проверяю, что бот поднялся"
READY=""
for _ in $(seq 1 30); do
  if docker compose logs bot 2>/dev/null | grep -q "готов"; then READY="yes"; break; fi
  sleep 2
done

if [ -n "$READY" ]; then
  ok "бот запущен"
else
  warn "в логе нет строки «готов» — посмотрите: docker compose logs bot"
fi

# Шаблон договора - docx юриста с подстановками. Проверяем не содержимое
# (docx бинарный, grep по нему не работает), а наличие: без файла бот
# запустится и упадёт только в момент выдачи первого договора.
if [ ! -s app/contract_template.docx ]; then
  printf '\n\033[33m  ВНИМАНИЕ: нет app/contract_template.docx.\033[0m\n'
  cat <<'EOF'
    Бот уже работает, но выдача договора упадёт: файл шаблона отсутствует
    или пуст. Верните app/contract_template.docx из архива установки
    и перезапустите: docker compose restart bot
EOF
fi

bold "Готово"
cat <<EOF
    Логи:      docker compose logs -f bot
    Рестарт:   docker compose restart bot
    Обновить:  docker compose up -d --build
    Настройки: nano .env  (после правки — рестарт)

    Отправьте боту /start и пройдите сценарий целиком:
    подписка → ФИО → оферта → контакт → анкета → фото документа
    → подтверждение → одобрение → договор → «Подписываю».
EOF
