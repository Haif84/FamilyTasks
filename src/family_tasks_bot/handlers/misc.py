from __future__ import annotations

import re
import math
from calendar import monthrange
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup

from family_tasks_bot.deps import get_repositories
from family_tasks_bot.db.repositories import NotificationRepository, PlannedTaskRepository, TaskRuntimeRepository
from family_tasks_bot.keyboards.stats_inline import (
    build_day_nav_markup,
    history_datetime_keyboard,
    monthly_nav_keyboard,
    weekly_nav_keyboard,
)
from family_tasks_bot.keyboards.reply import groups_menu, main_menu, misc_menu, stats_menu
from family_tasks_bot.presenters.stats_presenter import build_day_pages, build_week_pages, render_day_page_lines
from family_tasks_bot.services.bootstrap import ensure_member_context
from family_tasks_bot.states import GroupStates, NavStates, StatsStates
from family_tasks_bot.utils.datetime_localization import bump_local_datetime, family_tzinfo, parse_completed_at_utc_sql
from family_tasks_bot.version import APP_VERSION

router = Router(name="misc")
QUIET_RE = re.compile(r"^/quiet\s+(\d{2}:\d{2})-(\d{2}:\d{2})(?:\s+(all|[0-6]))?$")
STATS_RE = re.compile(r"^/stats(?:\s+(day|week|month))?$")
PAGE_SIZE = 10
STATS_HISTORY_CONTEXT_KEY = "stats_history_context"
WEEKDAY_SHORT_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MONTH_SHORT_RU = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def _family_tzinfo(timezone_name: str) -> ZoneInfo | timezone:
    return family_tzinfo(timezone_name)


def _prize_fund_view_text(amount: int) -> str:
    return f"Призовой фонд текущей недели: {max(0, int(amount))} руб."


PRIZE_ALGO_QUADRATIC = "quadratic"
PRIZE_ALGO_LINEAR = "linear"


def _normalize_prize_algorithm(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw in {PRIZE_ALGO_QUADRATIC, PRIZE_ALGO_LINEAR}:
        return raw
    return PRIZE_ALGO_QUADRATIC


def _prize_algorithm_description(algorithm: str) -> str:
    normalized = _normalize_prize_algorithm(algorithm)
    if normalized == PRIZE_ALGO_LINEAR:
        return (
            "Текущий алгоритм: Линейный "
            "(1-е: ceil25(Фонд/(Звезды1+Звезды2)*Звезды1), 2-е: Фонд-1-е)"
        )
    return (
        "Текущий алгоритм: Квадратичный "
        "(1-е: ceil25((Фонд/((Звезды1^2/Звезды2)+Звезды2))*(Звезды1^2/Звезды2)), 2-е: Фонд-1-е)"
    )


def _prize_fund_card_text(amount: int, algorithm: str) -> str:
    return f"{_prize_fund_view_text(amount)}\n{_prize_algorithm_description(algorithm)}"


def _prize_fund_view_keyboard(*, is_admin: bool) -> InlineKeyboardMarkup | None:
    rows = [[InlineKeyboardButton(text="Рассчитать", callback_data="prizefund:calc:start")]]
    rows.append([InlineKeyboardButton(text="Алгоритм расчета приза", callback_data="prizefund:algo:open")])
    if is_admin:
        rows.append([InlineKeyboardButton(text="Правка", callback_data="prizefund:edit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _prize_fund_input_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="prizefund:back")]]
    )


def _prize_calc_input_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="prizefund:calc:back")]]
    )


def _prize_algo_picker_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Квадратичный", callback_data=f"prizefund:algo:set:{PRIZE_ALGO_QUADRATIC}")],
            [InlineKeyboardButton(text="Линейный", callback_data=f"prizefund:algo:set:{PRIZE_ALGO_LINEAR}")],
            [InlineKeyboardButton(text="Назад", callback_data="prizefund:algo:back")],
        ]
    )


def _round_up_to_25(value: float) -> int:
    if value <= 0:
        return 0
    return int(math.ceil(value / 25.0) * 25)


def _calculate_first_second_prizes(
    prize_fund: int, first_stars: float, second_stars: float, algorithm: str = PRIZE_ALGO_QUADRATIC
) -> tuple[int | None, int | None]:
    normalized_fund = max(0, int(prize_fund))
    if first_stars <= 0 or second_stars <= 0:
        return (None, None)
    normalized_algorithm = _normalize_prize_algorithm(algorithm)
    if normalized_algorithm == PRIZE_ALGO_LINEAR:
        denom = first_stars + second_stars
        if denom <= 0:
            return (None, None)
        first_raw = (normalized_fund / denom) * first_stars
    else:
        ratio = (first_stars**2) / second_stars
        denom = ratio + second_stars
        if denom <= 0:
            return (None, None)
        first_raw = (normalized_fund / denom) * ratio
    first_prize = min(normalized_fund, _round_up_to_25(first_raw))
    second_prize = max(0, normalized_fund - first_prize)
    return (first_prize, second_prize)


def _weekly_prize_amounts(
    prize_fund: int, by_stars: list, algorithm: str = PRIZE_ALGO_QUADRATIC
) -> tuple[int | None, int | None]:
    if len(by_stars) < 2:
        return (None, None)
    first_stars = float(by_stars[0]["stars"] or 0)
    second_stars = float(by_stars[1]["stars"] or 0)
    return _calculate_first_second_prizes(prize_fund, first_stars, second_stars, algorithm=algorithm)


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
    return build_day_pages(
        entries,
        lambda raw: _to_family_local_datetime(raw, timezone_name),
        reverse_input=reverse_input,
    )


def _build_week_pages(
    entries: list,
    timezone_name: str,
    *,
    reverse_input: bool = True,
) -> list[dict]:
    return build_week_pages(
        entries,
        lambda raw: _to_family_local_datetime(raw, timezone_name),
        reverse_input=reverse_input,
    )


def _build_day_nav_markup(
    day_pages: list[dict],
    day_index: int,
    day_callback_builder,
    back_callback_data: str,
) -> InlineKeyboardMarkup | None:
    return build_day_nav_markup(day_pages, day_index, day_callback_builder, back_callback_data)


def _weekly_nav_keyboard(
    *,
    current_week_offset: int,
    current_week_start: str,
    next_week_start: str,
    prev_week_start: str,
    left_enabled: bool,
) -> InlineKeyboardMarkup:
    return weekly_nav_keyboard(
        current_week_offset=current_week_offset,
        current_week_start=current_week_start,
        next_week_start=next_week_start,
        prev_week_start=prev_week_start,
        left_enabled=left_enabled,
    )


def _monthly_nav_keyboard(
    *,
    current_month_offset: int,
    current_month_start: str,
    prev_month_start: str,
    left_enabled: bool,
) -> InlineKeyboardMarkup:
    return monthly_nav_keyboard(
        current_month_offset=current_month_offset,
        current_month_start=current_month_start,
        prev_month_start=prev_month_start,
        left_enabled=left_enabled,
    )


def _render_day_page_lines(
    title: str,
    day_pages: list[dict],
    day_index: int,
    entry_tail_builder,
    empty_text: str,
) -> tuple[list[str], int]:
    return render_day_page_lines(title, day_pages, day_index, entry_tail_builder, empty_text)


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
        return _build_day_pages(rows, timezone_name, reverse_input=True)
    elif mode == "task":
        rows = await runtime.list_recent_actions_by_task_all(family_id, int(ctx["task_id"]))
        return _build_week_pages(rows, timezone_name, reverse_input=True)
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
            [InlineKeyboardButton(text=_history_edit_header_text(page, ctx), callback_data="statsnoop")]
        )
        for time_part, entry in page["items"]:
            cid = int(entry["completion_id"])
            label = _history_edit_row_label(mode, time_part, entry)
            rows_kb.append([InlineKeyboardButton(text=label, callback_data=f"histedit:{cid}")])
    else:
        rows_kb.append([InlineKeyboardButton(text="История пуста", callback_data="statsnoop")])
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


@router.message(F.text == "Призовой фонд")
async def show_prize_fund(message: Message) -> None:
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await message.answer("Вы пока не добавлены в семью.")
        return
    prize_fund = await family_repo.get_weekly_prize_fund(ctx.family_id)
    algorithm = await family_repo.get_prize_calc_algorithm(ctx.family_id)
    await message.answer(
        _prize_fund_card_text(prize_fund, algorithm),
        reply_markup=_prize_fund_view_keyboard(is_admin=ctx.is_admin),
    )


@router.callback_query(F.data == "prizefund:edit")
async def prize_fund_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Нет прав.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer()
        return
    await state.set_state(StatsStates.waiting_prize_fund_amount)
    await state.update_data(
        prize_fund_target_chat_id=int(callback.message.chat.id),
        prize_fund_target_message_id=int(callback.message.message_id),
    )
    prompt = await callback.message.answer(
        "Введите сумму призового фонда текущей недели",
        reply_markup=_prize_fund_input_keyboard(),
    )
    await state.update_data(prize_fund_prompt_message_id=int(prompt.message_id))
    await callback.answer()


@router.callback_query(F.data == "prizefund:back")
async def prize_fund_edit_back(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    prompt_message_id = data.get("prize_fund_prompt_message_id")
    if callback.message is not None and prompt_message_id is not None:
        await _history_try_delete_message(
            callback.bot,
            int(callback.message.chat.id),
            int(prompt_message_id),
        )
    await state.set_state(None)
    await state.update_data(
        prize_fund_target_chat_id=None,
        prize_fund_target_message_id=None,
        prize_fund_prompt_message_id=None,
    )
    if callback.message is not None:
        try:
            await callback.message.edit_text("Изменение призового фонда отменено.", reply_markup=None)
        except TelegramBadRequest:
            await callback.message.answer("Изменение призового фонда отменено.")
    await callback.answer()


@router.callback_query(F.data == "prizefund:calc:start")
async def prize_fund_calc_start(callback: CallbackQuery, state: FSMContext) -> None:
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer()
        return
    await state.set_state(StatsStates.waiting_prize_calc_first_stars)
    prompt = await callback.message.answer(
        "Введите кол-во баллов на первом месте",
        reply_markup=_prize_calc_input_keyboard(),
    )
    await state.update_data(prize_calc_prompt_message_id=int(prompt.message_id))
    await callback.answer()


@router.callback_query(F.data == "prizefund:algo:open")
async def prize_fund_algo_open(callback: CallbackQuery) -> None:
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Нет прав.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer()
        return
    await callback.message.answer(
        "Укажите алгорит расчтета суммы приза за первое и второе места",
        reply_markup=_prize_algo_picker_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("prizefund:algo:set:"))
async def prize_fund_algo_set(callback: CallbackQuery) -> None:
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not ctx.is_admin:
        await callback.answer("Нет прав.", show_alert=True)
        return
    algorithm = _normalize_prize_algorithm((callback.data or "").split(":")[-1])
    updated = await family_repo.set_prize_calc_algorithm(ctx.family_id, algorithm)
    if not updated:
        await callback.answer("Не удалось сохранить алгоритм.", show_alert=True)
        return
    prize_fund = await family_repo.get_weekly_prize_fund(ctx.family_id)
    text = _prize_fund_card_text(prize_fund, algorithm)
    if callback.message is not None:
        try:
            await callback.message.edit_text(text, reply_markup=_prize_fund_view_keyboard(is_admin=ctx.is_admin))
        except TelegramBadRequest:
            await callback.message.answer(text, reply_markup=_prize_fund_view_keyboard(is_admin=ctx.is_admin))
    await callback.answer("Алгоритм обновлен.")


@router.callback_query(F.data == "prizefund:algo:back")
async def prize_fund_algo_back(callback: CallbackQuery) -> None:
    if callback.message is not None:
        try:
            await callback.message.edit_text("Выбор алгоритма отменен.", reply_markup=None)
        except TelegramBadRequest:
            await callback.message.answer("Выбор алгоритма отменен.")
    await callback.answer()


@router.callback_query(F.data == "prizefund:calc:back")
async def prize_fund_calc_back(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    prompt_message_id = data.get("prize_calc_prompt_message_id")
    if callback.message is not None and prompt_message_id is not None:
        await _history_try_delete_message(
            callback.bot,
            int(callback.message.chat.id),
            int(prompt_message_id),
        )
    await state.set_state(None)
    await state.update_data(
        prize_calc_prompt_message_id=None,
        prize_calc_first_stars=None,
    )
    if callback.message is not None:
        try:
            await callback.message.edit_text("Расчёт призов отменен.", reply_markup=None)
        except TelegramBadRequest:
            await callback.message.answer("Расчёт призов отменен.")
    await callback.answer()


@router.message(StatsStates.waiting_prize_fund_amount)
async def prize_fund_edit_save(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("Введите положительное число (например, 1500).")
        return
    amount = int(raw)
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await state.set_state(None)
        await message.answer("Вы пока не добавлены в семью.")
        return
    if not ctx.is_admin:
        await state.set_state(None)
        await message.answer("Эта команда доступна только администраторам.")
        return
    await family_repo.set_weekly_prize_fund(ctx.family_id, amount)
    algorithm = await family_repo.get_prize_calc_algorithm(ctx.family_id)
    data = await state.get_data()
    target_chat_id = data.get("prize_fund_target_chat_id")
    target_message_id = data.get("prize_fund_target_message_id")
    prompt_message_id = data.get("prize_fund_prompt_message_id")
    if prompt_message_id is not None:
        await _history_try_delete_message(
            message.bot,
            int(message.chat.id),
            int(prompt_message_id),
        )
    if target_chat_id is not None and target_message_id is not None:
        try:
            await message.bot.edit_message_text(
                text=_prize_fund_card_text(amount, algorithm),
                chat_id=int(target_chat_id),
                message_id=int(target_message_id),
                reply_markup=_prize_fund_view_keyboard(is_admin=True),
            )
        except TelegramBadRequest:
            await message.answer(
                _prize_fund_card_text(amount, algorithm),
                reply_markup=_prize_fund_view_keyboard(is_admin=True),
            )
    else:
        await message.answer(_prize_fund_card_text(amount, algorithm), reply_markup=_prize_fund_view_keyboard(is_admin=True))
    await state.set_state(None)
    await state.update_data(
        prize_fund_target_chat_id=None,
        prize_fund_target_message_id=None,
        prize_fund_prompt_message_id=None,
    )


@router.message(StatsStates.waiting_prize_calc_first_stars)
async def prize_fund_calc_first_stars(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("Введите положительное число баллов для первого места.")
        return
    data = await state.get_data()
    prompt_message_id = data.get("prize_calc_prompt_message_id")
    if prompt_message_id is not None:
        await _history_try_delete_message(
            message.bot,
            int(message.chat.id),
            int(prompt_message_id),
        )
    await state.update_data(prize_calc_first_stars=int(raw))
    await state.set_state(StatsStates.waiting_prize_calc_second_stars)
    prompt = await message.answer(
        "Введите кол-во баллов на втором месте",
        reply_markup=_prize_calc_input_keyboard(),
    )
    await state.update_data(prize_calc_prompt_message_id=int(prompt.message_id))


@router.message(StatsStates.waiting_prize_calc_second_stars)
async def prize_fund_calc_second_stars(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("Введите положительное число баллов для второго места.")
        return
    _, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await state.set_state(None)
        await message.answer("Вы пока не добавлены в семью.")
        return
    data = await state.get_data()
    prompt_message_id = data.get("prize_calc_prompt_message_id")
    if prompt_message_id is not None:
        await _history_try_delete_message(
            message.bot,
            int(message.chat.id),
            int(prompt_message_id),
        )
    first_stars = float(data.get("prize_calc_first_stars") or 0)
    second_stars = float(int(raw))
    prize_fund = await family_repo.get_weekly_prize_fund(ctx.family_id)
    algorithm = await family_repo.get_prize_calc_algorithm(ctx.family_id)
    first_prize, second_prize = _calculate_first_second_prizes(
        prize_fund, first_stars, second_stars, algorithm=algorithm
    )
    if first_prize is None or second_prize is None:
        await message.answer("Не удалось рассчитать призы: проверьте введенные баллы.")
    else:
        await message.answer(
            f"Первое место: {first_prize} руб.\n"
            f"Второе место: {second_prize} руб."
        )
    await state.set_state(None)
    await state.update_data(
        prize_calc_prompt_message_id=None,
        prize_calc_first_stars=None,
    )


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
    week_offset = 0
    by_user, active, scheduled, start_date, end_date = await runtime.stats_summary_for_week(
        ctx.family_id, timezone_name, week_offset=week_offset
    )
    by_stars = await runtime.stats_stars_by_user_for_week(ctx.family_id, timezone_name, week_offset=week_offset)
    by_task, _, _ = await runtime.stats_by_task_type_for_week(ctx.family_id, timezone_name, week_offset=week_offset)
    prize_fund = await family_repo.get_weekly_prize_fund(ctx.family_id)
    prize_algorithm = await family_repo.get_prize_calc_algorithm(ctx.family_id)
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
        first_prize, second_prize = _weekly_prize_amounts(prize_fund, by_stars, algorithm=prize_algorithm)
        for idx, row in enumerate(by_stars):
            line = f"- {row['display_name']}: {row['stars']}"
            if idx == 0 and first_prize is not None:
                line += f" (приз: {first_prize} руб.)"
            elif idx == 1 and second_prize is not None:
                line += f" (приз: {second_prize} руб.)"
            lines.append(line)
    if by_task:
        lines.append("По типам задач:")
        for row in by_task[:10]:
            lines.append(f"- {row['title']}: {row['cnt']}")
    lines.append(f"Активные задачи: {active}")
    lines.append(f"Запланированные задачи: {scheduled}")
    _, _, prev_week_start, _ = runtime._week_bounds_utc(timezone_name, week_offset - 1)
    _, _, next_week_start, _ = runtime._week_bounds_utc(timezone_name, week_offset + 1)
    left_enabled = await runtime.has_completions_for_week(ctx.family_id, timezone_name, week_offset=week_offset - 1)
    kb = _weekly_nav_keyboard(
        current_week_offset=week_offset,
        current_week_start=start_date,
        next_week_start=next_week_start,
        prev_week_start=prev_week_start,
        left_enabled=left_enabled,
    )
    await message.answer("\n".join(lines), reply_markup=kb)


@router.message(NavStates.in_stats_menu, F.text == "Текущий месяц")
async def stats_current_month(message: Message) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await message.answer("Вы пока не добавлены в семью.")
        return
    runtime = TaskRuntimeRepository(db)
    timezone_name = ctx.family_timezone or "UTC"
    month_offset = 0
    by_user, active, scheduled, start_date, end_date = await runtime.stats_summary_for_month(
        ctx.family_id, timezone_name, month_offset=month_offset
    )
    by_stars = await runtime.stats_stars_by_user_for_month(ctx.family_id, timezone_name, month_offset=month_offset)
    by_task, _, _ = await runtime.stats_by_task_type_for_month(ctx.family_id, timezone_name, month_offset=month_offset)
    lines = [
        f"Статистика за месяц ({start_date} - {end_date}):",
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
    _, _, prev_month_start, _ = runtime._month_bounds_utc(timezone_name, month_offset - 1)
    left_enabled = await runtime.has_completions_for_month(ctx.family_id, timezone_name, month_offset=month_offset - 1)
    kb = _monthly_nav_keyboard(
        current_month_offset=month_offset,
        current_month_start=start_date,
        prev_month_start=prev_month_start,
        left_enabled=left_enabled,
    )
    await message.answer("\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.startswith("statsw:"))
async def stats_week_callback(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return
    week_offset = int(callback.data.split(":")[1])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    runtime = TaskRuntimeRepository(db)
    timezone_name = ctx.family_timezone or "UTC"
    if week_offset > 0:
        await callback.answer()
        return
    if week_offset < 0:
        exists = await runtime.has_completions_for_week(ctx.family_id, timezone_name, week_offset=week_offset)
        if not exists:
            await callback.answer()
            return
    by_user, active, scheduled, start_date, end_date = await runtime.stats_summary_for_week(
        ctx.family_id, timezone_name, week_offset=week_offset
    )
    by_stars = await runtime.stats_stars_by_user_for_week(ctx.family_id, timezone_name, week_offset=week_offset)
    by_task, _, _ = await runtime.stats_by_task_type_for_week(ctx.family_id, timezone_name, week_offset=week_offset)
    prize_fund = await family_repo.get_weekly_prize_fund(ctx.family_id)
    prize_algorithm = await family_repo.get_prize_calc_algorithm(ctx.family_id)
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
        first_prize, second_prize = _weekly_prize_amounts(prize_fund, by_stars, algorithm=prize_algorithm)
        for idx, row in enumerate(by_stars):
            line = f"- {row['display_name']}: {row['stars']}"
            if idx == 0 and first_prize is not None:
                line += f" (приз: {first_prize} руб.)"
            elif idx == 1 and second_prize is not None:
                line += f" (приз: {second_prize} руб.)"
            lines.append(line)
    if by_task:
        lines.append("По типам задач:")
        for row in by_task[:10]:
            lines.append(f"- {row['title']}: {row['cnt']}")
    lines.append(f"Активные задачи: {active}")
    lines.append(f"Запланированные задачи: {scheduled}")
    _, _, prev_week_start, _ = runtime._week_bounds_utc(timezone_name, week_offset - 1)
    _, _, next_week_start, _ = runtime._week_bounds_utc(timezone_name, week_offset + 1)
    left_enabled = await runtime.has_completions_for_week(ctx.family_id, timezone_name, week_offset=week_offset - 1)
    kb = _weekly_nav_keyboard(
        current_week_offset=week_offset,
        current_week_start=start_date,
        next_week_start=next_week_start,
        prev_week_start=prev_week_start,
        left_enabled=left_enabled,
    )
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("statsmth:"))
async def stats_month_callback(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return
    month_offset = int(callback.data.split(":")[1])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    runtime = TaskRuntimeRepository(db)
    timezone_name = ctx.family_timezone or "UTC"
    if month_offset > 0:
        await callback.answer()
        return
    if month_offset < 0:
        exists = await runtime.has_completions_for_month(ctx.family_id, timezone_name, month_offset=month_offset)
        if not exists:
            await callback.answer()
            return
    by_user, active, scheduled, start_date, end_date = await runtime.stats_summary_for_month(
        ctx.family_id, timezone_name, month_offset=month_offset
    )
    by_stars = await runtime.stats_stars_by_user_for_month(ctx.family_id, timezone_name, month_offset=month_offset)
    by_task, _, _ = await runtime.stats_by_task_type_for_month(ctx.family_id, timezone_name, month_offset=month_offset)
    lines = [
        f"Статистика за месяц ({start_date} - {end_date}):",
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
    _, _, prev_month_start, _ = runtime._month_bounds_utc(timezone_name, month_offset - 1)
    left_enabled = await runtime.has_completions_for_month(ctx.family_id, timezone_name, month_offset=month_offset - 1)
    kb = _monthly_nav_keyboard(
        current_month_offset=month_offset,
        current_month_start=start_date,
        prev_month_start=prev_month_start,
        left_enabled=left_enabled,
    )
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    except TelegramBadRequest:
        pass
    await callback.answer()


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
    week_index: int,
    runtime: TaskRuntimeRepository,
    timezone_name: str,
    state: FSMContext,
    task_title: str,
    source_token: str = "root",
) -> None:
    rows = await runtime.list_recent_actions_by_task_all(family_id, task_id)
    week_pages = _build_week_pages(rows, timezone_name, reverse_input=True)
    if not week_pages:
        lines = ["Последние действия по задаче:", "Действий пока нет."]
        normalized_week_index = 0
    else:
        normalized_week_index = max(0, min(week_index, len(week_pages) - 1))
        page = week_pages[normalized_week_index]
        lines = ["Последние действия по задаче:", page["header"]]
        for day_part, row in page["items"]:
            lines.append(f"- {day_part} — {row['member_display_name']}")
    kb = _build_day_nav_markup(
        week_pages,
        normalized_week_index,
        lambda target_idx: f"statst:{task_id}:{target_idx}:{source_token}",
        f"statsback:task:{source_token}",
    )
    await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    await _save_stats_history_context(
        state,
        mode="task",
        day_index=normalized_week_index,
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
    _, task_id_raw, week_index_raw, *source_parts = parts
    task_id = int(task_id_raw)
    week_index = max(0, int(week_index_raw))
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
        week_index,
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
    week_index = max(0, int(m.group(3)))
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
        day_index=week_index,
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


def _history_card_text(entry: dict | object, timezone_name: str) -> str:
    row = dict(entry) if not isinstance(entry, dict) else entry
    local_completed_at = _to_family_local_timestamp(str(row["completed_at"]), timezone_name)
    local_added_at = _to_family_local_timestamp(str(row["added_at"]), timezone_name)
    local_updated_at = _to_family_local_timestamp(str(row["history_updated_at"]), timezone_name)
    action = _format_action_label(str(row["task_title"]), str(row["completion_mode"]))
    comment = str(row.get("comment_text") or "").strip()
    lines = [
        f"Запись истории #{row['completion_id']}",
        f"Действие: {action}",
        f"Исполнитель: {row['member_display_name']}",
        f"Дата/Время действия: {local_completed_at}",
        f"Дата/Время добавления действия: {local_added_at}",
        f"Дата/Время изменения истории: {local_updated_at}",
    ]
    if comment:
        lines.append(f"Комментарий: {comment}")
    return "\n".join(lines)


TELEGRAM_MESSAGE_MAX_LEN = 4096


def _telegram_safe_message_text(text: str, max_len: int = TELEGRAM_MESSAGE_MAX_LEN) -> str:
    raw = text or ""
    if len(raw) <= max_len:
        return raw
    tail = "\n…"
    return raw[: max_len - len(tail)] + tail


def _history_card_text_for_telegram(entry: dict, timezone_name: str) -> str:
    return _telegram_safe_message_text(_history_card_text(entry, timezone_name))


async def _hist_reply_text(
    callback: CallbackQuery,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    uid = int(callback.from_user.id)
    if callback.message is None:
        return await callback.bot.send_message(uid, text, reply_markup=reply_markup)
    try:
        return await callback.message.answer(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        return await callback.bot.send_message(uid, text, reply_markup=reply_markup)


def _history_norm_completed_at(value: str) -> str:
    raw = (value or "").strip().replace("T", " ")
    if not raw:
        return ""
    try:
        parsed = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return raw
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _parse_completed_at_utc_sql(value: str) -> datetime:
    return parse_completed_at_utc_sql(value)


def _history_bump_local_datetime(
    completed_at_utc_sql: str,
    tz_name: str,
    field: str,
    delta: int,
) -> str:
    return bump_local_datetime(completed_at_utc_sql, tz_name, field, delta)


def _history_datetime_keyboard(time_preview: str) -> InlineKeyboardMarkup:
    return history_datetime_keyboard(time_preview)


async def _history_try_delete_message(bot, chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=int(message_id))
    except TelegramBadRequest:
        pass


async def _history_cleanup_submessages(bot, data: dict, chat_id: int) -> None:
    await _history_try_delete_message(bot, chat_id, data.get("hist_exec_pick_msg_id"))
    await _history_try_delete_message(bot, chat_id, data.get("hist_dt_ui_msg_id"))
    await _history_try_delete_message(bot, chat_id, data.get("hist_comment_prompt_msg_id"))


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
    text = _history_card_text_for_telegram(row, timezone_name)
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


async def _history_refresh_from_state(
    state: FSMContext,
    *,
    bot,
    runtime: TaskRuntimeRepository,
    family_repo,
    family_id: int,
    timezone_name: str,
) -> bool:
    data = await state.get_data()
    draft = data.get("hist_edit_draft")
    if not isinstance(draft, dict):
        return False
    await _history_refresh_draft_card(
        bot,
        runtime,
        family_repo,
        family_id=family_id,
        draft=draft,
        timezone_name=timezone_name,
    )
    return True


def _history_entry_actions_keyboard(completion_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Исполнитель", callback_data=f"histeditexec:{completion_id}"),
                InlineKeyboardButton(text="Дата/Время", callback_data=f"histedittime:{completion_id}"),
            ],
            [InlineKeyboardButton(text="Комментарий", callback_data=f"histeditcomment:{completion_id}")],
            [
                InlineKeyboardButton(text="Удалить", callback_data=f"histeditdelask:{completion_id}"),
                InlineKeyboardButton(text="Обновить", callback_data=f"histapply:{completion_id}"),
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="histeditback"),
                InlineKeyboardButton(text="Отмена", callback_data="histeditcancel"),
            ],
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
    if callback.message is not None:
        data = await state.get_data()
        await _history_cleanup_submessages(
            callback.message.bot,
            data,
            int(callback.message.chat.id),
        )
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
    try:
        await _hist_reply_text(callback, HISTORY_EDIT_PROMPT, reply_markup=kb)
    except TelegramBadRequest:
        await callback.answer("Не удалось отправить список.", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data == "histeditcancel")
async def stats_history_edit_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer()
        return
    data = await state.get_data()
    await _history_cleanup_submessages(callback.message.bot, data, int(callback.message.chat.id))
    await _clear_state_keep_stats_context(state)
    try:
        await callback.message.edit_text("Операция отменена.", reply_markup=None)
    except TelegramBadRequest:
        await callback.message.answer("Операция отменена.")
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
    card = _history_card_text_for_telegram(entry, timezone_name)
    try:
        sent = await _hist_reply_text(callback, card, reply_markup=kb)
    except TelegramBadRequest:
        await callback.answer("Не удалось отправить карточку.", show_alert=True)
        return
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
        },
        hist_exec_pick_msg_id=None,
        hist_dt_ui_msg_id=None,
        hist_dt_baseline_utc=None,
        hist_dt_offset_minutes=0,
        hist_comment_prompt_msg_id=None,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("histeditexecback:"))
async def stats_history_edit_executor_picker_back(callback: CallbackQuery, state: FSMContext) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if callback.message is not None:
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
    await state.update_data(hist_exec_pick_msg_id=None)
    await state.set_state(None)
    runtime = TaskRuntimeRepository(db)
    timezone_name = ctx.family_timezone or "UTC"
    await _history_refresh_from_state(
        state,
        bot=callback.bot,
        runtime=runtime,
        family_repo=family_repo,
        family_id=ctx.family_id,
        timezone_name=timezone_name,
    )
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
    data = await state.get_data()
    draft = data.get("hist_edit_draft")
    if not isinstance(draft, dict) or int(draft.get("completion_id", 0)) != completion_id:
        await callback.answer("Сначала откройте запись.", show_alert=True)
        return
    await _history_try_delete_message(callback.bot, int(callback.message.chat.id), data.get("hist_exec_pick_msg_id"))
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
    try:
        sent = await _hist_reply_text(
            callback,
            "Выберите нового исполнителя:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await state.update_data(hist_exec_pick_msg_id=sent.message_id)
    except TelegramBadRequest:
        await state.set_state(None)
        await callback.answer("Не удалось показать список исполнителей.", show_alert=True)
        return
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
    await state.update_data(hist_exec_pick_msg_id=None)
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
    if callback.message is not None:
        data = await state.get_data()
        await _history_cleanup_submessages(
            callback.bot,
            data,
            int(callback.message.chat.id),
        )
        await state.update_data(
            hist_exec_pick_msg_id=None,
            hist_dt_ui_msg_id=None,
            hist_dt_baseline_utc=None,
            hist_comment_prompt_msg_id=None,
        )
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
    await _history_try_delete_message(
        callback.bot,
        int(callback.message.chat.id),
        data.get("hist_comment_prompt_msg_id"),
    )
    await state.set_state(StatsStates.waiting_history_comment)
    await state.update_data(history_comment_completion_id=completion_id)
    try:
        sent = await _hist_reply_text(
            callback,
            "Введите новый комментарий (пустое сообщение — очистить комментарий; «Отмена» — выйти без изменений):",
        )
        await state.update_data(hist_comment_prompt_msg_id=sent.message_id)
    except TelegramBadRequest:
        await state.set_state(None)
        await callback.answer("Не удалось отправить запрос.", show_alert=True)
        return
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
    data = await state.get_data()
    draft = data.get("hist_edit_draft")
    if not isinstance(draft, dict) or int(draft.get("completion_id", 0)) != completion_id:
        await callback.answer("Сначала откройте запись.", show_alert=True)
        return
    timezone_name = ctx.family_timezone or "UTC"
    baseline = str(draft.get("completed_at_utc") or "").strip()
    if not baseline:
        await callback.answer("Некорректное время черновика.", show_alert=True)
        return
    prev_dt = data.get("hist_dt_ui_msg_id")
    await _history_try_delete_message(callback.bot, int(callback.message.chat.id), prev_dt)
    await state.update_data(hist_dt_baseline_utc=baseline)
    current_local = _to_family_local_timestamp(baseline, timezone_name)
    try:
        sent = await _hist_reply_text(
            callback,
            f"Текущая дата/время (база): {current_local}\n"
            f"Измените время кнопками ниже (новое значение на первой кнопке).",
            reply_markup=_history_datetime_keyboard(current_local),
        )
        await state.update_data(hist_dt_ui_msg_id=sent.message_id)
    except TelegramBadRequest:
        await callback.answer("Не удалось отправить запрос.", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("histdt:"))
async def stats_history_edit_datetime_adjust(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer()
        return
    parts = callback.data.split(":")
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
    data = await state.get_data()
    draft = data.get("hist_edit_draft")
    if not isinstance(draft, dict):
        await callback.answer("Черновик записи не найден.", show_alert=True)
        return
    if len(parts) == 2 and parts[1] == "back":
        await _history_try_delete_message(
            callback.bot,
            int(callback.message.chat.id),
            data.get("hist_dt_ui_msg_id"),
        )
        await state.update_data(hist_dt_ui_msg_id=None, hist_dt_baseline_utc=None)
        await _history_refresh_from_state(
            state,
            bot=callback.bot,
            runtime=runtime,
            family_repo=family_repo,
            family_id=ctx.family_id,
            timezone_name=timezone_name,
        )
        await callback.answer()
        return
    if len(parts) != 3:
        await callback.answer()
        return
    _, sign, field = parts
    delta = 1 if sign == "+" else -1 if sign == "-" else 0
    if field == "m5":
        field = "m"
        delta *= 5
    if delta == 0 or field not in {"d", "M", "y", "h", "m"}:
        await callback.answer()
        return
    cur_utc = str(draft.get("completed_at_utc") or "").strip()
    if not cur_utc:
        await callback.answer("Некорректное время.", show_alert=True)
        return
    try:
        new_utc = _history_bump_local_datetime(cur_utc, timezone_name, field, delta)
        cur_dt = _parse_completed_at_utc_sql(cur_utc)
        new_dt = _parse_completed_at_utc_sql(new_utc)
    except ValueError:
        await callback.answer("Некорректное время.", show_alert=True)
        return
    draft["completed_at_utc"] = _history_norm_completed_at(new_utc)
    offset_delta_minutes = round((new_dt - cur_dt).total_seconds() / 60)
    offset_minutes = int(data.get("hist_dt_offset_minutes") or 0) + offset_delta_minutes
    await state.update_data(
        hist_edit_draft=draft,
        hist_dt_offset_minutes=offset_minutes,
    )
    preview = _to_family_local_timestamp(str(draft["completed_at_utc"]), timezone_name)
    baseline = str(data.get("hist_dt_baseline_utc") or cur_utc)
    header = (
        f"Текущая дата/время (база): {_to_family_local_timestamp(baseline, timezone_name)}\n"
        f"Измените время кнопками ниже (новое значение на первой кнопке)."
    )
    try:
        await callback.message.edit_text(header, reply_markup=_history_datetime_keyboard(preview))
    except TelegramBadRequest:
        pass
    await _history_refresh_from_state(
        state,
        bot=callback.bot,
        runtime=runtime,
        family_repo=family_repo,
        family_id=ctx.family_id,
        timezone_name=timezone_name,
    )
    await callback.answer("Время изменено в черновике. Нажмите «Обновить», чтобы сохранить в базе.")


@router.message(StatsStates.waiting_history_comment)
async def stats_history_edit_comment_save(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw.lower() == "отмена":
        data = await state.get_data()
        await _history_try_delete_message(
            message.bot,
            int(message.chat.id),
            data.get("hist_comment_prompt_msg_id"),
        )
        await state.set_state(None)
        await state.update_data(history_comment_completion_id=None, hist_comment_prompt_msg_id=None)
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
    await _history_try_delete_message(
        message.bot,
        int(message.chat.id),
        data.get("hist_comment_prompt_msg_id"),
    )
    await state.update_data(
        hist_edit_draft=draft,
        history_comment_completion_id=None,
        hist_comment_prompt_msg_id=None,
    )
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
    line = _telegram_safe_message_text(
        _history_line(entry, timezone_name).lstrip("- ").strip(),
        max_len=3500,
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Нет", callback_data=f"histeditdelno:{completion_id}"),
                InlineKeyboardButton(text="Да", callback_data=f"histeditdelyes:{completion_id}"),
            ]
        ]
    )
    try:
        await _hist_reply_text(
            callback,
            f"Вы точно хотите удалить запись в истории: {line}",
            reply_markup=kb,
        )
    except TelegramBadRequest:
        await callback.answer("Не удалось отправить подтверждение.", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("histeditdelno:"))
async def stats_history_delete_no(callback: CallbackQuery) -> None:
    completion_id = int(callback.data.split(":")[1])
    try:
        await _hist_reply_text(
            callback,
            "Удаление отменено.",
            reply_markup=_history_entry_actions_keyboard(completion_id),
        )
    except TelegramBadRequest:
        await callback.answer("Не удалось отправить ответ.", show_alert=True)
        return
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
    if callback.message is not None:
        data = await state.get_data()
        await _history_cleanup_submessages(
            callback.bot,
            data,
            int(callback.message.chat.id),
        )
    runtime = TaskRuntimeRepository(db)
    deleted = await runtime.delete_completion_entry(ctx.family_id, completion_id)
    if not deleted:
        await callback.answer("Не удалось удалить запись.", show_alert=True)
        return
    timezone_name = ctx.family_timezone or "UTC"
    kb = await _prepare_history_edit_reply_markup(state, runtime, ctx.family_id, timezone_name)
    try:
        await _hist_reply_text(callback, "Запись истории удалена.")
        await _hist_reply_text(callback, HISTORY_EDIT_PROMPT, reply_markup=kb)
    except TelegramBadRequest:
        await callback.answer("Запись удалена, но не удалось обновить список.", show_alert=True)
        return
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
