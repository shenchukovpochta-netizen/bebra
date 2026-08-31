"""Сборка ответов бота. HTML-разметка Telegram, никакого Markdown.

Markdown у Telegram ломается на любом подчёркивании и звёздочке из данных,
а в расписании они встречаются (номера аудиторий, «л+пр»). HTML требует
экранировать всего три символа - это надёжнее.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .logic import Status
from .model import (
    DAY_NAMES,
    LAST_WEEK,
    PARITY_NAMES,
    Occurrence,
    monday_of_week,
    pair_number,
    week_number,
)

MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

GROUP = "06-445"
COURSE = "3 курс"
PROGRAM = "нанотехнологии и микросистемная техника"


def esc(text: str | None) -> str:
    """Экранирование под HTML-разметку Telegram.

    Амперсанд обязательно первым: иначе уже подставленный &lt; превратится
    в &amp;lt; и пользователь увидит сырую сущность.
    """
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def human_date(day: date) -> str:
    return f"{DAY_NAMES[day.weekday()].lower()}, {day.day} {MONTHS[day.month - 1]}"


def hhmm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def minutes(total: int) -> str:
    """«1 ч 25 мин» вместо «85 мин»: на длинных промежутках так читаемее."""
    if total < 60:
        return f"{total} мин"
    hours, rest = divmod(total, 60)
    return f"{hours} ч" if rest == 0 else f"{hours} ч {rest} мин"


def week_line(week: int) -> str:
    if week < 1:
        return f"Семестр начнётся {monday_of_week(1).strftime('%d.%m.%Y')}"
    if week > LAST_WEEK:
        return "Семестр закончился"
    parity = "чётная" if week % 2 == 0 else "нечётная"
    return f"{week}-я учебная неделя ({parity})"


def time_header(occ: Occurrence) -> str:
    number = pair_number(occ.slot.start)
    tag = f"{number}-я пара · " if number else ""
    return f"<b>{tag}{hhmm(occ.starts_at)}–{hhmm(occ.ends_at)}</b>"


def lesson_body(lesson) -> str:
    """Занятие без времени: предмет, преподаватель, аудитория, оговорки."""
    lines = [f"📚 {esc(lesson.subject)}"
             + (f" <i>({esc(lesson.kind)})</i>" if lesson.kind else "")]
    if lesson.teacher:
        lines.append(f"👤 {esc(lesson.teacher)}")
    if lesson.room:
        lines.append(f"📍 {esc(lesson.room)}")
    # Подгруппы и чётность - не украшение: без них студент второй подгруппы
    # придёт на пару первой. Собираем в одну строку, чтобы не раздувать ответ.
    marks = [m for m in (esc(lesson.subgroup), PARITY_NAMES[lesson.parity], esc(lesson.note)) if m]
    if marks:
        lines.append(f"<i>ℹ️ {'; '.join(marks)}</i>")
    return "\n".join(lines)


def group_by_time(occs: tuple[Occurrence, ...]) -> list[tuple[Occurrence, ...]]:
    """Занятия, идущие в одно и то же время, - в одну группу.

    В одной клетке расписания их бывает два (четверг, 10:10: английский и
    немецкий у разных подгрупп). Без группировки ответ выглядел бы как две
    разные «2-е пары» подряд, будто одна идёт после другой.
    """
    groups: list[tuple[Occurrence, ...]] = []
    for occ in occs:
        if groups and (groups[-1][0].starts_at, groups[-1][0].ends_at) == (occ.starts_at, occ.ends_at):
            groups[-1] = groups[-1] + (occ,)
        else:
            groups.append((occ,))
    return groups


def lesson_block(occs: tuple[Occurrence, ...]) -> str:
    """Время один раз, под ним - все занятия этого времени."""
    return time_header(occs[0]) + "\n" + "\n\n".join(lesson_body(o.lesson) for o in occs)


def day_answer(day: date, occs: tuple[Occurrence, ...]) -> str:
    """Расписание одного дня."""
    week = week_number(day)
    head = f"📅 <b>{human_date(day).capitalize()}</b>\n{week_line(week)}"
    if week < 1 or week > LAST_WEEK:
        return f"{head}\n\nВ этот день пар нет: дата вне осеннего семестра."
    if not occs:
        return f"{head}\n\n🎉 Пар нет."
    return head + "\n\n" + "\n\n".join(lesson_block(g) for g in group_by_time(occs))


def status_answer(st: Status) -> str:
    """Ответ на «что сейчас» - главный экран бота."""
    head = (
        f"🕒 <b>{hhmm(st.moment)}</b>, {human_date(st.moment.date())} (МСК, UTC+3)\n"
        f"{week_line(st.week)}"
    )
    if not st.in_semester:
        tail = ""
        if st.following:
            tail = "\n\n⏭ <b>Первая пара семестра:</b>\n" + _following_block(st)
        return head + "\n\nСейчас пар нет: время вне осеннего семестра." + tail

    parts = [head]
    if st.current:
        left = st.minutes_left
        parts.append(
            f"▶️ <b>Сейчас идёт</b> (до конца {minutes(left)}):\n\n"
            + "\n\n".join(lesson_block(g) for g in group_by_time(st.current))
        )
    elif st.following and st.next_is_today:
        parts.append(f"☕️ <b>Сейчас перемена.</b> До следующей пары {minutes(st.minutes_until)}.")
    else:
        parts.append("✅ <b>Пары на сегодня закончились.</b>")

    if st.following:
        when = "Далее" if st.next_is_today else "Следующая пара"
        parts.append(f"⏭ <b>{when}</b> — {_when_phrase(st)}:\n\n" + _following_block(st))
    else:
        parts.append("🏁 Пар до конца семестра больше нет.")
    return "\n\n".join(parts)


def _when_phrase(st: Status) -> str:
    """«через 12 мин» для сегодняшней пары, «завтра в 08:30» для дальней."""
    nxt = st.following[0]
    if st.next_is_today:
        return f"через {minutes(st.minutes_until)}"
    days = (nxt.day - st.moment.date()).days
    prefix = "завтра" if days == 1 else human_date(nxt.day)
    return f"{prefix} в {hhmm(nxt.starts_at)}"


def _following_block(st: Status) -> str:
    return "\n\n".join(lesson_block(g) for g in group_by_time(st.following))


def week_answer(week: int, plan: tuple[tuple[date, tuple[Occurrence, ...]], ...]) -> str:
    """Вся неделя одним сообщением, компактными строками."""
    if week < 1 or week > LAST_WEEK:
        return f"Недели №{week} в осеннем семестре нет: недели идут с 1-й по {LAST_WEEK}-ю."
    monday = monday_of_week(week)
    saturday = monday + timedelta(days=5)
    head = (
        f"🗓 <b>{week_line(week)}</b>\n"
        f"{monday.strftime('%d.%m')} — {saturday.strftime('%d.%m')}"
    )
    body: list[str] = []
    for day, occs in plan:
        title = f"<b>{DAY_NAMES[day.weekday()]}, {day.strftime('%d.%m')}</b>"
        if not occs:
            body.append(f"{title}\n— пар нет")
            continue
        rows = []
        for occ in occs:
            room = f" · {esc(occ.lesson.room)}" if occ.lesson.room else ""
            kind = f" ({esc(occ.lesson.kind)})" if occ.lesson.kind else ""
            rows.append(
                f"{hhmm(occ.starts_at)}–{hhmm(occ.ends_at)} {esc(occ.lesson.subject)}{kind}{room}"
            )
        body.append(title + "\n" + "\n".join(rows))
    return head + "\n\n" + "\n\n".join(body)


def start_answer(st: Status) -> str:
    return (
        f"👋 Расписание группы <b>{GROUP}</b> ({COURSE}, {esc(PROGRAM)}), "
        f"осенний семестр 2026/2027.\n\n"
        f"{week_line(st.week)}. Время московское, UTC+3.\n\n"
        "Что умею:\n"
        "• <b>Сейчас</b> — какая пара идёт и какая начнётся следующей\n"
        "• <b>Сегодня</b> / <b>Завтра</b> — расписание дня\n"
        "• <b>Неделя</b> — вся текущая учебная неделя\n"
        "• <b>Дни</b> — расписание любого дня недели\n\n"
        "Команды: /now, /today, /tomorrow, /week, /day, /help"
    )


HELP = (
    f"📖 <b>Справка</b>\n\n"
    f"Бот показывает расписание группы {GROUP} ({COURSE}) на осенний семестр 2026/2027.\n\n"
    "<b>Команды</b>\n"
    "/now — какая пара идёт сейчас и какая скоро начнётся\n"
    "/today — расписание на сегодня\n"
    "/tomorrow — расписание на завтра\n"
    "/week — текущая учебная неделя целиком\n"
    "/week 7 — конкретная учебная неделя\n"
    "/day — выбрать день недели кнопкой\n"
    "/date 15.09.2026 — расписание на дату\n"
    "/help — эта справка\n\n"
    "<b>Как считаются недели</b>\n"
    f"Отсчёт идёт с 1 сентября. Неделя №1 — та, в которую попадает 01.09.2026, "
    f"то есть {monday_of_week(1).strftime('%d.%m')} — "
    f"{(monday_of_week(1) + timedelta(days=6)).strftime('%d.%m.%Y')}. "
    f"Всего учебных недель: {LAST_WEEK}, последняя заканчивается "
    f"{(monday_of_week(LAST_WEEK) + timedelta(days=6)).strftime('%d.%m.%Y')}.\n"
    "«ч.н.» в расписании — чётная неделя, «н.н.» — нечётная.\n\n"
    "Всё время — московское (UTC+3)."
)


# Telegram режет сообщение длиннее 4096 символов - причём не обрезает,
# а отвергает запрос целиком, и пользователь не получает ничего. Берём
# запас: HTML-теги считаются в лимит, а расписание недели у старших курсов
# растёт от семестра к семестру.
LIMIT = 3800


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    """Разбить длинный ответ по границам абзацев, не разрывая теги.

    Режем только по пустой строке между блоками: разрыв внутри блока
    оставил бы незакрытый <b>, и Telegram отверг бы уже вторую часть.
    Блок, который сам длиннее лимита, отдаём как есть - лучше получить
    ошибку на одном блоке, чем молча потерять половину расписания.
    """
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if current and len(candidate) > limit:
            parts.append(current)
            current = block
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts
