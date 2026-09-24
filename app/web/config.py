"""Конфигурация веб-панели CRM. Читается теми же помощниками, что у бота:
секреты через *_FILE, пустая переменная = не задана."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import _env, _int, _secret


@dataclass(frozen=True)
class WebConfig:
    pg: dict[str, Any]
    # Ключ подписи cookie сессии. Отдельный docker secret: утечка ключа -
    # это вход в панель без пароля.
    secret: str
    admin_login: str
    # Пароль первого администратора. Нужен один раз - при пустой таблице
    # crm.staff; дальше пароли живут в базе, и переменную можно убрать.
    admin_password: str
    # Токен бота - только для уведомлений клиентам (зачисление, аренда).
    # Панель работает и без него: уведомления тогда просто не уходят.
    bot_token: str
    storage_dir: Path
    port: int
    # За сколько дней до платежа считать аренду «на днях» в дашборде -
    # то же число, что у напоминаний бота.
    remind_before_days: int
    # Имя проката в шапке панели.
    title: str = "МАЙБАЙК"
    # Ссылка на оплату - та же, что у бота. Панель подставляет её
    # в предпросмотр рассылки: {pay_url} в шаблоне должен показывать
    # оператору то же, что увидит клиент.
    pay_url: str = ""
    # Панель стоит за Caddy (задан CRM_DOMAIN): адрес клиента брать из
    # X-Forwarded-For, иначе все входы выглядят как один адрес прокси.
    trust_proxy: bool = False
    # Кому из отправителей верить по X-Forwarded-For. По умолчанию - только
    # частные сети, в которых и живёт compose: Caddy приходит оттуда.
    # «Верить всем» здесь нельзя - uvicorn берёт ПЕРВЫЙ адрес в списке, и
    # клиент, дописавший свой заголовок, подставил бы в протокол подписи
    # выдуманный адрес. Свой прокси в другой сети задаётся этой настройкой
    # списком через запятую (адреса или сети: 10.0.0.0/8).
    trusted_proxies: str = "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"
    # Снимки сверки техники. Отдельный каталог и отдельный том: сканы
    # паспортов панель читает и не пишет, а эти снимки пишет она сама -
    # значит, каталог у них разный, и том с ПДн остаётся read-only.
    bike_photo_dir: Path = Path("/bikes")
    # Загруженные владельцем шаблоны документов, подпись и печать. Том
    # общий с ботом: панель пишет, бот читает.
    doc_dir: Path = Path("/doctemplates")
    # Служебный чат: панель пишет туда то же, что бот, - приход запчасти
    # под стоящий наряд. Пусто - командные сообщения из панели не уходят,
    # и это не ошибка: у панели может не быть своего бота.
    contract_chat_id: str = ""
    # Эквайринг Точки. Панель ходит в банк ровно за одним - за ссылкой
    # на оплату, которую оператор просит при клиенте. Опрос статусов
    # остаётся в процессе бота: круг по счетам из трёх веб-процессов
    # дёргал бы банк втройне, а нажатие кнопки - это один запрос.
    tochka_token: str = ""
    tochka_customer_code: str = ""
    # «Входящие»: ключ переписки (тот же, что у бота) и токен хука
    # /hook/inbox для шлюзов WhatsApp и n8n. Пустой токен - хука нет.
    inbox_key: str = ""
    inbox_hook_token: str = ""

    @classmethod
    def load(cls) -> WebConfig:
        return cls(
            pg={
                "user": _env("POSTGRES_USER", "mybike"),
                "password": _secret("POSTGRES_PASSWORD"),
                "database": _env("POSTGRES_DB", "mybike"),
                "host": _env("POSTGRES_HOST", "postgres"),
                "port": _int("POSTGRES_PORT", "5432"),
            },
            secret=_secret("CRM_SECRET"),
            admin_login=_env("CRM_ADMIN_LOGIN", "admin"),
            admin_password=_secret("CRM_ADMIN_PASSWORD", required=False),
            bot_token=_secret("BOT_TOKEN", required=False),
            storage_dir=Path(_env("STORAGE_DIR", "/files/kyc")),
            bike_photo_dir=Path(_env("BIKE_PHOTO_DIR", "/bikes")),
            doc_dir=Path(_env("DOC_TEMPLATE_DIR", "/doctemplates")),
            port=_int("CRM_PORT", "8080"),
            remind_before_days=_int("REMIND_BEFORE_DAYS", "2"),
            title=_env("CRM_TITLE", "МАЙБАЙК"),
            pay_url=_env("PAY_URL"),
            trust_proxy=bool(_env("CRM_DOMAIN", "")),
            trusted_proxies=_env(
                "CRM_TRUSTED_PROXIES",
                "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"),
            contract_chat_id=_env("CONTRACT_CHAT_ID"),
            tochka_token=_secret("TOCHKA_TOKEN", required=False),
            tochka_customer_code=_env("TOCHKA_CUSTOMER_CODE"),
            inbox_key=_secret("INBOX_KEY", required=False),
            inbox_hook_token=_secret("INBOX_HOOK_TOKEN", required=False),
        )
