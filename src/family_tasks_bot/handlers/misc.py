from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup

from family_tasks_bot.deps import get_repositories
from family_tasks_bot.db.repositories import NotificationRepository, PlannedTaskRepository, TaskRuntimeRepository
from family_tasks_bot.keyboards.reply import groups_menu, main_menu, misc_menu, stats_menu
from family_tasks_bot.services.bootstrap import ensure_member_context
from family_tasks_bot.states import GroupStates, NavStates, StatsStates
from family_tasks_bot.version import APP_VERSION

router = Router(name="misc")
QUIET_RE = re.compile(r"^/quiet\s+(\d{2}:\d{2})-(\d{2}:\d{2})(?:\s+(all|[0-6]))?$")
STATS_RE = re.compile(r"^/stats(?:\s+(day|week|month))?$")
PAGE_SIZE = 10
STATS_HISTORY_CONTEXT_KEY = "stats_history_context"
WEEKDAY_SHORT_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _family_tzinfo(timezone_name: str) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return timezone.utc


def _parse_raw_timestamp(raw_value: str) -> datetime | None:
    raw = (raw_value or "").strip()
    if not raw:
        return None
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _to_family_local_datetime(raw_value: str, timezone_name: str) -> datetime | None:
    parsed = _parse_raw_timestamp(raw_value)
    if parsed is None:
        return None
    local = parsed.astimezone(_family_tzinfo(timezone_name))
    return local


def _to_family_local_timestamp(raw_value: str, timezone_name: str) -> str:
    local = _to_family_local_datetime(raw_value, timezone_name)
    if local is None:
        return raw_value
    return local.strftime("%Y-%m-%d %H:%M")


def _build_day_pages(
    entries: list,
    timezone_name: str,
    *,
    reverse_input: bool = True,
) -> list[dict]:
    ordered_entries = list(reversed(entries)) if reverse_input else list(entries)
    pages_asc: list[dict] = []
    current_page: dict | None = None
    current_day_key: str | None = None

    def _make_page(day_key: str, weekday_short: str, header: str) -> dict:
        return {
            "day_key": day_key,
            "weekday_short": weekday_short,
            "weekday_cap": weekday_short.capitalize(),
            "header": header,
            "items": [],
        }

    for entry in ordered_entries:
        raw_completed_at = str(entry["completed_at"])
        local_dt = _to_family_local_datetime(raw_completed_at, timezone_name)
        if local_dt is None:
            day_key = f"raw:{raw_completed_at}"
            weekday_short = "?"
            day_header = "дата неизвестна:"
            time_part = raw_completed_at
        else:
            date_part = local_dt.strftime("%Y-%m-%d")
            weekday_short = WEEKDAY_SHORT_RU[local_dt.weekday()]
            day_key = date_part
            day_header = f"{weekday_short} ({date_part}):"
            time_part = local_dt.strftime("%H:%M")
        if day_key != current_day_key:
            current_page = _make_page(day_key, weekday_short, day_header)
            pages_asc.append(current_page)
            current_day_key = day_key
        current_page["items"].append((time_part, entry))

    return list(reversed(pages_asc))


def _build_day_nav_markup(
    day_pages: list[dict],
    day_index: int,
    day_callback_builder,
    back_callback_data: str,
) -> InlineKeyboardMarkup | None:
    left_button = InlineKeyboardButton(text=" ", callback_data="statsnoop")
    right_button = InlineKeyboardButton(text=" ", callback_data="statsnoop")
    if day_index + 1 < len(day_pages):
        target = day_pages[day_index + 1]["weekday_cap"]
        left_button = InlineKeyboardButton(
            text=f"< ({target})",
            callback_data=day_callback_builder(day_index + 1),
        )
    if day_index > 0:
        target = day_pages[day_index - 1]["weekday_cap"]
        right_button = InlineKeyboardButton(
            text=f"({target}) >",
            callback_data=day_callback_builder(day_index - 1),
        )
    middle_button = InlineKeyboardButton(text="Назад", callback_data=back_callback_data)
    return InlineKeyboardMarkup(inline_keyboard=[[left_button, middle_button, right_button]])


def _render_day_page_lines(
    title: str,
    day_pages: list[dict],
    day_index: int,
    entry_tail_builder,
    empty_text: str,
) -> tuple[list[str], int]:
    if not day_pages:
        return ([title, empty_text], 0)
    normalized_day_index = max(0, min(day_index, len(day_pages) - 1))
    page = day_pages[normalized_day_index]
    lines = [title, page["header"]]
    for time_part, entry in page["items"]:
        lines.append(f"- {time_part} {entry_tail_builder(entry)}")
    return (lines, normalized_day_index)


def _history_line(entry: dict, timezone_name: str) -> str:
    local_completed_at = _to_family_local_timestamp(str(entry["completed_at"]), timezone_name)
    effort_stars = int(entry["effort_stars"]) if hasattr(entry, "keys") and "effort_stars" in entry.keys() else 1
    return f"- {local_completed_at} | {entry['task_title']} | {entry['member_display_name']} | {effort_stars}★"


def _parse_local_datetime_to_utc(value: str, timezone_name: str) -> str | None:
    raw = (value or "").strip()
    try:
        local_naive = datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    tzinfo = _family_tzinfo(timezone_name)
    local_dt = local_naive.replace(tzinfo=tzinfo)
    utc_dt = local_dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%d %H:%M:%S")


def _groups_editor_keyboard(groups: list) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=f"{group['sort_order']}. {group['name']}", callback_data=f"groupedit:{group['id']}")]
        for group in groups
    ]
    rows.append([InlineKeyboardButton(text="Добавить группу", callback_data="groupadd")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_action_label(task_title: str, completion_mode: str) -> str:
    return f"{task_title} [{completion_mode}]"


def _default_stats_history_context() -> dict:
    return {
        "mode": "global",
        "day_index": 0,
        "user_id": None,
        "task_id": None,
        "source_token": "root",
        "member_display_name": None,
        "task_title": None,
    }


async def _get_stats_history_context(state: FSMContext) -> dict:
    data = await state.get_data()
    raw = data.get(STATS_HISTORY_CONTEXT_KEY)
    merged = _default_stats_history_context()
    if isinstance(raw, dict):
        merged.update(raw)
    return merged


async def _save_stats_history_context(state: FSMContext, **kwargs: object) -> None:
    cur = await _get_stats_history_context(state)
    cur.update(kwargs)
    await state.update_data({STATS_HISTORY_CONTEXT_KEY: cur})


async def _clear_state_keep_stats_context(state: FSMContext) -> None:
    preserved = (await state.get_data()).get(STATS_HISTORY_CONTEXT_KEY)
    await state.clear()
    if isinstance(preserved, dict):
        await state.update_data({STATS_HISTORY_CONTEXT_KEY: preserved})


def _truncate_button_text(text: str, max_len: int = 60) -> str:
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3]}..."


def _history_edit_row_label(mode: str, time_part: str, entry: dict) -> str:
    task_action = _format_action_label(str(entry["task_title"]), str(entry["completion_mode"]))
    executor = str(entry["member_display_name"])
    if mode == "member":
        line = f"{time_part} - {task_action}"
    elif mode == "task":
        line = f"{time_part} - {executor}"
    else:
        line = f"{time_part} - {task_action} - {executor}"
    return _truncate_button_text(line)


def _history_edit_header_text(page: dict, ctx: dict) -> str:
    day_key = str(page["day_key"])
    wc = str(page["weekday_cap"])
    if re.match(r"^\d{4}-\d{2}-\d{2}$", day_key):
        try:
            d = datetime.strptime(day_key, "%Y-%m-%d").date()
            dt_suffix = d.strftime("%d.%m.%Y")
        except ValueError:
            dt_suffix = day_key
    else:
        dt_suffix = day_key
    parts = [f"{wc} {dt_suffix}"]
    mode = str(ctx.get("mode") or "global")
    if mode == "member" and ctx.get("member_display_name"):
        parts.append(str(ctx["member_display_name"]))
    if mode == "task" and ctx.get("task_title"):
        parts.append(str(ctx["task_title"]))
    return _truncate_button_text(" - ".join(parts), max_len=120)


def _hist_nav_day_callback_builder(ctx: dict) -> Callable[[int], str]:
    mode = str(ctx.get("mode") or "global")

    def builder(target_idx: int) -> str:
        if mode == "global":
            return f"hedg:{target_idx}"
        if mode == "member":
            return f"hedm:{int(ctx['user_id'])}:{target_idx}"
        tid = int(ctx["task_id"])
        src = str(ctx.get("source_token") or "root")
        return f"hedt:{tid}:{src}:{target_idx}"

    return builder


def _hist_nav_back_callback_data(ctx: dict) -> str:
    mode = str(ctx.get("mode") or "global")
    if mode == "global":
        return "statsback:global"
    if mode == "member":
        return "statsback:member"
    return f"statsback:task:{ctx.get('source_token') or 'root'}"


async def _load_day_pages_for_stats_context(
    runtime: TaskRuntimeRepository, family_id: int, ctx: dict, timezone_name: str
) -> list[dict]:
    mode = str(ctx.get("mode") or "global")
    if mode == "member":
        rows = await runtime.list_recent_actions_by_member_all(family_id, int(ctx["user_id"]))
    elif mode == "task":
        rows = await runtime.list_recent_actions_by_task_all(family_id, int(ctx["task_id"]))
    else:
        rows = await runtime.list_recent_actions_all(family_id)
    return _build_day_pages(rows, timezone_name, reverse_input=True)


async def _build_history_edit_markup_for_context(
    runtime: TaskRuntimeRepository,
    family_id: int,
    ctx: dict,
    timezone_name: str,
) -> tuple[InlineKeyboardMarkup, int]:
    day_pages = await _load_day_pages_for_stats_context(runtime, family_id, ctx, timezone_name)
    day_index = int(ctx.get("day_index") or 0)
    if day_pages:
        normalized = max(0, min(day_index, len(day_pages) - 1))
    else:
        normalized = 0
    mode = str(ctx.get("mode") or "global")
    rows_kb: list[list[InlineKeyboardButton]] = []
    if day_pages:
        page = day_pages[normalized]
        rows_kb.append(
            [InlineKeyboardButton(text=_history_edit_header_text(page, ctx), callback_data="noop")]
        )
        for time_part, entry in page["items"]:
            cid = int(entry["completion_id"])
            label = _history_edit_row_label(mode, time_part, entry)
            rows_kb.append([InlineKeyboardButton(text=label, callback_data=f"histedit:{cid}")])
    else:
        rows_kb.append([InlineKeyboardButton(text="История пуста", callback_data="noop")])
    nav = _build_day_nav_markup(
        day_pages,
        normalized,
        _hist_nav_day_callback_builder(ctx),
        _hist_nav_back_callback_data(ctx),
    )
    if nav is not None:
        rows_kb.extend(nav.inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows_kb), normalized


HISTORY_EDIT_PROMPT = "Выберите запись истории для правки:"


async def _prepare_history_edit_reply_markup(
    state: FSMContext,
    runtime: TaskRuntimeRepository,
    family_id: int,
    timezone_name: str,
) -> InlineKeyboardMarkup:
    ctx = await _get_stats_history_context(state)
    kb, normalized = await _build_history_edit_markup_for_context(runtime, family_id, ctx, timezone_name)
    await _save_stats_history_context(state, day_index=normalized)
    return kb


@router.message(F.text == "Прочее")
async def open_misc(message: Message) -> None:
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    await message.answer("Раздел Прочее", reply_markup=misc_menu(is_admin=ctx.is_admin))


async def _send_alice_link_code(message: Message) -> None:
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await message.answer("Вы пока не добавлены в семью.")
        return
    code = await user_repo.create_alice_link_code(ctx.family_id, ctx.user_id, ttl_minutes=10)
    await message.answer(
        "Код привязки Алисы: "
        f"{code}\n"
        "Срок действия: 10 минут.\n"
        "Скажите в навыке Алисы этот код для привязки."
    )


@router.message(F.text == "Код для Алисы")
async def alice_link_code_from_button(message: Message) -> None:
    await _send_alice_link_code(message)


@router.message(F.text.regexp(r"^/alice_link$"))
async def alice_link_code_from_command(message: Message) -> None:
    await _send_alice_link_code(message)


@router.message(F.text == "Группы")
async def open_groups(message: Message, state: FSMContext) -> None:
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await message.answer("Вы пока не добавлены в семью.")
        return
    await state.set_state(NavStates.in_groups_menu)
    await message.answer("Меню групп", reply_markup=groups_menu(is_admin=ctx.is_admin))


@router.message(NavStates.in_groups_menu, F.text == "Список")
async def list_groups(message: Message) -> None:
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await message.answer("Вы пока не добавлены в семью.")
        return
    groups = await family_repo.list_groups(ctx.family_id)
    if not groups:
        await message.answer("Группы пока не добавлены.")
        return
    lines = ["Группы:"]
    for group in groups:
        lines.append(f"- {group['sort_order']}. {group['name']}")
    await message.answer("\n".join(lines))


@router.message(NavStates.in_groups_menu, F.text == "Правка групп")
async def edit_groups_open(message: Message) -> None:
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await message.answer("Вы пока не добавлены в семью.")
        return
    if not ctx.is_admin:
        await message.answer("Правка групп доступна только администраторам.")
        return
    groups = await family_repo.list_groups(ctx.family_id)
    await message.answer(
        "Выберите группу для правки:",
        reply_markup=_groups_editor_keyboard(groups),
    )


@router.message(F.text == "Статистика")
async def statistics(message: Message, state: FSMContext) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await message.answer("Вы пока не добавлены в семью.")
        return
    await state.set_state(NavStates.in_stats_menu)
    runtime = TaskRuntimeRepository(db)
    timezone_name = ctx.family_timezone or "UTC"
    await _send_recent_actions(message, runtime, ctx.family_id, timezone_name, state)
    await message.answer("Меню статистики:", reply_markup=stats_menu(is_admin=ctx.is_admin))


@router.message(NavStates.in_stats_menu, F.text == "Текущая неделя")
async def stats_current_week(message: Message) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await message.answer("Вы пока не добавлены в семью.")
        return
    runtime = TaskRuntimeRepository(db)
    timezone_name = ctx.family_timezone or "UTC"
    by_user, active, scheduled, start_date, end_date = await runtime.stats_summary_current_week(ctx.family_id, timezone_name)
    by_stars = await runtime.stats_stars_by_user_current_week(ctx.family_id, timezone_name)
    by_task, _, _ = await runtime.stats_by_task_type_current_week(ctx.family_id, timezone_name)
    lines = [
        f"Статистика за неделю ({start_date} - {end_date}):",
        f"Часовой пояс семьи: {timezone_name}",
    ]
    if by_user:
        lines.append("Выполнено по участникам:")
        for row in by_user:
            lines.append(f"- {row['display_name']}: {row['cnt']}")
    else:
        lines.append("Пока нет выполнений.")
    if by_stars:
        lines.append("Заработано звёзд по участникам:")
        for row in by_stars:
            lines.append(f"- {row['display_name']}: {row['stars']}")
    if by_task:
        lines.append("По типам задач:")
        for row in by_task[:10]:
            lines.append(f"- {row['title']}: {row['cnt']}")
    lines.append(f"Активные задачи: {active}")
    lines.append(f"Запланированные задачи: {scheduled}")
    await message.answer("\n".join(lines), reply_markup=stats_menu(is_admin=ctx.is_admin))


@router.message(NavStates.in_stats_menu, F.text == "По члену семьи")
async def stats_by_member_menu(message: Message) -> None:
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await message.answer("Вы пока не добавлены в семью.")
        return
    members = await family_repo.list_members_for_edit(ctx.family_id)
    buttons = [
        [InlineKeyboardButton(text=str(member["display_name"]), callback_data=f"statsm:{member['user_id']}:0")]
        for member in members
    ]
    await message.answer(
        "Выберите участника:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons or [[InlineKeyboardButton(text="Нет участников", callback_data="noop")]]
        ),
    )


@router.message(NavStates.in_stats_menu, F.text == "По задаче")
async def stats_by_task_menu(message: Message) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await message.answer("Вы пока не добавлены в семью.")
        return
    repo = PlannedTaskRepository(db)
    root_text, root_kb = await _build_stats_task_root_picker(repo, family_repo, ctx.family_id)
    await message.answer(
        root_text,
        reply_markup=root_kb,
    )


@router.message(F.text == "О боте")
async def about_bot(message: Message) -> None:
    await message.answer(f"Family Tasks Bot\nВерсия: {APP_VERSION}")


@router.message(F.text.regexp(r"^/stats"))
async def statistics_command(message: Message) -> None:
    match = STATS_RE.match((message.text or "").strip().lower())
    if not match:
        await message.answer("Формат: /stats [day|week|month]")
        return
    period = match.group(1) or "week"
    await _send_stats(message, period)


async def _send_stats(message: Message, period: str, reply_markup: ReplyKeyboardMarkup | None = None) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await message.answer("Вы пока не добавлены в семью.")
        return
    runtime = TaskRuntimeRepository(db)
    days = {"day": 1, "week": 7, "month": 30}[period]
    timezone_name = ctx.family_timezone or "UTC"
    by_user, active, scheduled = await runtime.stats_summary(ctx.family_id, days, timezone_name)
    by_task = await runtime.stats_by_task_type(ctx.family_id, days, timezone_name)
    title = {"day": "день", "week": "неделю", "month": "месяц"}[period]
    lines = [f"Статистика за {title}:", f"Часовой пояс семьи: {timezone_name}"]
    if by_user:
        lines.append("Выполнено по участникам:")
        for row in by_user:
            lines.append(f"- {row['display_name']}: {row['cnt']}")
    else:
        lines.append("Пока нет выполнений.")
    if by_task:
        lines.append("По типам задач:")
        for row in by_task[:10]:
            lines.append(f"- {row['title']}: {row['cnt']}")
    lines.append(f"Активные задачи: {active}")
    lines.append(f"Запланированные задачи: {scheduled}")
    await message.answer("\n".join(lines), reply_markup=reply_markup)


async def _send_recent_actions(
    message: Message,
    runtime: TaskRuntimeRepository,
    family_id: int,
    timezone_name: str,
    state: FSMContext,
    day_index: int = 0,
) -> None:
    rows = await runtime.list_recent_actions_all(family_id)
    day_pages = _build_day_pages(rows, timezone_name, reverse_input=True)
    lines, normalized_day_index = _render_day_page_lines(
        "Последние действия:",
        day_pages,
        day_index,
        lambda row: (
            f"| {row['task_title']} | {row['member_display_name']} | "
            f"{int(row['effort_stars']) if row['effort_stars'] is not None else 1}★"
        ),
        "История пока пуста.",
    )
    kb = _build_day_nav_markup(
        day_pages,
        normalized_day_index,
        lambda target_idx: f"statsg:{target_idx}",
        "statsback:global",
    )
    await message.answer("\n".join(lines), reply_markup=kb)
    await _save_stats_history_context(
        state,
        mode="global",
        day_index=normalized_day_index,
        user_id=None,
        task_id=None,
        source_token="root",
        member_display_name=None,
        task_title=None,
    )


@router.callback_query(F.data.startswith("statsg:"))
async def stats_global_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer()
        return
    day_index = max(0, int(callback.data.split(":")[1]))
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    runtime = TaskRuntimeRepository(db)
    timezone_name = ctx.family_timezone or "UTC"
    rows = await runtime.list_recent_actions_all(ctx.family_id)
    day_pages = _build_day_pages(rows, timezone_name, reverse_input=True)
    lines, normalized_day_index = _render_day_page_lines(
        "Последние действия:",
        day_pages,
        day_index,
        lambda row: (
            f"| {row['task_title']} | {row['member_display_name']} | "
            f"{int(row['effort_stars']) if row['effort_stars'] is not None else 1}★"
        ),
        "История пока пуста.",
    )
    kb = _build_day_nav_markup(
        day_pages,
        normalized_day_index,
        lambda target_idx: f"statsg:{target_idx}",
        "statsback:global",
    )
    await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    await _save_stats_history_context(
        state,
        mode="global",
        day_index=normalized_day_index,
        user_id=None,
        task_id=None,
        source_token="root",
        member_display_name=None,
        task_title=None,
    )
    await callback.answer()


async def _render_member_actions(
    callback: CallbackQuery,
    family_id: int,
    user_id: int,
    day_index: int,
    runtime: TaskRuntimeRepository,
    timezone_name: str,
    state: FSMContext,
    member_display_name: str,
) -> None:
    rows = await runtime.list_recent_actions_by_member_all(family_id, user_id)
    day_pages = _build_day_pages(rows, timezone_name, reverse_input=True)

    def _member_tail(row) -> str:
        line = f"- {row['task_title']} - {int(row['effort_stars'])}★"
        comment_text = str(row["comment_text"] or "").strip()
        if comment_text:
            line += f" - {comment_text}"
        return line

    lines, normalized_day_index = _render_day_page_lines(
        "Последние действия участника:",
        day_pages,
        day_index,
        _member_tail,
        "Действий пока нет.",
    )
    kb = _build_day_nav_markup(
        day_pages,
        normalized_day_index,
        lambda target_idx: f"statsm:{user_id}:{target_idx}",
        "statsback:member",
    )
    await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    await _save_stats_history_context(
        state,
        mode="member",
        day_index=normalized_day_index,
        user_id=user_id,
        member_display_name=member_display_name,
        task_id=None,
        task_title=None,
        source_token="root",
    )


async def _render_task_actions(
    callback: CallbackQuery,
    family_id: int,
    task_id: int,
    day_index: int,
    runtime: TaskRuntimeRepository,
    timezone_name: str,
    state: FSMContext,
    task_title: str,
    source_token: str = "root",
) -> None:
    rows = await runtime.list_recent_actions_by_task_all(family_id, task_id)
    day_pages = _build_day_pages(rows, timezone_name, reverse_input=True)
    lines, normalized_day_index = _render_day_page_lines(
        "Последние действия по задаче:",
        day_pages,
        day_index,
        lambda row: f"— {row['member_display_name']}",
        "Действий пока нет.",
    )
    kb = _build_day_nav_markup(
        day_pages,
        normalized_day_index,
        lambda target_idx: f"statst:{task_id}:{target_idx}:{source_token}",
        f"statsback:task:{source_token}",
    )
    await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    await _save_stats_history_context(
        state,
        mode="task",
        day_index=normalized_day_index,
        task_id=task_id,
        task_title=task_title,
        source_token=source_token,
        user_id=None,
        member_display_name=None,
    )


async def _build_stats_task_root_picker(
    repo: PlannedTaskRepository,
    family_repo,
    family_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    tasks = await repo.list_tasks(family_id)
    groups = await family_repo.list_groups(family_id)
    tasks_without_group = [task for task in tasks if task["group_id"] is None]
    rows: list[list[InlineKeyboardButton]] = []
    for task in tasks_without_group:
        rows.append([InlineKeyboardButton(text=str(task["title"]), callback_data=f"statst:{task['id']}:0:root")])
    for group in groups:
        group_id = int(group["id"])
        has_tasks = any(task["group_id"] is not None and int(task["group_id"]) == group_id for task in tasks)
        if not has_tasks:
            continue
        rows.append([InlineKeyboardButton(text=f'Группа "{group["name"]}"', callback_data=f"statstgrp:{group_id}")])
    if not rows:
        rows = [[InlineKeyboardButton(text="Нет задач", callback_data="noop")]]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="statstcancel")])
    return ("Выберите задачу:", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("statsm:"))
async def stats_member_callback(callback: CallbackQuery, state: FSMContext) -> None:
    _, user_id_raw, day_index_raw = callback.data.split(":")
    user_id = int(user_id_raw)
    day_index = max(0, int(day_index_raw))
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    runtime = TaskRuntimeRepository(db)
    timezone_name = ctx.family_timezone or "UTC"
    members = await family_repo.list_members_for_edit(ctx.family_id)
    member_display_name = next(
        (str(m["display_name"]) for m in members if int(m["user_id"]) == user_id),
        "",
    )
    await _render_member_actions(
        callback,
        ctx.family_id,
        user_id,
        day_index,
        runtime,
        timezone_name,
        state,
        member_display_name,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("statst:"))
async def stats_task_callback(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    _, task_id_raw, day_index_raw, *source_parts = parts
    task_id = int(task_id_raw)
    day_index = max(0, int(day_index_raw))
    source_token = source_parts[0] if source_parts else "root"
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    runtime = TaskRuntimeRepository(db)
    timezone_name = ctx.family_timezone or "UTC"
    repo = PlannedTaskRepository(db)
    task_row = await repo.get_task(ctx.family_id, task_id)
    task_title = str(task_row["title"]) if task_row is not None else ""
    await _render_task_actions(
        callback,
        ctx.family_id,
        task_id,
        day_index,
        runtime,
        timezone_name,
        state,
        task_title,
        source_token,
    )
    await callback.answer()


@router.callback_query(F.data == "statstroot")
async def stats_task_root_callback(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    root_text, root_kb = await _build_stats_task_root_picker(repo, family_repo, ctx.family_id)
    try:
        await callback.message.edit_text(root_text, reply_markup=root_kb)
    except TelegramBadRequest:
        await callback.message.answer(root_text, reply_markup=root_kb)
    await callback.answer()


@router.callback_query(F.data.startswith("statstgrp:"))
async def stats_task_group_callback(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return
    group_id = int(callback.data.split(":")[1])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    group = await family_repo.get_group(ctx.family_id, group_id)
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    tasks = await repo.list_tasks_by_group(ctx.family_id, group_id)
    rows: list[list[InlineKeyboardButton]] = []
    for task in tasks:
        rows.append([InlineKeyboardButton(text=str(task["title"]), callback_data=f"statst:{task['id']}:0:g{group_id}")])
    if not rows:
        rows = [[InlineKeyboardButton(text="Нет задач в группе", callback_data="noop")]]
    rows.append([InlineKeyboardButton(text="Назад", callback_data="statstroot")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="statstcancel")])
    text = f'Группа "{group["name"]}": выберите задачу.'
    try:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data == "statstcancel")
async def stats_task_cancel_callback(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return
    try:
        await callback.message.edit_text("Операция отменена.")
    except TelegramBadRequest:
        await callback.message.answer("Операция отменена.")
    await callback.answer()


@router.callback_query(F.data == "statsnoop")
async def stats_noop_callback(callback: CallbackQuery) -> None:
    await callback.answer()


async def _history_edit_nav_apply(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    mode: str,
    day_index: int,
    user_id: int | None = None,
    member_display_name: str | None = None,
    task_id: int | None = None,
    task_title: str | None = None,
    source_token: str = "root",
) -> None:
    if callback.message is None:
        await callback.answer()
        return
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Нет прав.", show_alert=True)
        return
    await _save_stats_history_context(
        state,
        mode=mode,
        day_index=max(0, day_index),
        user_id=user_id,
        member_display_name=member_display_name,
        task_id=task_id,
        task_title=task_title,
        source_token=source_token,
    )
    runtime = TaskRuntimeRepository(db)
    timezone_name = ctx.family_timezone or "UTC"
    kb = await _prepare_history_edit_reply_markup(state, runtime, ctx.family_id, timezone_name)
    try:
        await callback.message.edit_text(HISTORY_EDIT_PROMPT, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(HISTORY_EDIT_PROMPT, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("hedg:"))
async def stats_history_edit_nav_global(callback: CallbackQuery, state: FSMContext) -> None:
    day_index = max(0, int(callback.data.split(":")[1]))
    await _history_edit_nav_apply(
        callback,
        state,
        mode="global",
        day_index=day_index,
        user_id=None,
        member_display_name=None,
        task_id=None,
        task_title=None,
        source_token="root",
    )


@router.callback_query(F.data.startswith("hedm:"))
async def stats_history_edit_nav_member(callback: CallbackQuery, state: FSMContext) -> None:
    _, user_id_raw, day_index_raw = callback.data.split(":")
    user_id = int(user_id_raw)
    day_index = max(0, int(day_index_raw))
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    members = await family_repo.list_members_for_edit(ctx.family_id)
    member_display_name = next(
        (str(m["display_name"]) for m in members if int(m["user_id"]) == user_id),
        "",
    )
    await _history_edit_nav_apply(
        callback,
        state,
        mode="member",
        day_index=day_index,
        user_id=user_id,
        member_display_name=member_display_name,
        task_id=None,
        task_title=None,
        source_token="root",
    )


@router.callback_query(F.data.startswith("hedt:"))
async def stats_history_edit_nav_task(callback: CallbackQuery, state: FSMContext) -> None:
    m = re.match(r"^hedt:(\d+):(.+):(\d+)$", callback.data or "")
    if m is None:
        await callback.answer()
        return
    task_id = int(m.group(1))
    source_token = m.group(2)
    day_index = max(0, int(m.group(3)))
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    task_row = await repo.get_task(ctx.family_id, task_id)
    task_title = str(task_row["title"]) if task_row is not None else ""
    await _history_edit_nav_apply(
        callback,
        state,
        mode="task",
        day_index=day_index,
        user_id=None,
        member_display_name=None,
        task_id=task_id,
        task_title=task_title,
        source_token=source_token,
    )


@router.callback_query(F.data == "statsback:global")
async def stats_back_global(callback: CallbackQuery) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    await callback.message.answer("Меню статистики:", reply_markup=stats_menu(is_admin=ctx.is_admin))
    await callback.answer()


def _member_picker_markup(members: list) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=str(member["display_name"]), callback_data=f"statsm:{member['user_id']}:0")]
        for member in members
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=buttons or [[InlineKeyboardButton(text="Нет участников", callback_data="noop")]]
    )


@router.callback_query(F.data == "statsback:member")
async def stats_back_member(callback: CallbackQuery) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    members = await family_repo.list_members_for_edit(ctx.family_id)
    try:
        await callback.message.edit_text("Выберите участника:", reply_markup=_member_picker_markup(members))
    except TelegramBadRequest:
        await callback.message.answer("Выберите участника:", reply_markup=_member_picker_markup(members))
    await callback.answer()


@router.callback_query(F.data.startswith("statsback:task:"))
async def stats_back_task(callback: CallbackQuery) -> None:
    source_token = callback.data.split(":")[2]
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    if source_token == "root":
        text, kb = await _build_stats_task_root_picker(repo, family_repo, ctx.family_id)
    elif source_token.startswith("g") and source_token[1:].isdigit():
        group_id = int(source_token[1:])
        group = await family_repo.get_group(ctx.family_id, group_id)
        if group is None:
            await callback.answer("Группа не найдена.", show_alert=True)
            return
        tasks = await repo.list_tasks_by_group(ctx.family_id, group_id)
        rows: list[list[InlineKeyboardButton]] = []
        for task in tasks:
            rows.append([InlineKeyboardButton(text=str(task["title"]), callback_data=f"statst:{task['id']}:0:g{group_id}")])
        if not rows:
            rows = [[InlineKeyboardButton(text="Нет задач в группе", callback_data="noop")]]
        rows.append([InlineKeyboardButton(text="Назад", callback_data="statstroot")])
        rows.append([InlineKeyboardButton(text="Отмена", callback_data="statstcancel")])
        text = f'Группа "{group["name"]}": выберите задачу.'
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
    else:
        text, kb = await _build_stats_task_root_picker(repo, family_repo, ctx.family_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


def _history_card_text(entry: dict, timezone_name: str) -> str:
    local_completed_at = _to_family_local_timestamp(str(entry["completed_at"]), timezone_name)
    local_added_at = _to_family_local_timestamp(str(entry["added_at"]), timezone_name)
    local_updated_at = _to_family_local_timestamp(str(entry["history_updated_at"]), timezone_name)
    action = _format_action_label(str(entry["task_title"]), str(entry["completion_mode"]))
    comment = str(entry.get("comment_text") or "").strip()
    lines = [
        f"Запись истории #{entry['completion_id']}",
        f"Действие: {action}",
        f"Исполнитель: {entry['member_display_name']}",
        f"Дата/Время действия: {local_completed_at}",
        f"Дата/Время добавления действия: {local_added_at}",
        f"Дата/Время изменения истории: {local_updated_at}",
    ]
    if comment:
        lines.append(f"Комментарий: {comment}")
    return "\n".join(lines)


def _history_norm_completed_at(value: str) -> str:
    raw = (value or "").strip().replace("T", " ")
    if not raw:
        return ""
    try:
        parsed = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return raw
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _history_member_display_name(members: list[dict | object], user_id: int) -> str:
    for member in members:
        if int(member["user_id"]) == user_id:
            return str(member["display_name"])
    return str(user_id)


async def _history_refresh_draft_card(
    bot,
    runtime: TaskRuntimeRepository,
    family_repo,
    *,
    family_id: int,
    draft: dict,
    timezone_name: str,
) -> None:
    entry = await runtime.get_completion_entry(family_id, int(draft["completion_id"]))
    if entry is None:
        return
    members = await family_repo.list_members_for_edit(family_id)
    row = dict(entry)
    row["member_display_name"] = _history_member_display_name(members, int(draft["executor_user_id"]))
    row["completed_at"] = draft["completed_at_utc"]
    row["comment_text"] = draft.get("comment") or ""
    text = _history_card_text(row, timezone_name)
    kb = _history_entry_actions_keyboard(int(draft["completion_id"]))
    try:
        await bot.edit_message_text(
            text=text,
            chat_id=int(draft["card_chat_id"]),
            message_id=int(draft["card_message_id"]),
            reply_markup=kb,
        )
    except TelegramBadRequest:
        pass


def _history_entry_actions_keyboard(completion_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Исполнитель", callback_data=f"histeditexec:{completion_id}")],
            [InlineKeyboardButton(text="Дата/время", callback_data=f"histedittime:{completion_id}")],
            [InlineKeyboardButton(text="Комментарий", callback_data=f"histeditcomment:{completion_id}")],
            [InlineKeyboardButton(text="Обновить", callback_data=f"histapply:{completion_id}")],
            [InlineKeyboardButton(text="Удалить", callback_data=f"histeditdelask:{completion_id}")],
            [InlineKeyboardButton(text="Назад", callback_data="histeditback")],
        ]
    )


@router.message(NavStates.in_stats_menu, F.text == "Правка")
async def stats_history_edit_menu(message: Message, state: FSMContext) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await message.answer("Вы пока не добавлены в семью.")
        return
    if not ctx.is_admin:
        await message.answer("Правка истории доступна только администраторам.")
        return
    runtime = TaskRuntimeRepository(db)
    timezone_name = ctx.family_timezone or "UTC"
    kb = await _prepare_history_edit_reply_markup(state, runtime, ctx.family_id, timezone_name)
    await message.answer(HISTORY_EDIT_PROMPT, reply_markup=kb)


@router.callback_query(F.data == "histeditback")
async def stats_history_edit_back(callback: CallbackQuery, state: FSMContext) -> None:
    await _clear_state_keep_stats_context(state)
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Нет прав.", show_alert=True)
        return
    runtime = TaskRuntimeRepository(db)
    timezone_name = ctx.family_timezone or "UTC"
    kb = await _prepare_history_edit_reply_markup(state, runtime, ctx.family_id, timezone_name)
    await callback.message.answer(HISTORY_EDIT_PROMPT, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("histedit:"))
async def stats_history_edit_entry(callback: CallbackQuery, state: FSMContext) -> None:
    await _clear_state_keep_stats_context(state)
    completion_id = int(callback.data.split(":")[1])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Нет прав.", show_alert=True)
        return
    runtime = TaskRuntimeRepository(db)
    entry = await runtime.get_completion_entry(ctx.family_id, completion_id)
    if entry is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    timezone_name = ctx.family_timezone or "UTC"
    kb = _history_entry_actions_keyboard(completion_id)
    sent = await callback.message.answer(_history_card_text(entry, timezone_name), reply_markup=kb)
    comment_raw = entry["comment_text"] if "comment_text" in entry.keys() else None
    comment = (str(comment_raw).strip() if comment_raw is not None else "") or ""
    await state.update_data(
        hist_edit_draft={
            "completion_id": completion_id,
            "card_message_id": sent.message_id,
            "card_chat_id": sent.chat.id,
            "executor_user_id": int(entry["member_user_id"]),
            "completed_at_utc": _history_norm_completed_at(str(entry["completed_at"])),
            "comment": comment,
        }
    )
    await callback.answer()


@router.callback_query(F.data.startswith("histeditexecback:"))
async def stats_history_edit_executor_picker_back(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is not None:
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
    await state.set_state(None)
    await callback.answer()


@router.callback_query(F.data.startswith("histeditexec:"))
async def stats_history_edit_executor_start(callback: CallbackQuery, state: FSMContext) -> None:
    completion_id = int(callback.data.split(":")[1])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Нет прав.", show_alert=True)
        return
    entry = await TaskRuntimeRepository(db).get_completion_entry(ctx.family_id, completion_id)
    if entry is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    members = await family_repo.list_members_for_edit(ctx.family_id)
    buttons = [
        [
            InlineKeyboardButton(
                text=str(member["display_name"]),
                callback_data=f"histeditexecsel:{completion_id}:{member['user_id']}",
            )
        ]
        for member in members
    ]
    buttons.append([InlineKeyboardButton(text="Назад", callback_data=f"histeditexecback:{completion_id}")])
    await state.set_state(StatsStates.waiting_history_executor)
    await state.update_data(history_completion_id=completion_id)
    await callback.message.answer(
        "Выберите нового исполнителя:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("histeditexecsel:"))
async def stats_history_edit_executor_save(callback: CallbackQuery, state: FSMContext) -> None:
    _, completion_raw, user_raw = callback.data.split(":")
    completion_id = int(completion_raw)
    new_user_id = int(user_raw)
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Нет прав.", show_alert=True)
        return
    data = await state.get_data()
    draft = data.get("hist_edit_draft")
    if not isinstance(draft, dict) or int(draft.get("completion_id", 0)) != completion_id:
        await callback.answer("Сначала откройте запись.", show_alert=True)
        return
    draft["executor_user_id"] = new_user_id
    await state.update_data(hist_edit_draft=draft)
    if callback.message is not None:
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
    await state.set_state(None)
    runtime = TaskRuntimeRepository(db)
    timezone_name = ctx.family_timezone or "UTC"
    await _history_refresh_draft_card(
        callback.bot,
        runtime,
        family_repo,
        family_id=ctx.family_id,
        draft=draft,
        timezone_name=timezone_name,
    )
    await callback.answer("Исполнитель изменён в черновике. Нажмите «Обновить», чтобы сохранить в базе.")


@router.callback_query(F.data.startswith("histapply:"))
async def stats_history_edit_apply(callback: CallbackQuery, state: FSMContext) -> None:
    completion_id = int(callback.data.split(":")[1])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Нет прав.", show_alert=True)
        return
    data = await state.get_data()
    draft = data.get("hist_edit_draft")
    if not isinstance(draft, dict) or int(draft.get("completion_id", 0)) != completion_id:
        await callback.answer("Сначала откройте запись.", show_alert=True)
        return
    runtime = TaskRuntimeRepository(db)
    entry = await runtime.get_completion_entry(ctx.family_id, completion_id)
    if entry is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    orig_ex = int(entry["member_user_id"])
    orig_at = _history_norm_completed_at(str(entry["completed_at"]))
    raw_orig_cmt = entry["comment_text"] if "comment_text" in entry.keys() else None
    orig_cmt = (str(raw_orig_cmt).strip() if raw_orig_cmt is not None else "") or ""
    new_ex = int(draft["executor_user_id"])
    new_at = _history_norm_completed_at(str(draft["completed_at_utc"]))
    new_cmt = (str(draft.get("comment") or "")).strip()
    changed = False
    if new_ex != orig_ex:
        if not await runtime.update_completion_executor(ctx.family_id, completion_id, new_ex):
            await callback.answer("Не удалось изменить исполнителя.", show_alert=True)
            return
        changed = True
    if new_at != orig_at:
        if not await runtime.update_completion_datetime(ctx.family_id, completion_id, new_at):
            await callback.answer("Не удалось изменить дату/время.", show_alert=True)
            return
        changed = True
    if new_cmt != orig_cmt:
        if not await runtime.update_completion_comment(ctx.family_id, completion_id, new_cmt or None):
            await callback.answer("Не удалось изменить комментарий.", show_alert=True)
            return
        changed = True
    if not changed:
        await callback.answer("Нет изменений для сохранения.")
        return
    entry2 = await runtime.get_completion_entry(ctx.family_id, completion_id)
    if entry2 is None:
        await callback.answer("Запись не найдена после сохранения.", show_alert=True)
        return
    c2 = entry2["comment_text"] if "comment_text" in entry2.keys() else None
    comment2 = (str(c2).strip() if c2 is not None else "") or ""
    draft2 = {
        "completion_id": completion_id,
        "card_message_id": int(draft["card_message_id"]),
        "card_chat_id": int(draft["card_chat_id"]),
        "executor_user_id": int(entry2["member_user_id"]),
        "completed_at_utc": _history_norm_completed_at(str(entry2["completed_at"])),
        "comment": comment2,
    }
    await state.update_data(hist_edit_draft=draft2)
    timezone_name = ctx.family_timezone or "UTC"
    await _history_refresh_draft_card(
        callback.bot,
        runtime,
        family_repo,
        family_id=ctx.family_id,
        draft=draft2,
        timezone_name=timezone_name,
    )
    await callback.answer("Сохранено в базе.")


@router.callback_query(F.data.startswith("histeditcomment:"))
async def stats_history_edit_comment_start(callback: CallbackQuery, state: FSMContext) -> None:
    completion_id = int(callback.data.split(":")[1])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Нет прав.", show_alert=True)
        return
    data = await state.get_data()
    draft = data.get("hist_edit_draft")
    if not isinstance(draft, dict) or int(draft.get("completion_id", 0)) != completion_id:
        await callback.answer("Сначала откройте запись.", show_alert=True)
        return
    await state.set_state(StatsStates.waiting_history_comment)
    await state.update_data(history_comment_completion_id=completion_id)
    await callback.message.answer(
        "Введите новый комментарий (пустое сообщение — очистить комментарий; «Отмена» — выйти без изменений):"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("histedittime:"))
async def stats_history_edit_time_start(callback: CallbackQuery, state: FSMContext) -> None:
    completion_id = int(callback.data.split(":")[1])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Нет прав.", show_alert=True)
        return
    entry = await TaskRuntimeRepository(db).get_completion_entry(ctx.family_id, completion_id)
    if entry is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    timezone_name = ctx.family_timezone or "UTC"
    data = await state.get_data()
    draft = data.get("hist_edit_draft")
    if isinstance(draft, dict) and int(draft.get("completion_id", 0)) == completion_id:
        current_local = _to_family_local_timestamp(str(draft["completed_at_utc"]), timezone_name)
    else:
        current_local = _to_family_local_timestamp(str(entry["completed_at"]), timezone_name)
    await state.set_state(StatsStates.waiting_history_datetime)
    await state.update_data(history_completion_id=completion_id)
    await callback.message.answer(
        f"Текущее время действия (черновик): {current_local}\n"
        f"Введите новое время в формате YYYY-MM-DD HH:MM (локальное время семьи) или «Отмена»:"
    )
    await callback.answer()


@router.message(StatsStates.waiting_history_datetime)
async def stats_history_edit_time_save(message: Message, state: FSMContext) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await _clear_state_keep_stats_context(state)
        await message.answer("Вы пока не добавлены в семью.")
        return
    if not ctx.is_admin:
        await _clear_state_keep_stats_context(state)
        await message.answer("Эта команда доступна только администраторам.")
        return
    raw_value = (message.text or "").strip()
    if raw_value.lower() == "отмена":
        await state.set_state(None)
        await message.answer("Изменение времени отменено.")
        return
    timezone_name = ctx.family_timezone or "UTC"
    new_completed_at = _parse_local_datetime_to_utc(raw_value, timezone_name)
    if new_completed_at is None:
        await message.answer("Некорректный формат. Используйте YYYY-MM-DD HH:MM")
        return
    data = await state.get_data()
    completion_id = int(data.get("history_completion_id", 0))
    if completion_id <= 0:
        await state.set_state(None)
        await message.answer("Не удалось определить запись истории.")
        return
    draft = data.get("hist_edit_draft")
    if not isinstance(draft, dict) or int(draft.get("completion_id", 0)) != completion_id:
        await state.set_state(None)
        await message.answer("Черновик записи не найден. Откройте запись снова.")
        return
    draft["completed_at_utc"] = _history_norm_completed_at(new_completed_at)
    await state.update_data(hist_edit_draft=draft, history_completion_id=None)
    await state.set_state(None)
    runtime = TaskRuntimeRepository(db)
    await _history_refresh_draft_card(
        message.bot,
        runtime,
        family_repo,
        family_id=ctx.family_id,
        draft=draft,
        timezone_name=timezone_name,
    )
    await message.answer("Время изменено в черновике. Нажмите «Обновить», чтобы сохранить в базе.")


@router.message(StatsStates.waiting_history_comment)
async def stats_history_edit_comment_save(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw.lower() == "отмена":
        await state.set_state(None)
        await state.update_data(history_comment_completion_id=None)
        await message.answer("Редактирование комментария отменено.")
        return
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await state.set_state(None)
        await message.answer("Вы пока не добавлены в семью.")
        return
    if not ctx.is_admin:
        await state.set_state(None)
        await message.answer("Эта команда доступна только администраторам.")
        return
    data = await state.get_data()
    completion_id = int(data.get("history_comment_completion_id", 0))
    draft = data.get("hist_edit_draft")
    if completion_id <= 0 or not isinstance(draft, dict) or int(draft.get("completion_id", 0)) != completion_id:
        await state.set_state(None)
        await message.answer("Не удалось определить запись истории.")
        return
    draft["comment"] = raw
    await state.update_data(hist_edit_draft=draft, history_comment_completion_id=None)
    await state.set_state(None)
    runtime = TaskRuntimeRepository(db)
    timezone_name = ctx.family_timezone or "UTC"
    await _history_refresh_draft_card(
        message.bot,
        runtime,
        family_repo,
        family_id=ctx.family_id,
        draft=draft,
        timezone_name=timezone_name,
    )
    await message.answer("Комментарий изменён в черновике. Нажмите «Обновить», чтобы сохранить в базе.")


@router.callback_query(F.data.startswith("histeditdelask:"))
async def stats_history_delete_ask(callback: CallbackQuery) -> None:
    completion_id = int(callback.data.split(":")[1])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Нет прав.", show_alert=True)
        return
    runtime = TaskRuntimeRepository(db)
    entry = await runtime.get_completion_entry(ctx.family_id, completion_id)
    if entry is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    timezone_name = ctx.family_timezone or "UTC"
    line = _history_line(entry, timezone_name).lstrip("- ").strip()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Нет", callback_data=f"histeditdelno:{completion_id}"),
                InlineKeyboardButton(text="Да", callback_data=f"histeditdelyes:{completion_id}"),
            ]
        ]
    )
    await callback.message.answer(
        f"Вы точно хотите удалить запись в истории: {line}",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("histeditdelno:"))
async def stats_history_delete_no(callback: CallbackQuery) -> None:
    completion_id = int(callback.data.split(":")[1])
    await callback.message.answer(
        "Удаление отменено.",
        reply_markup=_history_entry_actions_keyboard(completion_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("histeditdelyes:"))
async def stats_history_delete_yes(callback: CallbackQuery, state: FSMContext) -> None:
    completion_id = int(callback.data.split(":")[1])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Нет прав.", show_alert=True)
        return
    runtime = TaskRuntimeRepository(db)
    deleted = await runtime.delete_completion_entry(ctx.family_id, completion_id)
    if not deleted:
        await callback.answer("Не удалось удалить запись.", show_alert=True)
        return
    timezone_name = ctx.family_timezone or "UTC"
    kb = await _prepare_history_edit_reply_markup(state, runtime, ctx.family_id, timezone_name)
    await callback.message.answer("Запись истории удалена.")
    await callback.message.answer(HISTORY_EDIT_PROMPT, reply_markup=kb)
    await callback.answer()


@router.message(StatsStates.waiting_history_executor)
async def stats_history_executor_waiting_hint(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == "Назад":
        _, user_repo, family_repo = get_repositories()
        ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
        await state.clear()
        await message.answer(
            "Главное меню",
            reply_markup=main_menu(is_admin=ctx.is_admin),
        )
        return
    await message.answer("Выберите исполнителя кнопкой из списка ниже.")


@router.callback_query(F.data == "groupadd")
async def groups_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Только администратор может править группы.", show_alert=True)
        return
    await state.set_state(GroupStates.waiting_group_name_create)
    await callback.answer()
    await callback.message.answer("Введите название новой группы:")


@router.callback_query(F.data.startswith("groupedit:"))
async def groups_edit_card(callback: CallbackQuery) -> None:
    _, group_id_raw = callback.data.split(":")
    group_id = int(group_id_raw)
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Только администратор может править группы.", show_alert=True)
        return
    group = await family_repo.get_group(ctx.family_id, group_id)
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Переименовать", callback_data=f"grouprename:{group_id}")],
            [InlineKeyboardButton(text="Удалить", callback_data=f"groupdelete:{group_id}")],
            [
                InlineKeyboardButton(text="Вверх", callback_data=f"groupmove:{group_id}:up"),
                InlineKeyboardButton(text="Вниз", callback_data=f"groupmove:{group_id}:down"),
            ],
        ]
    )
    await callback.answer()
    await callback.message.answer(f"Группа: {group['name']}\nПозиция: {group['sort_order']}", reply_markup=kb)


@router.callback_query(F.data.startswith("groupmove:"))
async def groups_move(callback: CallbackQuery) -> None:
    _, group_id_raw, direction = callback.data.split(":")
    group_id = int(group_id_raw)
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Только администратор может править группы.", show_alert=True)
        return
    moved = (
        await family_repo.move_group_up(ctx.family_id, group_id)
        if direction == "up"
        else await family_repo.move_group_down(ctx.family_id, group_id)
    )
    if not moved:
        await callback.answer("Перемещение недоступно", show_alert=True)
        return
    group = await family_repo.get_group(ctx.family_id, group_id)
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Переименовать", callback_data=f"grouprename:{group_id}")],
            [InlineKeyboardButton(text="Удалить", callback_data=f"groupdelete:{group_id}")],
            [
                InlineKeyboardButton(text="Вверх", callback_data=f"groupmove:{group_id}:up"),
                InlineKeyboardButton(text="Вниз", callback_data=f"groupmove:{group_id}:down"),
            ],
        ]
    )
    await callback.message.edit_text(
        f"Группа: {group['name']}\nПозиция: {group['sort_order']}",
        reply_markup=kb,
    )
    await callback.answer("Порядок групп обновлен")


@router.callback_query(F.data.startswith("grouprename:"))
async def groups_rename_start(callback: CallbackQuery, state: FSMContext) -> None:
    _, group_id_raw = callback.data.split(":")
    group_id = int(group_id_raw)
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Только администратор может править группы.", show_alert=True)
        return
    group = await family_repo.get_group(ctx.family_id, group_id)
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    await state.set_state(GroupStates.waiting_group_name_rename)
    await state.update_data(group_id=group_id)
    await callback.answer()
    await callback.message.answer(f"Текущее имя: {group['name']}\nВведите новое название группы:")


@router.callback_query(F.data.startswith("groupdelete:"))
async def groups_delete(callback: CallbackQuery) -> None:
    _, group_id_raw = callback.data.split(":")
    group_id = int(group_id_raw)
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Только администратор может править группы.", show_alert=True)
        return
    deleted = await family_repo.delete_group(ctx.family_id, group_id)
    if not deleted:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    groups = await family_repo.list_groups(ctx.family_id)
    await callback.message.answer("Группа удалена. Привязанные задачи переведены в «Без группы».")
    await callback.message.answer(
        "Выберите группу для правки:",
        reply_markup=_groups_editor_keyboard(groups),
    )
    await callback.answer()


@router.message(GroupStates.waiting_group_name_create)
async def groups_create_name_entered(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Название группы слишком короткое.")
        return
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await state.clear()
        await message.answer("Вы пока не добавлены в семью.")
        return
    if not ctx.is_admin:
        await state.clear()
        await message.answer("Только администратор может править группы.")
        return
    group_id = await family_repo.create_group(ctx.family_id, name)
    if group_id is None:
        await message.answer("Группа с таким названием уже существует или имя пустое.")
        return
    await state.set_state(NavStates.in_groups_menu)
    groups = await family_repo.list_groups(ctx.family_id)
    await message.answer(f"Группа «{name}» создана.")
    await message.answer("Выберите группу для правки:", reply_markup=_groups_editor_keyboard(groups))


@router.message(GroupStates.waiting_group_name_rename)
async def groups_rename_name_entered(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Название группы слишком короткое.")
        return
    data = await state.get_data()
    group_id = int(data.get("group_id", 0))
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await state.clear()
        await message.answer("Вы пока не добавлены в семью.")
        return
    if not ctx.is_admin:
        await state.clear()
        await message.answer("Только администратор может править группы.")
        return
    renamed = await family_repo.rename_group(ctx.family_id, group_id, name)
    if not renamed:
        await message.answer("Не удалось переименовать группу. Проверьте, что имя уникально.")
        return
    await state.set_state(NavStates.in_groups_menu)
    groups = await family_repo.list_groups(ctx.family_id)
    await message.answer(f"Группа переименована в «{name}».")
    await message.answer("Выберите группу для правки:", reply_markup=_groups_editor_keyboard(groups))


@router.message(F.text == "Назад")
async def back_to_main(message: Message, state: FSMContext) -> None:
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    await state.clear()
    await message.answer(
        "Главное меню",
        reply_markup=main_menu(is_admin=ctx.is_admin),
    )


@router.message(F.text.regexp(r"^/quiet\s+"))
async def set_quiet_mode(message: Message) -> None:
    match = QUIET_RE.match((message.text or "").strip())
    if not match:
        await message.answer("Формат: /quiet HH:MM-HH:MM [all|0..6]")
        return
    quiet_from, quiet_to, day_token = match.groups()
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await message.answer("Вы не состоите в семье.")
        return
    repo = NotificationRepository(db)
    is_all = day_token in (None, "all")
    day_of_week = None if is_all else int(day_token)
    await repo.set_quiet_interval(ctx.family_id, ctx.user_id, quiet_from, quiet_to, is_all, day_of_week)
    await message.answer("Тихий режим сохранен.")
