"""Доступ к Postgres. Тонкий слой поверх asyncpg, без ORM."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg

# Белый список колонок для UPDATE. Имена колонок нельзя передать параметром,
# они подставляются в SQL как текст - поэтому только из этого множества.
PATCHABLE = frozenset({
    "state", "full_name", "phone",
    "doc_file_id", "doc_path", "doc_sha256",
    "selfie_file_id", "selfie_path", "selfie_sha256",
    "doc_ocr", "name_match", "ocr_at",
    "oferta_version", "oferta_accepted_at", "pdn_version", "pdn_consent_at",
    "status", "reject_reason", "reviewed_by", "reviewed_at", "purge_after",
    "anketa_enc",
    "contract_no", "contract_path", "contract_sha256", "contract_status",
    "contract_issued_at", "contract_signed_at",
    "mod_chat_id", "mod_message_id",
})

JSON_COLUMNS = frozenset({"doc_ocr"})
# numeric в asyncpg - это Decimal. Питоновский float сюда передать нельзя:
# драйвер отвергнет аргумент, а не округлит его.
NUMERIC_COLUMNS = frozenset({"name_match"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Без явного кодека asyncpg отдаёт jsonb строкой, и код, ожидающий dict,
    падает на .get() уже в проде."""
    for type_name in ("json", "jsonb"):
        await conn.set_type_codec(
            type_name, encoder=json.dumps, decoder=json.loads, schema="pg_catalog",
        )


class Database:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, params: dict[str, Any]) -> "Database":
        """Параметры по отдельности, а не строкой DSN.

        Пароль из `openssl rand -base64 24` содержит "/" примерно в 40%
        случаев, и первый же слэш обрывает authority-часть URL: хост
        превращается в мусор, пароль теряется. Отдельные аргументы
        create_pool убирают этот класс ошибок целиком.
        """
        pool = await asyncpg.create_pool(
            **params, min_size=1, max_size=10, command_timeout=30, init=_init_connection,
        )
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    async def apply_schema(self, path: Path) -> None:
        await self.pool.execute(path.read_text(encoding="utf-8"))

    # ─────────────────────── журнал апдейтов ───────────────────────

    async def claim_update(self, update_id: int, tg_id: int | None, kind: str,
                           payload: dict) -> bool:
        """True, если апдейт наш и его надо обрабатывать.

        Повторная доставка того же update_id вернёт False. Зависший в
        processing дольше 60 секунд (упало исполнение) можно переиграть.
        """
        row = await self.pool.fetchrow(
            """
            insert into bot.updates_log (update_id, tg_id, kind, payload, state)
            values ($1, $2, $3, $4::jsonb, 'processing')
            on conflict (update_id) do update
              set state = 'processing', created_at = now()
              where bot.updates_log.state = 'processing'
                and bot.updates_log.created_at < now() - interval '60 seconds'
            returning update_id
            """,
            update_id, tg_id, kind, payload,
        )
        return row is not None

    async def finish_update(self, update_id: int) -> None:
        await self.pool.execute(
            "update bot.updates_log set state = 'done' where update_id = $1", update_id
        )

    # ─────────────────────── пользователи ───────────────────────

    async def upsert_user(self, tg_id: int, username: str | None) -> asyncpg.Record:
        """Заводит пользователя и одним запросом двигает окно рейт-лимита."""
        return await self.pool.fetchrow(
            """
            insert into bot.users (tg_id, username, rl_window, rl_count)
            values ($1, $2, now(), 1)
            on conflict (tg_id) do update set
              username  = coalesce(excluded.username, bot.users.username),
              rl_window = case when bot.users.rl_window < now() - interval '1 minute'
                               then now() else bot.users.rl_window end,
              rl_count  = case when bot.users.rl_window < now() - interval '1 minute'
                               then 1 else bot.users.rl_count + 1 end
            returning *
            """,
            tg_id, username,
        )

    async def get_user(self, tg_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow("select * from bot.users where tg_id = $1", tg_id)

    async def patch(self, tg_id: int, *, expected_state: str | None = None,
                    expected_status: str | None = None, **fields: Any) -> bool:
        """UPDATE с оптимистичной блокировкой.

        expected_state - состояние, которое обработчик видел на входе. Если
        параллельный апдейт (двойной тап, медиагруппа) уже сдвинул состояние,
        обновление не пройдёт и вернётся False. expected_status делает то же
        для решений модераторов: два админа, нажавшие «Одобрить» одновременно,
        иначе оба довели бы дело до конца и пользователь получил два письма.
        Значения None пишутся как NULL осознанно: так работает сброс анкеты.
        """
        unknown = set(fields) - PATCHABLE
        if unknown:
            raise ValueError(f"недопустимые колонки: {sorted(unknown)}")
        if not fields and expected_state is None and expected_status is None:
            return True
        # Ранний выход при пустом fields, но заданных guard-условиях был бы
        # ложным «да»: вызывающий решил бы, что блокировка прошла проверку.

        cols = list(fields)
        sets, values = [], []
        for i, col in enumerate(cols, start=2):
            value = fields[col]
            if col in JSON_COLUMNS:
                sets.append(f"{col} = ${i}::jsonb")   # dict сериализует кодек пула
            elif col in NUMERIC_COLUMNS:
                sets.append(f"{col} = ${i}")
                value = None if value is None else Decimal(str(value))
            else:
                sets.append(f"{col} = ${i}")
            values.append(value)

        guards = ""
        if expected_state is not None:
            values.append(expected_state)
            guards += f" and state = ${len(values) + 1}"
        if expected_status is not None:
            values.append(expected_status)
            guards += f" and status = ${len(values) + 1}"

        # updated_at добавляем в общий список, а не через запятую после join:
        # при пустом fields получилось бы "set , updated_at = now()".
        assignments = ", ".join([*sets, "updated_at = now()"])
        row = await self.pool.fetchrow(
            f"update bot.users set {assignments} "
            f"where tg_id = $1{guards} returning tg_id",
            tg_id, *values,
        )
        return row is not None

    async def log_event(self, tg_id: int | None, type_: str, payload: dict | None = None) -> None:
        await self.pool.execute(
            "insert into bot.events (tg_id, type, payload) values ($1, $2, $3::jsonb)",
            tg_id, type_, payload or {},
        )

    # ─────────────────────── файлы и OCR ───────────────────────

    async def count_duplicate_docs(self, tg_id: int, sha256: str) -> int:
        return await self.pool.fetchval(
            "select count(*) from bot.users "
            "where tg_id <> $1 and (doc_sha256 = $2 or selfie_sha256 = $2)",
            tg_id, sha256,
        ) or 0

    # ─────────────────────── договор ───────────────────────

    async def next_contract_seq(self) -> int:
        """Очередной номер договора.

        nextval, а не «max + 1»: два одновременных подтверждения при втором
        варианте получают один номер, и в двух бумажных договорах оказывается
        одинаковый реквизит. Пропуски в нумерации при откате транзакции
        допустимы, повтор - нет.
        """
        return int(await self.pool.fetchval("select nextval('bot.contract_seq')"))

    async def user_by_mod_message(self, chat_id: int, message_id: int) -> asyncpg.Record | None:
        """Кому принадлежит карточка модерации, на которую ответили.

        Ответ реплаем - это способ отклонить заявку с описанием ошибок.
        Разбирать tg_id из подписи карточки нельзя: любая правка формулировки
        в texts.py тихо ломала бы отказы.
        """
        return await self.pool.fetchrow(
            "select * from bot.users where mod_chat_id = $1 and mod_message_id = $2",
            chat_id, message_id,
        )

    async def clear_anketa(self, tg_id: int) -> None:
        """Стирает паспортные данные и адреса из базы.

        Вызывается, когда договор подписан и зафиксирован в Telegram: дальше
        эти данные боту не нужны, а хранить их «на всякий случай» - ровно
        то, за что спрашивают при проверке. Реквизиты самого договора
        (номер, отпечаток, момент подписания) остаются: без них нечем
        доказать, что подписано именно то, что выдано.
        """
        await self.pool.execute(
            "update bot.users set anketa_enc = null, updated_at = now() where tg_id = $1",
            tg_id,
        )

    async def set_purge_after(self, tg_id: int, days: int) -> None:
        await self.pool.execute(
            "update bot.users set purge_after = now() + ($2 || ' days')::interval, "
            "updated_at = now() where tg_id = $1",
            tg_id, str(days),
        )

    # ─────────────────────── ретеншен ───────────────────────

    async def rows_to_purge(self, limit: int = 200) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "select tg_id, doc_path, selfie_path, contract_path from bot.users "
            "where purge_after is not null and purge_after < now() "
            "  and (doc_path is not null or selfie_path is not null "
            "       or contract_path is not null) "
            "limit $1",
            limit,
        )

    async def clear_files(self, tg_id: int) -> None:
        """Стирает сканы и результаты распознавания.

        Хэши doc_sha256/selfie_sha256 переживают удаление намеренно: это
        единственное, чем ловится повторная регистрация того же документа
        с нового аккаунта. Хэш не позволяет восстановить изображение, но
        остаётся псевдонимным идентификатором - и это должно быть описано
        в политике обработки, а не подразумеваться.

        Файл договора удаляется вместе со сканами, а его реквизиты - номер,
        отпечаток текста и момент подписания - остаются: иначе нечем доказать,
        что было подписано, а сам договор в бумажном виде продолжает
        существовать у сторон.
        """
        await self.pool.execute(
            "update bot.users set doc_file_id = null, doc_path = null, "
            "selfie_file_id = null, selfie_path = null, "
            "doc_ocr = null, name_match = null, ocr_at = null, "
            "contract_path = null, anketa_enc = null, "
            "purge_after = null, updated_at = now() where tg_id = $1",
            tg_id,
        )

    async def prune_updates_log(self, days: int) -> int:
        result = await self.pool.execute(
            "delete from bot.updates_log where created_at < now() - ($1 || ' days')::interval",
            str(days),
        )
        return int(result.rsplit(" ", 1)[-1]) if result else 0
