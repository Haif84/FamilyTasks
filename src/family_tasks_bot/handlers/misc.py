from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from family_tasks_bot.deps import get_repositories
from family_tasks_bot.db.repositories import NotificationRepository, PlannedTaskRepository, TaskRuntimeRepository
from family_tasks_bot.keyboards.reply import main_menu, misc_menu, stats_menu
from family_tasks_bot.services.bootstrap import ensure_member_context
from family_tasks_bot.states import NavStates
from family_tasks_bot.version import APP_VERSION
router = Router(name="misc")
QUIET_RE = re.compile(r"^/quiet\s+(\d{2}:\d{2})-(\d{2}:\d{2})(?:\s+(all|[0-6]))?$")
STATS_RE = re.compile(r"^/stats(?:\s+(day|week|month))?$")
PAGE_SIZE = 10


@router.message(F.text == "Прочее")
async def open_misc(message: Message) -> None:
    await message.answer("Раздел Прочее", reply_markup=misc_menu())


@router.message(F.text == "Статистика")
async def statistics(message: Message, state: FSMContext) -> None:
    await state.set_state(NavStates.in_stats_menu)
    await message.answer(
        "Статистика: выберите режим просмотра.",
        reply_markup=stats_menu(),
    )


@router.message(NavStates.in_stats_menu, F.text == "По члену семьи")
async def stats_by_member_menu(message: Message) -> None:
    db, user_repo, family_repo = get_repositories()
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
    tasks = await repo.list_tasks(ctx.family_id)
    buttons = [
        [InlineKeyboardButton(text=str(task["title"]), callback_data=f"statst:{task['id']}:0")]
        for task in tasks
    ]
    await message.answer(
        "Выберите задачу:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons or [[InlineKeyboardButton(text="Нет задач", callback_data="noop")]]
        ),
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


async def _send_stats(message: Message, period: str) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None:
        await message.answer("Вы пока не добавлены в семью.")
        return
    runtime = TaskRuntimeRepository(db)
    days = {"day": 1, "week": 7, "month": 30}[period]
    by_user, active, scheduled = await runtime.stats_summary(ctx.family_id, days)
    by_task = await runtime.stats_by_task_type(ctx.family_id, days)
    title = {"day": "день", "week": "неделю", "month": "месяц"}[period]
    lines = [f"Статистика за {title}:"]
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
    await message.answer("\n".join(lines))


async def _render_member_actions(
    callback: CallbackQuery, family_id: int, user_id: int, offset: int, runtime: TaskRuntimeRepository
) -> None:
    rows = await runtime.list_recent_actions_by_member(family_id, user_id, PAGE_SIZE + 1, offset)
    has_more = len(rows) > PAGE_SIZE
    entries = rows[:PAGE_SIZE]
    lines = ["Последние действия участника:"]
    if entries:
        for row in entries:
            lines.append(f"- {row['completed_at']}")
    else:
        lines.append("Действий пока нет.")
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        prev_offset = max(0, offset - PAGE_SIZE)
        nav.append(InlineKeyboardButton(text="Назад (более поздние)", callback_data=f"statsm:{user_id}:{prev_offset}"))
    if has_more:
        nav.append(InlineKeyboardButton(text="Вперед (более ранние)", callback_data=f"statsm:{user_id}:{offset + PAGE_SIZE}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[nav] if nav else [])
    await callback.message.edit_text("\n".join(lines), reply_markup=kb if nav else None)


async def _render_task_actions(
    callback: CallbackQuery, family_id: int, task_id: int, offset: int, runtime: TaskRuntimeRepository
) -> None:
    rows = await runtime.list_recent_actions_by_task(family_id, task_id, PAGE_SIZE + 1, offset)
    has_more = len(rows) > PAGE_SIZE
    entries = rows[:PAGE_SIZE]
    lines = ["Последние действия по задаче:"]
    if entries:
        for row in entries:
            lines.append(f"- {row['completed_at']} — {row['display_name']}")
    else:
        lines.append("Действий пока нет.")
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        prev_offset = max(0, offset - PAGE_SIZE)
        nav.append(InlineKeyboardButton(text="Назад (более поздние)", callback_data=f"statst:{task_id}:{prev_offset}"))
    if has_more:
        nav.append(InlineKeyboardButton(text="Вперед (более ранние)", callback_data=f"statst:{task_id}:{offset + PAGE_SIZE}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[nav] if nav else [])
    await callback.message.edit_text("\n".join(lines), reply_markup=kb if nav else None)


@router.callback_query(F.data.startswith("statsm:"))
async def stats_member_callback(callback: CallbackQuery) -> None:
    _, user_id_raw, offset_raw = callback.data.split(":")
    user_id = int(user_id_raw)
    offset = max(0, int(offset_raw))
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    runtime = TaskRuntimeRepository(db)
    await _render_member_actions(callback, ctx.family_id, user_id, offset, runtime)
    await callback.answer()


@router.callback_query(F.data.startswith("statst:"))
async def stats_task_callback(callback: CallbackQuery) -> None:
    _, task_id_raw, offset_raw = callback.data.split(":")
    task_id = int(task_id_raw)
    offset = max(0, int(offset_raw))
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    runtime = TaskRuntimeRepository(db)
    await _render_task_actions(callback, ctx.family_id, task_id, offset, runtime)
    await callback.answer()


@router.message(F.text == "Назад")
async def back_to_main(message: Message, state: FSMContext) -> None:
    db, user_repo, family_repo = get_repositories()
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
