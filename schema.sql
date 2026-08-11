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
  -- Чем документ прислан: сжатым фото или файлом. file_id несёт в себе тип,
  -- и sendPhoto с file_id документа Telegram отвергает с 400 - значит знать
  -- это надо в момент отправки, а угадать по самому file_id нельзя.
  doc_is_photo       boolean     not null default true,

  -- Письменное согласие законного представителя: только у арендаторов
  -- 16-17 лет, у взрослых все четыре поля пусты. Хранится и удаляется
  -- по тем же правилам, что скан документа.
  parent_file_id     text,
  parent_path        text,
  parent_sha256      text,
  parent_is_photo    boolean     not null default true,

  oferta_version     text,
  oferta_accepted_at timestamptz,
  pdn_version        text,
  pdn_consent_at     timestamptz,
  -- Ознакомление с Политикой обработки ПДн - отдельный юридический факт,
  -- он фиксируется до согласия и со своей редакцией документа.
  policy_version     text,
  policy_ack_at      timestamptz,

  -- Анкета для договора: паспортные данные, адреса, дополнительные телефоны.
  -- Один столбец с шифротекстом (AES-256-GCM), а не колонка на поле:
  -- открытым текстом эти данные в базе быть не должны, а дамп базы делается
  -- руками и складывается рядом с ней. Ключ - отдельным docker secret.
  anketa_enc         text,

  -- Договор живёт своим циклом: заявка может быть одобрена, а договор
  -- ещё не подписан, и выдавать по нему велосипед нельзя.
  contract_no        text,
  contract_path      text,
  contract_sha256    text,
  contract_status    text        not null default 'none',  -- none|issued|signed
  contract_issued_at timestamptz,
  contract_signed_at timestamptz,

  -- Согласие на обработку ПДн - приложение к договору. Подписывается той же
  -- кнопкой и в тот же момент, что договор (contract_signed_at), поэтому
  -- своего момента подписи у него нет - только файл и отпечаток.
  soglasie_path      text,
  soglasie_sha256    text,

  -- Оплата аренды: между подписанием договора и Актом приёма-передачи.
  -- pay_chat_id/pay_message_id - карточка «ожидание оплаты» в служебном
  -- чате: к ней привязывается «Я оплатил(а)» клиента.
  pay_chat_id        bigint,
  pay_message_id     bigint,
  pay_confirmed_at   timestamptz,

  -- Где лежит карточка модерации. Нужно, чтобы отказ «с указанием ошибок»,
  -- написанный ответом на карточку, нашёл своего пользователя: разбирать
  -- tg_id из текста подписи - значит сломаться от любой правки формулировки.
  mod_chat_id        bigint,
  mod_message_id     bigint,

  -- Где лежит последняя карточка вопроса в поддержку. Отдельно от mod_*:
  -- карточка заявки живёт своей жизнью, и ответ на неё - это отказ,
  -- а ответ на карточку вопроса - сообщение пользователю.
  support_chat_id    bigint,
  support_message_id bigint,

  -- Данные выдачи: вин-номера, комплектация, срок и оплата. Не ПДн, поэтому
  -- открытым jsonb, а не в шифрованной анкете. Заполняет оператор ответом
  -- на приглашение (issue_chat_id/issue_message_id).
  issue_data         jsonb,
  issue_chat_id      bigint,
  issue_message_id   bigint,

  -- Акт приёма-передачи и Акт возврата: файлы, отпечатки, моменты подписи.
  act_in_path        text,
  act_in_sha256      text,
  act_in_signed_at   timestamptz,
  return_data        jsonb,
  return_chat_id     bigint,
  return_message_id  bigint,
  act_out_path       text,
  act_out_sha256     text,
  act_out_signed_at  timestamptz,

  -- Язык ветки частых вопросов. Выбирается первым вопросом ветки
  -- и запоминается; на остальной сценарий бота не влияет.
  faq_lang           text,

  -- Запрос клиента на закрытие аренды: причина с его слов и момент запроса.
  -- Причина попадает в отчёт о закрытии, поэтому хранится, а не только
  -- пересылается оператору.
  close_reason       text,
  close_requested_at timestamptz,

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
alter table bot.users add column if not exists doc_is_photo   boolean not null default true;
alter table bot.users add column if not exists pdn_version    text;
alter table bot.users add column if not exists pdn_consent_at timestamptz;
alter table bot.users add column if not exists purge_after    timestamptz;
alter table bot.users add column if not exists reject_reason  text;
alter table bot.users add column if not exists reviewed_by    bigint;
alter table bot.users add column if not exists reviewed_at    timestamptz;
alter table bot.users add column if not exists rl_window      timestamptz not null default now();
alter table bot.users add column if not exists rl_count       integer     not null default 0;
alter table bot.users add column if not exists anketa_enc         text;
alter table bot.users add column if not exists contract_no        text;
alter table bot.users add column if not exists contract_path      text;
alter table bot.users add column if not exists contract_sha256    text;
alter table bot.users add column if not exists contract_status    text not null default 'none';
alter table bot.users add column if not exists contract_issued_at timestamptz;
alter table bot.users add column if not exists contract_signed_at timestamptz;
alter table bot.users add column if not exists mod_chat_id        bigint;
alter table bot.users add column if not exists mod_message_id     bigint;
alter table bot.users add column if not exists parent_file_id     text;
alter table bot.users add column if not exists parent_path        text;
alter table bot.users add column if not exists parent_sha256      text;
alter table bot.users add column if not exists parent_is_photo    boolean not null default true;
alter table bot.users add column if not exists support_chat_id    bigint;
alter table bot.users add column if not exists support_message_id bigint;
alter table bot.users add column if not exists issue_data         jsonb;
alter table bot.users add column if not exists issue_chat_id      bigint;
alter table bot.users add column if not exists issue_message_id   bigint;
alter table bot.users add column if not exists act_in_path        text;
alter table bot.users add column if not exists act_in_sha256      text;
alter table bot.users add column if not exists act_in_signed_at   timestamptz;
alter table bot.users add column if not exists return_data        jsonb;
alter table bot.users add column if not exists return_chat_id     bigint;
alter table bot.users add column if not exists return_message_id  bigint;
alter table bot.users add column if not exists act_out_path       text;
alter table bot.users add column if not exists act_out_sha256     text;
alter table bot.users add column if not exists act_out_signed_at  timestamptz;
alter table bot.users add column if not exists policy_version     text;
alter table bot.users add column if not exists policy_ack_at      timestamptz;
alter table bot.users add column if not exists soglasie_path      text;
alter table bot.users add column if not exists soglasie_sha256    text;
alter table bot.users add column if not exists pay_chat_id        bigint;
alter table bot.users add column if not exists pay_message_id     bigint;
alter table bot.users add column if not exists pay_confirmed_at   timestamptz;
alter table bot.users add column if not exists close_reason       text;
alter table bot.users add column if not exists close_requested_at timestamptz;
alter table bot.users add column if not exists faq_lang           text;
-- По этим индексам ищется заявка при ответе оператора на приглашения
-- «данные выдачи» и «данные возврата».
create index if not exists users_issue_msg_idx on bot.users (issue_chat_id, issue_message_id)
  where issue_message_id is not null;
create index if not exists users_return_msg_idx on bot.users (return_chat_id, return_message_id)
  where return_message_id is not null;

-- Сквозная нумерация договоров. Последовательность, а не «максимум плюс один»:
-- два одновременных подтверждения иначе получают один и тот же номер, и в двух
-- разных бумажных договорах оказывается одинаковый реквизит.
create sequence if not exists bot.contract_seq as bigint start with 1;

-- Состояние wait_pdn снова живое: это экран ознакомления с Политикой
-- обработки ПДн. Прежняя миграция wait_pdn -> wait_oferta удалена
-- намеренно - она телепортировала бы людей с экрана политики на согласие
-- при каждом рестарте бота, и ознакомление оставалось бы незафиксированным.

-- Шаг селфи убран. Застрявшие в нём переводятся на подтверждение: документ
-- у них уже загружен, а обработчика для wait_selfie больше нет.
update bot.users set state = 'confirm' where state = 'wait_selfie';

-- Анкета для договора появилась позже подтверждения и модерации. Кто дошёл
-- до них на прошлой версии, анкету не заполнял - и упёрся бы в тупик:
-- «Подтверждаю» не соберёт карточку без реквизитов, а одобрение не соберёт
-- договор, и человек останется в pending без единого выхода.
-- Поэтому таких возвращаем на начало анкеты: ФИО и телефон у них уже есть,
-- переспрашивать всё заново незачем. Пустая анкета в состоянии confirm
-- бывает только у них: на новой версии каждый шаг анкеты её заполняет.
update bot.users
   set state = case when full_name is not null and phone is not null
                    then 'wait_birth' else 'wait_fio' end,
       status = 'new'
 where anketa_enc is null
   and state in ('confirm', 'pending')
   and status <> 'approved';

-- Селфи и распознавание убраны целиком. Колонки удаляются, а не оставляются
-- пустыми: мёртвая колонка в схеме - это вопрос «а что это?» на каждом
-- следующем чтении и риск, что кто-то начнёт её заполнять.
-- ВНИМАНИЕ: удаление необратимо. Если в базе есть боевые данные и они нужны,
-- снимите дамп ДО первого запуска этой версии.
alter table bot.users drop column if exists selfie_file_id;
alter table bot.users drop column if exists selfie_path;
alter table bot.users drop column if exists selfie_sha256;
alter table bot.users drop column if exists doc_ocr;
alter table bot.users drop column if exists name_match;
alter table bot.users drop column if exists ocr_at;

create index if not exists users_state_idx    on bot.users (state);
create index if not exists users_status_idx   on bot.users (status);
create index if not exists users_purge_idx    on bot.users (purge_after) where purge_after is not null;
create index if not exists users_doc_hash_idx on bot.users (doc_sha256)  where doc_sha256 is not null;
-- По этому индексу ищется пользователь при ответе на карточку модерации.
create index if not exists users_mod_msg_idx on bot.users (mod_chat_id, mod_message_id)
  where mod_message_id is not null;
-- А по этому - при ответе на карточку вопроса в поддержку.
create index if not exists users_support_msg_idx on bot.users (support_chat_id, support_message_id)
  where support_message_id is not null;

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
