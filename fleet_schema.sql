-- Схема парка и броней. Идемпотентна, как schema.sql: применяется и на
-- пустой, и на живой базе при каждом старте.
--
-- Применяет её ТОЛЬКО Telegram-бот (app/main.py). У MAX-бота своя база
-- (mybike_max), и парк в ней был бы вторым, несуществующим: техника одна,
-- учёт у неё должен быть один. MAX доберётся до парка через общее API
-- на следующем этапе.
--
-- Справочники (точки, модели, тарифы) сеются из app/faq.py посевом
-- app/fleet/seed.py - insert on conflict do nothing, поэтому правки
-- оператора в базе переживают рестарт. Бэкфилл единиц и аренд из
-- bot.users и bot.events - тоже в seed.py: ему нужны уже посеянные модели.

create schema if not exists fleet;

-- Точки выдачи. Сейчас это константы в faq.py; сюда они попадают посевом
-- и дальше живут своей жизнью - новую точку оператор заводит в базе,
-- не дожидаясь пересборки образа.
create table if not exists fleet.points (
  id         serial primary key,
  title      text        not null unique,
  address    text,
  open_hour  integer     not null default 10,
  close_hour integer     not null default 19,
  is_active  boolean     not null default true,
  created_at timestamptz not null default now()
);
-- Данные из таблицы владельца: координаты для карты и телефон точки.
alter table fleet.points add column if not exists lat   double precision;
alter table fleet.points add column if not exists lon   double precision;
alter table fleet.points add column if not exists phone text;

create table if not exists fleet.models (
  id           serial primary key,
  title        text        not null unique,
  -- Продление после согласованного срока, ₽/сутки. Колонка на модели,
  -- а не константа в коде: в прайсе она сегодня одна на все модели,
  -- но меняться может по-разному.
  extend_price integer     not null default 650,
  is_active    boolean     not null default true,
  created_at   timestamptz not null default now()
);
-- Карточка модели для приложения: фото, характеристики, описание.
-- photo_url - что показывать (локальный /static/... или внешняя ссылка),
-- photo_page - страница фото на Яндекс.Диске как запасной вариант.
alter table fleet.models add column if not exists photo_url   text;
alter table fleet.models add column if not exists photo_page  text;
alter table fleet.models add column if not exists description text;
alter table fleet.models add column if not exists specs       jsonb;

create table if not exists fleet.tariffs (
  id          serial primary key,
  model_id    integer not null references fleet.models (id),
  period_days integer not null,
  price       integer not null,               -- рубли, без копеек
  is_active   boolean not null default true,
  unique (model_id, period_days)
);

-- Конкретная единица техники. Ключ узнавания - вин-номер рамы: по нему
-- единица заводится автоматически при первой выдаче и находится при
-- следующих. model_id и point_id допускают NULL: рама из старой выдачи
-- может прийти с моделью, которой нет в справочнике, - единица важнее
-- заполненности карточки.
create table if not exists fleet.bikes (
  id            serial primary key,
  model_id      integer references fleet.models (id),
  point_id      integer references fleet.points (id),
  vin_frame     text        not null unique,
  vin_motor     text,
  status        text        not null default 'free',  -- free|booked|rented|service|lost
  battery_count integer     not null default 2,
  notes         text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index if not exists bikes_status_idx on fleet.bikes (status);

-- Бронь: удержание конкретной единицы под клиента с таймером. Статус paid
-- появится вместе с эквайрингом; сейчас бронь живёт только удержанием,
-- и просроченная снимается фоновой задачей.
create table if not exists fleet.bookings (
  id              bigserial primary key,
  bike_id         integer     not null references fleet.bikes (id),
  tg_id           bigint,                     -- пусто у телефонной брони
  note            text,                       -- телефон/имя со слов клиента
  status          text        not null default 'held',  -- held|issued|expired|cancelled
  hold_expires_at timestamptz not null,
  created_by      bigint,                     -- оператор, который удержал
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
-- Одна живая бронь на единицу - инвариант уровня базы, а не проверка
-- в коде: две параллельные брони на одну раму - это два клиента,
-- приехавших за одним велосипедом.
create unique index if not exists bookings_active_bike_idx
  on fleet.bookings (bike_id) where status = 'held';
create index if not exists bookings_expiry_idx
  on fleet.bookings (hold_expires_at) where status = 'held';

-- Мини-приложение: бронь знает, когда клиент обещал приехать и откуда
-- она пришла. Колонки добавляются идемпотентно, как миграции в schema.sql.
alter table fleet.bookings add column if not exists pickup_at timestamptz;
alter table fleet.bookings add column if not exists source text not null default 'operator';
-- Одна живая бронь на клиента: два удержания под одного человека - это
-- два велосипеда, снятые с витрины под один визит.
create unique index if not exists bookings_active_tg_idx
  on fleet.bookings (tg_id) where status = 'held' and tg_id is not null;

-- Аренда: от подписанного Акта приёма-передачи до Акта возврата.
-- bike_id допускает NULL у исторических строк: закрытая аренда из старого
-- события bot.events может не знать раму, а история важнее полноты.
create table if not exists fleet.rentals (
  id              bigserial primary key,
  bike_id         integer references fleet.bikes (id),
  tg_id           bigint      not null,
  contract_no     text,
  rent_term       text,                       -- срок дословно, как в договоре
  rent_price      text,
  due_at          date,                       -- разобранный конец срока, если распознан
  opened_at       timestamptz not null default now(),
  closed_at       timestamptz,
  close_notes     text,
  -- Ключ идемпотентности бэкфилла: закрытая аренда, восстановленная из
  -- события rental_closed, помнит его id и второй раз не вставится.
  source_event_id bigint unique
);
-- Одна активная аренда на единицу и одна на клиента - те же инварианты,
-- что и у брони: нарушение любого из них означает две выдачи одной рамы
-- или два велосипеда на руках у одного договора без следа в учёте.
create unique index if not exists rentals_active_bike_idx
  on fleet.rentals (bike_id) where closed_at is null and bike_id is not null;
create unique index if not exists rentals_active_tg_idx
  on fleet.rentals (tg_id) where closed_at is null;
create index if not exists rentals_tg_idx on fleet.rentals (tg_id, opened_at desc);
