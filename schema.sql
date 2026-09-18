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

  -- Вторая фотография документа: разворот с пропиской у паспорта,
  -- обратная сторона у прав. Необязательная - у клиента, приславшего
  -- одну, все четыре поля пусты. Хэш второй фотографии на дубли
  -- не проверяется: страница прописки у однофамильцев из одного дома
  -- совпадает законно, и это не фрод.
  doc2_file_id       text,
  doc2_path          text,
  doc2_sha256        text,
  doc2_is_photo      boolean     not null default true,

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

  -- Язык всего диалога с клиентом (выбирается первым вопросом /start).
  -- faq_lang - прежняя колонка только для ветки вопросов, оставлена ради
  -- бэкфилла и старых строк; код читает и пишет lang.
  lang               text,
  faq_lang           text,

  -- Сроки аренды. rent_until вычисляется из строки срока в данных выдачи
  -- («03.08 - 10.08») - по ней бот напоминает об окончании и считает
  -- просрочку. Отметки напоминаний хранятся, чтобы не слать их по кругу
  -- при каждом проходе; extend_until - дата из принятой заявки на
  -- продление, ждущая оплаты.
  rent_from          date,
  rent_until         date,
  extend_until       date,
  extend_chat_id     bigint,
  extend_message_id  bigint,
  remind_soon_at     timestamptz,
  remind_last_at     timestamptz,
  remind_overdue_at  timestamptz,

  -- Аренда с правом выкупа. Условия (сумма и число платежей) лежат
  -- в issue_data вместе с остальными данными выдачи; здесь - начало
  -- графика и следы самого перехода собственности. buyout_days -
  -- платежи, накопленные ПРОШЛЫМИ арендами: между двумя арендами клиент
  -- за велосипед не платит, и эти дни в график не идут, поэтому при
  -- новой выдаче накопленное замораживается числом, а buyout_from
  -- начинает отсчёт заново.
  buyout_from        date,
  buyout_days        integer not null default 0,
  buyout_done_at     timestamptz,
  buyout_path        text,
  buyout_sha256      text,
  buyout_signed_at   timestamptz,

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
alter table bot.users add column if not exists doc2_file_id   text;
alter table bot.users add column if not exists doc2_path      text;
alter table bot.users add column if not exists doc2_sha256    text;
alter table bot.users add column if not exists doc2_is_photo  boolean not null default true;
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
alter table bot.users add column if not exists lang               text;
alter table bot.users add column if not exists rent_from          date;
alter table bot.users add column if not exists rent_until         date;
alter table bot.users add column if not exists extend_until       date;
alter table bot.users add column if not exists extend_chat_id     bigint;
alter table bot.users add column if not exists extend_message_id  bigint;
alter table bot.users add column if not exists remind_soon_at     timestamptz;
alter table bot.users add column if not exists remind_last_at     timestamptz;
alter table bot.users add column if not exists remind_overdue_at  timestamptz;
alter table bot.users add column if not exists buyout_from        date;
alter table bot.users add column if not exists buyout_days        integer not null default 0;
alter table bot.users add column if not exists buyout_done_at     timestamptz;
alter table bot.users add column if not exists buyout_path        text;
alter table bot.users add column if not exists buyout_sha256      text;
alter table bot.users add column if not exists buyout_signed_at   timestamptz;
-- Бэкфилл: язык, выбранный раньше в ветке вопросов, становится языком
-- всего диалога. Идемпотентно: после первого прогона обновлять нечего.
update bot.users set lang = faq_lang where lang is null and faq_lang is not null;
-- Напоминания об окончании срока идут по этому индексу: активных аренд
-- со сроком заметно меньше, чем строк в таблице.
create index if not exists users_rent_until_idx on bot.users (rent_until)
  where rent_until is not null and act_out_signed_at is null;

-- По этим индексам ищется заявка при ответе оператора на приглашения
-- «данные выдачи» и «данные возврата».
create index if not exists users_issue_msg_idx on bot.users (issue_chat_id, issue_message_id)
  where issue_message_id is not null;
create index if not exists users_extend_msg_idx on bot.users (extend_chat_id, extend_message_id)
  where extend_message_id is not null;
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


-- ═══════════════════════════════ CRM ═══════════════════════════════
-- Учёт проката: клиенты, парк, тарифы, аренды, деньги. Живёт в своей
-- схеме crm и не трогает bot.users: у бота свой цикл (анкета, договор,
-- акты, ретеншен ПДн), у CRM - свой (баланс, начисления, парк). Связь
-- между ними - телефон и tg_id клиента, и только в одну сторону:
-- бот пишет в CRM, CRM в bot.users ничего не меняет.
--
-- Персональных данных здесь минимум: ФИО, телефон, номер договора.
-- Паспортные данные и адреса остаются в зашифрованной анкете бота.

create schema if not exists crm;

-- Сотрудники веб-панели. Пароль - scrypt (см. crm.logic.hash_password).
create table if not exists crm.staff (
  id             bigserial primary key,
  login          text        not null unique,
  password_hash  text        not null,
  name           text        not null default '',
  role           text        not null default 'manager',   -- admin|manager
  active         boolean     not null default true,
  created_at     timestamptz not null default now()
);

-- Профили доступа: матрица «раздел -> смотреть/менять» плюс отдельные
-- действия (деньги руками, паспортные документы). Матрица лежит одним
-- jsonb, а не таблицей связей: разделов десяток, читается она целиком
-- и всегда вместе с сотрудником.
--
-- «Владелец» встроенный и неизменяемый: профиль, которым можно отобрать
-- у себя же доступ к сотрудникам, запирает панель навсегда.
create table if not exists crm.access_profiles (
  id         bigserial primary key,
  code       text        unique,          -- owner|manager|tech у встроенных
  name       text        not null unique,
  perms      jsonb       not null default '{}'::jsonb,
  built_in   boolean     not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

insert into crm.access_profiles (code, name, perms, built_in) values
  ('owner',   'Владелец', '{"sections":{"dashboard":"edit","issue":"edit","clients":"edit","rentals":"edit","bikes":"edit","batteries":"edit","trackers":"edit","cash":"edit","mailing":"edit","service":"edit","claims":"edit","finance":"edit","tariffs":"edit","reports":"edit","import":"edit","staff":"edit","inventory":"edit","settings":"edit"},"actions":{"money_edit":true,"client_docs":true}}'::jsonb, true),
  ('manager', 'Менеджер', '{"sections":{"dashboard":"view","issue":"edit","clients":"edit","rentals":"edit","bikes":"view","batteries":"view","trackers":"view","cash":"edit","mailing":"view","service":"view","claims":"edit","finance":"view","tariffs":"view","reports":"view","inventory":"view"},"actions":{}}'::jsonb, false),
  ('tech',    'Механик',  '{"sections":{"dashboard":"view","bikes":"edit","batteries":"edit","trackers":"view","service":"edit","rentals":"view","reports":"view","inventory":"edit"},"actions":{}}'::jsonb, false)
on conflict (code) do update set
  -- встроенный профиль всегда подтягивается к коду, остальные - нет:
  -- их матрицу правит владелец, и перезапись затирала бы его настройку.
  perms = case when crm.access_profiles.built_in then excluded.perms
               else crm.access_profiles.perms end,
  built_in = excluded.built_in;

alter table crm.staff add column if not exists profile_id bigint
  references crm.access_profiles (id);

-- Сотрудники, заведённые до профилей: администратор - владелец, остальные -
-- менеджеры. Только там, где профиля ещё нет, поэтому повтор безопасен.
update crm.staff s set profile_id = p.id
  from crm.access_profiles p
 where s.profile_id is null
   and p.code = case when s.role = 'admin' then 'owner' else 'manager' end;

-- Тарифы: цена за период. Аренда копирует цену и период к себе
-- при оформлении, поэтому правка тарифа не меняет уже идущие аренды.
create table if not exists crm.tariffs (
  id             bigserial primary key,
  name           text        not null,
  period_days    integer     not null,
  price          numeric(12,2) not null,
  note           text,
  active         boolean     not null default true,
  sort           integer     not null default 100,
  created_at     timestamptz not null default now()
);

-- Парк. code - инвентарный номер, который написан на раме и по которому
-- велосипед называют в переписке. frame_no - заводской номер рамы: по нему
-- бот находит велосипед из формы выдачи оператора.
create table if not exists crm.bikes (
  id             bigserial primary key,
  code           text        not null unique,
  model          text        not null,
  frame_no       text,
  motor_no       text,
  battery_count  integer     not null default 2,
  -- available|rented|repair|reserved|lost|sold. rented ставит и снимает
  -- сама аренда; остальное - оператор.
  status         text        not null default 'available',
  purchase_price numeric(12,2),
  purchased_on   date,
  note           text,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create unique index if not exists bikes_frame_idx on crm.bikes (frame_no)
  where frame_no is not null;

-- Клиенты. phone - нормализованный (+7XXXXXXXXXX), это ключ связи
-- с ботом: клиент, заведённый руками до регистрации в боте, привяжет
-- свой Telegram, поделившись контактом в /cabinet.
create table if not exists crm.clients (
  id             bigserial primary key,
  full_name      text        not null,
  phone          text        not null unique,
  tg_id          bigint      unique,
  username       text,
  status         text        not null default 'active',   -- active|blocked|blacklist
  contract_no    text,
  note           text,
  source         text        not null default 'manual',   -- manual|bot|import
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

-- Аренды. Одна активная на клиента и одна на велосипед - это не правило
-- бизнеса, а защита от двойного начисления: два оператора, оформившие
-- одну выдачу дважды, иначе списывали бы с клиента вдвое.
--
-- billing: auto - начисление по периодам тарифа (дневной проход),
-- manual - начисления только явные (аренды, заведённые ботом из формы
-- выдачи: срок и цену там задаёт оператор, продление - отдельной формой).
-- billed_until - начало ещё не начисленного периода (исключительно).
create table if not exists crm.rentals (
  id             bigserial primary key,
  client_id      bigint      not null references crm.clients (id),
  bike_id        bigint      references crm.bikes (id),
  tariff_id      bigint      references crm.tariffs (id),
  tariff_name    text        not null,
  period_days    integer     not null,
  price          numeric(12,2) not null,
  billing        text        not null default 'auto',     -- auto|manual
  contract_no    text,
  started_on     date        not null,
  billed_until   date        not null,
  status         text        not null default 'active',   -- active|closed
  closed_on      date,
  close_note     text,
  -- Дедупликация напоминаний: не больше одного в день на аренду.
  notified_on    date,
  notified_kind  text,
  created_by     text,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create unique index if not exists rentals_active_client_idx on crm.rentals (client_id)
  where status = 'active';
create unique index if not exists rentals_active_bike_idx on crm.rentals (bike_id)
  where status = 'active' and bike_id is not null;
create index if not exists rentals_client_idx on crm.rentals (client_id, id desc);

-- Журнал денег. Одна таблица на всё: платежи (+), начисления (-),
-- штрафы и ремонт (-), возвраты (-), корректировки (±). Баланс клиента -
-- сумма amount, и никакой отдельной колонки «баланс», которая может
-- разойтись с журналом.
create table if not exists crm.ledger (
  id             bigserial primary key,
  client_id      bigint      not null references crm.clients (id),
  rental_id      bigint      references crm.rentals (id),
  kind           text        not null,   -- payment|charge|fine|refund|adjust
  amount         numeric(12,2) not null,
  method         text,                   -- sbp|cash|card|transfer|other
  period_from    date,
  period_to      date,
  note           text,
  created_by     text,
  created_at     timestamptz not null default now()
);
create index if not exists ledger_client_idx on crm.ledger (client_id, id desc);
create index if not exists ledger_created_idx on crm.ledger (created_at desc);
-- Одно начисление на период: повторный проход биллинга (рестарт, ручной
-- запуск из панели) упирается в индекс, а не списывает второй раз.
create unique index if not exists ledger_charge_period_idx on crm.ledger (rental_id, period_from)
  where kind = 'charge' and period_from is not null;

-- Заявка «Я оплатил(а)» из кабинета: клиент нажал кнопку, оператор
-- сверил поступление и зачислил. Сумму называет оператор, а не клиент:
-- в банке видно, сколько пришло на самом деле.
create table if not exists crm.payment_claims (
  id               bigserial primary key,
  client_id        bigint      not null references crm.clients (id),
  amount_hint      numeric(12,2),
  receipt_file_id  text,
  receipt_is_photo boolean     not null default true,
  status           text        not null default 'pending',  -- pending|confirmed|rejected
  -- Карточка заявки в служебном чате: к ней привязаны кнопки и ответ суммой.
  card_chat_id     bigint,
  card_message_id  bigint,
  ledger_id        bigint,
  resolved_by      text,
  resolved_at      timestamptz,
  created_at       timestamptz not null default now()
);
create index if not exists claims_pending_idx on crm.payment_claims (status)
  where status = 'pending';
create index if not exists claims_card_idx on crm.payment_claims (card_chat_id, card_message_id)
  where card_message_id is not null;

-- Журнал по велосипеду: смены статуса, ремонты с их стоимостью, заметки.
create table if not exists crm.bike_log (
  id             bigserial primary key,
  bike_id        bigint      not null references crm.bikes (id),
  kind           text        not null,   -- status|repair|note
  note           text,
  cost           numeric(12,2),
  created_by     text,
  created_at     timestamptz not null default now()
);
create index if not exists bike_log_idx on crm.bike_log (bike_id, id desc);

-- ─────────────────────── метрики парка ───────────────────────
-- Три числа, ради которых система существует: операционный парк,
-- % простоя и средний чек в день. Простой задним числом считается только
-- по журналу статусов, поэтому журнал ведёт триггер базы, а не код
-- приложения: код можно забыть, триггер - нет.
--
-- Статусы велосипеда: available|rented|repair|maintenance|reserved|
-- lost|sold|written_off. Операционный парк - первые пять; потерянные,
-- проданные и списанные в знаменатель простоя не попадают никогда.
alter table crm.bikes add column if not exists location text;
-- Амортизация считается честно и раздельно: рама по сроку службы и
-- остаточной стоимости, АКБ - по своей цене и своему сроку (в разы короче).
alter table crm.bikes add column if not exists service_months integer not null default 24;
alter table crm.bikes add column if not exists residual_price numeric(12,2) not null default 0;
alter table crm.bikes add column if not exists battery_price numeric(12,2);
alter table crm.bikes add column if not exists battery_service_months integer not null default 15;

-- Одометр. У велосипеда одно число - текущий пробег; у аренды два -
-- на выдаче и на возврате, чтобы «накатал за аренду» читалось строкой,
-- а не вычиталось по журналу. Пробег вводит оператор глазами с дисплея.
alter table crm.bikes   add column if not exists mileage_km    integer not null default 0;
alter table crm.rentals add column if not exists mileage_start integer;
alter table crm.rentals add column if not exists mileage_end   integer;

-- Сводка оператора: что клиент сказал по телефону про истекающий срок
-- («продлит» / «сдаёт») и до какой даты строку отложили. intent_until -
-- «оплачено до» на момент отметки: сдвинулась дата - намерение устарело.
alter table crm.rentals add column if not exists intent        text;
alter table crm.rentals add column if not exists intent_until  date;
alter table crm.rentals add column if not exists intent_by     text;
alter table crm.rentals add column if not exists intent_at     timestamptz;
alter table crm.rentals add column if not exists snooze_until  date;

create table if not exists crm.bike_status_log (
  id             bigserial primary key,
  bike_id        bigint      not null references crm.bikes (id),
  from_status    text,
  to_status      text        not null,
  changed_at     timestamptz not null default now(),
  -- кто менял: staff:логин, bot, import - если код сообщил через
  -- set_config('crm.actor', ...) в той же транзакции
  changed_by     text
);
create index if not exists bike_status_log_idx on crm.bike_status_log (bike_id, changed_at);

create or replace function crm.log_bike_status() returns trigger
language plpgsql as $$
begin
  if tg_op = 'INSERT' or old.status is distinct from new.status then
    insert into crm.bike_status_log (bike_id, from_status, to_status, changed_at, changed_by)
    values (new.id,
            case when tg_op = 'INSERT' then null else old.status end,
            new.status, now(),
            nullif(current_setting('crm.actor', true), ''));
  end if;
  return new;
end
$$;
drop trigger if exists bikes_status_log on crm.bikes;
create trigger bikes_status_log
  after insert or update of status on crm.bikes
  for each row execute function crm.log_bike_status();

-- Велосипеды, заведённые до появления журнала: история начинается с даты
-- их создания в CRM. Простой за месяцы до этого посчитать нечем - это
-- ограничение импорта из таблицы, и оно честно описано в CRM.md.
insert into crm.bike_status_log (bike_id, from_status, to_status, changed_at)
select b.id, null, b.status, b.created_at
from crm.bikes b
where not exists (select 1 from crm.bike_status_log l where l.bike_id = b.id);

-- ─────────────────────── ремонт по узлам ───────────────────────
-- Справочник узлов фиксированный: свободный ввод не даёт ответить,
-- какая модель дороже в ремонте и какой узел ломается чаще.
-- Расширяется только правкой этого списка (он идемпотентен).
create table if not exists crm.repair_nodes (
  code   text primary key,
  title  text not null,
  sort   integer not null default 0
);
insert into crm.repair_nodes (code, title, sort) values
  ('motor_wheel', 'Мотор-колесо', 10), ('controller', 'Контроллер', 20),
  ('battery', 'АКБ', 30), ('bms', 'BMS', 40), ('charger', 'Зарядное устройство', 50),
  ('brake_pads', 'Тормоза: колодки', 60), ('brake_disc', 'Тормоза: диск', 61),
  ('brake_lever', 'Тормоза: ручка', 62), ('brake_line', 'Тормоза: гидролиния', 63),
  ('frame', 'Рама', 70), ('fork', 'Вилка / амортизация', 71),
  ('headset', 'Рулевая колонка', 72), ('handlebar', 'Руль', 73),
  ('throttle', 'Ручка газа', 74), ('hall_sensors', 'Датчики Холла', 80),
  ('wiring', 'Проводка', 81), ('headlight', 'Фара', 90), ('taillight', 'Задний фонарь', 91),
  ('turn_signals', 'Поворотники', 92), ('horn', 'Сигнал', 93),
  ('wheel_front', 'Колесо переднее', 100), ('wheel_rear', 'Колесо заднее', 101),
  ('tube_tire', 'Камера / покрышка', 102),
  ('fender_front', 'Крыло переднее', 110), ('fender_rear', 'Крыло заднее', 111),
  ('rack', 'Багажник', 112), ('kickstand', 'Подножка', 113), ('saddle', 'Седло', 114),
  ('seatpost', 'Подседельный штырь', 115), ('chain_guard', 'Защита цепи', 116),
  ('mirrors', 'Зеркала', 117), ('phone_holder', 'Держатель телефона', 118),
  ('gps_tracker', 'GPS-трекер', 119), ('other', 'Прочее', 999)
on conflict (code) do update set title = excluded.title, sort = excluded.sort;

-- Позиция ремонта: узел, запчасти, работа. Шапка ремонта - запись
-- crm.bike_log вида repair с общей суммой, как и раньше: старые записи
-- без позиций остаются в отчёте по модели, но не по узлам.
create table if not exists crm.repair_items (
  id             bigserial primary key,
  log_id         bigint      not null references crm.bike_log (id) on delete cascade,
  bike_id        bigint      not null references crm.bikes (id),
  node           text        not null references crm.repair_nodes (code),
  parts_cost     numeric(12,2) not null default 0,
  labor_cost     numeric(12,2) not null default 0,
  note           text,
  created_at     timestamptz not null default now()
);
create index if not exists repair_items_idx on crm.repair_items (bike_id, created_at);
create index if not exists repair_items_node_idx on crm.repair_items (node, created_at);

-- ─────────────────────── сервис: виды работ и наряды ───────────────────────
--
-- Ремонт был только записью факта: «что сделали и сколько стоило». Наряд -
-- это процесс вокруг него: кто взял, на каком этапе, сколько суток стоит
-- и за чей счёт. Отсюда же второе направление бизнеса - ремонт чужой
-- техники: у наряда есть плательщик.
--
-- Деньги за чужой ремонт в crm.ledger НЕ попадают. Журнал - это аренда,
-- и средний чек считается по нему; смешать их значит испортить главную
-- метрику парка. Выручка наряда живёт на самом наряде, а в отчётах стоит
-- отдельным столбцом.

create table if not exists crm.work_types (
  id         bigserial primary key,
  title      text        not null unique,
  category   text        not null default 'Прочее',
  minutes    integer     not null default 0,     -- нормативное время
  price      numeric(12,2) not null default 0,   -- цена клиенту
  node       text        references crm.repair_nodes (code),
  active     boolean     not null default true,
  sort       integer     not null default 100,
  created_at timestamptz not null default now()
);
create index if not exists work_types_idx on crm.work_types (active, sort, title);

-- Наряд. Номер человекочитаемый и сквозной: на него ссылаются в переписке
-- и в чате сервиса, поэтому он не равен id.
create table if not exists crm.work_orders (
  id          bigserial primary key,
  no          text        not null unique,       -- РЕМ-000001
  bike_id     bigint      references crm.bikes (id),
  -- Чужая техника: своего велосипеда в парке нет, есть описание объекта.
  object_note text,
  payer       text        not null default 'own',   -- own|client
  client_id   bigint      references crm.clients (id),
  status      text        not null default 'new',   -- new|in_work|waiting|done|cancelled
  tech_id     bigint      references crm.staff (id),
  complaint   text,                                  -- с чем обратились
  estimate    numeric(12,2) not null default 0,      -- смета, согласована с клиентом
  total       numeric(12,2) not null default 0,      -- к оплате клиенту
  cost        numeric(12,2) not null default 0,      -- себестоимость: запчасти и работа
  paid_at     timestamptz,
  note        text,
  created_by  text,
  opened_at   timestamptz not null default now(),
  closed_at   timestamptz,
  log_id      bigint      references crm.bike_log (id)  -- запись ремонта после закрытия
);
create index if not exists work_orders_open_idx on crm.work_orders (status, opened_at desc);
create index if not exists work_orders_bike_idx on crm.work_orders (bike_id, opened_at desc);
create index if not exists work_orders_tech_idx on crm.work_orders (tech_id, status);
-- Один открытый наряд на велосипед: два параллельных - это два техника,
-- которые не знают друг о друге, и двойная смета клиенту.
create unique index if not exists work_orders_one_open on crm.work_orders (bike_id)
  where bike_id is not null and status in ('new', 'in_work', 'waiting');

create table if not exists crm.work_order_items (
  id           bigserial primary key,
  order_id     bigint      not null references crm.work_orders (id) on delete cascade,
  work_type_id bigint      references crm.work_types (id),
  title        text        not null,               -- копия названия на момент наряда
  node         text        references crm.repair_nodes (code),
  qty          integer     not null default 1,
  price        numeric(12,2) not null default 0,   -- клиенту за единицу
  parts_cost   numeric(12,2) not null default 0,   -- себестоимость запчастей
  labor_cost   numeric(12,2) not null default 0,   -- себестоимость работы
  note         text,
  created_at   timestamptz not null default now()
);
create index if not exists work_order_items_idx on crm.work_order_items (order_id, id);

-- Каталог работ на старте: то, что чинят чаще всего. Цены - заглушка,
-- правятся в панели; название уникально, поэтому повтор безопасен.
insert into crm.work_types (title, category, minutes, price, node, sort) values
  ('Замена АКБ',                  'Электрика', 15, 200, 'battery',      10),
  ('Диагностика электрики',       'Электрика', 45, 600, 'wiring',       20),
  ('Замена дисплея',              'Электрика', 30, 500, 'wiring',       30),
  ('Замена зарядного устройства', 'Электрика', 10, 150, 'charger',      40),
  ('Замена контроллера',          'Электрика', 60, 900, 'controller',   50),
  ('Замена мотор-колеса',         'Электрика', 90, 1500, 'motor_wheel', 60),
  ('Замена камеры',               'Ходовая',   15, 200, 'tube_tire',    70),
  ('Замена покрышки',             'Ходовая',   20, 250, 'tube_tire',    80),
  ('Замена тормозных колодок',    'Тормоза',   20, 300, 'brake_pads',   90),
  ('Прокачка тормозов',           'Тормоза',   30, 400, 'brake_line',  100),
  ('Замена тормозного диска',     'Тормоза',   25, 350, 'brake_disc',  110),
  ('Правка колеса',               'Ходовая',   30, 400, 'wheel_rear',  120),
  ('Замена цепи',                 'Ходовая',   25, 300, 'chain_guard', 130),
  ('Техобслуживание',             'ТО',        60, 700, 'other',       140),
  ('Замена фары',                 'Свет',      15, 200, 'headlight',   150),
  ('Замена подножки',             'Прочее',    10, 150, 'kickstand',   160)
on conflict (title) do nothing;

-- ─────────────────────── пересчёт техники ───────────────────────
--
-- Из ~190 велосипедов ~25 числятся потерянными. Пересчёт - единственный
-- способ узнать это не по памяти: список ожидаемого против того, что
-- нашли руками на точке.
--
-- Ведомость остаётся навсегда: «не нашли» - это не мнение оператора,
-- а документ с датой, точкой и тем, кто считал.

create table if not exists crm.stock_takes (
  id         bigserial primary key,
  no         text        not null unique,        -- ПРТ-000001
  scope      text        not null default 'all', -- all|location
  location   text,
  status     text        not null default 'open', -- open|done
  expected   integer     not null default 0,
  found      integer     not null default 0,
  missing    integer     not null default 0,
  extra      integer     not null default 0,
  note       text,
  created_by text,
  started_at timestamptz not null default now(),
  closed_at  timestamptz
);
create index if not exists stock_takes_idx on crm.stock_takes (started_at desc);
-- Открытая ведомость одна: два пересчёта разом делят парк пополам,
-- и в каждом половина техники оказывается «не найдена».
create unique index if not exists stock_takes_one_open on crm.stock_takes ((status))
  where status = 'open';

-- Строка ведомости. bike_id пуст у «лишних»: нашли то, чего в парке нет,
-- и записать это надо до того, как заведут карточку.
create table if not exists crm.stock_take_items (
  id         bigserial primary key,
  take_id    bigint      not null references crm.stock_takes (id) on delete cascade,
  bike_id    bigint      references crm.bikes (id),
  code       text,                                -- что прочитали на раме
  state      text        not null,                -- expected|found|missing|extra
  note       text,
  created_at timestamptz not null default now()
);
create index if not exists stock_take_items_idx on crm.stock_take_items (take_id, state);
-- Один велосипед в ведомости один раз: дважды отмеченный найденным
-- превратил бы «нашли 220 из 220» в «нашли 221».
create unique index if not exists stock_take_items_one on crm.stock_take_items
  (take_id, bike_id) where bike_id is not null;

-- ─────────────────────── реферальная программа ───────────────────────
--
-- Курьер приводит курьера: это самый дешёвый канал, который у проката
-- вообще есть. Код приглашения выдаётся клиенту в кабинете бота, друг
-- приходит по ссылке t.me/бот?start=<код>, и его путь виден целиком:
-- перешёл → зарегистрировался → взял велосипед → заплатил.
--
-- Бонус агенту - запись в crm.ledger вида adjust, а не payment: платежи
-- клиентов формируют средний чек парка, и бонус его бы завысил.

alter table crm.clients add column if not exists ref_code text;
alter table crm.clients add column if not exists invited_by bigint
  references crm.clients (id);
alter table crm.clients add column if not exists invited_at timestamptz;
create unique index if not exists clients_ref_code_idx on crm.clients (ref_code)
  where ref_code is not null;
create index if not exists clients_invited_by_idx on crm.clients (invited_by);

create table if not exists crm.referrals (
  id         bigserial primary key,
  agent_id   bigint      not null references crm.clients (id),
  -- Друг сначала известен только по Telegram: карточки клиента у него
  -- ещё нет, а переход уже был.
  tg_id      bigint      not null,
  client_id  bigint      references crm.clients (id),
  status     text        not null default 'click',  -- click|signed|rented|paid
  bonus      numeric(12,2) not null default 0,
  ledger_id  bigint      references crm.ledger (id),
  note       text,
  created_at timestamptz not null default now(),
  signed_at  timestamptz,
  rented_at  timestamptz,
  paid_at    timestamptz
);
-- Один переход на человека: перебрал ссылки трёх знакомых - друг остаётся
-- за первым. Иначе бонус за одного и того же друга платился бы трижды.
create unique index if not exists referrals_tg_idx on crm.referrals (tg_id);
create index if not exists referrals_agent_idx on crm.referrals (agent_id, status);

-- Настройки, которые меняет владелец, а не разработчик: размер бонуса и
-- порог оплаты. Ключ-значение, потому что настроек всего несколько и
-- отдельная таблица на каждую была бы дороже пользы.
create table if not exists crm.settings (
  key        text        primary key,
  value      text        not null,
  updated_at timestamptz not null default now(),
  updated_by text
);

-- ─────────────────── сотрудник и его Telegram ───────────────────
--
-- Техник получает свои наряды в Telegram, а не ходит за ними в панель.
-- Привязка самостоятельная: панель показывает одноразовый код, сотрудник
-- отправляет боту «/staff <код>». Код живёт до первого применения -
-- переслать его в общий чат безопаснее, чем пароль.

alter table crm.staff add column if not exists tg_id bigint;
alter table crm.staff add column if not exists tg_username text;
alter table crm.staff add column if not exists link_code text;
alter table crm.staff add column if not exists linked_at timestamptz;
create unique index if not exists staff_tg_idx on crm.staff (tg_id)
  where tg_id is not null;
create unique index if not exists staff_link_code_idx on crm.staff (link_code)
  where link_code is not null;

-- Канал привлечения: откуда клиент про нас узнал. Не то же, что source
-- (manual|bot|import) - тот говорит, каким путём завелась карточка,
-- а канал отвечает на вопрос «куда давать рекламу».
alter table crm.clients add column if not exists channel text;
create index if not exists clients_channel_idx on crm.clients (channel);

-- ───────────────────────────── склад запчастей ─────────────────────────────
--
-- Ремонт до склада считался «с потолка»: механик писал сумму запчастей
-- руками, а сколько их на полке, знал только он. Склад отвечает на два
-- вопроса: что стоит ремонт на самом деле и что пора заказать.
--
-- Запчасть привязана к узлу из crm.repair_nodes, а не к своему дереву
-- категорий: тогда отчёт «что ломается» и остаток на полке смотрят на
-- один справочник, и видно, есть ли запас по тому узлу, который сыплется.
--
-- Остатка колонкой нет намеренно - как и баланса у клиента: остаток есть
-- сумма движений. Иначе первая же гонка двух операторов разведёт колонку
-- и журнал, и верить будет нечему.

create table if not exists crm.suppliers (
  id         bigserial primary key,
  name       text        not null unique,
  phone      text,
  note       text,
  active     boolean     not null default true,
  created_at timestamptz not null default now()
);

create table if not exists crm.parts (
  id         bigserial primary key,
  title      text        not null unique,
  node       text        references crm.repair_nodes (code),
  unit       text        not null default 'шт',
  -- Средняя себестоимость: пересчитывается при каждом приходе.
  -- Цена клиенту - то, во что позиция встаёт в наряде за его счёт.
  cost       numeric(12,2) not null default 0,
  price      numeric(12,2) not null default 0,
  -- Неснижаемый остаток: ниже него позиция попадает в «пора заказать».
  min_stock  integer     not null default 0,
  model      text,                               -- совместимость; пусто - все
  active     boolean     not null default true,
  note       text,
  created_at timestamptz not null default now()
);
create index if not exists parts_node_idx on crm.parts (node);

-- Документ склада: приход ПРХ-000001 или списание СПС-000001.
create table if not exists crm.part_docs (
  id          bigserial primary key,
  no          text        not null unique,
  kind        text        not null,              -- receipt|write_off
  supplier_id bigint      references crm.suppliers (id),
  total       numeric(12,2) not null default 0,
  note        text,
  created_by  text,
  created_at  timestamptz not null default now()
);
create index if not exists part_docs_idx on crm.part_docs (kind, created_at desc);

-- Движение склада. qty со знаком: приход плюс, расход минус.
create table if not exists crm.part_moves (
  id         bigserial primary key,
  part_id    bigint      not null references crm.parts (id),
  -- receipt - приход, order - ушло в наряд, issue - выдали со склада,
  -- write_off - списание, count - правка по факту пересчёта.
  kind       text        not null,
  qty        integer     not null,
  cost       numeric(12,2) not null default 0,   -- себестоимость единицы
  doc_id     bigint      references crm.part_docs (id) on delete cascade,
  order_id   bigint      references crm.work_orders (id),
  note       text,
  created_by text,
  created_at timestamptz not null default now()
);
create index if not exists part_moves_part_idx on crm.part_moves (part_id, created_at desc);
create index if not exists part_moves_order_idx on crm.part_moves (order_id);

-- Заказ запчастей поставщику: ЗАП-000001. Приёмка превращается в приход.
create table if not exists crm.part_orders (
  id          bigserial primary key,
  no          text        not null unique,
  supplier_id bigint      references crm.suppliers (id),
  status      text        not null default 'new',   -- new|ordered|received|cancelled
  total       numeric(12,2) not null default 0,
  note        text,
  created_by  text,
  created_at  timestamptz not null default now(),
  ordered_at  timestamptz,
  closed_at   timestamptz,
  doc_id      bigint      references crm.part_docs (id)
);
create table if not exists crm.part_order_items (
  id         bigserial primary key,
  order_id   bigint      not null references crm.part_orders (id) on delete cascade,
  part_id    bigint      not null references crm.parts (id),
  qty        integer     not null default 1,
  price      numeric(12,2) not null default 0,
  -- Откуда взялась потребность: наряд ждёт запчасть, остаток ниже
  -- неснижаемого или вписали руками. По этому полю видно, кому верить.
  source     text        not null default 'manual',  -- order|min_stock|manual
  work_order_id bigint   references crm.work_orders (id),
  created_at timestamptz not null default now()
);
create unique index if not exists part_order_items_one on crm.part_order_items
  (order_id, part_id);

-- ─────────────────── замена велосипеда внутри аренды ───────────────────
--
-- Велосипед сломался - раньше приходилось закрывать аренду и открывать
-- новую: деньги, даты и договор при этом разъезжались. Замена оставляет
-- аренду той же, а что у клиента на руках, помнит этот журнал.
--
-- Строка открыта, пока велосипед у клиента: returned_on пуст. Пробег
-- пишется по каждой единице отдельно - иначе «накатал» после замены
-- считался бы от одометра чужого велосипеда.

create table if not exists crm.rental_bikes (
  id           bigserial primary key,
  rental_id    bigint      not null references crm.rentals (id) on delete cascade,
  bike_id      bigint      not null references crm.bikes (id),
  issued_on    date        not null default current_date,
  returned_on  date,
  mileage_start integer,
  mileage_end  integer,
  reason       text,
  created_by   text,
  created_at   timestamptz not null default now()
);
create index if not exists rental_bikes_idx on crm.rental_bikes (rental_id, id);
-- Один открытый велосипед на аренду: два «выданных» разом - это потерянный
-- велосипед, который никто не ищет.
create unique index if not exists rental_bikes_one_open on crm.rental_bikes (rental_id)
  where returned_on is null;

-- Подменный фонд: велосипеды, которые держат под замены, а не под выдачу.
-- Отдельного статуса нет намеренно - подменный тоже свободен, просто
-- предлагается первым при замене и последним при выдаче нового клиента.
alter table crm.bikes add column if not exists spare boolean not null default false;

-- ───────────────────────────── розыск ─────────────────────────────
--
-- Из ~190 велосипедов ~25 числятся потерянными. Потеря начинается
-- одинаково: клиент перестал платить и пропал, а велосипед остался
-- «в аренде» и никто его не ищет. Розыск - это отметка с датой и автором,
-- после которой велосипед перестаёт быть просто должником.
--
-- Отдельной таблицы нет: розыск - состояние аренды, а не сущность.

alter table crm.rentals add column if not exists search_at timestamptz;
alter table crm.rentals add column if not exists search_by text;
alter table crm.rentals add column if not exists search_note text;
create index if not exists rentals_search_idx on crm.rentals (search_at)
  where search_at is not null;

-- ─────────────────── закупки основных средств ───────────────────
--
-- Велосипеды приезжают партиями, а в парке живут поштучно. Закупка -
-- документ ЗАК-000001, который помнит, что и почём взяли: без него
-- «сколько мы вложили в парк» считалось по памяти владельца.
--
-- Амортизация по-прежнему живёт на велосипеде: партия может состоять
-- из разных моделей с разным сроком службы, и складывать их в одну
-- строку значило бы потерять единицу учёта.

create table if not exists crm.purchases (
  id           bigserial primary key,
  no           text        not null unique,          -- ЗАК-000001
  supplier_id  bigint      references crm.suppliers (id),
  purchased_on date        not null default current_date,
  total        numeric(12,2) not null default 0,
  note         text,
  created_by   text,
  created_at   timestamptz not null default now()
);
create index if not exists purchases_idx on crm.purchases (purchased_on desc);

alter table crm.bikes add column if not exists purchase_id bigint
  references crm.purchases (id);
create index if not exists bikes_purchase_idx on crm.bikes (purchase_id);

-- ───────────────── справочники: точки, модели, совместимость ─────────────────
--
-- Точки были константой в коде: две штуки, и менять их приходилось
-- разработчику. Теперь справочник, но старые значения остаются кодом
-- точки - в bikes.location лежит текст, и переписывать парк ради
-- красивого внешнего ключа дороже, чем пользы.

create table if not exists crm.locations (
  id         bigserial primary key,
  city       text        not null default 'Казань',
  name       text        not null unique,       -- совпадает с bikes.location
  address    text,
  note       text,
  active     boolean     not null default true,
  sort       integer     not null default 100,
  created_at timestamptz not null default now()
);

insert into crm.locations (name, city, sort) values
  ('Павлюхина', 'Казань', 10),
  ('Адоратского', 'Казань', 20)
on conflict (name) do nothing;

-- Каталог моделей: у клиента одно название, на раме другое. Заводское
-- держим отдельно, чтобы поиск по накладной находил, а клиент читал
-- человеческое.
create table if not exists crm.bike_models (
  id            bigserial primary key,
  title         text        not null unique,    -- как называем клиенту
  brand         text,
  factory_title text,
  battery_slots integer     not null default 2,
  active        boolean     not null default true,
  note          text,
  created_at    timestamptz not null default now()
);

create table if not exists crm.battery_models (
  id         bigserial primary key,
  title      text        not null unique,
  brand      text,
  voltage    integer,                           -- вольты
  capacity   numeric(6,2),                      -- ампер-часы
  price      numeric(12,2) not null default 0,
  service_months integer   not null default 15,
  active     boolean     not null default true,
  note       text,
  created_at timestamptz not null default now()
);

-- Совместимость: какая батарея подходит какой модели и какая основная.
-- Пары, а не матрица в jsonb: по ней ищут в обе стороны - «что подходит
-- этому велосипеду» и «куда встанет эта батарея».
create table if not exists crm.compat (
  bike_model_id    bigint not null references crm.bike_models (id) on delete cascade,
  battery_model_id bigint not null references crm.battery_models (id) on delete cascade,
  primary_fit      boolean not null default false,
  primary key (bike_model_id, battery_model_id)
);

-- ───────────────────────────── батареи ─────────────────────────────
--
-- Раньше батарея была счётчиком у велосипеда: «две штуки, цена и срок».
-- Этого хватало на амортизацию и не хватало ни на что другое - потерянную
-- батарею нельзя было найти, а подменную выдать под запись. Теперь это
-- отдельная единица со своей наклейкой, статусом и журналом.
--
-- Поля battery_* у велосипеда остаются: по ним считается амортизация
-- парка, пока батареи не заведены поштучно. Как только у велосипеда
-- появляются свои батареи, амортизацию берут с них.

create table if not exists crm.batteries (
  id             bigserial primary key,
  code           text        not null unique,     -- наклейка на корпусе
  model_id       bigint      references crm.battery_models (id),
  serial_no      text,
  -- new|available|rented|repair|maintenance|lost|written_off|sold.
  -- rented ставит и снимает выдача, как и у велосипеда.
  status         text        not null default 'available',
  location       text,
  -- Где батарея физически: стоит в велосипеде или выдана с арендой.
  bike_id        bigint      references crm.bikes (id),
  rental_id      bigint      references crm.rentals (id),
  cycles         integer     not null default 0,  -- циклов заряда
  purchase_price numeric(12,2),
  purchased_on   date,
  service_months integer     not null default 15,
  note           text,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create index if not exists batteries_status_idx on crm.batteries (status);
create index if not exists batteries_bike_idx on crm.batteries (bike_id);
create index if not exists batteries_rental_idx on crm.batteries (rental_id);

-- Журнал статусов батареи - тот же принцип, что у велосипеда: без него
-- «когда она пропала» выясняется по памяти оператора.
create table if not exists crm.battery_status_log (
  id          bigserial primary key,
  battery_id  bigint      not null references crm.batteries (id),
  from_status text,
  to_status   text        not null,
  changed_at  timestamptz not null default now(),
  changed_by  text
);
create index if not exists battery_status_log_idx on crm.battery_status_log
  (battery_id, changed_at);

create or replace function crm.log_battery_status() returns trigger
language plpgsql as $$
begin
  if tg_op = 'INSERT' or old.status is distinct from new.status then
    insert into crm.battery_status_log (battery_id, from_status, to_status,
                                        changed_at, changed_by)
    values (new.id,
            case when tg_op = 'INSERT' then null else old.status end,
            new.status, now(),
            nullif(current_setting('crm.actor', true), ''));
  end if;
  return new;
end
$$;
drop trigger if exists batteries_status_log on crm.batteries;
create trigger batteries_status_log
  after insert or update of status on crm.batteries
  for each row execute function crm.log_battery_status();

-- ─────────────────────────── трекеры ───────────────────────────
--
-- Велосипед без трекера ищут звонками: «где вы сейчас», «подъеду
-- завтра». Трекер отвечает на этот вопрос сам. Данные тянутся из
-- StarLine (app/services/starline.py) и складываются здесь: панель
-- в интернет не ходит, она читает базу.
--
-- Позиция дублируется в самой карточке трекера и в журнале позиций.
-- Это не избыточность ради удобства: карточка нужна на каждый чих
-- (карта, список, тревоги), а журнал растёт по точке на опрос, и
-- искать «последнюю» по нему на каждый экран - лишний скан.

create table if not exists crm.trackers (
  id         bigserial primary key,
  device_id  text        not null unique,   -- id устройства в кабинете StarLine
  alias      text,                          -- имя, которое ему дали там же
  bike_id    bigint      references crm.bikes (id),
  active     boolean     not null default true,
  last_seen  timestamptz,                   -- когда устройство выходило на связь
  lat        double precision,
  lon        double precision,
  speed      numeric(6,2),                  -- км/ч по данным устройства
  course     integer,                       -- направление, градусы
  voltage    numeric(5,2),                  -- питание трекера, В
  gsm_level  integer,
  alarm      boolean     not null default false,
  note       text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
-- Один трекер на велосипед: два «последних места» у одной рамы - это
-- не резервирование, а спор, которому верить.
create unique index if not exists trackers_bike_one on crm.trackers (bike_id)
  where bike_id is not null;

create table if not exists crm.tracker_positions (
  id          bigserial primary key,
  tracker_id  bigint      not null references crm.trackers (id) on delete cascade,
  lat         double precision not null,
  lon         double precision not null,
  speed       numeric(6,2),
  course      integer,
  recorded_at timestamptz not null
);
-- Опрос повторяется чаще, чем устройство шлёт точки: без этого индекса
-- один и тот же момент лёг бы в журнал десяток раз за час.
create unique index if not exists tracker_positions_one
  on crm.tracker_positions (tracker_id, recorded_at);
create index if not exists tracker_positions_idx
  on crm.tracker_positions (tracker_id, recorded_at desc);

create table if not exists crm.tracker_alerts (
  id         bigserial primary key,
  tracker_id bigint      not null references crm.trackers (id) on delete cascade,
  bike_id    bigint      references crm.bikes (id),
  -- moving|offline|alarm|low_power
  kind       text        not null,
  note       text,
  lat        double precision,
  lon        double precision,
  created_at timestamptz not null default now(),
  handled_at timestamptz,
  handled_by text
);
-- Одна открытая тревога каждого вида на трекер: иначе «едет без аренды»
-- писалось бы каждые пять минут, и в списке утонуло бы всё остальное.
create unique index if not exists tracker_alerts_one_open
  on crm.tracker_alerts (tracker_id, kind) where handled_at is null;
create index if not exists tracker_alerts_idx
  on crm.tracker_alerts (created_at desc);

-- ────────────────────── касса и банк ──────────────────────
--
-- Наличные на точке живут отдельно от журнала клиента. Журнал отвечает
-- на вопрос «сколько должен клиент», смена - на вопрос «сколько денег
-- в ящике и сходится ли». Это разные вопросы, и одной таблицей они не
-- отвечаются: платёж наличными попадает и туда, и туда, а размен,
-- инкассация и недостача - только в смену.

create table if not exists crm.cash_shifts (
  id         bigserial primary key,
  no         text        not null unique,   -- КСМ-000001
  location   text,
  status     text        not null default 'open',   -- open|closed
  opened_at  timestamptz not null default now(),
  opened_by  text,
  opening    numeric(12,2) not null default 0,      -- размен на начало
  closed_at  timestamptz,
  closed_by  text,
  counted    numeric(12,2),                 -- сколько насчитали руками
  expected   numeric(12,2),                 -- сколько должно быть
  diff       numeric(12,2),                 -- counted - expected
  note       text
);
-- Одна открытая смена на точку: две открытые - это два ответа на вопрос
-- «в чей ящик легли деньги», и оба неверные.
create unique index if not exists cash_shifts_one_open
  on crm.cash_shifts (coalesce(location, '')) where status = 'open';

create table if not exists crm.cash_moves (
  id         bigserial primary key,
  shift_id   bigint      not null references crm.cash_shifts (id) on delete cascade,
  kind       text        not null,          -- in|out
  amount     numeric(12,2) not null,        -- всегда положительная
  reason     text,
  ledger_id  bigint      references crm.ledger (id),
  created_at timestamptz not null default now(),
  created_by text
);
create index if not exists cash_moves_idx on crm.cash_moves (shift_id, id);

-- Выписка банка. Строка приходит из Точки и живёт здесь своей жизнью:
-- зачисление в журнал клиента - отдельное действие оператора, потому
-- что на счёт падает и выручка ремонта, и возвраты поставщиков, и
-- деньги, которые к прокату отношения не имеют.
create table if not exists crm.bank_txns (
  id         bigserial primary key,
  txn_id     text        not null unique,   -- id операции в банке
  account    text,
  booked_at  timestamptz not null,
  amount     numeric(12,2) not null,
  direction  text        not null,          -- credit|debit
  payer_name text,
  payer_inn  text,
  purpose    text,
  -- new|matched|ignored. matched - деньги уже в журнале клиента,
  -- ignored - платёж не наш (ремонт, возврат, личное).
  status     text        not null default 'new',
  client_id  bigint      references crm.clients (id),
  ledger_id  bigint      references crm.ledger (id),
  handled_at timestamptz,
  handled_by text,
  created_at timestamptz not null default now()
);
create index if not exists bank_txns_idx on crm.bank_txns (booked_at desc);
create index if not exists bank_txns_new on crm.bank_txns (status) where status = 'new';

-- ────────────────────── рассылки ──────────────────────
--
-- Рассылка - это не «написать всем». Курьеру, у которого велосипед на
-- руках, предложение «вернуться» выглядит издевательством, а должнику
-- скидка - поощрением. Поэтому у кампании есть аудитория, а у шаблона -
-- подстановки: имя, баланс, дата «оплачено до».
--
-- Текст хранится дважды: для Telegram с разметкой и для MAX простым
-- текстом. MAX разметку не понимает, а автоматически снятые теги
-- превращают «<b>3 000 ₽</b>» в «3 000 ₽» - но только там, где текст
-- писали с оглядкой на это. Отдельное поле честнее.

create table if not exists crm.message_templates (
  id         bigserial primary key,
  code       text        not null unique,
  title      text        not null,
  body       text        not null,      -- Telegram, с разметкой
  body_max   text,                      -- MAX, простым текстом
  active     boolean     not null default true,
  note       text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

insert into crm.message_templates (code, title, body, body_max, note) values
  ('debt', 'Напоминание о долге',
   E'Здравствуйте, {name}!\n\nПо велосипеду {bike} накопился долг {debt}.\nОплатить можно по ссылке: {pay_url}\n\nЕсли уже оплатили — напишите нам.',
   E'Здравствуйте, {name}!\n\nПо велосипеду {bike} накопился долг {debt}.\nОплатить можно по ссылке: {pay_url}\n\nЕсли уже оплатили — напишите нам.',
   'Аудитория «должники»'),
  ('comeback', 'Возвращайтесь',
   E'Здравствуйте, {name}!\n\nУ нас есть свободные велосипеды — можно забрать сегодня на Павлюхина или Адоратского.\nНапишите, и придержим за вами.',
   E'Здравствуйте, {name}!\n\nУ нас есть свободные велосипеды — можно забрать сегодня на Павлюхина или Адоратского.\nНапишите, и придержим за вами.',
   'Аудитория «уехали и не вернулись»'),
  ('expiring', 'Срок подходит',
   E'Здравствуйте, {name}!\n\nАренда велосипеда {bike} оплачена до {until}.\nПродлить — {pay_url}, сумма за период {price}.',
   E'Здравствуйте, {name}!\n\nАренда велосипеда {bike} оплачена до {until}.\nПродлить — {pay_url}, сумма за период {price}.',
   'Аудитория «истекает срок»')
on conflict (code) do nothing;

create table if not exists crm.campaigns (
  id          bigserial primary key,
  no          text        not null unique,   -- РСЛ-000001
  title       text        not null,
  template_id bigint      references crm.message_templates (id),
  audience    text        not null,
  -- draft|sending|done|cancelled. Рассылка не стартует сама: черновик
  -- существует именно затем, чтобы посмотреть на список получателей
  -- до того, как двести человек получат сообщение.
  status      text        not null default 'draft',
  created_by  text,
  created_at  timestamptz not null default now(),
  started_at  timestamptz,
  finished_at timestamptz,
  note        text
);

create table if not exists crm.campaign_sends (
  id          bigserial primary key,
  campaign_id bigint      not null references crm.campaigns (id) on delete cascade,
  client_id   bigint      not null references crm.clients (id),
  channel     text        not null,          -- tg|max
  status      text        not null default 'queued',  -- queued|sent|failed|skipped
  error       text,
  sent_at     timestamptz
);
-- Один получатель - одно сообщение в кампании: повторный запуск и гонка
-- отправителей иначе шлют клиенту второй раз то же самое.
create unique index if not exists campaign_sends_one
  on crm.campaign_sends (campaign_id, client_id);
create index if not exists campaign_sends_queue
  on crm.campaign_sends (campaign_id, status);

-- MAX-аккаунт клиента. Телеграм-аккаунт лежит в tg_id с самого начала;
-- MAX появился позже и живёт в своей базе, поэтому связь заводится
-- по телефону: мостом из MAX-бота или руками в карточке.
alter table crm.clients add column if not exists max_id bigint;
create unique index if not exists clients_max_idx on crm.clients (max_id)
  where max_id is not null;

-- ────────────── простая электронная подпись (ПЭП) ──────────────
--
-- Кнопка «подписываю» в боте фиксирует согласие, но не доказывает его:
-- в споре нужно показать, ЧТО именно подписали, КОГДА, КАКИМ кодом и
-- с какого адреса. Поэтому заявка на подпись хранит перечень документов
-- с их хэшами, а каждый шаг пишется в журнал.
--
-- Сам код не хранится нигде: в базе только его хэш вместе с токеном
-- ссылки. Утечка дампа не даёт подписать задним числом.

create table if not exists crm.sign_requests (
  id           bigserial primary key,
  no           text        not null unique,   -- ПЭП-000001
  client_id    bigint      not null references crm.clients (id),
  rental_id    bigint      references crm.rentals (id),
  -- Токен ссылки: клиент открывает страницу подписания по нему, без входа
  -- в панель. Длинный и случайный - это и есть вся его защита.
  token        text        not null unique,
  -- Перечень подписываемых документов: [{kind, title, sha256, path}].
  docs         jsonb       not null default '[]'::jsonb,
  -- Текст соглашения об ЭП ровно в том виде, в каком его приняли.
  agreement    text,
  code_hash    text,                          -- sha256(токен + ':' + код)
  code_at      timestamptz,
  attempts     integer     not null default 0,
  -- new|code|signed|cancelled
  status       text        not null default 'new',
  expires_at   timestamptz not null,
  signed_at    timestamptz,
  signed_ip    text,
  signed_agent text,
  note         text,
  created_by   text,
  created_at   timestamptz not null default now()
);
create index if not exists sign_requests_client_idx
  on crm.sign_requests (client_id, id desc);
create index if not exists sign_requests_open_idx
  on crm.sign_requests (status) where status in ('new', 'code');

create table if not exists crm.sign_events (
  id         bigserial primary key,
  request_id bigint      not null references crm.sign_requests (id) on delete cascade,
  -- created|opened|code_sent|code_wrong|signed|cancelled|expired
  kind       text        not null,
  at         timestamptz not null default now(),
  ip         text,
  user_agent text,
  note       text
);
create index if not exists sign_events_idx on crm.sign_events (request_id, id);

-- ────── каталог и цены проката: данные владельца ──────
--
-- Цена зависит от модели: Monster Truck+ и Kugoo V3 Pro стоят по-разному,
-- и плоский тариф «неделя — 3 000» это различие терял. tariffs.model -
-- название модели из каталога; пусто значит «для любой модели», такие
-- тарифы остаются запасными.

alter table crm.tariffs add column if not exists model text;
-- Одна цена на связку «вид + модель + срок» среди действующих: два тарифа
-- на одно и то же - это спор о цене прямо на выдаче. Индекс заводится
-- ниже, вместе с видом тарифа: до него колонки `kind` ещё нет.

-- Характеристики модели: их спрашивает каждый второй курьер, и раньше
-- ответ жил в голове оператора.
alter table crm.bike_models add column if not exists weight_kg     numeric(6,2);
alter table crm.bike_models add column if not exists speed_kmh     integer;
alter table crm.bike_models add column if not exists range_km      integer;
alter table crm.bike_models add column if not exists charge_hours  numeric(4,1);
alter table crm.bike_models add column if not exists wheel_size    text;
alter table crm.bike_models add column if not exists motor_watt    integer;
alter table crm.bike_models add column if not exists max_load_kg   integer;
alter table crm.bike_models add column if not exists size_note     text;
alter table crm.bike_models add column if not exists photo_url     text;
alter table crm.bike_models add column if not exists description   text;

-- Пункт выдачи: адрес, телефон, режим и координаты - для карты и для
-- ответа «где забрать».
alter table crm.locations add column if not exists public_title text;
alter table crm.locations add column if not exists phone        text;
alter table crm.locations add column if not exists hours        text;
alter table crm.locations add column if not exists lat          double precision;
alter table crm.locations add column if not exists lon          double precision;

update crm.locations set
  public_title = coalesce(public_title, 'Май Байк — сервис и аренда, Павлюхина'),
  address = coalesce(address, 'г. Казань, ул. Павлюхина, 97А'),
  phone = coalesce(phone, '+7 (904) 676-49-26'),
  hours = coalesce(hours, 'пн-вс: 10:00-19:00'),
  lat = coalesce(lat, 55.766900), lon = coalesce(lon, 49.148580)
 where name = 'Павлюхина';
update crm.locations set
  public_title = coalesce(public_title, 'Май Байк — сервис и аренда, Адоратского'),
  address = coalesce(address, 'г. Казань, ул. Адоратского, 11А'),
  phone = coalesce(phone, '+7 (904) 676-49-26'),
  hours = coalesce(hours, 'пн-вс: 10:00-19:00'),
  lat = coalesce(lat, 55.824319), lon = coalesce(lon, 49.147018)
 where name = 'Адоратского';

-- Каталог моделей и цены - из таблицы владельца. on conflict do nothing:
-- правки в панели важнее сида, перезаписывать их при каждом старте нельзя.
insert into crm.bike_models (title, brand, battery_slots, weight_kg, speed_kmh,
                             range_km, charge_hours, wheel_size, motor_watt,
                             max_load_kg, size_note, description)
values
  ('Monster Truck + (Два АКБ)', 'Monster', 2, 52, 60, 70, 6, '16 дюймов', 1200, 120,
   '120х43х110', 'Работаем 7/0, бесплатное обслуживание'),
  ('Monster Truck + с задними амортизаторами', 'Monster', 2, 52, 60, 70, 6,
   '16 дюймов', 1200, 120, '120х43х110', 'Работаем 7/0, бесплатное обслуживание'),
  ('Kugoo V3 Pro (Два АКБ)', 'Kugoo', 2, 52, 60, 70, 6, '16 дюймов', 1200, 120,
   '125х43х110', 'Работаем 7/0, бесплатное обслуживание'),
  ('Kugoo V3 Pro + (Два АКБ)', 'Kugoo', 2, 52, 60, 70, 6, '16 дюймов', 1200, 120,
   '125х43х110', 'Работаем 7/0, бесплатное обслуживание')
on conflict (title) do nothing;

insert into crm.tariffs (name, model, period_days, price, sort) values
  ('Неделя',   'Monster Truck + (Два АКБ)',                 7,  3000, 10),
  ('Две недели','Monster Truck + (Два АКБ)',               14,  5400, 20),
  ('Месяц',    'Monster Truck + (Два АКБ)',                30, 11000, 30),
  ('Неделя',   'Monster Truck + с задними амортизаторами',  7,  3300, 11),
  ('Две недели','Monster Truck + с задними амортизаторами',14,  5900, 21),
  ('Месяц',    'Monster Truck + с задними амортизаторами', 30, 12000, 31),
  ('Неделя',   'Kugoo V3 Pro (Два АКБ)',                    7,  3500, 12),
  ('Две недели','Kugoo V3 Pro (Два АКБ)',                  14,  6000, 22),
  ('Месяц',    'Kugoo V3 Pro (Два АКБ)',                   30, 12500, 32),
  ('Неделя',   'Kugoo V3 Pro + (Два АКБ)',                  7,  3500, 13),
  ('Две недели','Kugoo V3 Pro + (Два АКБ)',                14,  6000, 23),
  ('Месяц',    'Kugoo V3 Pro + (Два АКБ)',                 30, 12500, 33)
on conflict do nothing;

-- ────────────────────── приём оплаты ──────────────────────
--
-- Ссылка на оплату - это ещё не деньги. Пока клиент не заплатил,
-- в журнале ничего быть не должно: `ledger` - факт, а не намерение.
-- Поэтому счёт живёт отдельной таблицей и попадает в журнал ровно один
-- раз - когда банк подтвердил оплату, и `ledger_id` это фиксирует.
--
-- Автосписание - тот же счёт, только вида `auto`: его создаёт не
-- оператор, а суточный проход. Отдельной таблицы под него нет
-- намеренно: клиенту всё равно, кто нажал кнопку, а отчёт «сколько
-- пришло эквайрингом» не должен складывать две сущности.
create table if not exists crm.pay_orders (
  id          bigserial primary key,
  no          text        not null unique,       -- СЧТ-000001
  client_id   bigint      not null references crm.clients (id),
  rental_id   bigint      references crm.rentals (id),
  amount      numeric(12,2) not null,
  purpose     text        not null,
  kind        text        not null default 'link',  -- link|auto
  -- new: ссылка ещё не получена; sent: клиенту отдана; paid: банк
  -- подтвердил; failed: банк отказал; cancelled: сняли руками.
  status      text        not null default 'new',
  provider    text        not null default 'tochka',
  operation_id text,
  link        text,
  error       text,
  ledger_id   bigint      references crm.ledger (id),
  created_by  text,
  created_at  timestamptz not null default now(),
  sent_at     timestamptz,
  paid_at     timestamptz,
  checked_at  timestamptz
);
-- Одна операция банка - один счёт: повторный ответ эквайринга не
-- заведёт второй платёж.
create unique index if not exists pay_orders_operation_idx
  on crm.pay_orders (operation_id) where operation_id is not null;
create index if not exists pay_orders_open_idx
  on crm.pay_orders (status, created_at desc) where status in ('new', 'sent');
create index if not exists pay_orders_client_idx
  on crm.pay_orders (client_id, created_at desc);

-- Карта клиента для автосписания. Номера карты здесь нет и быть не
-- может: хранится токен эквайринга и четыре последние цифры, чтобы
-- оператор и клиент понимали, о какой карте речь.
create table if not exists crm.card_tokens (
  id         bigserial primary key,
  client_id  bigint      not null references crm.clients (id),
  provider   text        not null default 'tochka',
  token      text        not null,
  mask       text,                              -- 4477
  expires    text,                              -- 12/28
  active     boolean     not null default true,
  created_at timestamptz not null default now(),
  used_at    timestamptz
);
-- Одна действующая карта на клиента: привязали новую - старая уходит.
create unique index if not exists card_tokens_one
  on crm.card_tokens (client_id, provider) where active;

-- ────────────────────── уведомления ──────────────────────
--
-- Каталог уведомлений живёт в коде (`logic.NOTICES`), а здесь - только
-- то, что владелец в нём поменял. Так новое уведомление появляется в
-- панели само, без вставки в схему, а строки, которых нет, читаются
-- как «по умолчанию».
--
-- `at_hour is null` - «сразу по событию»: такие уходят в момент, когда
-- событие случилось, и часа у них нет.
create table if not exists crm.notices (
  code       text        primary key,
  enabled    boolean     not null default true,
  at_hour    smallint,
  at_minute  smallint    not null default 0,
  -- Переопределение получателя для командных: по умолчанию служебный чат
  -- из настроек бота.
  chat_id    text,
  -- Параметры конкретного уведомления: за сколько дней предупреждать,
  -- через сколько звать на ТО. Белый список - в logic.NOTICES.
  extra      jsonb       not null default '{}'::jsonb,
  updated_by text,
  updated_at timestamptz not null default now()
);

-- История отправок: кому и чем кончилось. Текст сообщения здесь не
-- хранится - он собирается из шаблона и данных клиента, а копия текста
-- через месяц уже не отвечает ни на один вопрос, зато весит.
create table if not exists crm.notice_log (
  id         bigserial primary key,
  code       text        not null,
  client_id  bigint      references crm.clients (id),
  target     text        not null,          -- client|chat|channel
  status     text        not null,          -- sent|failed|skipped
  detail     text,
  created_at timestamptz not null default now()
);
create index if not exists notice_log_idx on crm.notice_log (created_at desc);
create index if not exists notice_log_code_idx
  on crm.notice_log (code, created_at desc);

-- ────────────────────── смета и счёт за ремонт ──────────────────────
--
-- Смета - это не «число в поле». Клиенту уходит перечень работ и цена,
-- и он на неё отвечает: пока не ответил, наряд стоит в «на согласовании»
-- и техник за него не берётся. Отказ - тоже ответ: наряд закрывается,
-- а не висит.
alter table crm.work_orders add column if not exists estimate_sent_at timestamptz;
alter table crm.work_orders add column if not exists approved_at timestamptz;
-- Кто согласовал: «клиент» - нажал кнопку в боте, имя оператора - сказал
-- вживую. Различие нужно: на спор «я такого не заказывал» это ответ.
alter table crm.work_orders add column if not exists approved_by text;
alter table crm.work_orders add column if not exists declined_at timestamptz;

-- Счёт за ремонт - тот же счёт, что и за аренду, но привязан к наряду.
-- Красная линия: оплата ремонта в crm.ledger НЕ попадает - журнал это
-- аренда, и средний чек считается по нему. Выручка ремонта живёт на
-- наряде, поэтому оплаченный счёт с нарядом ставит work_orders.paid_at
-- и ничего не пишет в журнал.
alter table crm.pay_orders add column if not exists work_order_id bigint
  references crm.work_orders (id);
create index if not exists pay_orders_work_idx
  on crm.pay_orders (work_order_id) where work_order_id is not null;

-- ────────────────────── баллы и отзывы ──────────────────────
--
-- Баллы - не деньги, а наша скидка. В журнале они живут записью вида
-- `bonus`: отдельный вид нужен, чтобы их было видно и чтобы они никогда
-- не попали в `payment`. Платежи формируют средний чек - бонус завысил
-- бы его, и три числа парка стали бы врать.
--
-- Эта таблица - не копия журнала, а ответ на вопрос «за что начислили».
-- Баланс по-прежнему считается по `ledger`, здесь только повод.
create table if not exists crm.bonuses (
  id         bigserial primary key,
  client_id  bigint      not null references crm.clients (id),
  -- referral: агенту за друга; friend: самому другу; review: за отзыв;
  -- manual: руками, с заметкой оператора.
  kind       text        not null,
  amount     numeric(12,2) not null,
  ledger_id  bigint      references crm.ledger (id),
  ref_id     bigint      references crm.referrals (id),
  note       text,
  created_by text,
  created_at timestamptz not null default now()
);
create index if not exists bonuses_client_idx on crm.bonuses (client_id, id desc);
create index if not exists bonuses_kind_idx on crm.bonuses (kind, created_at desc);
-- Бонус за отзыв - один раз на клиента: второй отзыв той же рукой на
-- той же площадке площадка и сама не примет.
create unique index if not exists bonuses_review_once
  on crm.bonuses (client_id) where kind = 'review';
-- Бонус другу - тоже один: приглашение отрабатывает однократно.
create unique index if not exists bonuses_friend_once
  on crm.bonuses (client_id) where kind = 'friend';

-- ────────────────── ввод техники в эксплуатацию ──────────────────
--
-- Новый велосипед не появляется в парке готовым: его собирают, клеят
-- номер, ставят трекер и госномер. До сверки он в статусе `new` -
-- «новое на сборке», и выдать его нельзя.
--
-- Сверка - это не галочка «всё хорошо», а отметка по каждому полю
-- паспорта: кто и когда подтвердил, что номер на раме совпадает с тем,
-- что в карточке. Переписать номер из накладной и сверкой это не
-- назвать - для того и фотография.
alter table crm.bikes add column if not exists plate_no text;
-- Госномер и трекер установлены физически. Отдельно от номера: номер
-- может быть выписан, а таблички на велосипеде ещё нет.
alter table crm.bikes add column if not exists plate_ok boolean not null default false;
alter table crm.bikes add column if not exists tracker_ok boolean not null default false;
-- {"frame_no": {"at": "2026-09-17", "by": "staff:1", "photo": "…"}, …}
-- jsonb, а не пять колонок: полей паспорта со временем станет больше,
-- а колонка на каждое - это правка схемы на каждое.
alter table crm.bikes add column if not exists checked jsonb not null default '{}'::jsonb;
alter table crm.bikes add column if not exists commissioned_at timestamptz;
alter table crm.bikes add column if not exists commissioned_by text;
create index if not exists bikes_new_idx on crm.bikes (status) where status = 'new';

-- ────────────────── свои шаблоны документов ──────────────────
--
-- У каждого вида документа всегда включён ровно один шаблон: наш или
-- ваш. Загрузили свой и включили - наш выключается сам. Выключить оба
-- нельзя: выдачу тогда нечем оформить, и это не настройка, а поломка.
--
-- Сам файл лежит на диске (том `doctemplates`), здесь - только имя,
-- размер и отпечаток: держать docx в базе значит возить его в каждом
-- дампе и в каждом бэкапе.
create table if not exists crm.doc_templates (
  id          bigserial primary key,
  kind        text        not null,          -- contract|act_in|act_out|…
  filename    text        not null,          -- имя на диске, собираем сами
  original    text,                          -- как файл назывался у владельца
  size_bytes  integer     not null default 0,
  sha256      text,
  -- Включён ли ваш шаблон. Выключенный остаётся в архиве: вернуть
  -- прошлую редакцию договора бывает нужно ровно тогда, когда спорят
  -- по уже подписанному.
  active      boolean     not null default false,
  note        text,
  uploaded_by text,
  uploaded_at timestamptz not null default now()
);
create index if not exists doc_templates_kind_idx
  on crm.doc_templates (kind, uploaded_at desc);
-- Один включённый свой шаблон на вид.
create unique index if not exists doc_templates_one_active
  on crm.doc_templates (kind) where active;

-- Подпись и печать организации. Не в `crm.settings`: там значения-строки,
-- а это файлы, и им нужны размер и отпечаток, как шаблонам.
create table if not exists crm.company_marks (
  kind        text        primary key,       -- signature|stamp
  filename    text        not null,
  size_bytes  integer     not null default 0,
  uploaded_by text,
  uploaded_at timestamptz not null default now()
);

-- ────── позиции аренды: доп. аккумулятор за деньги ──────
--
-- Курьер берёт второй аккумулятор, чтобы не заряжаться в середине смены,
-- и это отдельные деньги. Раньше у аренды была одна цена за период - цена
-- велосипеда, и вторая батарея уезжала бесплатно.
--
-- `rentals.price` остаётся ценой периода ЦЕЛИКОМ: по ней идёт начисление
-- и по ней считается средний чек. Здесь лежит расшифровка - из чего эта
-- цена сложилась. Иначе пришлось бы складывать цену в двух местах и
-- однажды сложить по-разному.

-- Тариф бывает не только на велосипед: у аккумулятора своя цена и свой
-- срок. kind разделяет их, чтобы «неделя» велосипеда и «неделя» батареи
-- не спорили за один и тот же уникальный индекс.
alter table crm.tariffs add column if not exists kind text not null default 'bike';

-- Прошлый индекс не знал про вид и запрещал батарее иметь свою «неделю».
drop index if exists crm.tariffs_model_period_idx;
create unique index if not exists tariffs_kind_model_period_idx
  on crm.tariffs (kind, coalesce(model, ''), period_days) where active;

-- Цена велосипеда на момент выдачи. `rentals.price` - цена периода
-- целиком, вместе с позициями; вычитать их обратно каждый раз, когда
-- позицию снимают, нельзя: тариф к тому времени могли поднять, и аренда
-- молча переоценилась бы задним числом.
alter table crm.rentals add column if not exists base_price numeric(12,2);
update crm.rentals set base_price = price where base_price is null;

create table if not exists crm.rental_extras (
  id         bigserial primary key,
  rental_id  bigint      not null references crm.rentals (id) on delete cascade,
  kind       text        not null default 'battery',
  battery_id bigint      references crm.batteries (id),
  title      text        not null,
  -- Цена за тот же период, что и у аренды: смешивать сутки с неделей
  -- в одной строке значит потерять смысл суммы.
  price      numeric(12,2) not null default 0,
  added_at   timestamptz not null default now(),
  added_by   text,
  removed_at timestamptz,
  removed_by text
);
create index if not exists rental_extras_idx
  on crm.rental_extras (rental_id, removed_at);
-- Одна батарея на аренде одной строкой: две строки на ту же батарею -
-- это двойная цена за одну вещь.
create unique index if not exists rental_extras_battery_once
  on crm.rental_extras (rental_id, battery_id)
  where removed_at is null and battery_id is not null;

-- ────── паспорт аккумулятора и его сверка ──────
--
-- У велосипеда сверка появилась раньше: номер наклейки, серийный номер,
-- госномер, трекер. У батареи ровно та же беда - её заводят с накладной,
-- не глядя на корпус, а потом ищут «ту самую 70 Ач» по всему складу.
--
-- Напряжение и ёмкость ЗДЕСЬ, а не только в каталоге модели: в каталоге
-- лежит паспорт модели, а на корпусе - табличка конкретной батареи, и
-- они расходятся чаще, чем хотелось бы. Сверяют то, что на корпусе.

alter table crm.batteries add column if not exists volts     integer;
alter table crm.batteries add column if not exists amp_hours numeric(6,2);
-- Отметки сверки: поле паспорта -> {at, by, photo}. jsonb, а не таблица:
-- полей пять, они меняются вместе с формой, и отдельная таблица на пять
-- строк - это join ради join.
alter table crm.batteries add column if not exists checked jsonb not null
  default '{}'::jsonb;
alter table crm.batteries add column if not exists commissioned_at timestamptz;
alter table crm.batteries add column if not exists commissioned_by text;

-- ────── пересчёт считает и батареи ──────
--
-- Ведомость считала только велосипеды, а на складе лежат ещё 120 батарей,
-- и теряются они не реже. `what` - что считали: всё, велосипеды или
-- аккумуляторы. Отдельной ведомости на батареи нет намеренно: человек с
-- телефоном обходит точку один раз и вводит номера подряд, а какой из
-- них чей - разбирается система.

alter table crm.stock_takes add column if not exists what text not null
  default 'bikes';
alter table crm.stock_take_items add column if not exists battery_id bigint
  references crm.batteries (id);
-- Одна батарея в ведомости один раз - по той же причине, что и велосипед.
create unique index if not exists stock_take_items_one_battery
  on crm.stock_take_items (take_id, battery_id) where battery_id is not null;

-- ────── тревога как задача ──────
--
-- Тревога была отметкой: подняли - сняли. На практике их разбирают, как
-- задачи: одну берут в работу, другую откладывают до вечера, третью
-- признают нормой («да, этот велосипед у нас в гараже без связи»).
-- Ложная тревога и разобранная выглядели одинаково, и понять, чем
-- занимался оператор, было нельзя.
--
-- Закрытой остаётся тревога с `handled_at`. Состояние - внутри открытой,
-- поэтому частичный уникальный индекс по-прежнему держит одну открытую
-- тревогу вида на трекер. Это и делает «норму» самоочищающейся: пока
-- причина держится, тревога висит и второй раз не поднимается, а
-- исчезнет причина - опрос закроет её сам.

alter table crm.tracker_alerts add column if not exists level text not null
  default 'yellow';                             -- urgent|yellow
alter table crm.tracker_alerts add column if not exists state text not null
  default 'new';                                -- new|working|snoozed|normal
alter table crm.tracker_alerts add column if not exists taken_by text;
alter table crm.tracker_alerts add column if not exists taken_at timestamptz;
alter table crm.tracker_alerts add column if not exists snooze_until timestamptz;
create index if not exists tracker_alerts_open_idx
  on crm.tracker_alerts (state, created_at desc) where handled_at is null;

-- Когда трекер последний раз ехал. Ради тревоги «не двигается при
-- аренде»: `last_seen` - это выход на связь, а стоящий велосипед выходит
-- на связь исправно. Считать по журналу позиций нельзя - он живёт месяц.
alter table crm.trackers add column if not exists moved_at timestamptz;

-- ────── свои фильтры списков ──────
--
-- «Долг больше нуля, точка Павлюхина, отсортировать по суткам» - набор,
-- который оператор собирает каждое утро заново. Сохранённый фильтр - это
-- просто строка запроса под именем, своя у каждого сотрудника: чужие
-- фильтры в списке мешают, а общие на всех превращаются в свалку.

create table if not exists crm.saved_views (
  id         bigserial primary key,
  staff_id   bigint      not null references crm.staff (id) on delete cascade,
  section    text        not null,          -- путь списка: /rentals, /bikes…
  name       text        not null,
  -- Строка запроса без «?»: q=..&status=..&sort=..
  query      text        not null default '',
  created_at timestamptz not null default now()
);
create index if not exists saved_views_idx
  on crm.saved_views (staff_id, section, id);
-- Одно имя на список у сотрудника: два «моих должника» с разными
-- фильтрами - это спор о том, который из них настоящий.
create unique index if not exists saved_views_one_name
  on crm.saved_views (staff_id, section, lower(name));

-- ────── мелочи: пробег в журнале статусов, контакты менеджера ──────
--
-- Пробег при каждой смене статуса. Раньше он записывался только при
-- выдаче и возврате, и на вопрос «сколько накатал, пока был в ремонте»
-- ответить было нечем. Колонку заполняет тот же триггер: отдельным
-- запросом её забудут заполнить в первом же новом месте.

alter table crm.bike_status_log add column if not exists mileage_km integer;

create or replace function crm.log_bike_status() returns trigger
language plpgsql as $$
begin
  if tg_op = 'INSERT' or old.status is distinct from new.status then
    insert into crm.bike_status_log (bike_id, from_status, to_status, changed_at,
                                     changed_by, mileage_km)
    values (new.id,
            case when tg_op = 'INSERT' then null else old.status end,
            new.status, now(),
            nullif(current_setting('crm.actor', true), ''),
            new.mileage_km);
  end if;
  return new;
end
$$;

-- Клиент: запасные телефоны и место работы. Второй и третий номер бот
-- собирает в анкете, но она зашифрована и остаётся единственным местом
-- для собранного ботом; здесь - то, что оператор записал руками или
-- привёз импорт. Работодатель и стаж - для отчёта «кто наш клиент»,
-- не для документов.
alter table crm.clients add column if not exists phone2 text;
alter table crm.clients add column if not exists phone3 text;
alter table crm.clients add column if not exists employer text;
alter table crm.clients add column if not exists experience text;
