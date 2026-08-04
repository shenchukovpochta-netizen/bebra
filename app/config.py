"""Конфигурация. Секреты читаются из файлов, а не из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import logic


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    """Значение переменной окружения.

    Пустая строка считается отсутствием значения. Это не придирка: docker
    compose подставляет "" для любого ${VAR}, которого нет в .env, и наивное
    os.environ.get(name, default) вернёт "" вместо значения по умолчанию.
    Так пустыми уезжали версии оферты и политики, а пустой CHANNEL_URL давал
    кнопку с url="" - Telegram отвергает такое сообщение целиком.
    """
    value = os.environ.get(name)
    if value is None or not value.strip():
        value = default
    if required and not value:
        raise RuntimeError(f"не задана переменная окружения {name}")
    return value or ""


def _secret(name: str, *, required: bool = True) -> str:
    """Поддержка суффикса _FILE: значение читается из файла (docker secrets).

    Пароли и ключи не должны лежать в окружении: `docker inspect`, дампы
    процессов и логи падений вытаскивают env целиком.
    """
    path = os.environ.get(f"{name}_FILE")
    if path:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except PermissionError as exc:
            # Вне swarm compose монтирует файл секрета как есть, с владельцем
            # и правами хоста. Процесс бота работает под uid 10001, и файл
            # root:root 600 он не откроет. Голый PermissionError на путь
            # /run/secrets/... ничего не подсказывает: путь-то внутренний.
            raise RuntimeError(
                f"нет доступа к файлу секрета {name}: {path}. На сервере, "
                f"в каталоге проекта: chown 10001:10001 secrets/*"
            ) from exc
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"файл секрета {name} не найден: {path}. Проверьте, что "
                f"secrets/ не пуст: ls -l secrets/"
            ) from exc
    else:
        value = os.environ.get(name, "").strip()
    if required and not value:
        raise RuntimeError(f"не задан секрет {name} (или {name}_FILE)")
    return value


def _ids(raw: str) -> tuple[int, ...]:
    return tuple(int(x) for x in raw.replace(",", " ").split() if x.strip())


def _int(name: str, default: str | None = None, *, required: bool = False) -> int:
    raw = _env(name, default, required=required)
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} должно быть целым числом, получено {raw!r}") from exc


def _int_or_none(name: str) -> int | None:
    """Необязательное числовое значение.

    Отличать «не задано» от нуля обязательно: message_thread_id = 0 - это
    не «без темы», а невалидный номер темы, и Telegram отвергнет отправку.
    """
    raw = _env(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} должно быть целым числом, получено {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    bot_token: str
    channel_id: int
    admin_chat_id: int
    admins: tuple[int, ...]

    # Параметры подключения по отдельности, а не строкой DSN. Пароль из
    # `openssl rand -base64 24` содержит "/" примерно в 40% случаев, и первый
    # же слэш обрывает authority-часть URL: хост становится мусором, пароль
    # теряется. Отдельные аргументы create_pool снимают вопрос целиком.
    pg: dict[str, Any]
    storage_dir: Path

    # Ключ шифрования анкеты. Паспортные данные и адреса лежат в базе только
    # в зашифрованном виде, ключ - отдельным файлом, чтобы дамп базы без него
    # ничего не давал.
    pdn_key: str
    # Личка того, кто утверждает договоры (@arenda_velo_kazan). Бот не может
    # написать первым, поэтому владелец аккаунта обязан один раз нажать /start,
    # а сюда прописывается его ЧИСЛОВОЙ id - username Telegram API не примет.
    contract_chat_id: int
    # Куда уходит подписанный договор. Обычно группа с темами: fix_topic_id -
    # номер темы «Фиксация сдачи».
    fix_chat_id: int
    fix_topic_id: int | None
    contract_template: Path

    channel_url: str
    oferta_url: str
    oferta_version: str
    pdn_url: str
    pdn_version: str
    video_url: str

    purge_approved_days: int
    purge_rejected_days: int
    updates_log_days: int
    rate_soft: int
    rate_hard: int

    auto_approve: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def consent_version(self) -> str:
        """Редакция согласия, которая пишется в базу.

        Совпадает с редакцией документа: данные никуда за пределы проката
        не передаются, отличать «согласился до подключения обработчика»
        от «после» больше не нужно. Фиксировать редакцию всё равно надо -
        иначе не доказать, под какой человек подписался.
        """
        return self.pdn_version

    @classmethod
    def load(cls) -> "Config":
        return cls(
            bot_token=_secret("BOT_TOKEN"),
            channel_id=_int("CHANNEL_ID", required=True),
            admin_chat_id=_int("ADMIN_CHAT_ID", required=True),
            admins=_ids(_env("ADMINS", required=True)),
            pg={
                "user": _env("POSTGRES_USER", "mybike"),
                "password": _secret("POSTGRES_PASSWORD"),
                "database": _env("POSTGRES_DB", "mybike"),
                "host": _env("POSTGRES_HOST", "postgres"),
                "port": _int("POSTGRES_PORT", "5432"),
            },
            storage_dir=Path(_env("STORAGE_DIR", "/files/kyc")),
            pdn_key=_secret("PDN_KEY"),
            # По умолчанию договоры едут в тот же чат модерации, что и заявки:
            # так бот остаётся работоспособным, пока владелец @arenda_velo_kazan
            # не нажал /start и его числовой id ещё неизвестен.
            contract_chat_id=_int("CONTRACT_CHAT_ID", _env("ADMIN_CHAT_ID", required=True)),
            fix_chat_id=_int("FIX_CHAT_ID", _env("ADMIN_CHAT_ID", required=True)),
            fix_topic_id=_int_or_none("FIX_TOPIC_ID"),
            contract_template=Path(
                _env("CONTRACT_TEMPLATE", "/srv/app/contract_template.md")),
            channel_url=_env("CHANNEL_URL", "https://t.me/mybike"),
            oferta_url=_env("OFERTA_URL", required=True),
            oferta_version=_env("OFERTA_VERSION", "2026-01-15"),
            # Необязателен: согласие на обработку данных включено в оферту.
            # Если политику опубликуют отдельным документом - заполните
            # переменную, и на экране согласия появится вторая кнопка.
            pdn_url=_env("PDN_URL"),
            # Редакция согласия. По умолчанию совпадает с редакцией оферты,
            # потому что согласие лежит внутри неё. Фиксировать всё равно надо:
            # иначе не доказать, под какой редакцией человек подписался.
            pdn_version=_env("PDN_VERSION", _env("OFERTA_VERSION", "2026-01-15")),
            video_url=_env("VIDEO_URL", "https://youtu.be/CyZzskq8o0o"),
            purge_approved_days=_int("PURGE_APPROVED_DAYS", "90"),
            purge_rejected_days=_int("PURGE_REJECTED_DAYS", "3"),
            updates_log_days=_int("UPDATES_LOG_DAYS", "7"),
            rate_soft=_int("RATE_SOFT", str(logic.RATE_SOFT_DEFAULT)),
            rate_hard=_int("RATE_HARD", str(logic.RATE_HARD_DEFAULT)),
            auto_approve=_env("AUTO_APPROVE", "0") == "1",
        )
