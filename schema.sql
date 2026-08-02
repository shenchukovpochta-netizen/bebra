-- Схема бота. Идемпотентна: применяется и на пустой, и на живой базе.

create schema if not exists bot;

create table if not exists bot.users (
  tg_id              bigint primary key,
  username           text,
  state              text        not null default 'new',
  full_name          text,
  phone              text,

  doc_file_id        text,
  doc_path           text,
  doc_sha256         text,
  selfie_file_id     text,
  selfie_path        text,
  selfie_sha256      text,

  -- OCR: сырой текст документа не храним, только структурные поля и долю
  -- совпадения ФИО. Живёт до purge_after вместе со сканами.
  doc_ocr            jsonb,
  name_match         numeric(3,2),
  ocr_at             timestamptz,

  oferta_version     text,
  oferta_accepted_at timestamptz,
  pdn_version        text,
  pdn_consent_at     timestamptz,

  status             text        not null default 'new',  -- new|pending|approved|rejected
  reject_reason      text,
  reviewed_by        bigint,
  reviewed_at        timestamptz,
  purge_after        timestamptz,

  rl_window          timestamptz not null default now(),
  rl_count           integer     not null default 0,

  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

-- Миграции для уже развёрнутой базы: `create table if not exists` на живой
-- таблице не добавит новых колонок, и обновление молча выехало бы с половиной
-- полей. Повторный прогон безвреден.
alter table bot.users add column if not exists doc_path       text;
alter table bot.users add column if not exists doc_sha256     text;
alter table bot.users add column if not exists selfie_path    text;
alter table bot.users add column if not exists selfie_sha256  text;
alter table bot.users add column if not exists doc_ocr        jsonb;
alter table bot.users add column if not exists name_match     numeric(3,2);
alter table bot.users add column if not exists ocr_at         timestamptz;
alter table bot.users add column if not exists pdn_version    text;
alter table bot.users add column if not exists pdn_consent_at timestamptz;
alter table bot.users add column if not exists purge_after    timestamptz;
alter table bot.users add column if not exists reject_reason  text;
alter table bot.users add column if not exists reviewed_by    bigint;
alter table bot.users add column if not exists reviewed_at    timestamptz;
alter table bot.users add column if not exists rl_window      timestamptz not null default now();
alter table bot.users add column if not exists rl_count       integer     not null default 0;

-- Согласие на обработку данных переехало внутрь оферты, состояние wait_pdn
-- больше не обрабатывается. Без этой строки застрявшие в нём пользователи
-- не получили бы ни одного обработчика: их сообщения проваливались бы
-- в меню, а выйти можно было бы только угадав /start.
update bot.users set state = 'wait_oferta' where state = 'wait_pdn';

create index if not exists users_state_idx    on bot.users (state);
create index if not exists users_status_idx   on bot.users (status);
create index if not exists users_purge_idx    on bot.users (purge_after) where purge_after is not null;
create index if not exists users_doc_hash_idx on bot.users (doc_sha256)  where doc_sha256 is not null;

-- Журнал апдейтов как двухфазный клейм: processing -> done.
-- Упавшая обработка оставляет запись в processing, и повторная доставка
-- того же update_id переигрывает его, а не выбрасывает.
create table if not exists bot.updates_log (
  update_id  bigint primary key,
  tg_id      bigint,
  kind       text,
  state      text        not null default 'processing',
  payload    jsonb,       -- без телефона, ФИО и текстов: только структура
  created_at timestamptz not null default now()
);
alter table bot.updates_log add column if not exists state text not null default 'processing';
create index if not exists updates_log_created_idx on bot.updates_log (created_at desc);

create table if not exists bot.events (
  id         bigserial primary key,
  tg_id      bigint,
  type       text not null,
  payload    jsonb,
  created_at timestamptz not null default now()
);
create index if not exists events_tg_idx on bot.events (tg_id, created_at desc);
