#!/usr/bin/env bash
# Обновление установленного проекта одной командой. От root, из каталога
# проекта на сервере:
#   bash update.sh /root/mybike-bot.zip
# Порядок: дамп базы и прежний код в backups/ -> архив во временный
# каталог -> код поверх (без .env, secrets/, ваших docx и бэкапов) ->
# bootstrap.sh (новые секреты, сборка, запуск) -> проверка. Не снялся
# дамп - код не трогаем: обновлять без отката нельзя.
set -euo pipefail

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mОШИБКА: %s\033[0m\n' "$*" >&2; exit 1; }

# Путь к архиву - от каталога, откуда запустили, поэтому до cd в проект:
# иначе «bash /opt/mybike-bot/update.sh mybike-bot.zip» из /root искал бы
# архив в /opt/mybike-bot.
ZIP="${1:-}"
if [ -z "$ZIP" ]; then die "укажите архив: bash update.sh /root/mybike-bot.zip"; fi
if [ ! -f "$ZIP" ]; then die "нет файла $ZIP"; fi
ZIP="$(cd "$(dirname "$ZIP")" && pwd)/$(basename "$ZIP")"
cd "$(dirname "$0")"
if [ "$(id -u)" -ne 0 ]; then die "запускать от root"; fi
# Обновляют установленное: без .env это первая установка, и ей нужен
# install.sh с вопросами, а не код поверх пустоты.
if [ ! -f .env ] || [ ! -f docker-compose.yml ]; then
  die "здесь нет установленного проекта (.env, docker-compose.yml). Первая установка - bash install.sh"
fi

set -a; . ./.env; set +a
DB_USER="${POSTGRES_USER:-mybike}"
DB_NAME="${POSTGRES_DB:-mybike}"
STAMP="$(date +%F-%H%M%S)"
DUMP="backups/pre-update-$STAMP.sql.gz"
CODE="backups/pre-update-$STAMP-code.tar.gz"

rollback() {
  cat <<EOF

    Откат к моменту перед обновлением - база из дампа, затем прежний код:
      docker compose stop bot crm
      docker compose exec -T postgres psql -U $DB_USER -d $DB_NAME -v ON_ERROR_STOP=1 \\
        -c 'drop schema if exists crm cascade; drop schema if exists bot cascade'
      gunzip -c $DUMP | docker compose exec -T postgres psql -U $DB_USER -d $DB_NAME -v ON_ERROR_STOP=1
      tar -xzf $CODE
      bash bootstrap.sh
    Схемы сносятся до заливки: дамп создаёт их заново, а поверх живых
    таблиц он упал бы на первом же «уже существует». Код возвращается
    до запуска: новый при старте снова применил бы свою схему к базе.
EOF
}

# ─── 1. Дамп ─────────────────────────────────────────────────────────────────
say "дамп базы: $DUMP"
mkdir -p backups
# pipefail: код выхода - у pg_dump, а не у gzip; оборванный дамп не станет
# «удачным» файлом. В конце plain-дампа - «dump complete»: по ней видно,
# что pg_dump дошёл до конца, а не умер на середине. Хвост - с запасом:
# с 16.10 после неё идёт «\unrestrict <ключ>», и последней она не бывает.
if ! docker compose exec -T postgres pg_dump -U "$DB_USER" -d "$DB_NAME" \
    | gzip > "$DUMP.tmp"; then
  rm -f "$DUMP.tmp"
  die "дамп не снялся - обновление не начато, код не тронут. Postgres запущен? docker compose ps"
fi
TAIL="$(gzip -cd "$DUMP.tmp" | tail -n 20 || true)"
case "$TAIL" in
  *"PostgreSQL database dump complete"*) mv "$DUMP.tmp" "$DUMP" ;;
  *) rm -f "$DUMP.tmp"; die "дамп оборван - обновление не начато, код не тронут" ;;
esac
printf '    %s, %s\n' "$DUMP" "$(du -h "$DUMP" | cut -f1)"

# Прежний код - рядом с дампом: архив прошлой версии к откату обычно уже
# перезаписан новым (scp кладёт его под тем же именем), а дамп без кода
# ничего не откатывает. Без .env, секретов, docx и бэкапов: обновление
# их не трогает, а секретам в каталоге бэкапов не место.
say "прежний код: $CODE"
if ! tar -czf "$CODE.tmp" --exclude=./.env --exclude=./secrets --exclude=./backups \
    --exclude='./app/*.docx' .; then
  rm -f "$CODE.tmp"
  die "прежний код не сохранился - обновление не начато, код не тронут"
fi
mv "$CODE.tmp" "$CODE"
# Дальше любой сбой - уже с дампом в руках: пусть человек сразу видит откат.
trap 'printf "\n\033[31mОбновление прервано.\033[0m\n" >&2; rollback' ERR

# ─── 2. Архив ────────────────────────────────────────────────────────────────
for tool in unzip rsync; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    say "ставлю $tool"
    apt-get install -y -qq "$tool" >/dev/null
  fi
done
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
say "распаковываю $ZIP во временный каталог"
unzip -q "$ZIP" -d "$TMP" || die "архив не распаковался - код не тронут"
# Внутри архива проект лежит в папке (mybike-bot/); ищем её по файлам, а
# не по имени: переименованный архив не должен ломать обновление.
FOUND="$(find "$TMP" -maxdepth 3 -name docker-compose.yml -print -quit)"
if [ -z "$FOUND" ]; then die "в архиве нет docker-compose.yml - это не архив проекта"; fi
SRC="$(dirname "$FOUND")"
for f in bootstrap.sh schema.sql app/web/app.py; do
  if [ ! -f "$SRC/$f" ]; then die "в архиве нет $f - это не архив проекта, код не тронут"; fi
done

# ─── 3. Код ──────────────────────────────────────────────────────────────────
say "переношу код: .env, secrets/, ваши docx и backups/ остаются"
# rsync пишет новый файл рядом и переименовывает: этот скрипт bash читает
# по ходу, и старая копия остаётся открытой до конца - подмена не рвёт его.
# --checksum, а не размер и время: у архива с одинаковым временем файлов
# правка без смены размера иначе молча не доехала бы.
rsync -a --checksum --exclude='/.env' --exclude='/secrets/' --exclude='/app/*.docx' \
  --exclude='/backups/' "$SRC/" ./
# Документ, которого на сервере ещё нет (новый вид в новой версии),
# доезжает: без файла бот не поднимется. Уже лежащий - ваш, его не трогаем.
rsync -a --ignore-existing --include='*.docx' --exclude='*' "$SRC/app/" ./app/
# CRLF из архива, собранного на Windows, ломает shebang и .env.
sed -i 's/\r$//' bootstrap.sh install.sh update.sh .env.example schema.sql
chmod +x bootstrap.sh install.sh update.sh

# ─── 4. Сборка и запуск ──────────────────────────────────────────────────────
say "bootstrap.sh: недостающие секреты, сборка, запуск"
if ! bash bootstrap.sh; then
  printf '\n\033[31mbootstrap.sh не прошёл.\033[0m Логи: docker compose logs --tail 50\n' >&2
  rollback
  exit 1
fi

# ─── 5. Проверка ─────────────────────────────────────────────────────────────
say "состояние контейнеров"
docker compose ps
HOST="${CRM_BIND:-127.0.0.1}"
if [ "$HOST" = "0.0.0.0" ]; then HOST=127.0.0.1; fi
URL="http://$HOST:${CRM_PORT:-8080}/healthz"
say "панель: $URL"
# Панель применяет схему при старте - первые секунды она не отвечает.
ok=""
for _ in $(seq 1 30); do
  if curl -fsS -m 5 "$URL" >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
if [ -z "$ok" ]; then
  printf '\033[31m    панель не отвечает: docker compose logs --tail 50 crm\033[0m\n' >&2
  rollback
  exit 1
fi
printf '    панель отвечает\n'

say "готово"
rollback
