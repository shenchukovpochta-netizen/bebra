# CODE.md — инструкция по коду МАЙБАЙК

Этот файл отвечает на вопрос «как устроен код и куда класть правку».
Он не повторяет CRM.md: там написано, что система делает для проката,
здесь — как она это делает внутри и по каким правилам её менять.
Бизнес-договорённости и красные линии — в CLAUDE.md, они главнее.

Разделы идут от общего к частному: сперва карта и слои, потом путь
клиента через код, база и деньги, потом панель, фон и внешние сервисы,
в конце — тесты и порядок внесения правок.

Номеров строк в ссылках нет намеренно: файл живёт, строка съезжает от
первой же правки, и через неделю указатель врёт. Ищите по имени функции.

| Раздел | О чём |
|---|---|
| [Карта проекта](#карта-проекта) | три процесса, один образ, тома и секреты, дерево модулей |
| [Слои и правила](#слои-и-правила) | чистая логика, доступ к базе, сервис, транспорт; что считается нарушением |
| [Путь клиента через код](#путь-клиента-через-код) | от `/start` до закрытия аренды: какая функция что записала |
| [База данных](#база-данных) | две схемы, идемпотентный `schema.sql`, инварианты индексами, белые списки колонок |
| [Деньги в коде](#деньги-в-коде) | журнал без колонки баланса, виды записей, «оплачено до», идемпотентность начислений |
| [Веб-панель](#веб-панель) | один файл маршрутов, права одним стражем, инструменты списков, новый раздел по шагам |
| [Фоновые процессы и расписание](#фоновые-процессы-и-расписание) | шесть циклов, дневной проход, центр уведомлений |
| [Внешние сервисы](#внешние-сервисы) | StarLine, Точка, tesseract, MAX, ПДн, docx |
| [Тесты и проверки](#тесты-и-проверки) | заглушка базы, живой Postgres, `ruff`, `consistency.py` |
| [Как вносить правки](#как-вносить-правки) | порядок шагов, добавить поле, добавить экран, чего не делать |

## Карта проекта

Один образ, три роли. `Dockerfile` собирает единственный образ (python:3.12-slim плюс
tesseract), а роль задаётся командой: `python -m app.main` для Telegram-бота, `python -m
app.web` для панели, `python -m app.max_main` для бота в MAX. Поэтому правка общего модуля
задевает все три процесса сразу, а пересборка нужна одна.

### Контейнеры

| Сервис | Что делает | Как поднимается |
|---|---|---|
| `postgres` | `postgres:16-alpine`, единственный источник истины, схемы `bot` и `crm` | всегда, healthcheck `pg_isready` |
| `bot` | диспетчер aiogram на long polling плюс шесть фоновых циклов | всегда, `stop_grace_period: 30s` |
| `crm` | веб-панель FastAPI под uvicorn, порт 8080 внутри | всегда, наружу `${CRM_BIND:-127.0.0.1}:${CRM_PORT:-8080}` |
| `backup` | `pg_dump -Z6` раз в сутки в `./backups`, чистка старше `BACKUP_KEEP_DAYS` | всегда |
| `caddy` | HTTPS для панели по домену, реверс на `crm:8080` | профиль `https` |
| `bot-max` | зеркало сценария в мессенджере MAX, своя база `mybike_max` | профиль `max` |

Панель слушает `127.0.0.1` сервера: без профиля `https` вход только через SSH-туннель. Дамп
в `backup` пишется во временный файл и переименовывается по коду возврата `pg_dump`, без
трубы в `gzip`: иначе оборванный дамп стал бы «удачным».

### Что происходит внутри процесса бота

`app/main.py` собирает всё в одной корутине `run()`:

1. Читает `Config.load()`, проверяет шаблоны договора и согласия (`load_template`) и формат
   `PAY_URL`. Проверки на старте, а не при выдаче: опечатка в пути иначе всплыла бы после
   слов «заявка одобрена».
2. Подключает базу и применяет `schema.sql` через `db.apply_schema`.
3. Вешает `PipelineMiddleware` на `dp.update` (клейм апдейта, загрузка пользователя,
   рейт-лимит, гейт подписки) и включает роутеры строго по порядку: `cabinet`, `staff`,
   `fleet`, `moderation`, `contract`, `registration`, `faq`, `menu`. Порядок несущий, в
   `app/main.py` он прокомментирован построчно. `menu` последний: у него ловушка на любое
   сообщение.
4. Запускает семь задач `asyncio.create_task`.

| Задача | Модуль | Период |
|---|---|---|
| ретеншен ПДн и чистка журнала апдейтов | `tasks.retention_loop` | `INTERVAL_SECONDS`, 6 часов |
| напоминания о сроке, акты выкупа, сводка, дневной проход CRM | `tasks.reminders_loop`, внутри зовёт `crm.billing.run_daily` | круг цикла, а что делать решает расписание уведомлений |
| опрос трекеров StarLine и тревоги | `crm.tracking.tracking_loop` | `STARLINE_POLL_SECONDS`, по умолчанию 300 |
| выписка Точки в `crm.bank_txns` | `crm.banking.banking_loop` | `TOCHKA_POLL_SECONDS`, по умолчанию 1800 |
| статусы счетов эквайринга и автосписание | `crm.paying.paying_loop` | минута |
| отправка кампаний в Telegram и MAX | `crm.mailing.mailing_loop` | свой круг |
| «Входящие»: ответы из панели в Telegram, MAX и Авито, сигналы о новых, опрос чатов Авито | `crm.inbox.inbox_loop` | 15 секунд, Авито — `AVITO_POLL_SECONDS` |

Все фоновые опросы живут здесь, а не в панели: веб-процессов может быть несколько, и каждый
спрашивал бы банк об одном и том же. Правило «панель в интернет не ходит» — про фоновые
опросы: один запрос при клиенте она делает, когда оператор жмёт «Выставить счёт»
(`acquiring()` в `app/web/app.py`). `SIGTERM` не убивает процесс сразу: обработчик зовёт
`dp.stop_polling()`, а `finally` отменяет задачи и ждёт `tasks.drain()`.

### Что происходит внутри панели

`app/web/__main__.py`: конфиг, база, `apply_schema`, `CrmDB`, `ensure_admin` (создаёт
первого админа и пишет пароль в лог), опционально `Bot` только для уведомлений клиентам. Без
токена панель работает, просто молчит.

`app/web/app.py` это около двухсот маршрутов, разделённых комментариями по разделам:
дашборд, клиенты, парк, аренды, тарифы, отчёты, сервис, справочники, батареи, ПЭП, рассылки,
касса, трекеры, склад, пересчёт, импорт. Обвязки две: `SessionMiddleware` с cookie
`crm_session` снаружи и проверка входа внутри, порядок важен и подписан в коде. Страница без
входа должна попасть в кортеж `PUBLIC = ("/login", "/static", "/healthz", "/sign/", "/hook/")`;
хук `/hook/inbox` проверяет свой токен сам, без сессии. Формы
без JS-фреймворка: страница это шаблон, действие это POST и редирект.

`forwarded_allow_ips` открывается только когда задан `CRM_DOMAIN`: Caddy приходит из сети
compose, а uvicorn по умолчанию верит заголовкам лишь от `127.0.0.1`. Доверяем при этом
не всем, а сетям прокси (`CRM_TRUSTED_PROXIES`): uvicorn берёт ПЕРВЫЙ адрес из
`X-Forwarded-For`, и клиент, приславший свой заголовок, подставил бы в протокол подписи
выдуманный адрес. Тот же `CRM_DOMAIN` включает флаг `Secure` у cookie сессии - в профиле
https Caddy слушает и 80, и 443, и адрес без схемы отправил бы её открытым текстом.
На вход через SSH-туннель это не влияет: `http://localhost` браузеры считают
доверенным источником и `Secure`-cookie оттуда принимают.

### Как три процесса делят одну базу

Схему применяют и бот, и панель, и MAX-бот. Скрипт идемпотентен, но `if not exists` не
спасает от гонки на пустой базе, поэтому `apply_schema` берёт `pg_advisory_xact_lock(7331)`
и выстраивает стартующих в очередь. Внутри процесса бота `CrmDB(db.pool)` садится на тот же
пул asyncpg: кабинет клиента и синхронизация `bot -> crm` работают без второго подключения.
MAX-бот исключение: у него своя база `mybike_max` (идентификаторы двух мессенджеров в общей
таблице перепутали бы людей), а в основную он ходит отдельным соединением только чтобы
проставить `max_id` в карточке клиента. Там же лежит `MID_MIGRATION`: в MAX идентификатор
сообщения строка, и колонки карточек расширяются до `text`.

### Тома и секреты

| Том | Кто пишет | Кто читает |
|---|---|---|
| `pgdata` | postgres | postgres |
| `kycfiles` | `bot`, `bot-max` | `crm` смонтирован `:ro`, потому что там ПДн |
| `bikefiles` | `crm` | `crm` |
| `doctemplates` | `crm` | `bot` смонтирован `:ro` |
| `caddydata` | caddy | caddy |

Шаблоны `app/*.docx` попадают в образ вместе с каталогом (`COPY app ./app`), но том
`doctemplates` кладётся поверх: свой шаблон владелец загружает в панели, и пересборка для
этого не нужна.

Секретов двенадцать, все файлами в `secrets/`: `db_password`, `bot_token`, `pdn_key`,
`crm_secret`, `crm_admin_password`, `max_bot_token`, `starline_app_secret`,
`starline_password`, `tochka_token`, `avito_client_secret`, `inbox_key`,
`inbox_hook_token`. Содержимое окружения видно в `docker inspect` и в
трейсбеках, поэтому в переменной лежит путь, а не значение: `_secret()` в `app/config.py`
читает суффикс `*_FILE`. `bootstrap.sh` генерирует `db_password`, `pdn_key`, `crm_secret`,
`crm_admin_password` и `inbox_key`, требует `bot_token` руками и создаёт пустыми остальные: пустой файл
означает «интеграции нет», и цикл просто не запускается. `_env()` считает пустую строку
отсутствием значения, потому что compose подставляет `""` для любой незаданной `${VAR}`.

### Дерево модулей

| Файл | Строка про него |
|---|---|
| `app/main.py` | точка входа бота: конфиг, роутеры, шесть фоновых задач, остановка |
| `app/max_main.py` | точка входа MAX: свой конфиг из `MAX_*`, создание базы, мост в CRM |
| `app/web/__main__.py` | точка входа панели: uvicorn, первый админ |
| `app/web/app.py` | все маршруты и формы панели |
| `app/web/config.py`, `app/config.py` | конфигурация панели и бота, секреты через файлы |
| `app/db.py` | доступ к схеме `bot`, тонкий слой над asyncpg, без ORM |
| `app/crm/db.py` | доступ к схеме `crm`, самый большой модуль слоя, возвращает dict и Decimal |
| `app/logic.py` | чистая логика бота: валидация, шаги анкеты, реквизиты договора |
| `app/crm/logic.py` | чистая логика CRM: деньги, периоды, простой, амортизация, списки, каталоги `NOTICES` и `TEMPLATE_FIELDS` |
| `app/crm/service.py` | операции CRM, общие для панели и бота: одна реализация на оба входа |
| `app/crm/notify.py` | уведомления клиенту в Telegram, ошибка доставки это предупреждение, не исключение |
| `app/crm/sync.py` | односторонняя синхронизация `bot -> crm` по событиям документов |
| `app/crm/billing.py` | дневной проход: начисления, напоминания, сводка |
| `app/crm/notices.py` | ворота уведомлений: включено ли, пора ли, что из этого вышло |
| `app/crm/banking.py`, `paying.py`, `tracking.py`, `mailing.py` | фоновые циклы выписки, счетов, трекеров, рассылок |
| `app/crm/company.py`, `doctemplates.py`, `esign.py` | реквизиты снимком, выбор шаблона документа, текст соглашения об ЭП |
| `app/crm/import_xlsx.py` | импорт рабочей таблицы «ДЕЙСТВУЮЩИЕ АРЕНДАТОРЫ» |
| `app/crm/opsgroup.py` | рабочая группа точек: сверка форм из тем с базой, ответы про долг и трекер (бывший n8n) |
| `app/crm/inbox.py` | «Входящие»: запись обращений из ботов, отправка ответов из очереди, опрос чатов Авито |
| `app/handlers/*.py` | сценарий Telegram: кабинет, договор, вопросы, парк из чата, меню, модерация, регистрация, привязка сотрудника, рабочая группа точек (`ops.py`, подключается первым) |
| `app/middlewares.py`, `app/filters.py` | конвейер до обработчиков и общие фильтры |
| `app/texts.py`, `app/i18n/*.py` | тексты бота и переводы на восемь языков (`i18n.PACKS`) |
| `app/faq.py`, `app/faq_i18n.py` | факты проката и автоответы, русский в `faq.py` вместе с фактами |
| `app/keyboards.py` | клавиатуры Telegram |
| `app/services/contract.py` | заполнение docx-шаблона подстановками `{{ поле }}` |
| `app/services/crypto.py` | шифрование анкеты в базе |
| `app/services/mrz.py`, `ocr.py` | разбор машиночитаемой зоны и вызов tesseract в том же контейнере |
| `app/services/starline.py`, `tochka.py` | клиенты внешних API, разбор ответа отделён от сети ради тестов |
| `app/max/*.py` | транспорт MAX: клиент API, цикл опроса, обработчики, клавиатуры, разборы |
| `schema.sql` | вся схема одним файлом, читается сверху вниз |
| `consistency.py` | сверка compose, `.env.example`, `config.py`, `deploy.ps1` и схемы между собой |
| `tests/fake_crm.py` | `CrmDB` в памяти; живой Postgres тоже есть — `tests/test_crm_pg.py` и `tests/test_web_pg.py` поднимают его через `pgserver` |

`aiohttp` в `requirements.txt` не значится: он приходит с aiogram, а `starline.py` и
`tochka.py` импортируют его внутри `_session()`, чтобы панели он не понадобился.

### Хочу поменять X, смотреть сюда

| Что меняю | Куда идти |
|---|---|
| текст, который видит клиент | `app/texts.py`, переводы `app/i18n/*.py` |
| адреса, график, ответ на частый вопрос | `app/faq.py`, переводы `app/faq_i18n.py` |
| шаг анкеты или валидацию | `app/logic.py`, функция `anketa_steps` |
| поведение бота в Telegram | `app/handlers/*.py` и порядок роутеров в `app/main.py` |
| то же в MAX | `app/max/handlers.py`, чистые разборы в `app/max/parse.py` |
| расчёт денег, простоя, амортизации | `app/crm/logic.py` |
| запись операции в базу | `app/crm/service.py`, уведомление отдельно в `app/crm/notify.py` |
| SQL по схеме `crm` | `app/crm/db.py` |
| таблицу, колонку, триггер, индекс | `schema.sql`, только через него |
| страницу панели | маршрут в `app/web/app.py` плюс шаблон `app/web/templates/<имя>.html` |
| вёрстку панели | `app/web/templates/base.html`, `app/web/static/style.css` |
| доступ к странице без входа | кортеж `PUBLIC` в `app/web/app.py` |
| новое уведомление | каталог `NOTICES` в `app/crm/logic.py`, ворота `app/crm/notices.py` |
| подстановку в шаблон рассылки | `TEMPLATE_FIELDS` в `app/crm/logic.py` |
| новый фоновый цикл | модуль в `app/crm/`, `create_task` в `app/main.py` и отмена в его `finally` |
| новую переменную окружения | `.env.example`, `docker-compose.yml`, `app/config.py` или `app/web/config.py` |
| новый файл в проекте | список заливки в `deploy.ps1`, иначе `consistency.py` ругнётся |
| работу с внешним API | `app/services/starline.py`, `app/services/tochka.py`, `app/services/avito.py`, циклы в `app/crm/` |
| входящее из нового канала или шлюза | разбор тела хука `parse_inbound` в `app/crm/logic.py`, запись `service.inbox_in`, отправка ответа `send_once` в `app/crm/inbox.py` |
| разбор формы из рабочей группы точек | `parse_ops_*` в `app/crm/logic.py`, сверка в `app/crm/opsgroup.py`, темы в `filters.ops_topic` |
| договор, акты, печать | `app/services/contract.py`, шаблоны `app/*.docx`, выбор шаблона `app/crm/doctemplates.py` |

Перед коммитом: `python3 -m unittest discover -s tests -q`, `ruff check .`, `python3
consistency.py`.

## Слои и правила

Код разделён на четыре слоя, и граница между ними проходит по импортам. Открыв незнакомый
файл, посмотрите на его шапку: список импортов говорит о том, что здесь разрешено, точнее
любого описания.

### Четыре слоя

| Слой | Файлы | Что живёт | Чего там нет |
|---|---|---|---|
| Чистая логика | `app/logic.py`, `app/crm/logic.py` | деньги, периоды, проверки форм, права, форматирование | базы, сети, Telegram |
| Доступ к базе | `app/db.py`, `app/crm/db.py` | SQL, транзакции, белые списки колонок | бизнес-правил, текстов для человека |
| Сервис | `app/crm/service.py` | правила операции, порядок шагов, отказы | SQL, HTTP, aiogram |
| Транспорт | `app/web/app.py`, `app/handlers/`, `app/max/` | разбор формы, показ ошибки, редирект | вычислений и SQL |

### Чистая логика

`app/logic.py` импортирует только stdlib: `re`, `collections.abc`, `dataclasses`,
`datetime`, `decimal`, `pathlib`, `typing`. `app/crm/logic.py` добавляет к stdlib ровно один
импорт проекта, `from .. import logic as bot_logic`. Так сделано, чтобы тесты гонялись без
установленного окружения: `tests/test_logic.py` и `tests/test_crm_logic.py` не требуют ни
aiogram, ни asyncpg.

Проверки возвращают `logic.Check` (`app/crm/logic.py`), датакласс из трёх полей:

```python
@dataclass(frozen=True)
class Check:
    ok: bool
    value: Any = None
    error: str = ""
```

Текущий день передаётся параметром (`today=`, `now=`), а не берётся из `date.today()` внутри
функции: тест, зависящий от сегодняшнего дня, однажды падает сам по себе.

Новое денежное или календарное правило пишется здесь, потому что здесь его проверяет тест за
миллисекунды.

### Доступ к базе

`app/crm/db.py` это один класс `CrmDB`, около трёхсот публичных методов (ровно столько же у
заглушки `tests/fake_crm.py`), без ORM. Каждый метод возвращает `dict` или `list[dict]`, а
не `asyncpg.Record`: строки уходят в шаблоны Jinja и в тексты Telegram, там словарь удобнее.
Суммы возвращаются `Decimal`.

Правила слоя:

- Имена колонок в UPDATE подставляются в SQL текстом, поэтому берутся только из белых
  списков в шапке файла: `BIKE_FIELDS`, `CLIENT_FIELDS`, `TARIFF_FIELDS` и соседние. Колонка
  вне списка роняет `ValueError` в `_set_clause`, и это правильное поведение.
- Атомарность задаётся здесь, а не выше. Что должно случиться вместе, лежит в одном методе:
  `create_rental` вставляет аренду и ставит велосипеду `rented` одной транзакцией,
  `charge_period` вставляет начисление и двигает `billed_until`.
- Автор изменения приходит параметром `by` и кладётся в `set_config('crm.actor', …, true)` в
  той же транзакции: оттуда его читает триггер `crm.log_bike_status`. Апдейт статуса без
  `by` пишет в журнал пустого автора.
- Текстов для человека тут нет: слово `ServiceError` в `app/crm/db.py` не встречается ни
  разу. Метод возвращает `False` или `None`, объясняет вызывающий.

### Сервисный слой

`app/crm/service.py` импортирует только `esign`, `logic`, `notices`, `notify`. Базы он не
знает: доступ приходит первым позиционным параметром.

```python
async def open_rental(crm: Any, *, client: dict, bike: dict | None,
                      tariff: dict, ...) -> int
```

`Any` здесь не лень, а контракт. В тестах на это место встаёт `tests/fake_crm.py`, схема crm
в памяти с теми же методами и теми же уникальными ограничениями. Поэтому `tests/test_web.py`
поднимает настоящую панель через `TestClient` без Postgres.

Отказ операции это `ServiceError`. Его текст показывается человеку как есть, поэтому пишется
по-русски и по делу.

Здесь же решается, что можно сорвать, а что нельзя. Сбой реферальной программы в
`open_rental` ловится и уходит в лог: учёт приглашений не вправе сорвать выдачу велосипеда.
Сбой `crm.create_rental` не ловится, кроме нарушения уникального индекса, которое
переводится в сообщение про второго оператора.

Уведомления клиенту живут отдельно, в `app/crm/notify.py`, и вызываются после того, как
запись в базе состоялась. Причина в шапке файла: зачисление уже случилось, и откатывать его
из-за того, что клиент заблокировал бота, нельзя.

### Транспорт

`app/web/app.py` это одна функция `create_app`, внутри которой замыканием лежат все маршруты
и обвязка: `render`, `flash`, `redirect`, `who`, `form`. SQL в файле нет ни строки, решения
берутся из `logic`, операции из `service`.

Права проверяются не в маршрутах, а одним стражем: middleware `auth` берёт раздел по адресу
через `logic.section_for` и сверяет уровень обёртками `may_view` и `may_edit` (они и зовут
`logic.can_view` / `logic.can_edit`): для GET и HEAD по «смотреть», для остального по
«менять». Так не появляются дыры вида «страницу закрыли, а POST оставили», и новый маршрут
закрывается сам, если его префикс есть в `SECTION_PATHS`.

Обработчики бота в `app/handlers/` получают `crm` из middleware (`app/middlewares.py`), SQL
в них тоже нет. События бота доезжают до CRM через `app/crm/sync.py`, который вызывает тот
же `service`. Про `app/max/handlers.py` сказано в его шапке прямо: своей логики решений там
нет, файл повторяет структуру `app/handlers/*`, чтобы расхождение двух ботов искалось в
одинаково названных функциях.

`CrmDB` создаётся только на входе в процесс: `app/main.py`, `app/web/__main__.py`,
`app/max_main.py`. Исключение одно, `app/crm/import_xlsx.py`, где импорт внутри функции
нужен для запуска импорта из командной строки.

### Одна операция через все слои

Выдача велосипеда клиенту, от кнопки до журнала.

| Шаг | Где | Что делает |
|---|---|---|
| 1 | `app/web/app.py`, `issue_create` | читает форму, подбирает тариф модели через `logic.match_tariff` и считает доп. аккумуляторы через `logic.battery_extra_price` |
| 2 | там же | проверяет поля: `logic.check_date`, `cost_field`, `logic.check_mileage`; при ошибке возвращает на форму |
| 3 | `app/crm/service.py`, `open_rental` | проверяет статус клиента и велосипеда, ловит вторую активную аренду, считает цену периода через `logic.period_price` |
| 4 | `app/crm/db.py`, `create_rental` | одна транзакция: аренда плюс `bikes.status = 'rented'` |
| 5 | `app/crm/service.py`, `charge_due` | берёт у `logic.due_periods` список периодов по сегодня |
| 6 | `app/crm/db.py`, `charge_period` | начисление в `ledger` и сдвиг `billed_until`, повтор гасит уникальный индекс |
| 7 | маршрут | платёж через `service.add_entry`, уведомление `notify.rental_opened`, `flash` и редирект |

Ни один слой не перепрыгнут: маршрут не пишет в `ledger` напрямую, сервис не трогает SQL,
логика не знает, что её позвали из веба.

### Что считается нарушением слоя

| Нарушение | Чем больно |
|---|---|
| SQL в маршруте или в обработчике бота | операция перестаёт быть общей, панель и бот расходятся в том, что пишут в базу |
| Бизнес-правило в `db.py` | правило не проверить тестом без Postgres, и его дублируют в сервисе |
| `crm.close_rental` мимо `service.close_rental` | аренда закрыта, а батареи остались «у клиента»: возврат живёт в сервисе, и обход слоя терял по две штуки на аренду |
| `crm.add_ledger` мимо `service` | знак и вид записи ставятся руками, бонус попадает в `payment` и завышает средний чек |
| Смена статуса без `by` | `bike_status_log` теряет автора, по спорной смене спросить некого |
| Два апдейта вместо одной транзакции | сбой между ними оставит аренду без начисления или велосипед свободным под клиентом |
| `date.today()` внутри чистой функции | тест перестаёт быть воспроизводимым |
| Новая зависимость в `logic.py` | тесты логики перестают гоняться на голом stdlib |

### Перед коммитом

```
python3 -m unittest discover -s tests -q
ruff check .
python3 consistency.py
```

`consistency.py` сверяет файлы между собой: переменные `docker-compose.yml` против
`.env.example`, колонки кода против `schema.sql`, новые файлы против списков в `deploy.ps1`.
Тесты этого не видят.

## Путь клиента через код

Путь идёт по двум машинам состояний сразу. Бот ведёт `bot.users.state` и `bot.users.status`
(константы в `app/logic.py`), CRM ведёт `crm.clients`, `crm.rentals` и `crm.ledger`. Связь
односторонняя: события бота отражаются в CRM через `app/crm/sync.py`, обратно не течёт
ничего. Поэтому «клиент» в боте и «клиент» в панели это разные строки, связанные по `tg_id`
и телефону.

### Что надо знать до первой правки

Любой переход состояния идёт через `Database.patch` (`app/db.py`) с `expected_state` или
`expected_status`. Это оптимистичная блокировка: двойной тап, медиагруппа и два модератора
получают `False` и молча выходят. Патч без ожидаемого состояния затирает чужой переход.

Колонка, которой нет в `PATCHABLE` (`app/db.py`), роняет `patch` с `ValueError`. Имя колонки
подставляется в SQL текстом, параметром его не передать, поэтому новая колонка `bot.users`
добавляется в два места: в `schema.sql` и в этот набор.

Объект `crm` приезжает в обработчики из `PipelineMiddleware` (`app/middlewares.py`) и по
умолчанию `None`. Событийные функции `app/crm/sync.py` (`on_contract_signed`,
`on_payment_confirmed`, `on_rental_started`, `on_rental_extended`, `on_rental_closed`) ловят
исключения в лог: сбой учёта не вправе остановить выдачу договора или акта. Вспомогательные
(`client_from_bot`, `card_flag`) своего перехвата не держат - их зовут изнутри этих же
функций, под их же `try`.

### Событие, функция, что записалось

| Событие | Функция | Состояние до → после | Что записалось |
|---|---|---|---|
| `/start` | `cmd_start`, `start_flow` (`app/handlers/registration.py`) | `new` → `wait_lang`, с известным языком сразу `wait_fio` | строка `bot.users`; код из `?start=` уходит в `_catch_invite` → `service.ref_click`, в `crm.referrals` появляется переход |
| Выбор языка | `cb_lang` | `wait_lang` → `wait_fio` | `lang`, событие `lang_set` |
| ФИО | `st_fio` | `wait_fio` → `wait_pdn` | `full_name`, событие `fio_set` |
| «Ознакомлен(а)» с Политикой | `cb_policy` | `wait_pdn` → `wait_oferta` | `policy_version`, `policy_ack_at` |
| Согласие на обработку ПДн | `cb_oferta` | `wait_oferta` → `wait_contact` | `oferta_version`, `oferta_accepted_at`, `pdn_version`, `pdn_consent_at` |
| Контакт кнопкой | `st_contact` | `wait_contact` → первый шаг анкеты (`logic.next_state`) | `phone` |
| Ответ анкеты | `st_anketa` → `_advance` | шаг → следующий по `logic.ANKETA_STEPS` | весь `anketa_enc` целиком через `Vault.encrypt`: поля лежат в одном шифрованном столбце |
| Фото документа | `st_doc` | `wait_doc` → `wait_doc2` | `doc_file_id`, `purge_after=NULL`; фоном `_process_upload` пишет `doc_path`, `doc_sha256` и считает дубли |
| Вторая страница или «хватит одного» | `st_doc2`, `cb_doc_enough` → `_after_doc` | `wait_doc2` → `confirm`, у 16-17 лет `wait_parent_consent` | `doc2_*` |
| «Подтверждаю» | `cb_confirm` | `confirm` → `pending`, статус `new` → `pending` | событие `submitted`; `send_moderation_card` пишет `mod_chat_id`, `mod_message_id`; `sync.card_flag` CRM только читает |
| «Одобрить» | `cb_approve` → `_decide` (`app/handlers/moderation.py`) | статус `pending` → `approved`, состояние остаётся `pending` | `reviewed_by`, `reviewed_at`; приглашение выдачи, `issue_chat_id`, `issue_message_id` |
| Отказ кнопкой или текстом | `cb_reject_reason`, `mod_reply` | статус → `rejected`; состояние по `logic.reject_back_to` у отказа кнопкой | `reject_reason`, `purge_after` по `purge_rejected_days` |
| Данные выдачи от оператора | `_issue_reply` → `logic.parse_issue_form` | статус `approved`, состояние не двигается | `issue_data`, `rent_from`, `rent_until`, сброшенные `remind_*_at` |
| Сборка договора | `contract.issue` (`app/handlers/contract.py`) | `pending` → `wait_sign` | `contract_no` из `nextval('bot.contract_seq')`, `contract_path`, `contract_sha256`, `soglasie_*`, `contract_status='issued'`, `contract_issued_at` |
| «Подписываю» | `cb_sign` | `wait_sign` → `wait_payment` | `contract_status='signed'`, `contract_signed_at`, пересобранные файлы. **CRM:** `sync.on_contract_signed` → `client_from_bot` заводит `crm.clients` или привязывает `tg_id` к карточке, найденной по телефону |
| Показ суммы клиенту | `start_payment` | состояние ставит вызывающий | `pay_chat_id`, `pay_message_id` |
| «Оплата получена» | `cb_pay` | `wait_payment` → `wait_act_sign` | `pay_confirmed_at`. **CRM:** `sync.on_payment_confirmed` → `crm.add_ledger` вида `payment`, затем `service.ref_paid` |
| Подпись Акта приёма | `cb_act_sign` | `wait_act_sign` → `approved` | `act_in_signed_at`, `act_in_path`, `act_in_sha256`, приглашение возврата. **CRM:** `sync.on_rental_started` → `crm.start_rental_charged`: `crm.rentals`, первое начисление в `crm.ledger` и `bikes.status='rented'` одной транзакцией; `service.ref_rented` |
| Оператор принял продление | `_extend_reply` | `approved` → `wait_payment` | `extend_until`, новая цена в `issue_data` |
| Продление оплачено | `cb_pay` → `_apply_extension` | `wait_payment` → `approved` | `rent_until`, сброс `remind_*_at`. **CRM:** `sync.on_rental_extended` → `crm.extend_rental_paid`: платёж и начисление за новый срок одной транзакцией |
| «Я оплатил(а)» в кабинете | `cabinet.cb_paid` (`app/handlers/cabinet.py`) | не меняется | строка `crm.payment_claims` (частичный уникальный индекс на открытую заявку), карточка оператору |
| Оператор зачислил заявку | `cb_claim`, `claim_amount_reply` → `cabinet.credit` | не меняется | `service.credit_claim` → `crm.credit_claim`: заявка `confirmed` и `crm.ledger` вида `payment` одной транзакцией |
| Клиент просит закрыть аренду | `menu.start_close`, `st_close_reason` | `approved` → `wait_close_reason` → `approved` | `close_reason`, `close_requested_at`, `return_chat_id`, `return_message_id` |
| Форма закрытия от оператора | `_return_reply` → `contract.send_act_out` | → `wait_return_sign` | `return_data` |
| Подпись Акта возврата | `cb_return_sign` | `wait_return_sign` → `approved` | `act_out_signed_at`, `act_out_path`, `act_out_sha256`, событие `rental_closed`. **CRM:** `sync.on_rental_closed` → `crm.close_rental`: аренда `closed`, велосипед `available`, позиции `rental_extras` сняты |

### Начисления идут по двум разным правилам

Аренда из бота заводится с `billing='manual'`: так её собирает
`crm_logic.rental_from_issue`. Дневной проход её не трогает, `service.charge_due` выходит на
первой строке. Периоды ей добавляют события бота: выдача (`start_rental_charged`) и
продление (`extend_rental_paid`).

Аренда из панели заводится `service.open_rental`, и режим выбирает оператор в форме
`/rentals/new`; по умолчанию это `billing='auto'`, и тогда периоды ей начисляет
`billing.run_daily` → `service.charge_all` → `service.charge_due` → `crm.charge_period`.
Повтор безвреден: одинаковый период упирается в уникальный индекс, `charge_period`
возвращает `False` и не списывает ничего. Поэтому лишний ручной запуск прохода допустим, а
пропущенный догоняется сам.

### Напоминания: тоже два прохода

Оба живут в одном цикле `tasks.reminders_loop` (`app/tasks.py`) и имеют отдельные отметки
«сегодня сделано», чтобы сбой одного не отменял другой.

| Проход | Что читает | Чем помечает |
|---|---|---|
| `tasks.remind_once` → `_notify_deadline` | `bot.users.rent_until` активных аренд бота | `remind_soon_at`, `remind_last_at`, `remind_overdue_at` |
| `billing.run_daily` → `billing.remind_once` → `send_reminder` | `crm.active_rentals()`, срок считается из баланса (`logic.covered_until`) | `crm.mark_notified`, запись в `notices.record` |

Отметка ставится и при недоставке. Заблокировавший бота клиент иначе заставлял бы систему
стучаться к нему каждые пятнадцать минут.

### Места, где легко сломать путь

`sync.on_rental_closed` ищет клиента только через `crm.client_by_tg`, без поиска по
телефону, который есть в `client_from_bot`. Клиент без привязанного `tg_id` в CRM останется
с открытой арендой, хотя акт возврата подписан.

Закрытие идёт через сервисный слой с обеих сторон: и панель, и `sync.on_rental_closed` зовут
`service.close_rental`, а он после закрытия аренды возвращает батареи
(`crm.return_batteries`). Это не мелочь стиля: пока бот закрывал аренду прямым
`crm.close_rental`, батареи оставались «у клиента» навсегда - по две штуки на каждую
закрытую из бота аренду. Такой же обход слоя в новой ветке даст такую же потерю.

`sync._bike_for` возвращает `None`, если велосипед с этой рамой уже `rented`. Аренда тогда
создаётся без `bike_id`: это ошибка оператора, и её оставили видимой, а не спрятали в тихом
переносе техники.

Смена статуса велосипеда пишется в `crm.bike_status_log` триггером, и пробег снимается тем
же триггером со строки велосипеда. Пробег обновляется одним запросом со статусом
(`close_rental` пишет `mileage_end` и статус велосипеда в одной транзакции), отдельный
апдейт записал бы в журнал вчерашнее число.

Файлы удаляются вручную там же, где база перестаёт на них ссылаться (`issue`, `cb_sign`,
`cb_act_sign`, `st_doc`). Ретеншен ищет по путям в базе, и файл без ссылки не найдёт уже
никогда.

## База данных

### Две схемы, одна база, один пул

Схем две. `bot` это путь клиента до договора: состояние диалога, файлы документов, отметки
напоминаний.
`crm` это учёт: клиенты, аренды, парк, деньги. Разделение по владельцу процесса, а не по
сущности,
поэтому в схему `bot` пишет только бот; панель её читает (договор, состояние
диалога, отметки напоминаний), но не правит. База при этом одна и пул один:
`app/main.py` поднимает `Database`, применяет схему и строит `CrmDB(db.pool)`, так что
кабинет клиента и синхронизация бота в CRM обходятся без второго подключения. Панель делает
то же
в `app/web/__main__.py`. ORM нет, только asyncpg и SQL текстом.

Соединение настраивает `_init_connection` (`app/db.py`): кодек jsonb, иначе asyncpg отдаёт
его строкой
и код, ждущий dict, падает на `.get()`, и пояс сессии из `TZ`, иначе «за сегодня» начинается
в 03:00.

### Миграций нет, есть идемпотентный файл

Схема целиком лежит в `schema.sql` и читается сверху вниз. Нумерованных миграций нет
намеренно:
файл один, порядок применения задан порядком строк. Применяется он при каждом старте,
`Database.apply_schema` (`app/db.py`), и вызывают его три точки входа: `app/main.py`,
`app/max_main.py`, `app/web/__main__.py`.

```python
async with self.pool.acquire() as conn, conn.transaction():
    await conn.execute("select pg_advisory_xact_lock(7331)")
    await conn.execute(sql)
```

Блокировка нужна потому, что `if not exists` не спасает от гонки: бот и панель стартуют
одновременно, на пустой базе оба создают одни и те же объекты, один падал бы с duplicate
key.
Отсюда главное правило: каждая строка файла выполняется много раз. Таблицы это
`create table if not exists`, колонки `add column if not exists`, функции `create or
replace`,
а бэкфилл пишется так, чтобы второй прогон ничего не менял: `update bot.users set lang =
faq_lang
where lang is null and faq_lang is not null`.

У сида владельца то же правило, и `on conflict do nothing` для него годится не
всегда: уникальный индекс тарифов частичный (`where active`), и выключенная
владельцем строка из него выпадает - конфликта нет, и каждый старт вставлял
её заново, уже активной. Поэтому цены заходят как `insert … select … where not
exists`, то есть «уже есть» считается по строке любой активности. Сид, который
опирается на частичный индекс, стоит проверять тестом на живом Postgres:
`test_disabled_tariff_is_not_resurrected_by_the_seed`.

### Ключевые таблицы

| Группа | Таблицы | Что внутри |
|---|---|---|
| Бот | `bot.users`, `bot.updates_log`, `bot.events` | анкета и документы, клейм апдейтов `processing -> done`, события |
| Люди | `crm.clients`, `crm.staff`, `crm.access_profiles`, `crm.referrals` | арендаторы, сотрудники с профилем прав, приглашения |
| Парк | `crm.bikes`, `crm.batteries`, `crm.bike_status_log`, `crm.battery_status_log`, `crm.purchases` | техника, журналы статусов, партии закупки |
| Аренда | `crm.rentals`, `crm.rental_bikes`, `crm.rental_extras`, `crm.tariffs` | аренда, выданная техника, позиции сверх велосипеда, цены |
| Деньги | `crm.ledger`, `crm.pay_orders`, `crm.payment_claims`, `crm.card_tokens`, `crm.cash_shifts`, `crm.cash_moves`, `crm.bank_txns` | журнал, счета, заявки на зачисление, касса, выписка |
| Сервис | `crm.work_orders`, `crm.work_order_items`, `crm.work_types`, `crm.bike_log`, `crm.repair_items`, `crm.repair_nodes` | наряды, два прайса, шапка ремонта и позиции по узлам |
| Склад | `crm.parts`, `crm.part_moves`, `crm.part_docs`, `crm.part_orders`, `crm.part_order_items`, `crm.suppliers` | номенклатура, движения, приход, заказы поставщику |
| Устройства | `crm.trackers`, `crm.tracker_positions`, `crm.tracker_alerts`, `crm.tracker_commands` | StarLine: состояние, позиции, тревоги, очередь блокировки мотора |
| Общение и документы | `crm.message_templates`, `crm.campaigns`, `crm.campaign_sends`, `crm.notices`, `crm.notice_log`, `crm.sign_requests`, `crm.sign_events`, `crm.doc_templates`, `crm.company_marks` | рассылки, уведомления, ПЭП, свои шаблоны docx, подпись и печать |
| Справочники | `crm.locations`, `crm.bike_models`, `crm.battery_models`, `crm.compat`, `crm.settings`, `crm.saved_views` | точки, каталог, совместимость, настройки ключ-значение, фильтры |

Колонок «баланс» и «остаток» нет: хранимое число разошлось бы с журналом. Баланс клиента это
`sum(amount)`
по `crm.ledger` (`app/crm/db.py`), остаток запчасти это `sum(qty)` по `crm.part_moves`
(`app/crm/db.py`).

### Инварианты держит база, а не код

| Инвариант | Где | Индекс |
|---|---|---|
| Одна активная аренда на клиента | `schema.sql` | `rentals_active_client_idx` on `(client_id) where status = 'active'` |
| Одна активная аренда на велосипед | `schema.sql` | `rentals_active_bike_idx` on `(bike_id) where status = 'active' and bike_id is not null` |
| Одно начисление на период аренды | `schema.sql` | `ledger_charge_period_idx` on `(rental_id, period_from) where kind = 'charge' and period_from is not null` |
| Один открытый наряд на велосипед | `schema.sql` | `work_orders_one_open` on `(bike_id) where bike_id is not null and status in ('new','in_work','approve','waiting')` |
| Одна открытая ведомость пересчёта | `schema.sql` | `stock_takes_one_open` on `((status)) where status = 'open'` |
| Одна открытая смена на точку | `schema.sql` | `cash_shifts_one_open` on `(coalesce(location, '')) where status = 'open'` |
| Одна цена на вид, модель и срок | `schema.sql` | `tariffs_kind_model_period_idx` on `(kind, coalesce(model,''), period_days) where active` |

Индексы частичные: уникальность действует на живых строках, закрытые аренды в неё не
попадают.
Проверять «уже есть» отдельным запросом бесполезно, два обработчика пройдут проверку
одновременно,
поэтому код ловит `asyncpg.UniqueViolationError`: `link_client_tg` (`app/crm/db.py`),
`charge_period` (`app/crm/db.py`, повтор периода возвращает False и ничего не списывает).

Журнал статусов держат триггеры `crm.log_bike_status` и `crm.log_battery_status`
(`schema.sql`
и `1124`; первый переопределён ниже, `schema.sql`, когда к нему добавился пробег). Оба висят
`after insert or update of status` и пишут строку при заводе техники и при смене статуса.
Правила два:

- Автор берётся из `current_setting('crm.actor', true)`, значит его кладут в ту же
  транзакцию:
  `select set_config('crm.actor', $1, true)` перед UPDATE, как в `update_bike`
(`app/crm/db.py`).
- Пробег триггер снимает со строки велосипеда (`new.mileage_km`), поэтому его пишут ТЕМ ЖЕ
  обновлением, что и статус: отдельный апдейт записал бы вчерашнее число (`app/web/app.py`).

### Как добавить колонку

Допишите в конец `schema.sql` секцию: комментарий с причиной и сам ALTER. Не правьте `create
table`
выше, на живой базе он не выполнится и обновление молча выедет без колонки (`schema.sql`).

```sql
-- ────── короткое название ──────
-- Зачем колонка и что было без неё.
alter table crm.bikes add column if not exists foo text;
create index if not exists bikes_foo_idx on crm.bikes (foo) where foo is not null;
```

Дальше добавьте имя в белый список в `app/crm/db.py` (`BIKE_FIELDS`, `RENTAL_FIELDS`,
`ORDER_FIELDS`
и так далее по таблице), иначе UPDATE упадёт с `ValueError: недопустимые колонки`.
Суммы только `numeric(12,2)`, даты только `timestamptz`, число из внешнего API прогоняйте
через
`_money` (`app/crm/db.py`): float в колонку numeric asyncpg не примет. И прогоните проверки:

```bash
python3 -m unittest discover -s tests -q
ruff check .
python3 consistency.py
```

`test_columns_exist_in_schema` (`tests/test_crm_sql.py`) сверяет колонки из запросов кода со
`schema.sql`, `consistency.py` делает то же по всему `app/`, а
`test_mileage_column_survives_reapply`
(`tests/test_crm_pg.py`) применяет схему второй раз и проверяет, что колонка на месте.

### Почему белые списки колонок обязательны

Имя колонки нельзя передать параметром: `$1` подставляет значение, идентификатор кладётся в
SQL
текстом. Этим занимается `_set_clause` (`app/crm/db.py`), она же сверяет поля с
разрешёнными:

```python
sets, values = _set_clause(fields, BIKE_FIELDS, 2)
```

Сверка обязательна потому, что поля доезжают до неё пачкой прямо из формы: `app/web/app.py`
вызывает `await crm.update_bike(bike_id, **fields)`, и без белого списка имя поля из запроса
стало бы
куском SQL. У бота свой `PATCHABLE` (`app/db.py`) для `bot.users`, и его `patch`
(`app/db.py`)
вдобавок проверяет `expected_state` и `expected_status`: два админа, нажавшие «Одобрить»
одновременно,
иначе оба довели бы дело до конца. Та же причина вне SQL: сортировка списков идёт по белому
списку
в `logic.sort_rows` (`app/crm/logic.py`).

## Деньги в коде

### Журнал один, колонки баланса нет

Все деньги клиента лежат в одной таблице `crm.ledger` (`schema.sql`). Баланс нигде не
хранится, он считается суммой: `CrmDB.client_balance` (`app/crm/db.py`) делает `select
coalesce(sum(amount), 0) from crm.ledger where client_id = $1`, а чистая `logic.balance`
(`app/crm/logic.py`) складывает те же строки в памяти - она для тестов и для расчётов над
уже прочитанным журналом, в самом приложении не вызывается. Причина: две копии одного числа
рано или поздно разъезжаются, а сумму строк не подделать забытым апдейтом.

Знак живёт в самой сумме, отдельного поля «приход или расход» нет: платёж кладётся плюсом,
начисление минусом, и баланс получается обычным `sum`.

| Колонка | Зачем |
|---|---|
| `client_id` | чей баланс, единственное обязательное отношение |
| `rental_id` | к какой аренде относится, пусто у общих штрафов и корректировок |
| `kind` | вид записи, он же задаёт знак |
| `amount` | `numeric(12,2)` со знаком |
| `method` | `sbp`, `cash`, `card`, `transfer`, `other`, только у денежных видов |
| `period_from`, `period_to` | период начисления, по `period_from` работает защита от двойного списания |
| `shift_id` | кассовая смена, в которую попали наличные (`schema.sql`) |

### Виды записей

Словарь `logic.KINDS` и знаки `logic.KIND_SIGN` (`app/crm/logic.py`).

| Вид | Знак | Что значит |
|---|---|---|
| `payment` | + | клиент заплатил. Только по этим строкам считается средний чек |
| `charge` | − | начисление за период аренды. Пишет биллинг, руками недоступно |
| `fine` | − | штраф или ремонт за счёт арендатора |
| `refund` | − | деньги вернули клиенту |
| `adjust` | ± | корректировка, единственный вид со свободным знаком |
| `bonus` | + | баллы: они меняют баланс, но платежом не считаются |

Оператор вводит число без знака, знак ставит `logic.signed_amount(kind, amount)`
(`app/crm/logic.py`). Писать `-amount` в вызывающем коде не нужно и вредно: у `adjust` знак
берётся из ввода, у остальных перебивается видом.

### Как считается «оплачено до»

Начисления идут вперёд, целым периодом, поэтому отправная точка это `rentals.billed_until`,
дата, с которой начинается ещё не начисленный период. Сама дата платежа выводится из
баланса:

```python
logic.covered_until(billed_until: date, bal, price, period_days) -> date   # logic.py
logic.days_left(until: date | None, *, today: date | None = None) -> int | None
logic.rental_summary(rental: dict | None, bal, *, today: date) -> dict     # logic.py
```

Баланс ноль значит, что всё начисленное оплачено и следующий платёж в `billed_until`. Плюс
на балансе двигает дату вперёд целыми периодами (`bal // price`), минус отнимает с
округлением вверх: долг в рубль уже означает неоплаченный период. Округление вверх сделано
без float, явной проверкой остатка `debt % price`.

Дата исключительная: «оплачено до 20.09» значит, что 20.09 наступает следующий платёж. Один
и тот же `rental_summary` зовут и панель, и кабинет бота: клиенту и оператору положено
видеть одну дату.

### Начисление периодов и его идемпотентность

```python
logic.due_periods(billed_until, period_days, *, today) -> list[tuple[date, date]]  # logic.py
service.charge_due(crm, *, rental: dict, today: date) -> int                       # service.py
service.charge_all(crm, *, today: date) -> int                                     # service.py
```

`due_periods` возвращает список, а не один период: процесс мог не работать несколько дней, и
проход обязан догнать пропущенное. Сверху стоит ограничение `MAX_PERIOD_DAYS` на случай
`billed_until` из прошлого века после кривого импорта. `charge_due` работает только при
`billing = 'auto'` и активной аренде, аренды из бота начисляются событиями.

Идемпотентность держится не на коде, а на базе: частичный уникальный индекс
`ledger_charge_period_idx` по `(rental_id, period_from)` для `kind = 'charge'`
(`schema.sql`). `CrmDB.charge_period` (`app/crm/db.py`) ловит `UniqueViolationError` и
возвращает `False`, ничего не списав, а вставку и сдвиг `billed_until` делает одной
транзакцией. Поэтому повторный запуск дневного прохода (`app/crm/billing.py`) безвреден. По
тому же принципу собраны `CrmDB.start_rental_charged` и `CrmDB.extend_rental_paid`
(`app/crm/db.py` и `:698`): платёж и начисление пишутся вместе, иначе сбой между ними уводил
клиента в плюс на целый период.

Цена периода считается в одном месте, `logic.period_price(base, extras)`
(`app/crm/logic.py`): велосипед плюс живые позиции вроде доп. аккумулятора. Складывать её
второй раз в своём коде нельзя, однажды сложат по-другому.

### Что в журнал не попадает

| Что | Где живёт | Почему не в журнале |
|---|---|---|
| Ремонт чужой техники | `work_orders.total`, `work_orders.paid_at` | журнал это аренда, средний чек считается по нему и чужой самокат его завысил бы |
| Выставленный счёт | `crm.pay_orders` | счёт это намерение, журнал это факт. В `ledger` он превращается один раз, при подтверждённой оплате, и `pay_orders.ledger_id` это фиксирует |
| Оплаченный счёт за ремонт | `work_orders.paid_at` | та же красная линия |

Красная линия реализована в `CrmDB.mark_pay_paid` (`app/crm/db.py`): если у счёта есть
`work_order_id`, функция ставит наряду `paid_at`, закрывает счёт и возвращает `None`, не
написав в журнал ни строки. Она же берёт счёт `for update` и молча выходит на уже
оплаченном: у банка легко спросить статус дважды.

Баллы в журнал попадают, но никогда видом `payment`. `CrmDB.grant_bonus` (`app/crm/db.py`)
пишет `kind = 'bonus'` и повод в `crm.bonuses` одной транзакцией, `CrmDB.pay_referral_bonus`
(`app/crm/db.py`) делает то же для агента. Скидка по акции идёт иначе: `charge_due`
(`app/crm/service.py`) до начисления подбирает одну акцию через `service.promo_for_period`
и `logic.pick_promo`, а `CrmDB.charge_period(..., bonus=...)` пишет начисление, строку
`bonus` и повод в `crm.bonuses` одной транзакцией. Откат начисления (повтор периода)
откатывает и скидку; частичный уникальный индекс `bonuses_promo_period_once` страхует от
второй скидки на тот же период. Выручку на средний чек отбирает
`CrmDB.rental_revenue` (`app/crm/db.py`) - строго `kind = 'payment'`, - а делит её на
велосипеде-дни аренды уже `logic.fleet_metrics`. Бонус, попавший в платежи, испортил бы одно
из трёх чисел парка.

### Чем пользоваться вместо записи в базу

Писать `insert into crm.ledger` из своего кода не нужно: у каждого случая есть функция,
которая уже знает про знак, смену, транзакцию и защиту от двойного нажатия.

```python
# service.py  ручная запись из панели: знак по виду, charge запрещён
service.add_entry(crm, client, *, kind, amount: Decimal, method, note, by,
                  rental_id=None) -> int
# service.py  заявка «я оплатил»: закрытие заявки и платёж одной транзакцией,
#                None значит, что её уже закрыл другой оператор
service.credit_claim(crm, claim, amount: Decimal, *, by, method="sbp") -> int | None
# service.py строка выписки Точки; уже разобранная - ServiceError,
#              вставка платежа и отметка строки одной транзакцией
service.credit_bank_txn(crm, txn, client, *, by, method="transfer") -> int
# service.py счёт закрыт наличными или переводом
service.credit_pay_order(crm, order, *, by, method="cash") -> int | None
# service.py  баллы руками, никогда не платёж
service.grant_manual_bonus(crm, client, amount: Decimal, *, note, by) -> Decimal
# service.py  проход по всем арендам: сбой одной - в лог, остальные начисляются,
#             а в конце ChargeError(done, failed): проход сделанным не считается,
#             следующий круг повторит его, начисленное защищено индексом
service.charge_all(crm, *, today, applied=None) -> int
# service.py  какая акция ляжет на период, который сейчас начислится; только
#             чтение, пишет charge_period(bonus=...) в транзакции начисления,
#             и там же, под замком строки акции, решается предел применений
service.promo_for_period(crm, *, rental, period_from, period_to, today) -> dict | None
# service.py  та же выборка до денег, для шага выдачи: error - отказ (кода нет,
#             срок вышел, клиенту не положен), note - другая акция выгоднее,
#             deferred - начало в будущем, скидку с оплаты сейчас не снимать
service.preview_promo(crm, *, client, tariff, started_on, code, today) -> dict
# service.py   выдача: аренда, позиции и первый период
service.open_rental(crm, *, client, bike, tariff, started_on, contract_no, by, ...) -> int
# service.py   заявка из кабинета: одна открытая на клиента, при аренде не принимается;
#              close_booking зовёт выдача, cancel_booking - панель и сам клиент
service.create_booking(crm, *, client, model, tariff, location, wanted_on) -> dict
```

`add_entry` отказывает на `kind == "charge"`: начисления делает биллинг, для ручной суммы
есть корректировка или штраф. Способ `cash` сам находит смену через `service.cash_shift_id`
(`app/crm/service.py`), выбирая её по тому, кто принял деньги, а не по окну времени: точек
две, смены на них открыты одновременно.

### Правила

Суммы только `Decimal`, `float` не допускается нигде: asyncpg не примет float в колонку
`numeric`, а копейки от сложения поплывут. Приводить числа из базы и из API надо через
`logic.to_money` (`app/crm/logic.py`), ввод человека разбирать через `logic.parse_money` и
проверять `logic.check_amount` (`app/crm/logic.py`), который отбивает ноль, отрицательное
там, где его быть не должно, и суммы больше `MAX_AMOUNT`.

Каждая денежная операция это строка в журнале, в той же транзакции, что и событие, которое
её породило. «Запишем сейчас, пересчитаем потом» ломает баланс, потому что баланс и есть
журнал.

Деньги, периоды и простой покрыты тестами, и они обязательны для любой правки здесь:

```bash
python3 -m unittest tests.test_crm_logic -q      # деньги, периоды, «оплачено до»
python3 -m unittest discover -s tests -q         # полный прогон
ruff check .
```

## Веб-панель

Панель это отдельный процесс (`app/web/__main__.py`): свой порт, свой жизненный цикл рядом с
ботом, падение одного не задевает другого. Схему она применяет сама при старте, потому что
панель поднимают раньше бота, а таблицы `crm` нужны ей с первой секунды.

### Один файл маршрутов

Все маршруты панели (на сегодня 209: 94 GET и 115 POST) лежат в `app/web/app.py` внутри
одной функции:

```python
def create_app(*, crm: Any, db: Any, cfg: WebConfig, bot: Any = None) -> FastAPI
```

Так сделано, чтобы `crm`, `cfg`, `bot`, `templates` и помощники (`render`, `flash`,
`redirect`, `may_view`, `may_edit`, `denied`, `who`, `list_tools`) были замыканиями, а не
глобальными объектами: обработчику нечего импортировать, тесту нечего подменять монкипатчем.
Разделы отбиты комментарными линейками вида `# ─────── парк ───────`, по ним и ищут нужное
место.

Панель не считает и не пишет сама:

| Что | Где живёт |
|---|---|
| Числа, проверки формы, справочники | `app/crm/logic.py` |
| Изменение, задевающее несколько таблиц | `app/crm/service.py` |
| Запросы | `CrmDB`, `app/crm/db.py` |
| Сообщение клиенту | `app/crm/notify.py` |

В обработчике остаётся: разобрать форму, проверить право, позвать `service`, положить flash,
вернуть редирект. Автор правки уезжает в `service` параметром `by=who(request)` (строка
`staff:логин`), оттуда в `set_config('crm.actor')` той же транзакции, и попадает в журналы
статусов. К `db` (схема `bot`) панель ходит ровно в пяти местах, только за карточкой
пользователя бота.

Форма это всегда POST и редирект 303 (`redirect()`), сообщение оператору через
`flash(request, текст, kind)`. Flash кладётся присваиванием, а не `append`: сессия Starlette
пишет cookie только при изменении своих ключей и правку вложенного списка не видит.
`render()` сам подкладывает в контекст `staff` и накопленные сообщения.

### Права

Право это пара «раздел плюс уровень», и отдельно несколько действий.

```python
logic.SECTIONS       # код -> название раздела
logic.ACTIONS        # money_edit, client_docs
logic.SECTION_PATHS  # префикс адреса -> код раздела
logic.section_for(path) -> str | None
logic.can_view(staff, code) / can_edit(staff, code) / can_act(staff, action)
```

Страж один на все маршруты, это middleware `auth`: GET и HEAD требуют `can_view`, любой
другой метод `can_edit`. Поэтому дыра «страницу закрыли, а POST оставили» в новом
обработчике не заводится. Исключения перечислены константами рядом: `PUBLIC` (вход, статика,
`/healthz`, `/sign/<токен>` для клиента) и `ALWAYS_OPEN` (`/logout`, `/me`, `/me/password`).

`section_for` сравнивает по префиксу, и точка в разделителях обязательна: `/clients.csv`
должен попасть в раздел `clients`, иначе выгрузка оказалась бы открыта всем. Это закреплено
тестом `tests/test_access.py`, `test_section_for_path`.

Руками право проверяют в двух случаях: GET, который показывает форму правки (`/bikes/new`
зовёт `may_edit`), и отдельное действие (`logic.can_act(request.state.staff, "client_docs")`
перед выдачей договора: в нём паспортные данные, и право на него не равно праву на
карточку). Отказ возвращает `denied(request, code)`, страницу 403 с названием недостающего
права и ссылкой на `/me`, а не голый 403: оператор должен увидеть, чего ему не хватает и
кому писать.

Порядок `add_middleware` обратный: `SessionMiddleware` добавляется последним, чтобы сессия
распаковалась до `auth`.

Профиль хранится в `crm.access_profiles.perms` (jsonb). `logic.normalize_perms` отбрасывает
неизвестные коды, поэтому профиль старой версии не откроет исчезнувший раздел. Встроенные
профили перезаписываются из `schema.sql` при каждом старте, правленные владельцем нет: `on
conflict (code) do update set perms = case when built_in then excluded.perms else старое
end`.

### Инструменты списков

Один набор на все списки: парк, аренды, наряды, склад, трекеры. Каждый со своим устройством
разъедется на первой правке.

```python
tools = list_tools(request, rows, allowed=BIKE_SORTS)
```

`allowed` это белый список «ключ в адресе -> имя поля в строке»:

```python
BIKE_SORTS = {"code": "code", "model": "model", "status": "status",
              "location": "location", "mileage": "mileage_km",
              "client": "full_name", "idle": "idle_days"}
```

Белый список, а не имя поля прямо из `?sort=`: параметр адреса это чужая строка.

`list_tools` возвращает `rows` (видимая страница), `all_rows` (всё найденное, для итогов),
`total`, `page`, `pages`, `size`, `shown`, `sort`, `dir`, `query`. Внутри `logic.sort_rows`
(пустые значения уходят в конец при любом направлении, иначе сортировка по клиенту выносит
наверх всё невыданное), `logic.page_of` и `logic.check_list_size` при `LIST_SIZES = (50,
100, 300)`. Итог в подвале считается по `total`, а не по показанному: «итого 77 аренд» при
пятидесяти на экране и есть ответ на вопрос, ради которого список открывали.

`clean_query(request, drop=("page",))` собирает ссылки сортировки: фильтры сохраняются,
номер страницы сбрасывается, а новые `sort` и `dir` дописываются в хвост и перекрывают
прежние, потому что Starlette при разборе строки запроса оставляет последнее значение.

Сохранённый фильтр это строка запроса под именем, своя у сотрудника: `views_of(request,
"/bikes")`, `POST /views`, `POST /views/{view_id}/delete`. Удаление проверяет владельца в
самом запросе (`crm.drop_saved_view(view_id, staff_id=...)`), чужой фильтр не трогается.

Выгрузка это маршрут вида `/bikes.{ext}` и один вызов `_table(ext, "bikes", header, rows)`:

- `xlsx` через openpyxl, рабочий вид: числа остаются числами, даты датами, шапка закреплена;
- `csv` с BOM, точкой с запятой и десятичной запятой, для тех, кто грузит выгрузку к себе;
- неизвестное расширение это 404, а не молчаливый csv: соврать в имени файла значит обмануть
  оператора один раз и потерять доверие к выгрузке навсегда;
- строка, начинающаяся с `=`, `+`, `-`, `@`, табуляции или возврата каретки
  (`_FORMULA_STARTS`), остаётся строкой в обоих видах: в csv `_cell` оборачивает её в
  `="..."`, в xlsx цикл `_xlsx` ставит ячейке `data_type = "s"`: ФИО приходит из бота как
  набрал человек, и `=HYPERLINK(...)` в имени превратило бы выгрузку в фишинговую ссылку;
- фильтры выгрузка читает из того же запроса, что и экран: выгружают то, что видят.

### Шаблоны

Jinja2, 83 файла в `app/web/templates/`, без сборки и без фронтенд-фреймворка: страница это
шаблон, действие это POST формы и редирект. Справочники и помощники прокидываются один раз в
`templates.env.globals` при создании приложения (`BIKE_STATUSES`, `ORDER_STATUSES`,
`can_view`, `money`, `home_for` и ещё около сотни), поэтому в контекст маршрута их класть не
нужно. Свои фильтры два: `|dmy` и `|iso`.

`base.html` держит боковое меню: группы и порядок пунктов написаны в нём руками, а не
выводятся из `SECTIONS`. Пункт показывается по `can_view(staff, код)`, заголовок группы
считается заранее (`{% set g_fleet = ... %}`): у механика половина групп пуста, и подпись
над пустотой выглядела бы поломкой. Активный пункт определяется по
`request.url.path.startswith(...)`. На телефоне меню открывается чекбоксом `#navtoggle`,
разметкой, без скрипта.

Партиалы начинаются с подчёркивания: `_list.html` (макросы `th`, `footer`, `views`),
`_summary.html`, `_passport.html`, вкладки `_report_tabs.html`, `_cash_tabs.html`,
`_parts_tabs.html`, `_trackers_tabs.html`. Скриптов во всей панели два: карта (`_map.html`)
и пересчёт суммы периода на шаге выдачи (`issue.html`). Стилей два, оба подключены в
`base.html`: `app/web/static/style.css` (вся вёрстка, цвета переменными `:root`, тёмная тема
через `prefers-color-scheme`) и `app/web/static/fonts.css` (локальные `@font-face`, шрифты
лежат рядом в `static/fonts/` - панель не ходит за ними в интернет).

Оба подключены с меткой сборки: `/static/style.css?v={{ static_v }}`, где `static_v` -
восемь знаков от имён, размеров и времени правки файлов `static/`, посчитанные один раз
на старте (`static_stamp` в `app/web/app.py`). Новый файл в `static/` метку меняет сам.
Голый `StaticFiles` не шлёт `Cache-Control` вовсе, браузер решает сам - и держал вчерашний
`style.css` часами: новая разметка ехала по чужим правилам, а лечилось это только
Ctrl+Shift+R. Обёртка `CachedStatic` даёт год тем адресам, где метка есть, и `no-cache`
остальным: на шрифты ссылается сам `fonts.css` без метки, и подменённый файл должен
подхватываться сразу. Страницы панели отдаются с `no-store` - на них баланс и телефон
клиента, и кнопка «назад» после выхода не должна доставать их из памяти браузера.

В самой таблице стилей есть правило `[hidden]{display:none !important}` - оно стоит ДО
`input,select,textarea{display:block}`, потому что авторский стиль сильнее браузерного
`[hidden]`, и технический чекбокс бокового меню иначе вылезает полем во всю ширину.

### Новый раздел, по шагам

Пример сквозной и выдуманный: раздел «Снаряжение», код `gear`. Файла
`app/web/templates/gear.html` в проекте нет - он появится на шаге 5.

1. Право. В `app/crm/logic.py` добавить код в `SECTIONS` и строку в `SECTION_PATHS`, префикс
   без завершающего слэша: `("/gear", "gear")`.
2. Владелец. Ничего дописывать не нужно: профиль `owner` в `BUILT_IN_PROFILES` описан как
   `dict.fromkeys(SECTIONS, "edit")`, и новый раздел попадает в него сам. Встроенный профиль
   подтягивается из схемы при каждом старте, и без этой правки раздел закрыт даже владельцу.
3. Маршрут в `app/web/app.py`, новой секцией с линейкой-комментарием:

```python
GEAR_SORTS = {"title": "title", "qty": "qty"}

@app.get("/gear")
async def gear(request: Request) -> Response:
    rows = await crm.gear(limit=10000)          # запрос живёт в CrmDB
    tools = list_tools(request, rows, allowed=GEAR_SORTS)
    return render(request, "gear.html", rows=tools["rows"], tools=tools,
                  views=await views_of(request, "/gear"))
```

Право здесь не проверяется: за GET и за POST уже ответил страж. Проверить `may_edit`
придётся только в GET, который показывает форму правки.

4. Выгрузка, тем же фильтром, что и экран:

```python
@app.get("/gear.{ext}")
async def gear_table(request: Request, ext: str) -> Response:
    rows = await crm.gear(limit=10000)
    return _table(ext, "gear", ["Название", "Штук"],
                  [[g["title"], g["qty"]] for g in rows])
```

5. Шаблон `app/web/templates/gear.html`: `{% extends "base.html" %}`, `{% import
   "_list.html" as list %}`, заголовки через `list.th('Название', 'title', tools)`, подвал
   `list.footer(tools, '/gear', N)`, фильтры `list.views(views, '/gear', tools, staff)`.
6. Пункт меню в `base.html`, внутрь подходящей группы, под `{% if can_view(staff, 'gear')
   %}`, и код раздела в условие заголовка группы.
7. Списки файлов. Новый шаблон дописать в `$webTemplates` в `deploy.ps1`, иначе `python3
   consistency.py` упадёт: он сверяет состав проекта со списком заливки.

### Проверка

```
python3 -m unittest discover -s tests -q
ruff check .
python3 consistency.py
```

Панель тестируется через `fastapi.testclient.TestClient` поверх `tests/fake_crm.py` (база в
памяти), обвязка в `tests/test_web.py`. Права лежат в `tests/test_access.py`, списки,
сортировка и выгрузки в `tests/test_list_tools.py`.

## Фоновые процессы и расписание

Все фоновые циклы живут в процессе бота (`app/main.py`, контейнер `bot`, `CMD ["python",
"-m", "app.main"]`). В веб-процессе (`python -m app.web`) фоновых задач нет ни одной: панель
в интернет не ходит, а контейнеров панели может быть несколько, и каждый опрашивал бы банк
об одном и том же. Процесс MAX (`app/max_main.py`) держит только ретеншен, дневного прохода
там нет намеренно: начисления должен делать ровно один процесс.

### Какие циклы существуют

Задачи заводятся в `run()` в `app/main.py` через `asyncio.create_task` и отменяются там же в
`finally` (`cancel()` плюс общий `gather(..., return_exceptions=True)`).

| Цикл | Файл | Период | Что делает |
|---|---|---|---|
| `retention_loop` | `app/tasks.py` | `INTERVAL_SECONDS`, 6 часов | `purge_once`: удаляет сканы по сроку, чистит журнал апдейтов на `cfg.updates_log_days` |
| `reminders_loop` | `app/tasks.py` | `REMIND_INTERVAL_SECONDS`, 15 минут | Напоминания бота о сроке, сводка в служебный чат, акты выкупа, и на каждом круге зовёт `billing.run_daily` |
| `tracking_loop` | `app/crm/tracking.py` | `cfg.starline_poll_seconds`, 300 с | `poll_once`: относит команды блокировки в StarLine, пишет состояния трекеров, поднимает новые тревоги и закрывает исчезнувшие |
| `banking_loop` | `app/crm/banking.py` | `cfg.tochka_poll_seconds`, 1800 с | `import_once`: выписка Точки за `STATEMENT_DAYS` = 3 дня плюс `auto_credit`, если владелец его включил |
| `paying_loop` | `app/crm/paying.py` | `POLL_SECONDS`, 60 с | Опрашивает открытые счета эквайринга, закрывает просроченные, раз в сутки делает автосписание |
| `mailing_loop` | `app/crm/mailing.py` | `POLL_SECONDS`, 20 с | Берёт кампании в статусе «отправляется» и шлёт порцию `BATCH` = 50 сообщений |

Три цикла выходят сразу, если интеграция не настроена: `tracking_loop` и `banking_loop`
проверяют `client.ready`, `paying_loop` проверяет `acquiring.token`. Задача при этом просто
завершается, и `cancel()` в `finally` ей не вредит.

### Дневной проход

Суточной работой занимается `reminders_loop`, а не отдельный планировщик: круг в 15 минут и
так есть, а cron внутри контейнера означал бы второе подключение к базе и второй экземпляр
логики.

Внутри круга две независимые памяти и два разных часа:

- `bot_done_on`, одна отметка на сутки для напоминаний самого бота. Час берётся из
  `cfg.remind_hour_utc` (умолчание 7) и сравнивается с `datetime.now(UTC)` в
  `tasks.due_today`.
- `crm_done`, словарь «код уведомления, дата» для `billing.run_daily`. Туда передаётся
  местное время (`datetime.now()`, контейнеры живут в Europe/Moscow), потому что часы
  уведомлений владелец задаёт в панели по-московски.

`due_today` и `logic.notice_due` проверяют не «ровно в этот час», а «в этот час или позже,
если сегодня ещё не делали»: бота перезапускают среди дня, и привязка к минуте молча съедала
бы сутки напоминаний.

Память лежит в переменных цикла, перезапуск её обнуляет. Повтор защищён там, где он дорог:
начисление упирается в уникальный индекс `ledger_charge_period_idx` (`schema.sql`),
напоминание клиенту отсекает `crm.mark_notified` (поле `rentals.notified_on`), просьба об
отзыве отсекается `rentals.review_asked_at`, приглашение на ТО отсекается историей
`crm.notice_log`. Сводка после перезапуска может уйти второй раз, и это дешевле потерянной.

### Расписание уведомлений

Каталог уведомлений в коде, `logic.NOTICES` в `app/crm/logic.py`. Строка каталога описывает
группу, адресата, час и подсказку; необязательный `params` задаёт белый список числовых
параметров.

В таблице `crm.notices` лежат только правки владельца. `logic.notice_settings` кладёт их
поверх каталога, поэтому строки в базе может не быть вовсе, а чужие ключи в `extra`
отбрасываются по белому списку каталога. Отсюда же следует главное для разработчика: новое
уведомление появляется в панели `/notices` без единой правки схемы и шаблона.

`hour: None` означает «сразу по событию». Такое уведомление расписание не ловит:
`notice_due` при `at_hour is None` возвращает `False`, а панель не даёт перенести его на час
(`notice_save` в `app/web/app.py` читает `at_hour` из формы только если он есть в умолчаниях
каталога).

Ворота отправки, `app/crm/notices.py`:

```python
notices.due(state, code, now, done) -> bool   # включено и пора
notices.mark(done, code, today) -> None       # память прохода
notices.send_client(crm, code, client_id, sender) -> bool
notices.send_team(crm, bot, code, text, default_chat, *, reply_markup=None, client_id=None) -> bool
notices.record(crm, code, *, status, client_id=None, detail=None) -> None
```

Доставку они на себя не берут: у каждого уведомления свой текст, язык и клавиатура, и сбор
их в одном месте переписал бы сюда половину `app/crm/notify.py`. `send_team` сам разбирает,
куда слать: переопределение владельца (`chat_for`) или служебный чат.

### Когда ставится отметка

Правило разное для денег и для сообщений, и это осознанно.

| Что | Где | Когда ставится | Почему так |
|---|---|---|---|
| `charge` | `billing.run_daily` | После удачного `service.charge_all` | Сбой базы в начисленный час иначе оставил бы парк без начислений до завтра; повтор безвреден, период защищён индексом |
| Уведомления по расписанию | `billing.run_daily` | До работы, сразу после `due(code)` | Неудачная отправка иначе повторялась бы каждые 15 минут до полуночи |
| Напоминание конкретной аренде | `billing.remind_once` | До отправки, `crm.mark_notified` | Заблокировавший бота клиент не должен дёргать систему весь день |
| Выключенное напоминание | `billing.remind_once` | Всё равно помечается отправленным | Иначе обратное включение тумблера обрушит на клиента всё накопленное |
| `charged_on` автосписания | `paying.paying_loop` | В `finally` | Одна попытка в сутки при любом исходе: до прохода нельзя (сбой отменит списание молча), не ставить вовсе тоже нельзя |
| `bot_done_on` | `tasks.reminders_loop` | После удачного прохода | Сбой Telegram в назначенный час иначе стоил бы клиентам суток молчания |

### Как сбой одного круга не ломает остальные

У каждого цикла свой `while True` с `try / except Exception` и `log.exception`;
`asyncio.CancelledError` обязательно пробрасывается наружу, иначе задача переживёт
`cancel()` при остановке и `main.run` зависнет в `gather`. Скопируйте этот порядок в любой
новый цикл, он одинаков во всех шести.

Почти каждый шаг `run_daily` обёрнут в свой `try`, поэтому не собравшаяся сводка по розыску
не отменяет проверку расхождений и наоборот. Исключений два, и оба обрывают остаток прохода
`return`: после `log.exception("CRM: проход напоминаний не удался")` и после
`log.exception("сводка по оплатам не доставлена")` в блоке `daily_digest`. Отметки
прерванных шагов при этом не поставлены, и они уйдут на следующем круге, через 15 минут, но
в тот же день. Если правите этот блок, знайте, что это отклонение от общего правила, а не
опечатка соседних строк.

Запись в историю отправок падать не даёт: `notices.record` глушит ошибку, потому что
сообщение клиенту уже ушло. Чтение настроек тоже: `notices.settings` при сбое возвращает
умолчания каталога, то есть «включено».

### Как добавить новое уведомление

1. Строка в `logic.NOTICES` (`app/crm/logic.py`). Схему не трогаем.

```python
"battery_low": {
    "group": "team", "target": "chat", "hour": 10,
    "title": "Свободных АКБ меньше нормы",
    "hint": "Выдавать станет нечего раньше, чем приедет закупка.",
    "params": {"min_free": 5},
},
```

2. Текст. Клиенту - функцией в `app/crm/notify.py` рядом с `maintenance_invite` и
   `review_ask`: текст собирается из `texts`, клавиатура своя, ошибка доставки возвращается
   `False`, а не исключением. Команде - обычной строкой на месте.

3. Отправка. Событийное (`hour: None`) зовётся в момент события:

```python
await notices.send_client(crm, "battery_low", client_id,
                          lambda: notify.battery_low(bot, rental))
```

По расписанию, блоком в `billing.run_daily` рядом с соседями:

```python
if due("battery_low"):
    notices.mark(done, "battery_low", today)
    try:
        left = await report_battery_low(bot, crm, cfg, chat_id=chat("battery_low"))
        if left:
            log.info("CRM: свободных АКБ осталось %s", left)
    except Exception:                                # noqa: BLE001
        log.exception("CRM: сводка по АКБ не собрана")
```

Своя функция отчёта обязана молчать, когда сообщать нечего: ежедневное «всё в порядке»
перестают читать через неделю, а вместе с ним и то, ради чего сообщение есть. Так сделаны
`report_search`, `report_integrity` и `report_silent_estimates`.

4. Параметр из `params` читается через `logic.notice_param(state.get("battery_low"),
   "min_free", 5)`, мусор в базе откатывается к умолчанию каталога.

5. Тест. Расписание проверяется в `tests/test_schedule.py` (проход гоняется с часами и
   памятью, как на сервере), тумблер и история в `tests/test_notices.py`:

```
python3 -m unittest tests.test_schedule tests.test_notices -q
```

Чего делать не надо: заводить цикл в `app/web/app.py`, звать `billing.run_daily` из панели
(сейчас его зовёт только `tasks.reminders_loop`, ручной режим `done=None` оставлен тестам) и
слать командное уведомление прямым `bot.send_message` мимо `notices.send_team`, иначе
назначенный владельцем получатель перестанет что-либо значить.

## Внешние сервисы

Всё, что ходит наружу, собрано в `app/services/` и `app/max/client.py`. Остальной код
получает разобранные словари и про сеть не знает: так разбор ответа проверяется тестом без
интернета, а замена сервиса трогает один файл.

### Граница «панель в интернет не ходит»

Панель импортирует из `app/services` ровно два модуля (`app/web/app.py`): `contract` (сборка
docx, сети не касается) и `tochka`. StarLine в коде панели нет вовсе (в шаблонах он
встречается только словом). Наружу панель ходит в две стороны: в Telegram - уведомлениями
клиенту и `bot.get_me()` для ссылки-приглашения, и в банк - функцией `acquiring()` в
`app/web/app.py`, которая создаёт `TochkaClient` на один запрос по кнопке оператора: ссылку
на оплату просят при клиенте, и ждать круга опроса оператору негде. Все фоновые опросы живут
в процессе бота, там уже есть расписание и бот для сообщений, а веб-процессов может быть
несколько.

| Опрос | Файл | Период по умолчанию | Клиент |
|---|---|---|---|
| Трекеры и очередь команд | `app/crm/tracking.py` | 300 с (`STARLINE_POLL_SECONDS`) | `StarlineClient` |
| Выписка банка | `app/crm/banking.py` | 1800 с (`TOCHKA_POLL_SECONDS`) | `TochkaClient` |
| Счета эквайринга и автосписание | `app/crm/paying.py` | 60 с (константа `POLL_SECONDS`) | тот же `TochkaClient` |
| Рассылки | `app/crm/mailing.py` | свой цикл | `MaxClient` или `None` |

Все четыре цикла собираются в `app/main.py` и при ненастроенном сервисе выходят сразу,
записав строку в лог: `tracking_loop` и `banking_loop` смотрят на `client.ready`,
`paying_loop` на `acquiring.token`. Ненастроенный сервис ничего не ломает, остальная система
работает как раньше.

### StarLine, `app/services/starline.py`

Позиции велосипедов и одна команда устройству. Авторизация в четыре шага
(`application/getCode`, `application/getToken`, `user/login`, `auth.slid`), дальше все
запросы идут с cookie `slnet`. Токены кэшируются в самом клиенте: `APP_TOKEN_TTL` 3 часа,
`USER_TOKEN_TTL` 20 часов при заявленных 4 и 24, запас на то, чтобы протухший токен не стоил
лишнего круга авторизации на каждом опросе.

Ошибка приходит не HTTP-статусом, а телом с `state`, поэтому конверт разбирает общий
`check_state`: без него «не авторизован» выглядел бы как пустой список устройств. `_call`
повторяет запрос ровно один раз при 401 и 403, сбросив cookie: дальше это уже неверный
пароль, а не протухание.

`parse_device` переименовывает координаты сразу, и порядок зависит от версии ответа: опрос
читает v2 `user_info`, где `x` это широта, `y` долгота (так их берёт Home Assistant); у v3
`/data` (есть блок `common`) наоборот. Перепутать их значит увезти Казань в Казахстан.
Тревога - `car_state.alarm` и флаги `car_alr_state` (v3: `alarm_state`) без `ts` и без
`hijack`: «антиограбление» это наша же блокировка мотора. Координаты «0, 0» - это
«спутников нет»: точка не пишется ни в карточку, ни в журнал, а `track_line` её
пропускает. Плитка «на связи» и тревога «молчит» считаются только по порогу
`tracker_offline_hours` от более позднего из точки и `ts_activity`
(`logic.tracker_seen_at`): в подвале связь есть, спутников нет. Поле `online`
(`status == 1`) `parse_device` отдаёт, но CRM его не хранит. Расшаренные на кабинет
трекеры (`shared_devices`) входят в тот же список. Блокировка мотора: `block_motor` вызывает
`set_param` с параметром `hijack` (`BLOCK_PARAM`), ответ v1 не конверт `state/desc`, а
`{"code": 200}`, всё остальное отказ и его текст уходит оператору как есть.

Настройка: `STARLINE_APP_ID`, `STARLINE_LOGIN` в `.env`, секрет приложения и пароль файлами
(`STARLINE_APP_SECRET_FILE`, `STARLINE_PASSWORD_FILE`, то есть `secrets/starline_app_secret`
и `secrets/starline_password`). Пусто: `ready` даёт `False`, опрос не стартует, раздел
«Карта» пуст. Тесты без сети: `tests/test_trackers.py`, `tests/test_block.py` подменяют
`session_factory`.

### Точка Банк, `app/services/tochka.py`

Два разных API под одним токеном: выписка (`open-banking/v1.0`) и эквайринг
(`acquiring/v1.0`). Токен долгоживущий, ходит заголовком `Authorization: Bearer`, отдельной
цепочки авторизации нет.

Выписка берётся в два шага, потому что банк собирает её не мгновенно: `request_statement`
возвращает номер, `read_statement` читает её и отдаёт пару «строки, готовность» - готовность
по статусу банка, а не по числу строк: за выходные операций может не быть вовсе. Ожидание
оставлено вызывающему: `statement()` возвращает `statement_id` наружу, `banking_loop` держит
его в `pending` и следующий круг читает по номеру, не заказывая заново. Заказывать каждый
раз новую и читать её тут же значит не прочитать выписку никогда.

Эквайринг: `payment_link` (ссылка с чеком 54-ФЗ, позиция чека одной строкой в
`receipt_items`), `payment_status`, `charge_saved_card` (блок `Recurrent` с токеном карты),
`ping` для кнопки проверки в панели. `payment_state` сводит десяток формулировок банка к
трём словам `paid`, `dead`, `pending`, сравнивая статус в верхнем регистре без дефисов и
подчёркиваний: банк пишет их по-разному в разных версиях ответа. `_card` достаёт токен для
автосписания, и если банк токена не прислал, автосписания просто не будет.

Настройка: `TOCHKA_TOKEN_FILE` (`secrets/tochka_token`), `TOCHKA_ACCOUNT_ID`,
`TOCHKA_CUSTOMER_CODE`. Для выписки нужны токен и счёт (`ready`), для эквайринга токен и код
клиента. Счетов может быть несколько через запятую (`parse_accounts`): банк принимает один
счёт на запрос, поэтому `banking.import_once` заказывает и читает выписку по каждому, а
номера заказанных выписок держит словарём «счёт → номер» (`pending`). Отказ банка по одному
счёту пишется в лог и не останавливает остальные; отказ по всем - ошибка круга. Корень НУЦ
Минцифры для `enter.tochka.com` - `app/services/tochka_ca.pem`, только в сессиях Точки
(`ssl_context`). Пусто: выписка не тянется, панель не показывает кнопку ссылки (`acquiring()` вернёт
`None`), клиент платит по обычному `PAY_URL`. Эквайринг можно выключить и при настроенном
токене: `acquiring_live()` спрашивает `logic.acquiring_enabled(settings)`, выключатель в
панели, а не удаление токена из окружения. Тесты: `tests/test_paying.py`,
`tests/test_cash.py`.

### Авито, `app/services/avito.py`

Messenger API: токен по `client_credentials` (живёт сутки, держится до `expires_in` минус
две минуты), `self_id` один раз, чаты, сообщения чата, отправка текста. Ключи - основного
аккаунта компании: ключ сотрудника не видит чатов по объявлениям компании. Грабли, на
которых сломаться проще всего:

- просроченный токен у Авито - 403, а не 401: `_call` берёт новый и повторяет ровно раз;
- 402 - это не сбой, а тариф без API сообщений: `inbox_loop` ставит опрос на час, панель
  видит это по `settings.inbox_avito_state` и показывает плашку;
- версии путей разные: чаты v2, сообщения v3 со слэшем на конце, отправка v1;
- список сообщений приходит то списком, то `{"messages": [...]}`;
- тело ответа читается внутри `async with` сессии: после выхода из неё оно недоступно.

Разбор (`parse_chat`, `parse_message`) отделён от сети. Шум - системные сообщения,
автоответы (`flow_id`) и заглушки «перейдите на подписку» - в ленту не попадает. Опрос
(`inbox.avito_once`) перечитывает чат, только когда его последнее сообщение сменилось
(`inbox_threads.ext_cursor`), и первым кругом берёт только сутки (`inbox_avito_since`).
Своё сообщение из приложения Авито пишется исходящим и снимает ожидание.

### Чтение документа, `app/services/ocr.py` и `mrz.py`

Наружу не уходит ничего: tesseract стоит в том же образе (`Dockerfile`), картинка передаётся
ему через stdin, на диск вторым файлом не ложится. `ocr.available()` проверяет наличие
бинарника, и без него модуль молча возвращает `None`.

`ocr._prepared` готовит два варианта: сначала нижние 35 % страницы (`BOTTOM_STRIP`), где на
развороте лежит МЧЗ, потом всю страницу, обе обесцвечены и растянуты до 2000 px
(`TARGET_WIDTH`). Без Pillow остаётся исходная картинка. `read` считает результат похожим на
МЧЗ по наличию `<<`.

`app/services/mrz.py` чистый разбор без сети и без зависимостей: три раскладки (внутренний
паспорт РФ, загранпаспорт, карта), контрольные цифры по весам 7-3-1 ICAO 9303. Контрольные
цифры и есть причина, по которой OCR здесь допустим: неверно прочитанный символ валит сумму,
и поле просто не показывается, а не подставляется тихо неверным.

`ocr.card_line(data, anketa)` собирает одну строку для карточки модератора и зовётся при
сборке карточки, а не при загрузке фото: прочитанное живёт только в подписи и в базу не
попадает вторым экземпляром паспортных данных. Tesseract блокирующий, поэтому уходит в
`asyncio.to_thread`; любая неудача это пустая строка, карточка уходит как раньше. Тест:
`tests/test_mrz.py`.

### MAX, `app/max/`

Зеркало Telegram-бота: логика, тексты, шифрование анкеты и сборка договора общие, своё
только транспорт и обработчики. Вся сеть в `app/max/client.py`, токен подставляется
параметром `access_token` в каждый запрос.

Особенности API, из-за которых код выглядит так: файлы грузятся в два шага (`POST /uploads`,
затем форма на выданный url), и отправка сразу после загрузки отвечает
`attachment.not.ready`, поэтому `MaxClient.send` переигрывает её `NOT_READY_RETRIES` = 5 раз
с паузой в секунду (загрузка при этом - `upload_file`). `download` читает кусками и
обрывается на лимите, иначе «фото» на гигабайт осело бы в памяти до проверки размера.
`is_member` при ошибке API возвращает `False`: недоступность проверки подписки не должна
становиться способом её обойти.

Запуск отдельным контейнером: `docker compose --profile max up -d`, точка входа
`app/max_main.py`, своя база (`MAX_POSTGRES_DB`, по умолчанию `mybike_max`), потому что
идентификаторы пользователей двух мессенджеров живут в разных пространствах.
`CRM_POSTGRES_DB` в `app/max_main.py` это мост в основную базу, чтобы MAX-аккаунт попал в
карточку клиента. Без `MAX_BOT_TOKEN` рассылки создают `max_client = None` (`app/main.py`),
и сообщения MAX-клиентам помечаются пропущенными, а не теряются молча.

### Персональные данные

Анкета шифруется в базе целиком одним текстовым столбцом `bot.users.anketa_enc`, алгоритм
AES-256-GCM (`app/services/crypto.py`). Ключ живёт docker secret `secrets/pdn_key`
(`PDN_KEY_FILE`) и в дамп не попадает, поэтому дамп без файла ключа паспортных данных не
раскрывает. `load_key` принимает base64 и hex по 32 байта: ошибка формата не должна стоить
разбирательства на боевом сервере. Токен начинается с `v1:`, версия формата оставлена на
случай смены алгоритма. `decrypt` при порче или чужом ключе возвращает `{}` и пишет в лог
только тип исключения: у логов нет срока удаления, который есть у анкеты.

Сканы лежат на томе `kycfiles` (`STORAGE_DIR`, по умолчанию `/files/kyc`).
`app/services/files.py`: размер проверяется по `getFile` до чтения в память, каталог
получает права `0700`, файл открывается сразу с `0600` через `os.open(..., O_EXCL)`, чтобы
между созданием и `chmod` не было окна. Расширение задаёт словарь `SLOT_EXT`, а не аргумент:
имя файла перед удалением сверяется с `logic.STORE_FILE_NAME`, и произвольное расширение
означало бы, что ретеншен такой файл не опознает и не удалит никогда. `store` возвращает ещё
и sha256, он нужен не для целостности, а против повторной регистрации того же документа с
другого аккаунта.

Удаление: `tasks.purge_once` раз в шесть часов. `purge_after` ставится через
`db.set_purge_after`: 90 дней после одобрения (`PURGE_APPROVED_DAYS`) и 3 дня после отказа
(`PURGE_REJECTED_DAYS`, `app/handlers/moderation.py`). Строка с подписанным актом приёма и
неподписанным актом возврата не чистится: велосипед у клиента, документы нужны до конца
аренды. Путь перед `unlink` сверяется с `logic.is_safe_store_path`, а если файл не удалился,
ссылка в базе остаётся: иначе он пролежал бы на диске вечно и без следа. `db.clear_files`
обнуляет пути и `anketa_enc`, но сохраняет `doc_sha256` и реквизиты договора.

Тома в `docker-compose.yml`: панель монтирует `kycfiles:/files:ro` и пишет только на свой
`bikefiles`, бот читает `doctemplates:/doctemplates:ro`, а пишет туда панель.

### Документы docx, `app/services/contract.py`

Документ не собирается заново, а заполняется файл юриста: в zip заменяется только
`word/document.xml`, все прочие части копируются байт в байт, поэтому стили, колонтитулы и
нумерация остаются теми же. Подстановка вида `{{ поле }}`, значения экранируются под XML,
неизвестное поле остаётся в документе видимой пометкой `«нет поля X»`: опечатка в шаблоне
должна бросаться в глаза, а не тихо выкидывать реквизит.

```python
build(template_path: Path, ctx: dict, marks: dict[str, bytes] | None) -> tuple[bytes, str]
```

Отпечаток считается по тексту `document.xml` с пустым значением на месте самого поля
`contract_sha256`, иначе проверить его по готовому документу было бы нечем. Хэшировать весь
docx бессмысленно: zip несёт время сборки, и два одинаковых договора дали бы разные хэши.
Подпись и печать вставляются после подсчёта отпечатка (`put_marks`, размеры в `MARK_FIELDS`,
EMU), картинка идёт inline; вместе с ней правятся `word/_rels/document.xml.rels` и
`[Content_Types].xml`, без записи типа png документ не откроется вовсе. Нет картинки,
подстановка просто исчезает.

Откуда берутся подстановки, `_context` в `app/handlers/contract.py`, четыре слоя в таком
порядке:

| Слой | Что даёт |
|---|---|
| `logic.contract_context(data, anketa, number=, today=)` | анкета клиента, номер и дата договора, пустое поле становится прочерком |
| `logic.issue_context(data.get("issue_data"))` | данные выдачи: вин-номера, комплектация, срок, цена |
| `cfg.purge_approved_days` и `signed_at` | срок хранения из конфигурации и момент подписи |
| `company.context(company.snapshot())` | реквизиты организации снимком из `crm.settings`, TTL 300 с |

Дата берётся из момента выдачи, а не из «сегодня»: договор пересобирается при переотправке и
при подписании, и без фиксации даты сохранённый отпечаток перестал бы соответствовать
документу.

Какой шаблон взять, решает `doctemplates.path_for(kind, ours, cfg.doc_dir)`: синхронная
функция по снимку, потому что в момент сборки базы уже нет. Свой шаблон владельца
проверяется `load_template` прямо там, и нечитаемый молча уступает нашему: без договора
клиента оставлять нельзя. Вид `esign` в `logic.DOC_CODE_ONLY`, файлом он не подменяется
никогда. Загружаемый шаблон проверяется до сохранения в `doctemplates.check_upload`: только
`.docx`, до 10 МБ, и хотя бы одна подстановка.

Добавляете поле в шаблон: допишите его в один из слоёв `_context`, иначе в документе
появится `«нет поля»`. Тесты: `tests/test_contract.py`, `tests/test_documents.py`.

## Тесты и проверки

Набор на стандартном `unittest`, без pytest: pytest не значится ни в `requirements.txt`, ни
в `pyproject.toml`, и прогон не должен требовать ничего, кроме Python. 67 файлов
`tests/test_*.py`, 1659 тестов, полный прогон около шести минут.

### Как устроена обвязка

Каждый файл начинается одинаково: кладёт корень проекта в `sys.path`, импортирует тяжёлые
зависимости внутри `try/except ImportError` и выставляет флаг `HAVE_*`, а класс помечает
`@unittest.skipUnless(HAVE_*, "...")`. Смысл в том, что на голом stdlib прогон не падает, а
честно пропускает то, что проверить нечем.

| Вид | Файлы | На чём работает |
|---|---|---|
| чистая логика | `tests/test_logic.py`, `tests/test_crm_logic.py`, `tests/test_i18n.py`, `tests/test_config.py` | только stdlib |
| SQL без базы | `tests/test_sql.py`, `tests/test_crm_sql.py` | записывающий пул, `pglast` |
| панель и бот на заглушке | `tests/test_web.py` и ещё около сорока файлов | `tests/fake_crm.py`, фейковый бот |
| живой Postgres | `tests/test_crm_pg.py`, `tests/test_web_pg.py` | `pgserver`, `asyncpg`, `httpx` |
| скрипты установки | `tests/test_scripts.py` | `bash`, `openssl` |

Общая обвязка панели лежит в `tests/test_web.py`: классы `WebCase`, `FakeBotDB`, `FakeBot`.
Новый тест панели наследует `WebCase`, а не переписывает свой `setUp`.

Два стиля импорта заглушки рабочие оба: `from tests.fake_crm import FakeCrm` (корень в
`sys.path` кладёт сам файл) и `from fake_crm import FakeCrm` (каталог `tests` кладёт
discover). Имена модулей при этом разные, то есть и объекты разные, поэтому в новом файле
берите первый стиль.

### Фейковая база и почему её держат в синхроне

`tests/fake_crm.py` повторяет контракт `app/crm/db.py`: 305 публичных методов там, 305
здесь, имена совпадают ровно. Живого Postgres большинству тестов не нужно, а подъём базы на
каждый тест стоил бы минут прогона.

Чего в заглушке нет по устройству:

- триггеров `crm.log_bike_status` и `crm.log_battery_status`: журнал статусов заглушка пишет
  сама, прямо в теле метода;
- уникальных индексов СУБД: «одна активная аренда на клиента и на велосипед», «одно
  начисление на период» воспроизведены проверками на Python;
- типов Postgres: `numeric`, `timestamptz`, `jsonb` подменены на `Decimal`, `datetime`,
  `dict`;
- транзакций: откатывать нечего, словарь уже изменён.

Автоматической сверки заглушки с настоящим слоем нет. Расхождение ловится только тестами на
pgserver, поэтому новый метод `CrmDB` требует трёх правок сразу: `app/crm/db.py`,
`tests/fake_crm.py` и список `calls()` в `tests/test_crm_sql.py`.

Сид заглушки повторяет `schema.sql`: `_seed_profiles` берёт `crm_logic.BUILT_IN_PROFILES`,
`_seed_work_types` кладёт два вида работ. Профили и виды работ нумеруются отдельными
счётчиками (`_profile_seq`, `_work_type_seq`), иначе они съели бы первые id и клиент из
`seed()` перестал бы быть первым.

### Когда тест обязан идти на живом Postgres

`tests/test_crm_pg.py` поднимает встроенный Postgres пакетом `pgserver` во временном
каталоге, на каждый тест дропает схемы `crm` и `bot` и применяет `schema.sql` дважды подряд:
второй проход и есть проверка идемпотентности.

| Что проверяем | Примеры тестов |
|---|---|
| триггеры | `test_status_log_is_written_by_trigger`, `test_status_log_keeps_the_mileage_on_postgres` |
| частичные уникальные индексы | `test_one_active_rental_per_client_and_bike`, `test_cash_belongs_to_one_shift_of_two` |
| транзакция целиком | `test_rental_and_its_first_charge_are_one_transaction` |
| повторное применение схемы | `test_estimate_columns_survive_reapply`, `test_mileage_column_survives_reapply` |
| сид владельца при повторном старте | `test_disabled_tariff_is_not_resurrected_by_the_seed`, `test_work_price_seed_on_postgres` |
| сутки и месяцы по часовому поясу | `test_bikes_in_status_by_day_on_postgres`, `test_money_by_day_on_postgres` |

Пул открывается с `init=_init_connection` из `app/db.py`: он ставит кодек json/jsonb (без
него asyncpg отдаёт строку, а код ждёт `dict`) и часовой пояс из `TZ`. Тест, который
поднимет пул мимо этой функции, проверит не то, что работает в проде.

`tests/test_web_pg.py` гоняет страницы и формы панели через ASGI на той же настоящей базе.
Он существует ради расхождений между SQL и заглушкой, которые тесты на `FakeCrm` увидеть не
могут.

### SQL без базы

`tests/test_crm_sql.py` подсовывает `CrmDB` записывающий пул, прогоняет методы из `calls()`
и проверяет у пойманного запроса четыре вещи: плейсхолдеры `$1..$N` идут подряд без дыр,
число аргументов равно максимальному номеру, `pglast` разбирает текст как Postgres, каждая
колонка из `колонка = $n` встречается в `schema.sql`. Отдельно проверяется, что
`update_bike(1, evil="x")` поднимает `ValueError`: имя колонки из адреса строки это чужая
строка. `tests/test_sql.py` делает то же для `app/db.py` и белого списка `PATCHABLE`.

### consistency.py

Скрипт сверяет файлы проекта между собой, то есть ровно то, чего не видит ни один тест:
расхождение живёт не в коде, а между кодом и конфигурацией.

1. `docker-compose.yml` против `.env.example`: подставляемая `${ПЕРЕМЕННАЯ}` без строки в
   примере даст в контейнере пустую строку, а лишняя строка в примере не доедет до
   контейнера вовсе.
2. `app/config.py` против compose: прочитанная переменная, которую compose не передаёт, и
   секрет без проброса `ИМЯ_FILE`.
3. Состав проекта против списков в `deploy.ps1`: файл есть, но не заливается, либо в списке
   есть файл, которого нет, и заливка упадёт.
4. Колонки, встреченные в коде, против `schema.sql`.
5. Значения-заглушки в `.env.example` против условий в `bootstrap.sh`: проверка «это ещё
   заглушка» иначе молча не срабатывает.
6. Вызовы `texts.ИМЯ.format(...)`: разбор AST и сверка именованных аргументов с `{полями}`
   шаблона. Шаблон один, а ботов два, и поле, подставленное в Telegram-боте и забытое в MAX,
   падает `KeyError` уже на живом клиенте.

Скрипт печатает список расхождений и возвращает ненулевой код, `deploy.ps1` на этом
останавливает деплой.

### Линтер

```bash
ruff check .
```

Настройки в `pyproject.toml`: длина строки 100, цель `py311`, наборы `E, F, W, I, UP, B,
ASYNC`. Выключены `ASYNC109` и `ASYNC240`, они советуют обёртки trio/anyio, а проект на
чистом asyncio. `E501` снят с `app/i18n/*.py` и `app/faq_i18n.py`: перенос строки перевода
это риск для текста, который читает клиент.

### Что прогоняют перед коммитом

```bash
python3 -m unittest discover -s tests -q        # весь набор, ~6 минут
ruff check .
python3 consistency.py
python3 -m compileall -q app                    # то же, что делает deploy.ps1
```

Отдельные прогоны, пока правка не готова:

```bash
python3 -m unittest tests.test_crm_logic -q          # один файл
python3 -m unittest discover -s tests -k test_money  # по имени теста
```

Тестовых зависимостей в `requirements.txt` нет намеренно: в образ они не едут. Локально
ставятся отдельно, без них `tests/test_crm_pg.py`, `tests/test_web_pg.py` и проверка
синтаксиса SQL просто пропускаются.

```bash
pip install httpx pgserver pglast ruff
```

Новый файл проекта сразу добавляется в списки `deploy.ps1`, иначе `consistency.py` остановит
деплой строкой «файлы есть в проекте, но deploy.ps1 их не заливает». Списки разбиты по
каталогам (`$root`, `$app`, `$i18n`, `$handlers`, `$crm`, `$web`, `$webTemplates`), потому
что scp кладёт файлы в указанный каталог и из общего списка переводы уехали бы в `app/`, а
не в `app/i18n/`.

## Как вносить правки

Порядок один на любую задачу: схема, доступ к базе, логика, экран, заглушка для тестов,
тест, списки заливки. Он выведен из того, как проект проверяет сам себя: тесты сверяют код
со `schema.sql`, а `consistency.py` сверяет файлы между собой.

### Чем проверять

```bash
python3 -m unittest discover -s tests -q      # весь прогон
python3 -m unittest tests.test_web -q         # панель на заглушке базы
ruff check .                                  # линтер, настройки в pyproject.toml
python3 consistency.py                        # расхождения между файлами
```

`consistency.py` ловит то, чего не видит ни один тест: переменную в `docker-compose.yml` без
строки в `.env.example`, секрет без `*_FILE`, файл проекта, которого нет в списках
`deploy.ps1`, вызов `texts.X.format()` с лишним или недостающим полем. `deploy.ps1`
прогоняет тесты, `compileall` и `consistency.py` до копирования и падает на первой же
проверке, поэтому расхождение в списках останавливает деплой целиком.

Схема применяется при каждом старте бота и панели (`app/db.py`, метод `apply_schema`) под
`pg_advisory_xact_lock(7331)`: бот и панель поднимаются одновременно и иначе спорили бы за
одни и те же объекты. Нумерованных миграций нет, `schema.sql` читается сверху вниз, и
выигрывает последнее определение: функция `crm.log_bike_status` описана дважды -
сперва без пробега, ниже с пробегом, - и работает вторая. Новое дописывается в конец
файла.

Живая база в тестах есть: `tests/test_crm_pg.py` и `tests/test_web_pg.py` поднимают
встроенный Postgres через `pgserver` и применяют настоящий `schema.sql`. Без `pgserver` эти
классы молча пропускаются, и правка схемы остаётся непроверенной, поэтому перед изменением
`schema.sql` убедитесь, что они не в скипе.

### Как добавить поле

| Шаг | Файл | Что именно |
|---|---|---|
| 1 | `schema.sql`, конец файла | `alter table crm.bikes add column if not exists <поле> text;` |
| 2 | `app/crm/db.py` | имя поля в нужный `frozenset`: `BIKE_FIELDS`, `CLIENT_FIELDS`, `RENTAL_FIELDS`, `ORDER_FIELDS` и так далее |
| 3 | `app/crm/logic.py` | проверка значения: `check_name`, `check_amount`, `check_date`, `check_code`, `check_note` |
| 4 | `app/web/app.py` | чтение из формы в сборщике полей раздела, например `_bike_fields` |
| 5 | `app/web/templates/*_form.html` | поле ввода и вывод на карточке |
| 6 | `tests/fake_crm.py` | то же поле в заглушке, она повторяет контракт `CrmDB` |
| 7 | `tests/` | тест на новое поведение |

Белый список обязателен: имена колонок подставляются в SQL текстом, и `_set_clause` бросает
`ValueError: недопустимые колонки` на всё, чего в списке нет. Это же проверяет
`tests/test_crm_sql.py::test_unknown_columns_rejected`, а `test_columns_exist_in_schema`
ловит обратную ошибку, когда колонка в коде есть, а в схеме её забыли.

Смена статуса велосипеда пишется триггером, поэтому пробег меняется тем же `update_bike`,
что и статус: триггер берёт `new.mileage_km` из строки, и отдельный апдейт пробега записал
бы в журнал вчерашнее число. Автора передавайте параметром `by=` (в `db.py` это
`set_config('crm.actor', …)` внутри той же транзакции), иначе в журнале останется пусто.

### Как добавить раздел панели

1. `app/crm/logic.py`: код и название в `SECTIONS`, пара «префикс пути, код» в
   `SECTION_PATHS`. Страж один на все маршруты, он живёт в middleware `auth` в
   `app/web/app.py` и зовёт `logic.section_for(path)`; раздел без строки в `SECTION_PATHS`
   открыт любому вошедшему.
2. `schema.sql`, `insert into crm.access_profiles`: добавьте раздел в JSON профиля `owner`
   (и при необходимости в `manager`, `tech`). В коде у владельца `dict.fromkeys(SECTIONS,
   "edit")`, и расхождение двух мест ловит
   `tests/test_crm_pg.py::test_access_profiles_match_the_code`.
3. `app/web/app.py`: обработчик `@app.get("/<путь>")` и `render(request, "<шаблон>.html",
   …)`. Внутри обработчика проверки прав не нужны для страницы раздела, но нужны для
   выгрузок и для чужих разделов: `may_view`, `may_edit`, ответ через `denied(request,
   code)`.
4. Список строк собирайте через `list_tools(request, rows, allowed=XXX_SORTS)`: сортировка
   идёт только по белому списку колонок, подвал считает итог по всему найденному, а не по
   странице.
5. Выгрузка отдельным маршрутом `/<путь>.{ext}` через `_table(ext, stem, header, rows)`: он
   отдаёт xlsx и csv, а на любое другое расширение возвращает 404.
6. `app/web/templates/`: сам шаблон. Новая константа из `logic` в шаблоне не видна, пока она
   не добавлена в `templates.env.globals` внутри `create_app`.
7. `app/web/templates/base.html`: ссылка в меню, обёрнутая в `{% if can_view(staff, '<код>')
   %}`.
8. `deploy.ps1`: каждый новый шаблон в `$webTemplates`, новый модуль в `$crm`, `$web` или
   `$services`, новый тест в `$tests`.

### Как добавить уведомление

Схему трогать не нужно: каталог живёт в коде (`logic.NOTICES`), а в `crm.notices` лежат
только правки владельца, поэтому новая строка каталога появляется в панели сама.

1. `app/crm/logic.py`, словарь `NOTICES`: код, `group` (`client`, `team`, `channel`),
   `target` (`client`, `chat`, `channel`), `hour`, `title`, `hint`, при необходимости
   `params` (белый список ключей `extra`).
2. `hour: None` означает «сразу по событию», такое уведомление отправляется в месте события
   через `notices.send_client(...)` или `notices.send_team(...)`. Уведомление с часом
   подхватывает суточный проход `billing.run_daily`: своя ветка `if due("<код>"):
   notices.mark(done, "<код>", today)`. Час без ветки в проходе не заработает, и тумблер в
   панели не включит ничего.
3. Отправляйте только через `app/crm/notices.py`: там проверка «включено ли» и запись в
   историю, а сбой записи в историю отправку не отменяет.
4. Проверка: `tests/test_notices.py` сверяет, что `logic.notice_settings([])` покрывает весь
   `NOTICES`.

### Как добавить внешний сервис

| Слой | Файл | Правило |
|---|---|---|
| Клиент | `app/services/<имя>.py` | только сеть и разбор ответа, про базу ничего; разбор отдельной чистой функцией (как `parse_device`, `parse_transaction`), чтобы тест шёл без интернета. `aiohttp` импортируется внутри метода: панели он не нужен |
| Опрос | `app/crm/<имя>.py` | круг со своим интервалом, пишет в базу через `CrmDB`, сообщения через `notices.send_team` |
| Запуск | `app/main.py` | `asyncio.create_task(...)` рядом с `tracking`, `banking`, `paying`. Пустые настройки: задача завершается сразу и ничего не делает |
| Настройки | `app/config.py` | `_env`, `_int` для обычных значений, `_secret(..., required=False)` для токена |
| Контейнер | `docker-compose.yml` | `ИМЯ: ${ИМЯ}` в `environment` сервиса bot, файл секрета в верхнем блоке `secrets:`, `ИМЯ_FILE: /run/secrets/...` и код секрета в списке `secrets:` сервиса |
| Пример | `.env.example` | строка `ИМЯ=""`. Переменная без пары ловится `consistency.py` в обе стороны |
| Установка | `bootstrap.sh` | `[ -f secrets/<имя> ] || : > secrets/<имя>`: без файла compose не поднимется, пустой файл читается как «интеграции нет» |
| Заливка | `deploy.ps1` | оба новых модуля в `$services` и `$crm`, тест в `$tests` |

Правило «панель в интернет не ходит» - про фоновые опросы: они живут в процессе бота, а у
панели к новому сервису остаётся в лучшем случае один запрос по кнопке оператора (так
устроен `acquiring()` - ссылку на оплату просят при клиенте). Фоновый круг из трёх
веб-процессов дёргал бы чужое API втройне.

### Ловушки

| Признак | Что произошло |
|---|---|
| `consistency.py`: «файлы есть в проекте, но deploy.ps1 их не заливает» | новый файл не внесли в списки `deploy.ps1` |
| `ValueError: недопустимые колонки` при сохранении формы | колонку добавили в схему, но не в `frozenset` в `app/crm/db.py` |
| `AttributeError` в `tests/test_web.py` | добавили метод в `CrmDB` и не добавили его в `tests/fake_crm.py` |
| Страница открывается сотруднику без прав | не добавили префикс в `logic.SECTION_PATHS`; проверьте и выгрузку, точка в проверке префикса и держит `/clients.csv` внутри раздела |
| `test_access_profiles_match_the_code` красный | новый раздел есть в `logic.SECTIONS`, но не в JSON профиля `owner` в `schema.sql` |
| В шаблоне пусто на месте нового значения | константа `logic` не зарегистрирована в `templates.env.globals` в `create_app` |
| Список не сортируется по новой колонке | её нет в белом списке `XXX_SORTS`, `sort_rows` молча возвращает исходный порядок |
| В `bike_status_log` пробег отстаёт на день, автор пуст | пробег обновили отдельным запросом или не передали `by=` в `update_bike` |
| Панель после обновления развалилась: меню столбцом, поля во всю ширину | браузер держит старую `style.css`. Метка сборки в адресе это лечит; если её нет - вернуть |
| Правка в `schema.sql` не действует | ниже по файлу лежит более позднее `create or replace` того же объекта |
| Выключенная владельцем строка справочника возвращается после перезапуска | сид зашёл через `on conflict do nothing`, а уникальный индекс частичный (`where active`) |
| Клиент вернул велосипед, а батареи числятся у него | закрытие прошло мимо `service.close_rental`: возврат батарей живёт там |
| Наличные сошлись на одной точке и дали недостачу на другой | платёж записан без `shift_id`, а смены на двух точках открыты одновременно |
| MAX-бот падает с `KeyError` на живом клиенте | поле добавили в шаблон `texts.py` и подставили только в Telegram. `consistency.py` разбирает каждый `texts.X.format()` статически, поэтому прогоняйте его до деплоя |
