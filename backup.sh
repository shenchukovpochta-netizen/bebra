#!/bin/sh
# Сервис backup: дамп базы раз в сутки, зашифрованная копия в облако S3 и
# раз в неделю проверка, что копия разворачивается в живую базу.
#
#   backup.sh              круг сервиса (так его запускает compose)
#   backup.sh dump         дамп сейчас
#   backup.sh upload       отправить последний дамп в облако сейчас
#   backup.sh check        проверить восстановление сейчас
#   backup.sh list         что лежит в облаке
#   backup.sh fetch [имя]  скачать и расшифровать копию из облака в /backups
#
# Итог каждого шага лежит в /backups/.state/*.json и одной строкой JSON
# уходит в crm.settings (ключ backup_status): бот раз в час читает его и
# пишет владельцу, панель показывает. Сам сервис в Telegram не ходит -
# токена бота у него нет и не должно быть.
#
# Образ - postgres:16-alpine, там busybox, а не bash: только POSIX sh
# плюс `local`, который понимают и busybox, и dash.

set -u

DIR="${BACKUP_DIR:-/backups}"
STATE="$DIR/.state"
TICK="${BACKUP_TICK:-600}"
RETRY=3600
KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"
DB="${POSTGRES_DB:-mybike}"
CHECK_DB="${DB}_restore_check"
KEY_FILE="${BACKUP_KEY_FILE:-/run/secrets/backup_key}"
S3_SECRET_FILE="${BACKUP_S3_SECRET_FILE:-/run/secrets/backup_s3_secret}"
PG_PASSWORD_FILE="${BACKUP_PG_PASSWORD_FILE:-/run/secrets/db_password}"
# Таблицы, по которым сверяется развёрнутая копия: без удаления по сроку,
# поэтому число строк в свежей копии и в живой базе почти одно. Пусты все
# пять - дамп снят с пустой базы (has_rows).
CHECK_TABLES="crm.clients crm.bikes crm.rentals crm.ledger crm.bike_status_log"
# Запас места сверх двух размеров базы для проверки восстановления (room).
CHECK_RESERVE_KB=$((512 * 1024))

PUSH_SQL="insert into crm.settings (key, value, updated_by)
values ('backup_status', :'status', 'backup')
on conflict (key) do update set value = excluded.value,
  updated_by = excluded.updated_by, updated_at = now();"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
file_iso() { date -u -r "$1" +%Y-%m-%dT%H:%M:%SZ; }

# Ошибка чужой программы - в JSON: печатная ASCII без кавычек и обратной
# косой, последние две строки без отметки времени rclone, не длиннее 300
# знаков. Резать по байтам русский текст нельзя: половина буквы - неверный
# UTF-8, и Postgres отказался бы записать весь отчёт.
clean() {
  printf '%s\n' "$1" | sed -e 's|^[0-9][0-9][0-9][0-9]/[0-9/]* [0-9:]* ||' \
      -e 's/^[A-Z]* *: //' \
    | grep -v '^[[:space:]]*$' | tail -n 2 | tr '\n\r\t' '   ' | tr -cd ' -~' | tr -s ' ' \
    | tr -d '"\\' | sed -e 's/^ *//' -e 's/ *$//' | cut -c1-300
}

jstr() { if [ -n "$1" ]; then printf '"%s"' "$1"; else printf null; fi; }

put() {
  mkdir -p "$STATE"
  printf '%s\n' "$2" > "$STATE/$1.json.tmp" && mv "$STATE/$1.json.tmp" "$STATE/$1.json"
}

# Строковое поле прошлого отчёта: при сбое «последний удачный» не теряется.
field() {
  sed -n "s/.*\"$2\":\"\([^\"]*\)\".*/\1/p" "$STATE/$1.json" 2>/dev/null | head -n 1
}

# report ЧАСТЬ ok|fail ТЕКСТ [поля JSON через запятую]
report() {
  local at last ok err
  at=$(now_iso)
  if [ "$2" = ok ]; then
    last=$at ok=true err=null
  else
    last=$(field "$1" last_ok) ok=false err=$(jstr "$3")
  fi
  put "$1" "{\"at\":\"$at\",\"ok\":$ok,\"error\":$err,\"last_ok\":$(jstr "$last")${4:+,$4}}"
}

part() { if [ -s "$STATE/$1.json" ]; then tr -d '\n' < "$STATE/$1.json"; else printf null; fi; }

# Отчёт в базу - только когда он поменялся. Сбой (база ещё без схемы на
# первом старте) не страшен: следующий круг отправит его снова.
push() {
  local status out
  status=$(printf '{"dump":%s,"offsite":%s,"restore":%s}' \
    "$(part dump)" "$(part offsite)" "$(part restore)")
  [ "$status" = "$(cat "$STATE/pushed" 2>/dev/null)" ] && return 0
  if out=$(printf '%s\n' "$PUSH_SQL" \
      | psql -X -q -v ON_ERROR_STOP=1 -v status="$status" -d "$DB" 2>&1); then
    printf '%s' "$status" > "$STATE/pushed"
  else
    log "отчёт в базу не записан: $out" >&2
    return 1
  fi
}

latest() { ls -1 "$DIR"/mybike-*.sql.gz 2>/dev/null | sort | tail -n 1; }

# ─── дамп ────────────────────────────────────────────────────────────────
dump() {
  local name out
  name="mybike-$(date +%F).sql.gz"
  rm -f "$DIR/$name.tmp"
  # Без трубы: код выхода должен быть у pg_dump, а не у gzip - иначе
  # оборванный дамп переименовывался бы в «удачный».
  if out=$(pg_dump -d "$DB" -Z6 -f "$DIR/$name.tmp" 2>&1); then
    mv "$DIR/$name.tmp" "$DIR/$name"
    log "бэкап: $DIR/$name"
    report dump ok "" "\"file\":\"$name\",\"size\":$(wc -c < "$DIR/$name" | tr -d ' ')"
  else
    rm -f "$DIR/$name.tmp"
    log "бэкап не удался: $out" >&2
    report dump fail "pg_dump: $(clean "$out")"
    return 1
  fi
  # -exec, а не -delete: -delete есть не в каждой сборке busybox.
  find "$DIR" -maxdepth 1 -name 'mybike-*.sql.gz' -mtime +"$KEEP_DAYS" -exec rm -f {} +
}

# ─── облако ──────────────────────────────────────────────────────────────
offsite_on() { [ -n "${BACKUP_S3_BUCKET:-}" ]; }

target() {
  local host=${BACKUP_S3_ENDPOINT:-}
  host=${host#*://}
  clean "${host%%/*}/${BACKUP_S3_BUCKET:-}${BACKUP_S3_PREFIX:+/$BACKUP_S3_PREFIX}"
}

# Облако настраивается окружением rclone, без файла конфига: ключи не
# лежат на диске контейнера. Шифрует rclone crypt (XSalsa20-Poly1305) до
# отправки - провайдер видит только шифр и имя файла с датой.
rclone_env() {
  local key secret
  key=$(tr -d '\r\n' < "$KEY_FILE" 2>/dev/null)
  secret=$(tr -d '\r\n' < "$S3_SECRET_FILE" 2>/dev/null)
  MISSING=""
  [ -n "${BACKUP_S3_ENDPOINT:-}" ] || MISSING="$MISSING BACKUP_S3_ENDPOINT"
  [ -n "${BACKUP_S3_ACCESS_KEY:-}" ] || MISSING="$MISSING BACKUP_S3_ACCESS_KEY"
  [ -n "$secret" ] || MISSING="$MISSING secrets/backup_s3_secret"
  # Без ключа шифрования в облако не уходит ничего: открытая копия базы
  # у чужого провайдера хуже, чем её отсутствие.
  [ -n "$key" ] || MISSING="$MISSING secrets/backup_key"
  if [ -n "$MISSING" ]; then
    MISSING="не заданы:$MISSING"
    return 1
  fi
  # RCLONE_CONFIG=/dev/null: файла конфига нет намеренно, и rclone не
  # должен писать об этом в лог на каждый вызов.
  export RCLONE_CONFIG=/dev/null RCLONE_CONFIG_S3_TYPE=s3 RCLONE_CONFIG_S3_PROVIDER=Other \
    RCLONE_CONFIG_S3_ENDPOINT="$BACKUP_S3_ENDPOINT" \
    RCLONE_CONFIG_S3_REGION="${BACKUP_S3_REGION:-}" \
    RCLONE_CONFIG_S3_ACCESS_KEY_ID="$BACKUP_S3_ACCESS_KEY" \
    RCLONE_CONFIG_S3_SECRET_ACCESS_KEY="$secret" \
    RCLONE_CONFIG_S3_NO_CHECK_BUCKET=true \
    RCLONE_CONFIG_OFFSITE_TYPE=crypt \
    RCLONE_CONFIG_OFFSITE_REMOTE="s3:$BACKUP_S3_BUCKET${BACKUP_S3_PREFIX:+/$BACKUP_S3_PREFIX}" \
    RCLONE_CONFIG_OFFSITE_FILENAME_ENCRYPTION=off \
    RCLONE_CONFIG_OFFSITE_DIRECTORY_NAME_ENCRYPTION=false
  # Ключ - через stdin: в аргументах его было бы видно в списке процессов.
  RCLONE_CONFIG_OFFSITE_PASSWORD=$(printf '%s' "$key" | rclone obscure -) || {
    MISSING="rclone не принял ключ"
    return 1
  }
  export RCLONE_CONFIG_OFFSITE_PASSWORD
}

# Есть ли в дампе хоть одна строка таблиц CHECK_TABLES. Данные в дампе
# pg_dump - блоки «COPY схема.таблица (...) FROM stdin;», строки, «\.».
# Первая же строка - ответ: дальше gunzip не читает, и большой дамп не
# разжимается целиком.
has_rows() {
  [ "$(gunzip -c "$1" 2>/dev/null | awk -v want=" $CHECK_TABLES " '
    copy {
      if ($0 == "\\.") copy = 0
      else if (hit) { print "yes"; exit }
      next
    }
    /^COPY .* FROM stdin;$/ { copy = 1; hit = index(want, " " $2 " ") }')" = yes ]
}

upload() {
  local file=$1 name where out prune=""
  name=$(basename "$file")
  where="\"enabled\":true,\"target\":$(jstr "$(target)")"
  if ! rclone_env; then
    report offsite fail "$MISSING" "$where"
    return 1
  fi
  # Новый сервер: пустой дамп не должен лечь в облако «последней копией»
  # поверх настоящих - её бы и скачали при восстановлении. Судится сам
  # файл, а не живая база: после восстановления база уже полна, а
  # последним на диске лежит дамп, снятый до него. И не по одним
  # клиентам: у новой точки парк заведён раньше первого клиента.
  if ! has_rows "$file" \
      && [ -n "$(rclone lsf -q --files-only offsite:daily/ 2>/dev/null)" ]; then
    report offsite fail "дамп $name снят с пустой базы, а в облаке есть копии: не выгружаю его поверх них. Базу восстановили - снимите дамп заново: backup.sh dump (INSTALL.md). Новая установка - копия уйдёт, как только появится парк" "$where"
    return 1
  fi
  if ! out=$(rclone copyto -q "$file" "offsite:daily/$name" 2>&1); then
    report offsite fail "выгрузка: $(clean "$out")" "$where"
    return 1
  fi
  # Недельная - тот же файл вторым заходом, если за шесть дней её не
  # было: серверное копирование S3-совместимые хранилища понимают
  # по-разному, а лишний дамп раз в неделю ничего не стоит.
  if [ -z "$(rclone lsf -q --files-only --use-server-modtime --max-age 6d offsite:weekly/ 2>/dev/null)" ]; then
    if ! out=$(rclone copyto -q "$file" "offsite:weekly/$name" 2>&1); then
      report offsite fail "недельная копия: $(clean "$out")" "$where"
      return 1
    fi
  fi
  printf '%s' "$name" > "$STATE/uploaded"
  log "копия в облаке: $name"
  # Старые копии удаляются по дате загрузки. 0 - не удалять вовсе: так
  # работает ключ без права удаления, а срок держит правило бакета.
  if [ "${BACKUP_S3_KEEP_DAYS:-14}" -gt 0 ] 2>/dev/null; then
    out=$(rclone delete -q --use-server-modtime --min-age "${BACKUP_S3_KEEP_DAYS:-14}d" \
      offsite:daily/ 2>&1) || prune=$(clean "$out")
    if [ -z "$prune" ] && [ "${BACKUP_S3_KEEP_WEEKS:-8}" -gt 0 ] 2>/dev/null; then
      out=$(rclone delete -q --use-server-modtime --min-age "$((${BACKUP_S3_KEEP_WEEKS:-8} * 7))d" \
        offsite:weekly/ 2>&1) || prune=$(clean "$out")
    fi
  fi
  [ -z "$prune" ] || log "старые копии в облаке не удалены: $prune" >&2
  report offsite ok "" "$where,\"file\":\"$name\",\"prune_error\":$(jstr "$prune")"
}

# ─── проверка восстановления ─────────────────────────────────────────────
count() {
  local n
  n=$(psql -X -At -d "$1" -c "select count(*) from $2" 2>/dev/null) || n=-1
  case $n in ''|*[!0-9]*) n=-1 ;; esac
  printf '%s' "$n"
}

# near В_БАЗЕ В_КОПИИ. Копия свежая (минуты после дампа), но проверку
# запускают и руками днём: расхождение до 5 % или до 10 строк - норма.
# Пустая таблица в копии при непустой в базе - нет. Таблицы нет ни там,
# ни там - база ещё без схемы. Нет в копии, а в базе пустая - тоже норма:
# первый круг нового сервера снял дамп до schema.sql, а бот применил её,
# пока шла проверка. Терять было нечего.
near() {
  local diff
  [ "$1" = -1 ] && [ "$2" = -1 ] && return 0
  [ "$1" = 0 ] && [ "$2" = -1 ] && return 0
  [ "$1" = -1 ] || [ "$2" = -1 ] && return 1
  [ "$1" -gt 0 ] && [ "$2" -eq 0 ] && return 1
  diff=$(($1 - $2))
  [ "$diff" -lt 0 ] && diff=$((-diff))
  [ "$diff" -le 10 ] || [ $((diff * 100)) -le $(($1 * 5)) ]
}

check_fail() {
  dropdb --if-exists "$CHECK_DB" >/dev/null 2>&1
  rm -f "$STATE/check.sql.gz"
  log "проверка восстановления не прошла: $1" >&2
  report restore fail "$1" "$2"
  return 1
}

# Копия разворачивается в тот же кластер: данные и WAL ложатся на диск
# боевой базы. Кончится место - Postgres встанет посреди ночи, а с ним
# бот и панель, поэтому без места проверки нет. Нужно две базы (данные и
# WAL) и запас. Мерится том /backups: на сервере это тот же диск, что у
# docker. Не узнали размер или место - не мешаем: проверка скажет сама.
room() {
  local size free need
  size=$(psql -X -At -d "$DB" -c "select pg_database_size(current_database())" 2>/dev/null)
  free=$(df -Pk "$DIR" 2>/dev/null | awk 'NR == 2 { print $4 }')
  case $size in ''|*[!0-9]*) return 0 ;; esac
  case $free in ''|*[!0-9]*) return 0 ;; esac
  need=$((size / 1024 * 2 + CHECK_RESERVE_KB))
  [ "$free" -ge "$need" ] && return 0
  ROOM="проверка не запускалась: свободно $((free / 1024)) МБ, а развернуть копию рядом с боевой базой нужно около $((need / 1024)) МБ - забитый диск уронил бы Postgres. Освободите место на диске"
  return 1
}

# Из облака, если оно включено: так проверяется вся цепочка - ключ,
# целость шифра и сам дамп. Без облака - последний дамп на диске.
check() {
  local src=local name file where out tables="" bad="" t got live
  mkdir -p "$STATE"
  rm -f "$STATE/check.sql.gz"
  offsite_on && src=offsite
  room || { check_fail "$ROOM" "\"source\":\"$src\""; return 1; }
  if [ "$src" = offsite ]; then
    rclone_env || { check_fail "$MISSING" '"source":"offsite"'; return 1; }
    name=$(rclone lsf -q --files-only offsite:daily/ 2>/dev/null \
      | grep '^mybike-.*\.sql\.gz$' | sort | tail -n 1)
    [ -n "$name" ] || { check_fail "в облаке нет ни одной копии" '"source":"offsite"'; return 1; }
    file="$STATE/check.sql.gz"
    if ! out=$(rclone copyto -q "offsite:daily/$name" "$file" 2>&1); then
      check_fail "скачать $name: $(clean "$out")" "\"source\":\"offsite\",\"file\":\"$name\""
      return 1
    fi
  else
    file=$(latest)
    [ -n "$file" ] || { check_fail "дампов на диске нет" '"source":"local"'; return 1; }
    name=$(basename "$file")
  fi
  where="\"source\":\"$src\",\"file\":\"$name\""
  out=$(gunzip -t "$file" 2>&1) || { check_fail "архив повреждён: $(clean "$out")" "$where"; return 1; }
  dropdb --if-exists "$CHECK_DB" >/dev/null 2>&1
  out=$(createdb -T template0 "$CHECK_DB" 2>&1) \
    || { check_fail "createdb: $(clean "$out")" "$where"; return 1; }
  # gunzip -t уже прошёл, так что код трубы - это код psql.
  out=$(gunzip -c "$file" | psql -X -q -v ON_ERROR_STOP=1 -d "$CHECK_DB" -o /dev/null 2>&1) \
    || { check_fail "psql: $(clean "$out")" "$where"; return 1; }
  for t in $CHECK_TABLES; do
    got=$(count "$CHECK_DB" "$t")
    live=$(count "$DB" "$t")
    tables="$tables${tables:+,}\"$t\":[$got,$live]"
    near "$live" "$got" || bad="$bad${bad:+; }$t: в копии $got, в базе $live"
  done
  dropdb --if-exists "$CHECK_DB" >/dev/null 2>&1
  rm -f "$STATE/check.sql.gz"
  if [ -n "$bad" ]; then
    log "проверка восстановления: числа не сходятся: $bad" >&2
    report restore fail "не сходится: $bad" "$where,\"tables\":{$tables}"
    return 1
  fi
  log "проверка восстановления прошла: $name ($src)"
  report restore ok "" "$where,\"tables\":{$tables}"
}

# Следующая проверка: через неделю после удачной, через сутки после
# неудачной. Час вычтен, чтобы она попала на ночной дамп, а не за ним.
check_scheduled() {
  local rc next
  check
  rc=$?
  if [ "$rc" -eq 0 ]; then next=$((7 * 86400 - 3600)); else next=$((86400 - 3600)); fi
  echo $(($(date +%s) + next)) > "$STATE/restore_next"
  return "$rc"
}

# ─── круг ────────────────────────────────────────────────────────────────
start() {
  local last t
  mkdir -p "$STATE"
  # Недописанный дамп прошлого запуска. Только свой и только на старте:
  # в ./backups пишет и update.sh (pre-update-*.tmp), и чистка на каждом
  # круге обрывала бы его дамп посреди обновления.
  rm -f "$DIR"/mybike-*.sql.gz.tmp
  # Прежний сервис отчётов не писал: последний дамп на диске и есть
  # последний удачный. Иначе бот сутки считал бы, что бэкапа нет.
  last=$(latest)
  if [ ! -s "$STATE/dump.json" ] && [ -n "$last" ]; then
    t=$(file_iso "$last")
    put dump "{\"at\":\"$t\",\"ok\":true,\"error\":null,\"last_ok\":\"$t\",\"file\":\"$(basename "$last")\",\"size\":$(wc -c < "$last" | tr -d ' ')}"
  fi
  if offsite_on; then
    grep -q '"enabled":true' "$STATE/offsite.json" 2>/dev/null \
      || put offsite "{\"enabled\":true,\"target\":$(jstr "$(target)")}"
  else
    put offsite '{"enabled":false}'
  fi
}

NEXT_DUMP=0
NEXT_UPLOAD=0

tick() {
  local now dumped="" last
  now=$(date +%s)
  if [ ! -f "$DIR/mybike-$(date +%F).sql.gz" ] && [ "$now" -ge "$NEXT_DUMP" ]; then
    if dump; then dumped=yes; else NEXT_DUMP=$((now + RETRY)); fi
  fi
  last=$(latest)
  if offsite_on && [ -n "$last" ] && [ "$now" -ge "$NEXT_UPLOAD" ] \
      && [ "$(cat "$STATE/uploaded" 2>/dev/null)" != "$(basename "$last")" ]; then
    upload "$last" || NEXT_UPLOAD=$((now + RETRY))
  fi
  # Проверка - сразу за ночным дампом, когда подошёл срок; самая первая -
  # сразу, чтобы владелец узнал о рабочей копии в день установки.
  if [ -n "$last" ]; then
    if [ ! -f "$STATE/restore_next" ] || { [ -n "$dumped" ] \
        && [ "$now" -ge "$(cat "$STATE/restore_next" 2>/dev/null || echo 0)" ]; }; then
      check_scheduled
    fi
  fi
  push
}

# Ручной запуск через `docker compose exec` не должен столкнуться с кругом.
locked() {
  local rc
  mkdir -p "$STATE"
  exec 9>"$STATE/lock"
  # Без flock (урезанный busybox) - без замка: он страхует ручной запуск,
  # а не круг, и останавливать из-за него бэкап нельзя.
  if command -v flock >/dev/null 2>&1; then flock 9 2>/dev/null || true; fi
  "$@"
  rc=$?
  exec 9>&-
  return "$rc"
}

loop() {
  # sleep ждёт в фоне: так SIGTERM от `docker compose stop` доходит сразу,
  # а не через десять минут. И гасится вместе со сценарием.
  trap 'kill $! 2>/dev/null; exit 0' TERM INT
  locked start
  while :; do
    locked tick
    sleep "$TICK" &
    wait $!
  done
}

fetch() {
  local name=${1:-}
  rclone_env || { echo "$MISSING" >&2; return 1; }
  if [ -z "$name" ]; then
    name=$(rclone lsf -q --files-only offsite:daily/ | grep '^mybike-.*\.sql\.gz$' | sort | tail -n 1)
  fi
  [ -n "$name" ] || { echo "в облаке нет ни одной копии" >&2; return 1; }
  case $name in */*) ;; *) name="daily/$name" ;; esac
  rclone copyto "offsite:$name" "$DIR/$(basename "$name")" || return 1
  echo "$DIR/$(basename "$name")"
}

PGPASSWORD=$(tr -d '\r\n' < "$PG_PASSWORD_FILE" 2>/dev/null)
PGHOST="${PGHOST:-postgres}"
PGUSER="${POSTGRES_USER:-mybike}"
export PGPASSWORD PGHOST PGUSER

case "${1:-loop}" in
  loop) loop ;;
  dump) locked dump; rc=$?; push; exit "$rc" ;;
  upload)
    offsite_on || { echo "облако выключено: BACKUP_S3_BUCKET пуст" >&2; exit 1; }
    last=$(latest)
    [ -n "$last" ] || { echo "дампов на диске нет" >&2; exit 1; }
    locked upload "$last"; rc=$?; push; exit "$rc" ;;
  check) locked check_scheduled; rc=$?; push; exit "$rc" ;;
  list)
    rclone_env || { echo "$MISSING" >&2; exit 1; }
    for d in daily weekly; do echo "== $d"; rclone lsl "offsite:$d/"; done ;;
  fetch) fetch "${2:-}" ;;
  *) sed -n '2,11p' "$0" >&2; exit 2 ;;
esac
