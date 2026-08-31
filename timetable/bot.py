"""Хендлеры и точка входа бота расписания.

Long polling, а не вебхук: боту не нужен ни публичный адрес, ни сертификат,
ни открытый порт - он только читает встроенное расписание.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import date, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from . import keyboards, logic, texts
from .config import Config
from .model import LAST_WEEK, monday_of_week

log = logging.getLogger("timetable")
router = Router()


async def answer(message: Message, text: str) -> None:
    """Отправка с учётом лимита длины сообщения Telegram.

    Клавиатуру прикрепляем только в личке: в группе ReplyKeyboardMarkup
    меняет поле ввода всем участникам сразу, а расписание там спрашивает
    один человек.
    """
    markup = keyboards.MAIN if message.chat.type == "private" else None
    for chunk in texts.split_message(text):
        await message.answer(chunk, reply_markup=markup)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await answer(message, texts.start_answer(logic.status(logic.now_msk())))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await answer(message, texts.HELP)


@router.message(Command("now", "next"))
@router.message(F.text == keyboards.NOW)
async def cmd_now(message: Message) -> None:
    await answer(message, texts.status_answer(logic.status(logic.now_msk())))


@router.message(Command("today"))
@router.message(F.text == keyboards.TODAY)
async def cmd_today(message: Message) -> None:
    await _send_day(message, logic.now_msk().date())


@router.message(Command("tomorrow"))
@router.message(F.text == keyboards.TOMORROW)
async def cmd_tomorrow(message: Message) -> None:
    await _send_day(message, logic.now_msk().date() + timedelta(days=1))


def current_week() -> int:
    """Учебная неделя, к которой относятся ответы «на эту неделю»."""
    return logic.display_week(logic.now_msk().date())


@router.message(Command("week"))
async def cmd_week(message: Message, command: CommandObject) -> None:
    """/week - текущая неделя, /week N - конкретная."""
    raw = (command.args or "").strip()
    if not raw:
        # Без номера отвечаем ровно как кнопка «Неделя»: одно и то же
        # действие не должно давать двух разных ответов.
        await answer(message, texts.week_answer(current_week(), logic.week_plan(current_week())))
        return
    week = logic.parse_week(raw)
    if week is None:
        # Ввод обрезаем: без ограничения длинная строка после экранирования
        # раздувает ответ за лимит Telegram, и сообщение не уходит вовсе.
        await answer(message, f"Не понял номер недели: <code>{texts.esc(raw[:32])}</code>. "
                              f"Нужно число от 1 до {LAST_WEEK}, например /week 7.")
        return
    if not 1 <= week <= LAST_WEEK:
        await answer(message, texts.week_answer(week, ()))
        return
    await answer(message, texts.week_answer(week, logic.week_plan(week)))


@router.message(F.text == keyboards.WEEK)
async def btn_week(message: Message) -> None:
    week = current_week()
    await answer(message, texts.week_answer(week, logic.week_plan(week)))


@router.message(Command("day"))
@router.message(F.text == keyboards.DAYS)
async def cmd_day(message: Message) -> None:
    await message.answer("Какой день показать?", reply_markup=keyboards.DAY_PICKER)


@router.callback_query(F.data.startswith("day:"))
async def pick_day(call: CallbackQuery) -> None:
    # Ответить на callback нужно всегда: иначе у пользователя на кнопке
    # висят «часики» до самого таймаута Telegram.
    await call.answer()
    try:
        weekday = int(call.data.split(":", 1)[1])
    except ValueError:
        return
    if not 0 <= weekday <= 5 or call.message is None:
        return
    day = monday_of_week(current_week()) + timedelta(days=weekday)
    await answer(call.message, texts.day_answer(day, logic.occurrences(day)))


@router.message(Command("date"))
async def cmd_date(message: Message, command: CommandObject) -> None:
    """/date 15.09.2026 - расписание на конкретную дату."""
    raw = (command.args or "").strip()
    day = logic.parse_date(raw)
    if day is None:
        await answer(message, "Дату нужно писать как <code>ДД.ММ.ГГГГ</code> или "
                              "<code>ДД.ММ</code>, например /date 15.09.2026")
        return
    await _send_day(message, day)


@router.message(F.chat.type == "private", F.text)
async def fallback(message: Message) -> None:
    """Любой другой текст в личке: подсказка вместо молчания.

    Только в личке. В группе Telegram доставляет боту все сообщения со
    слэша, включая команды чужих ботов, и бот отвечал бы «Не понял» на
    каждую из них, навязывая всем свою клавиатуру.
    """
    await answer(message, "Не понял. Нажмите кнопку ниже или посмотрите /help.")


async def _send_day(message: Message, day: date) -> None:
    await answer(message, texts.day_answer(day, logic.occurrences(day)))


def _install_stop_handlers(dp: Dispatcher) -> None:
    """SIGTERM от docker stop и systemd должен останавливать опрос по-хорошему."""
    running: set[asyncio.Task] = set()

    def stop() -> None:
        # Ссылку на задачу держим в множестве: голый create_task может быть
        # собран сборщиком мусора, и остановка тихо не произойдёт.
        task = asyncio.create_task(dp.stop_polling())
        running.add(task)
        task.add_done_callback(running.discard)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop)
        except NotImplementedError:
            pass  # Windows: сигналы через add_signal_handler не заводятся


async def run(cfg: Config) -> None:
    bot = Bot(cfg.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    _install_stop_handlers(dp)
    try:
        # get_me проверяет токен сразу. Без него бот «запускается» молча и
        # падает только на первом апдейте - то есть когда его уже ждут.
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            log.error("Telegram не принял токен. Проверьте SCHEDULE_BOT_TOKEN "
                      "или выпустите новый у @BotFather")
            return
        except TelegramNetworkError as exc:
            log.error("нет связи с api.telegram.org (%s). Проверьте интернет, "
                      "DNS и прокси на сервере", exc)
            return
        log.info("бот @%s запущен: расписание %s, учебных недель %d",
                 me.username, texts.GROUP, LAST_WEEK)
        # Оставшийся от прошлого запуска вебхук молча съедает все апдейты, и
        # long polling получает пустоту: снимаем его перед стартом.
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        log.info("останавливаюсь")
        await bot.session.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # Конфигурацию читаем до запуска цикла: забытый токен должен давать
    # одну понятную строку, а не десять кадров стека.
    try:
        cfg = Config.load()
    except RuntimeError as exc:
        print(f"Бот не запущен: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
