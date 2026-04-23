from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from family_tasks_bot.deps import get_repositories
from family_tasks_bot.db.repositories import NotificationRepository, PlannedTaskRepository, TaskRuntimeRepository
from family_tasks_bot.handlers.common import deny_if_no_family
from family_tasks_bot.keyboards.inline import tasks_keyboard
from family_tasks_bot.keyboards.reply import back_menu, planned_tasks_menu
from family_tasks_bot.services.auth import AccessContext, can_add_to_execution, can_edit_planned_tasks
from family_tasks_bot.services.bootstrap import ensure_member_context
from family_tasks_bot.services.notifications import notify_family
from family_tasks_bot.states import NavStates, PlannedTaskStates, RuntimeTaskStates
from family_tasks_bot.utils.validators import is_valid_hhmm

router = Router(name="tasks")


def _task_caption(task: dict | object) -> str:
    effort_stars = int(task["effort_stars"]) if task["effort_stars"] is not None else 1
    stars = _stars_text(effort_stars)
    suffix = " (неактивна)" if not bool(task["is_active"]) else ""
    return f"{task['sort_order']}. {task['title']} [{stars}]{suffix}"


def _family_tzinfo(timezone_name: str) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return timezone.utc


def _to_family_local_timestamp(raw_value: str, timezone_name: str) -> str:
    raw = (raw_value or "").strip()
    if not raw:
        return raw_value
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return raw_value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(_family_tzinfo(timezone_name))
    return local.strftime("%Y-%m-%d %H:%M")


def _undo_last_completion_confirm_keyboard(completion_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Нет", callback_data=f"undolast:no:{completion_id}"),
                InlineKeyboardButton(text="Да", callback_data=f"undolast:yes:{completion_id}"),
            ]
        ]
    )


async def _clear_callback_inline_keyboard(callback: CallbackQuery) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text("Задача выбрана.", reply_markup=None)
    except TelegramBadRequest:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass


def _manual_completion_confirm_text(*, executor_phrase: str, task: dict | object) -> str:
    return f"Вы действительно хотите добавить выполненную {executor_phrase} задачу '{task['title']}'?"


def _add_execution_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Отмена", callback_data="addexecconfirm:cancel"),
                InlineKeyboardButton(text="Назад", callback_data="addexecconfirm:back"),
            ],
            [InlineKeyboardButton(text="Добавить", callback_data="addexecconfirm:add")],
            [InlineKeyboardButton(text="Добавить (еще одну)", callback_data="addexecconfirm:addmore")],
        ]
    )


def _manual_fin_tz(tz_name: str) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(tz_name or "UTC")
    except Exception:
        return timezone.utc


def _parse_completed_at_utc_sql(value: str) -> datetime:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty")
    if "T" not in raw and " " in raw:
        raw = raw.replace(" ", "T", 1)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _bump_manual_completion_local_datetime(
    completed_at_utc_sql: str,
    tz_name: str,
    field: str,
    delta: int,
) -> str:
    utc = _parse_completed_at_utc_sql(completed_at_utc_sql)
    tz = _manual_fin_tz(tz_name)
    local = utc.astimezone(tz)
    y, M, d, h, mi, sec = (
        local.year,
        local.month,
        local.day,
        local.hour,
        local.minute,
        local.second,
    )
    if field == "m":
        local2 = local + timedelta(minutes=delta)
    elif field == "h":
        local2 = local + timedelta(hours=delta)
    elif field == "d":
        local2 = local + timedelta(days=delta)
    elif field == "M":
        total = y * 12 + (M - 1) + delta
        y2, m0 = divmod(total, 12)
        M2 = m0 + 1
        max_d = monthrange(y2, M2)[1]
        d2 = min(d, max_d)
        local2 = local.replace(year=y2, month=M2, day=d2)
    elif field == "y":
        y2 = y + delta
        max_d = monthrange(y2, M)[1]
        d2 = min(d, max_d)
        local2 = local.replace(year=y2, day=d2)
    else:
        local2 = local
    return local2.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _manual_completion_final_keyboard(
    *,
    is_admin: bool,
    requires_comment: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if is_admin:
        rows.append(
            [
                InlineKeyboardButton(text="Исполнитель", callback_data="mcfin:exec"),
                InlineKeyboardButton(text="Дата/Время", callback_data="mcfin:dt"),
            ]
        )
    if requires_comment:
        rows.append([InlineKeyboardButton(text="Комментарий", callback_data="mcfin:comment")])
    rows.append(
        [
            InlineKeyboardButton(text="Добавить (+1)", callback_data="mcfin:addmore"),
            InlineKeyboardButton(text="Добавить", callback_data="mcfin:add"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="Назад", callback_data="mcfin:back"),
            InlineKeyboardButton(text="Отмена", callback_data="mcfin:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _manual_completion_datetime_keyboard(time_preview: str) -> InlineKeyboardMarkup:
    fields = ["d", "M", "y", "h", "m"]
    labels_up = ["День+", "Мес+", "Год+", "Час+", "Мин+"]
    labels_dn = ["День−", "Мес−", "Год−", "Час−", "Мин−"]
    row_up = [InlineKeyboardButton(text=labels_up[i], callback_data=f"mcdt:+:{fields[i]}") for i in range(5)]
    row_dn = [InlineKeyboardButton(text=labels_dn[i], callback_data=f"mcdt:-:{fields[i]}") for i in range(5)]
    preview = time_preview if len(time_preview) <= 64 else f"{time_preview[:61]}..."
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=preview, callback_data="noop")],
            row_up,
            row_dn,
            [InlineKeyboardButton(text="Назад", callback_data="mcdt:back")],
        ]
    )


def _stars_text(stars: int) -> str:
    normalized = max(1, min(5, int(stars)))
    return ("★" * normalized) + ("☆" * (5 - normalized))


async def _manual_done_root_keyboard(
    runtime: TaskRuntimeRepository,
    family_repo,
    family_id: int,
) -> InlineKeyboardMarkup:
    tasks_without_group = await runtime.list_planned_tasks_without_group(family_id)
    groups = await family_repo.list_groups(family_id)
    rows: list[list[InlineKeyboardButton]] = []
    for task in tasks_without_group:
        rows.append(
            [
                InlineKeyboardButton(
                    text=str(task["title"]),
                    callback_data=f"manualdone:root:{task['id']}",
                )
            ]
        )
    for group in groups:
        tasks_in_group = await runtime.list_planned_tasks_by_group(family_id, int(group["id"]))
        if not tasks_in_group:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=f'Группа "{group["name"]}"',
                    callback_data=f"manualgroup:{group['id']}",
                )
            ]
        )
    if not rows:
        rows = [[InlineKeyboardButton(text="Нет доступных задач", callback_data="manualgroup:none")]]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="manualgroup:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _manual_done_group_keyboard(tasks: list[dict | object], group_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for task in tasks:
        rows.append(
            [
                InlineKeyboardButton(
                    text=str(task["title"]),
                    callback_data=f"manualdone:group:{group_id}:{task['id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Назад", callback_data="manualgroup:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _manual_done_for_member_picker_keyboard(members: list[dict | object]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for member in members:
        rows.append(
            [
                InlineKeyboardButton(
                    text=str(member["display_name"]),
                    callback_data=f"manualforuser:{member['user_id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="manualforuser:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _manual_done_for_member_root_keyboard(
    runtime: TaskRuntimeRepository,
    family_repo,
    family_id: int,
    target_user_id: int,
) -> InlineKeyboardMarkup:
    tasks_without_group = await runtime.list_planned_tasks_without_group(family_id)
    groups = await family_repo.list_groups(family_id)
    rows: list[list[InlineKeyboardButton]] = []
    for task in tasks_without_group:
        rows.append(
            [
                InlineKeyboardButton(
                    text=str(task["title"]),
                    callback_data=f"manualfordone:{target_user_id}:root:{task['id']}",
                )
            ]
        )
    for group in groups:
        tasks_in_group = await runtime.list_planned_tasks_by_group(family_id, int(group["id"]))
        if not tasks_in_group:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=f'Группа "{group["name"]}"',
                    callback_data=f"manualforgroup:{target_user_id}:{group['id']}",
                )
            ]
        )
    if not rows:
        rows = [
            [
                InlineKeyboardButton(
                    text="Нет доступных задач",
                    callback_data=f"manualforgroup:{target_user_id}:none",
                )
            ]
        ]
    rows.append(
        [
            InlineKeyboardButton(
                text="Отмена",
                callback_data=f"manualforgroup:{target_user_id}:cancel",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _manual_done_for_member_group_keyboard(
    tasks: list[dict | object], target_user_id: int, group_id: int
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for task in tasks:
        rows.append(
            [
                InlineKeyboardButton(
                    text=str(task["title"]),
                    callback_data=f"manualfordone:{target_user_id}:group:{group_id}:{task['id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Назад", callback_data=f"manualforgroup:{target_user_id}:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _member_display_name(members: list[dict | object], target_user_id: int) -> str | None:
    for member in members:
        if int(member["user_id"]) == target_user_id:
            return str(member["display_name"])
    return None


async def _add_execution_root_keyboard(
    runtime: TaskRuntimeRepository,
    family_repo,
    family_id: int,
) -> InlineKeyboardMarkup:
    tasks_without_group = await runtime.list_planned_tasks_without_group(family_id)
    groups = await family_repo.list_groups(family_id)
    rows: list[list[InlineKeyboardButton]] = []
    for task in tasks_without_group:
        rows.append(
            [
                InlineKeyboardButton(
                    text=str(task["title"]),
                    callback_data=f"addexec:root:{task['id']}",
                )
            ]
        )
    for group in groups:
        tasks_in_group = await runtime.list_planned_tasks_by_group(family_id, int(group["id"]))
        if not tasks_in_group:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=f'Группа "{group["name"]}"',
                    callback_data=f"addexecgroup:{group['id']}",
                )
            ]
        )
    if not rows:
        rows = [[InlineKeyboardButton(text="Нет доступных задач", callback_data="addexecgroup:none")]]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="addexecgroup:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _add_execution_group_keyboard(tasks: list[dict | object], group_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for task in tasks:
        rows.append(
            [
                InlineKeyboardButton(
                    text=str(task["title"]),
                    callback_data=f"addexec:group:{group_id}:{task['id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Назад", callback_data="addexecgroup:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _manual_fin_local_display(utc_sql: str, tz_name: str) -> str:
    raw = (utc_sql or "").strip()
    if not raw:
        return "—"
    try:
        dt = _parse_completed_at_utc_sql(raw)
    except ValueError:
        return raw
    tz = _manual_fin_tz(tz_name)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


async def _manual_fin_executor_phrase(ctx: AccessContext, family_repo, data: dict) -> str:
    uid_exec = int(data.get("m_fin_completed_by", 0))
    if uid_exec == ctx.user_id:
        return "вами"
    members = await family_repo.list_members_for_edit(ctx.family_id)
    name = _member_display_name(members, uid_exec) or str(uid_exec)
    return f"участником {name}"


async def _manual_fin_seed_state(
    state: FSMContext,
    *,
    planned_task_id: int,
    completed_by_user_id: int,
    actor_user_id: int,
    scope: str,
    scope_id: int,
    add_more: bool,
    for_member: bool,
    target_member_name: str,
    task_requires_comment: bool,
    initial_comment: str,
    chat_id: int,
) -> None:
    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await state.update_data(
        m_fin_planned_task_id=planned_task_id,
        m_fin_completed_by=completed_by_user_id,
        m_fin_actor_user_id=actor_user_id,
        m_fin_scope=scope,
        m_fin_scope_id=scope_id,
        m_fin_add_more=add_more,
        m_fin_for_member=1 if for_member else 0,
        m_fin_target_name=target_member_name,
        m_fin_task_requires_comment=1 if task_requires_comment else 0,
        m_fin_comment=initial_comment.strip(),
        m_fin_completed_at_utc=utc_now,
        m_fin_chat_id=chat_id,
        m_fin_final_msg_id=None,
        m_fin_dt_ui_msg_id=None,
        m_fin_dt_baseline_utc=None,
        m_fin_exec_pick_msg_id=None,
    )
    await state.set_state(RuntimeTaskStates.waiting_manual_completion_draft)


async def _manual_fin_answer_final(
    message: Message,
    state: FSMContext,
    ctx: AccessContext,
    family_repo,
    task: dict | object,
) -> None:
    data = await state.get_data()
    tz = ctx.family_timezone or "UTC"
    when = _manual_fin_local_display(str(data.get("m_fin_completed_at_utc") or ""), tz)
    executor_phrase = await _manual_fin_executor_phrase(ctx, family_repo, data)
    comment = (data.get("m_fin_comment") or "").strip()
    req_c = int(data.get("m_fin_task_requires_comment") or 0) == 1
    lines = [
        _manual_completion_confirm_text(executor_phrase=executor_phrase, task=task),
        f"Время выполнения: {when}",
    ]
    if req_c:
        lines.append(f"Комментарий: {comment or '(пусто)'}")
    text = "\n".join(lines)
    kb = _manual_completion_final_keyboard(
        is_admin=ctx.is_admin,
        requires_comment=req_c,
    )
    chat_id = int(data.get("m_fin_chat_id") or message.chat.id)
    mid = data.get("m_fin_final_msg_id")
    if mid is not None:
        try:
            await message.bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=int(mid),
                reply_markup=kb,
            )
            return
        except TelegramBadRequest:
            pass
    sent = await message.answer(text, reply_markup=kb)
    await state.update_data(m_fin_final_msg_id=sent.message_id)


async def _finalize_manual_completion(
    bot,
    runtime: TaskRuntimeRepository,
    notify_repo: NotificationRepository,
    *,
    family_id: int,
    planned_task_id: int,
    completed_by_user_id: int,
    actor_user_id: int,
    actor_chat_id: int | None,
    comment_text: str | None = None,
    completed_at_utc: str | None = None,
) -> None:
    await runtime.add_manual_completion(
        family_id,
        planned_task_id,
        completed_by_user_id,
        comment_text=comment_text,
        actor_user_id=actor_user_id,
        completed_at_utc=completed_at_utc,
    )
    await _process_dependencies(
        bot,
        runtime,
        notify_repo,
        family_id,
        planned_task_id,
        actor_user_id,
        actor_chat_id,
    )


async def _manual_fin_try_delete_message(bot, chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=int(message_id))
    except TelegramBadRequest:
        pass


async def _manual_fin_cleanup_submessages(bot, data: dict, chat_id: int) -> None:
    await _manual_fin_try_delete_message(bot, chat_id, data.get("m_fin_dt_ui_msg_id"))
    await _manual_fin_try_delete_message(bot, chat_id, data.get("m_fin_exec_pick_msg_id"))


async def _manual_fin_success_finish(
    message: Message,
    state: FSMContext,
    *,
    runtime: TaskRuntimeRepository,
    family_repo,
    ctx: AccessContext,
    task_title: str,
) -> None:
    data = await state.get_data()
    add_more = bool(data.get("m_fin_add_more", False))
    scope = str(data.get("m_fin_scope", "root"))
    scope_id = int(data.get("m_fin_scope_id", 0))
    for_member = int(data.get("m_fin_for_member", 0)) == 1
    target_user_id = int(data.get("m_fin_completed_by", 0))
    member_name = str(data.get("m_fin_target_name", "")).strip()
    chat_id = int(data.get("m_fin_chat_id") or message.chat.id)
    await _manual_fin_cleanup_submessages(message.bot, data, chat_id)
    success_text = f"Задача {task_title} добавлена"
    mid = data.get("m_fin_final_msg_id")
    if mid is not None:
        try:
            await message.bot.edit_message_text(
                success_text,
                chat_id=chat_id,
                message_id=int(mid),
                reply_markup=None,
            )
        except TelegramBadRequest:
            await message.answer(success_text)
    else:
        await message.answer(success_text)
    await state.clear()
    family_id = ctx.family_id
    if not add_more:
        return
    if for_member:
        display_name = member_name
        if not display_name and target_user_id > 0:
            members = await family_repo.list_members_for_edit(family_id)
            display_name = _member_display_name(members, target_user_id) or "Участник"
        if target_user_id > 0:
            payload = await _manual_member_level_payload(
                runtime,
                family_repo,
                family_id=family_id,
                target_user_id=target_user_id,
                member_name=display_name or "Участник",
                scope=scope,
                scope_id=scope_id,
            )
            if payload is not None:
                level_text, level_kb = payload
                await message.answer(level_text, reply_markup=level_kb)
    else:
        payload = await _manual_self_level_payload(
            runtime,
            family_repo,
            family_id=family_id,
            scope=scope,
            scope_id=scope_id,
        )
        if payload is not None:
            level_text, level_kb = payload
            await message.answer(level_text, reply_markup=level_kb)


async def send_planned_tasks_overview(message: Message, ctx: AccessContext) -> None:
    db, _, family_repo = get_repositories()
    repo = PlannedTaskRepository(db)
    text = await _planned_tasks_overview_text(repo, family_repo, ctx.family_id)
    await message.answer(text)


async def _planned_tasks_overview_text(repo: PlannedTaskRepository, family_repo, family_id: int) -> str:
    tasks = await repo.list_tasks(family_id)
    groups = await family_repo.list_groups(family_id)
    if not tasks and not groups:
        return "Список плановых задач пуст."

    tasks_without_group: list = []
    tasks_by_group: dict[int, list] = {}
    for task in tasks:
        group_id = task["group_id"]
        if group_id is None:
            tasks_without_group.append(task)
            continue
        tasks_by_group.setdefault(int(group_id), []).append(task)

    lines = ["Задачи без группы:"]
    if tasks_without_group:
        for task in tasks_without_group:
            lines.append(f"- {_task_caption(task)}")
    else:
        lines.append("- нет")
    for group in groups:
        group_id = int(group["id"])
        group_name = str(group["name"])
        lines.append(f'Группа "{group_name}":')
        group_tasks = tasks_by_group.get(group_id, [])
        if group_tasks:
            for task in group_tasks:
                lines.append(f"- {_task_caption(task)}")
        else:
            lines.append("- нет задач")
    return "\n".join(lines)


async def _planned_tasks_edit_root_keyboard(
    repo: PlannedTaskRepository,
    family_repo,
    family_id: int,
) -> InlineKeyboardMarkup:
    tasks = await repo.list_tasks(family_id)
    groups = await family_repo.list_groups(family_id)
    rows: list[list[InlineKeyboardButton]] = []
    for task in tasks:
        if task["group_id"] is None:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=_task_caption(task),
                        callback_data=f"editpt:{task['id']}:0",
                    )
                ]
            )
    for group in groups:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f'Группа "{group["name"]}"',
                    callback_data=f"grouptasks:{group['id']}",
                )
            ]
        )
    if not rows:
        rows = [[InlineKeyboardButton(text="Нет доступных задач", callback_data="noop")]]
    rows.append([InlineKeyboardButton(text="Назад", callback_data="pteditback")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_planned_tasks_edit_root(message: Message, ctx: AccessContext) -> None:
    db, _, family_repo = get_repositories()
    repo = PlannedTaskRepository(db)
    kb = await _planned_tasks_edit_root_keyboard(repo, family_repo, ctx.family_id)
    await message.answer("Плановые задачи для редактирования:", reply_markup=kb)


@router.message(F.text == "Текущие задачи")
async def current_tasks(message: Message) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        return
    if not ctx.is_admin:
        await message.answer("Раздел «Текущие задачи» доступен только администраторам семьи.")
        return
    runtime = TaskRuntimeRepository(db)
    rows = await runtime.list_active_instances(ctx.family_id)
    buttons = [{"id": str(row["id"]), "title": str(row["title"])} for row in rows]
    await message.answer(
        "Выберите выполненное действие:",
    )
    await message.answer(
        "Текущие задачи:",
        reply_markup=tasks_keyboard(buttons, "done"),
    )
    await message.answer(
        "Для возврата:",
        reply_markup=back_menu(),
    )


@router.message(F.text == "Добавить выполненную")
async def add_completed(message: Message) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        return
    runtime = TaskRuntimeRepository(db)
    kb = await _manual_done_root_keyboard(runtime, family_repo, ctx.family_id)
    await message.answer(
        "Выберите выполненную задачу.\n"
        "После выбора будут созданы зависимые обязательные задачи.",
    )
    await message.answer(
        "Плановые задачи:",
        reply_markup=kb,
    )


@router.message(F.text == "Добавить выполненную (за ...)")
async def add_completed_for_member(message: Message) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        return
    if not can_add_to_execution(ctx):
        await message.answer("Добавление задач за участника доступно только администраторам.")
        return
    members = await family_repo.list_members_for_edit(ctx.family_id)
    kb = _manual_done_for_member_picker_keyboard(members)
    await message.answer("Выберите участника семьи:", reply_markup=kb)


@router.message(F.text == "Добавить к выполнению")
async def add_to_execution(message: Message, state: FSMContext) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        return
    if not can_add_to_execution(ctx):
        await message.answer("Добавление задач к выполнению доступно только администраторам.")
        return
    runtime = TaskRuntimeRepository(db)
    kb = await _add_execution_root_keyboard(runtime, family_repo, ctx.family_id)
    await message.answer(
        "Выберите задачу к выполнению.\n"
        "После выбора можно добавить сейчас или ввести время чч:мм.",
    )
    await message.answer(
        "Плановые задачи:",
        reply_markup=kb,
    )
    await state.clear()


async def _manual_self_level_payload(
    runtime: TaskRuntimeRepository,
    family_repo,
    *,
    family_id: int,
    scope: str,
    scope_id: int,
) -> tuple[str, InlineKeyboardMarkup] | None:
    if scope == "group":
        group = await family_repo.get_group(family_id, scope_id)
        if group is None:
            return None
        tasks = await runtime.list_planned_tasks_by_group(family_id, scope_id)
        text = f'Группа "{group["name"]}": выберите выполненную задачу.'
        if not tasks:
            text = f'Группа "{group["name"]}": активных задач нет.'
        kb = _manual_done_group_keyboard(tasks, scope_id)
    else:
        kb = await _manual_done_root_keyboard(runtime, family_repo, family_id)
        text = "Плановые задачи:"
    return text, kb


async def _show_manual_self_level(
    callback: CallbackQuery,
    runtime: TaskRuntimeRepository,
    family_repo,
    *,
    family_id: int,
    scope: str,
    scope_id: int,
) -> None:
    payload = await _manual_self_level_payload(
        runtime,
        family_repo,
        family_id=family_id,
        scope=scope,
        scope_id=scope_id,
    )
    if payload is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    text, kb = payload
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)


async def _manual_member_level_payload(
    runtime: TaskRuntimeRepository,
    family_repo,
    *,
    family_id: int,
    target_user_id: int,
    member_name: str,
    scope: str,
    scope_id: int,
) -> tuple[str, InlineKeyboardMarkup] | None:
    if scope == "group":
        group = await family_repo.get_group(family_id, scope_id)
        if group is None:
            return None
        tasks = await runtime.list_planned_tasks_by_group(family_id, scope_id)
        kb = _manual_done_for_member_group_keyboard(tasks, target_user_id, scope_id)
        text = f'Исполнитель: {member_name}\nГруппа "{group["name"]}": выберите выполненную задачу.'
        if not tasks:
            text = f'Исполнитель: {member_name}\nГруппа "{group["name"]}": активных задач нет.'
    else:
        kb = await _manual_done_for_member_root_keyboard(runtime, family_repo, family_id, target_user_id)
        text = f"Исполнитель: {member_name}\nВыберите выполненную задачу."
    return text, kb


async def _show_manual_member_level(
    callback: CallbackQuery,
    runtime: TaskRuntimeRepository,
    family_repo,
    *,
    family_id: int,
    target_user_id: int,
    member_name: str,
    scope: str,
    scope_id: int,
) -> None:
    payload = await _manual_member_level_payload(
        runtime,
        family_repo,
        family_id=family_id,
        target_user_id=target_user_id,
        member_name=member_name,
        scope=scope,
        scope_id=scope_id,
    )
    if payload is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    text, kb = payload
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)


async def _add_execution_level_payload(
    runtime: TaskRuntimeRepository,
    family_repo,
    *,
    family_id: int,
    scope: str,
    scope_id: int,
) -> tuple[str, InlineKeyboardMarkup] | None:
    if scope == "group":
        group = await family_repo.get_group(family_id, scope_id)
        if group is None:
            return None
        tasks = await runtime.list_planned_tasks_by_group(family_id, scope_id)
        kb = _add_execution_group_keyboard(tasks, scope_id)
        text = f'Группа "{group["name"]}": выберите задачу к выполнению.'
        if not tasks:
            text = f'Группа "{group["name"]}": активных задач нет.'
    else:
        kb = await _add_execution_root_keyboard(runtime, family_repo, family_id)
        text = "Плановые задачи:"
    return text, kb


async def _show_add_execution_level(
    callback: CallbackQuery,
    runtime: TaskRuntimeRepository,
    family_repo,
    *,
    family_id: int,
    scope: str,
    scope_id: int,
) -> None:
    payload = await _add_execution_level_payload(
        runtime,
        family_repo,
        family_id=family_id,
        scope=scope,
        scope_id=scope_id,
    )
    if payload is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    text, kb = payload
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("manualgroup:"))
async def add_completed_group_callback(callback: CallbackQuery) -> None:
    token = callback.data.split(":", 1)[1]
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    runtime = TaskRuntimeRepository(db)
    if token == "cancel":
        try:
            await callback.message.edit_text("Операция отменена.")
        except TelegramBadRequest:
            await callback.message.answer("Операция отменена.")
        await callback.answer()
        return
    if token in {"back", "none"}:
        kb = await _manual_done_root_keyboard(runtime, family_repo, ctx.family_id)
        try:
            await callback.message.edit_text("Плановые задачи:", reply_markup=kb)
        except TelegramBadRequest:
            await callback.message.answer("Плановые задачи:", reply_markup=kb)
        await callback.answer()
        return

    group_id = int(token)
    group = await family_repo.get_group(ctx.family_id, group_id)
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    tasks = await runtime.list_planned_tasks_by_group(ctx.family_id, group_id)
    kb = _manual_done_group_keyboard(tasks, group_id)
    text = f'Группа "{group["name"]}": выберите выполненную задачу.'
    if not tasks:
        text = f'Группа "{group["name"]}": активных задач нет.'
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("manualforuser:"))
async def add_completed_for_member_pick_user(callback: CallbackQuery) -> None:
    token = callback.data.split(":", 1)[1]
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not can_add_to_execution(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    if token == "cancel":
        await callback.message.edit_text("Операция отменена.")
        await callback.answer()
        return
    target_user_id = int(token)
    members = await family_repo.list_members_for_edit(ctx.family_id)
    member_name = _member_display_name(members, target_user_id)
    if member_name is None:
        await callback.answer("Участник не найден.", show_alert=True)
        return
    runtime = TaskRuntimeRepository(db)
    kb = await _manual_done_for_member_root_keyboard(runtime, family_repo, ctx.family_id, target_user_id)
    text = f"Исполнитель: {member_name}\nВыберите выполненную задачу."
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("manualforgroup:"))
async def add_completed_for_member_group(callback: CallbackQuery) -> None:
    _, target_user_raw, group_token = callback.data.split(":")
    target_user_id = int(target_user_raw)
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not can_add_to_execution(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    members = await family_repo.list_members_for_edit(ctx.family_id)
    member_name = _member_display_name(members, target_user_id)
    if member_name is None:
        await callback.answer("Участник не найден.", show_alert=True)
        return
    runtime = TaskRuntimeRepository(db)
    if group_token == "cancel":
        try:
            await callback.message.edit_text("Операция отменена.")
        except TelegramBadRequest:
            await callback.message.answer("Операция отменена.")
        await callback.answer()
        return
    if group_token in {"back", "none"}:
        kb = await _manual_done_for_member_root_keyboard(runtime, family_repo, ctx.family_id, target_user_id)
        try:
            await callback.message.edit_text(
                f"Исполнитель: {member_name}\nВыберите выполненную задачу.",
                reply_markup=kb,
            )
        except TelegramBadRequest:
            await callback.message.answer(
                f"Исполнитель: {member_name}\nВыберите выполненную задачу.",
                reply_markup=kb,
            )
        await callback.answer()
        return
    group_id = int(group_token)
    group = await family_repo.get_group(ctx.family_id, group_id)
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    tasks = await runtime.list_planned_tasks_by_group(ctx.family_id, group_id)
    if not tasks:
        await callback.answer("В группе нет активных задач.", show_alert=True)
        return
    kb = _manual_done_for_member_group_keyboard(tasks, target_user_id, group_id)
    text = f'Исполнитель: {member_name}\nГруппа "{group["name"]}": выберите выполненную задачу.'
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("addexecgroup:"))
async def add_execution_group_callback(callback: CallbackQuery) -> None:
    token = callback.data.split(":", 1)[1]
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not can_add_to_execution(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    runtime = TaskRuntimeRepository(db)
    if token == "cancel":
        try:
            await callback.message.edit_text("Операция отменена.")
        except TelegramBadRequest:
            await callback.message.answer("Операция отменена.")
        await callback.answer()
        return
    if token in {"back", "none"}:
        kb = await _add_execution_root_keyboard(runtime, family_repo, ctx.family_id)
        try:
            await callback.message.edit_text("Плановые задачи:", reply_markup=kb)
        except TelegramBadRequest:
            await callback.message.answer("Плановые задачи:", reply_markup=kb)
        await callback.answer()
        return

    group_id = int(token)
    group = await family_repo.get_group(ctx.family_id, group_id)
    if group is None:
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    tasks = await runtime.list_planned_tasks_by_group(ctx.family_id, group_id)
    if not tasks:
        await callback.answer("В группе нет активных задач.", show_alert=True)
        return
    kb = _add_execution_group_keyboard(tasks, group_id)
    text = f'Группа "{group["name"]}": выберите задачу к выполнению.'
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.message(F.text == "Плановые задачи")
async def planned_tasks_menu_open(message: Message, state: FSMContext) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        return
    await state.set_state(NavStates.in_planned_tasks_menu)
    await message.answer(
        "Плановые задачи",
        reply_markup=planned_tasks_menu(is_admin=ctx.is_admin),
    )
    await send_planned_tasks_overview(message, ctx)


@router.message(
    NavStates.in_planned_tasks_menu,
    F.text.in_({"Править", "Добавить", "Добавить (по-умолчанию)"}),
)
async def planned_tasks_admin_actions(message: Message, state: FSMContext) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        return
    if not can_edit_planned_tasks(ctx):
        await message.answer("Эта команда доступна только администраторам.")
        return
    repo = PlannedTaskRepository(db)
    if message.text == "Править":
        await send_planned_tasks_edit_root(message, ctx)
        return
    if message.text == "Добавить":
        await state.set_state(PlannedTaskStates.waiting_title)
        await message.answer("Введите название плановой задачи:")
        return
    defaults = await repo.list_default_tasks()
    buttons = [{"id": str(item["id"]), "title": str(item["title"])} for item in defaults]
    await message.answer(
        "Выберите задачу по-умолчанию:",
        reply_markup=tasks_keyboard(buttons, "adddefault"),
    )


@router.message(NavStates.in_planned_tasks_menu, F.text == "Список")
async def list_planned_tasks(message: Message) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        return
    await send_planned_tasks_overview(message, ctx)


@router.message(PlannedTaskStates.waiting_title)
async def planned_task_title_entered(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if len(title) < 2:
        await message.answer("Название слишком короткое.")
        return
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    repo = PlannedTaskRepository(db)
    task_id = await repo.create_task(ctx.family_id, title, ctx.user_id)
    await state.update_data(task_id=task_id)
    await state.set_state(PlannedTaskStates.waiting_schedule)
    await message.answer("Введите расписание как список времени через запятую (например: 06:30,20:00) или '-' без расписания.")


@router.message(PlannedTaskStates.waiting_schedule)
async def planned_task_schedule_entered(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    task_id = int(data["task_id"])
    text = (message.text or "").strip()
    db, _, _ = get_repositories()
    repo = PlannedTaskRepository(db)
    if text != "-":
        for item in [x.strip() for x in text.split(",") if x.strip()]:
            if not is_valid_hhmm(item):
                await message.answer(f"Некорректное время: {item}")
                return
        for item in [x.strip() for x in text.split(",") if x.strip()]:
            for day in range(7):
                await repo.add_schedule(task_id, item, day)
    await state.clear()
    await message.answer("Плановая задача сохранена.")


async def _build_task_editor_payload(
    repo: PlannedTaskRepository, family_id: int, task_id: int, origin_group_id: int = 0
) -> tuple[str, InlineKeyboardMarkup, int] | None:
    task = await repo.get_task(family_id, task_id)
    if task is None:
        return None
    deps = await repo.list_dependencies(family_id, task_id)
    is_active = bool(task["is_active"])
    requires_comment = bool(task["requires_comment"])
    effort_stars = max(1, min(5, int(task["effort_stars"])))
    state_text = "Активна" if is_active else "Неактивна"
    comment_text = "Да" if requires_comment else "Нет"
    group_text = str(task["group_name"] or "Без группы")
    created_by_name = str(task["created_by_name"] or "").strip()
    created_by_text = created_by_name if created_by_name else f"ID {int(task['created_by'])}"
    lines = [
        f"Задача #{task_id}: {task['title']}",
        f"Кем добавлено: {created_by_text}",
        f"Позиция в списке: {task['sort_order']}",
        f"Статус: {state_text}",
        f"Комментарий: {comment_text}",
        f"Трудоемкость: {_stars_text(effort_stars)} ({effort_stars})",
        f"Группа: {group_text}",
        "Зависимости:",
    ]
    if deps:
        for dep in deps:
            req = "обязательная" if dep["is_required"] else "опциональная"
            lines.append(
                f"- {dep['child_title']} [{req}, {dep['delay_mode']}, {dep['default_delay_minutes']} мин]"
            )
    else:
        lines.append("- нет")

    dep_buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="Изменить имя", callback_data=f"editpttitle:{task_id}:{origin_group_id}")],
        [
            InlineKeyboardButton(
                text="Без комментария" if requires_comment else "С комментарием",
                callback_data=f"editptcomment:{task_id}:{0 if requires_comment else 1}:{origin_group_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text=("✓ " if effort_stars == stars else "") + ("★" * stars),
                callback_data=f"editpteffort:{task_id}:{stars}:{origin_group_id}",
            )
            for stars in range(1, 6)
        ],
        [InlineKeyboardButton(text="Группа", callback_data=f"editptgroup:{task_id}:{origin_group_id}")],
        [InlineKeyboardButton(text="Удалить", callback_data=f"editptdelask:{task_id}:{origin_group_id}")],
        [
            InlineKeyboardButton(
                text="Деактивировать" if is_active else "Активировать",
                callback_data=f"editptactive:{task_id}:{0 if is_active else 1}:{origin_group_id}",
            )
        ],
        [
            InlineKeyboardButton(text="Вверх", callback_data=f"editptmove:{task_id}:up:{origin_group_id}"),
            InlineKeyboardButton(text="Вниз", callback_data=f"editptmove:{task_id}:down:{origin_group_id}"),
        ],
        [InlineKeyboardButton(text="Добавить зависимость", callback_data=f"adddep:{task_id}")],
        [InlineKeyboardButton(text="Назад", callback_data=f"grouptasks:{origin_group_id}")],
    ]
    for dep in deps:
        dep_buttons.append(
            [
                InlineKeyboardButton(
                    text=f"Изменить: {dep['child_title']}",
                    callback_data=f"depedit:{task_id}:{dep['child_task_id']}",
                ),
                InlineKeyboardButton(
                    text=f"Удалить: {dep['child_title']}",
                    callback_data=f"depdel:{task_id}:{dep['child_task_id']}",
                ),
            ]
        )
    kb = InlineKeyboardMarkup(inline_keyboard=dep_buttons)
    return ("\n".join(lines), kb, int(task["sort_order"]))


async def _send_task_editor(
    message: Message,
    repo: PlannedTaskRepository,
    family_id: int,
    task_id: int,
    origin_group_id: int = 0,
) -> bool:
    payload = await _build_task_editor_payload(repo, family_id, task_id, origin_group_id)
    if payload is None:
        await message.answer("Задача не найдена.")
        return False
    text, kb, _ = payload
    await message.answer(text, reply_markup=kb)
    return True


async def _refresh_task_editor_message(
    message: Message,
    repo: PlannedTaskRepository,
    family_id: int,
    task_id: int,
    origin_group_id: int = 0,
    *,
    allow_send_fallback: bool = True,
) -> tuple[bool, int | None]:
    payload = await _build_task_editor_payload(repo, family_id, task_id, origin_group_id)
    if payload is None:
        await message.answer("Задача не найдена.")
        return (False, None)
    text, kb, sort_order = payload
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as exc:
        err = str(exc).lower()
        if "message is not modified" in err:
            pass
        elif (
            "message can't be edited" in err
            or "message to edit not found" in err
            or "there is no text in the message" in err
        ):
            if not allow_send_fallback:
                return (False, None)
            await message.answer(text, reply_markup=kb)
        else:
            raise
    return (True, sort_order)


@router.callback_query(F.data.startswith("editpt:"))
async def edit_task_entry(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    task_id = int(parts[1])
    origin_group_id = int(parts[2]) if len(parts) > 2 else 0
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if not can_edit_planned_tasks(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    shown = await _send_task_editor(callback.message, repo, ctx.family_id, task_id, origin_group_id)
    if not shown:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("editptmove:"))
async def edit_task_move(callback: CallbackQuery) -> None:
    _, task_id_raw, direction, *rest = callback.data.split(":")
    task_id = int(task_id_raw)
    origin_group_id = int(rest[0]) if rest else 0
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if not can_edit_planned_tasks(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    moved = await (repo.move_task_up(ctx.family_id, task_id) if direction == "up" else repo.move_task_down(ctx.family_id, task_id))
    if not moved:
        await callback.answer("Перемещение недоступно", show_alert=True)
        return
    updated, sort_order = await _refresh_task_editor_message(
        callback.message,
        repo,
        ctx.family_id,
        task_id,
        origin_group_id,
    )
    if not updated:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    if sort_order is None:
        await callback.answer("Порядок обновлён")
        return
    await callback.answer(f"Порядок обновлён. Текущая позиция: {sort_order}")


@router.callback_query(F.data.startswith("editptactive:"))
async def edit_task_active_toggle(callback: CallbackQuery) -> None:
    _, task_id_raw, active_raw, *rest = callback.data.split(":")
    task_id = int(task_id_raw)
    target_active = active_raw == "1"
    origin_group_id = int(rest[0]) if rest else 0
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if not can_edit_planned_tasks(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    updated = await repo.set_task_active(ctx.family_id, task_id, target_active)
    if not updated:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    refreshed, _ = await _refresh_task_editor_message(
        callback.message,
        repo,
        ctx.family_id,
        task_id,
        origin_group_id,
    )
    if not refreshed:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    await callback.answer("Задача активирована" if target_active else "Задача деактивирована")


@router.callback_query(F.data.startswith("editptcomment:"))
async def edit_task_comment_toggle(callback: CallbackQuery) -> None:
    _, task_id_raw, requires_comment_raw, *rest = callback.data.split(":")
    task_id = int(task_id_raw)
    target_requires_comment = requires_comment_raw == "1"
    origin_group_id = int(rest[0]) if rest else 0
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if not can_edit_planned_tasks(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    updated = await repo.set_task_requires_comment(ctx.family_id, task_id, target_requires_comment)
    if not updated:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    refreshed, _ = await _refresh_task_editor_message(
        callback.message,
        repo,
        ctx.family_id,
        task_id,
        origin_group_id,
    )
    if not refreshed:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    await callback.answer("Режим комментария обновлен")


@router.callback_query(F.data.startswith("editpteffort:"))
async def edit_task_effort_stars(callback: CallbackQuery) -> None:
    _, task_id_raw, stars_raw, *rest = callback.data.split(":")
    task_id = int(task_id_raw)
    target_stars = max(1, min(5, int(stars_raw)))
    origin_group_id = int(rest[0]) if rest else 0
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if not can_edit_planned_tasks(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    updated = await repo.set_task_effort_stars(ctx.family_id, task_id, target_stars)
    if not updated:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    refreshed, _ = await _refresh_task_editor_message(
        callback.message,
        repo,
        ctx.family_id,
        task_id,
        origin_group_id,
    )
    if not refreshed:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    await callback.answer(f"Трудоемкость обновлена: {target_stars}★")


@router.callback_query(F.data.startswith("editpttitle:"))
async def edit_task_title_start(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    task_id = int(parts[1])
    origin_group_id = int(parts[2]) if len(parts) > 2 else 0
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if not can_edit_planned_tasks(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    task = await repo.get_task(ctx.family_id, task_id)
    if task is None:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    await state.set_state(PlannedTaskStates.waiting_edit_title)
    await state.update_data(edit_task_id=task_id, edit_task_origin_group_id=origin_group_id)
    await callback.message.answer(f"Текущее имя: {task['title']}\nВведите новое название задачи:")
    await callback.answer()


@router.callback_query(F.data.startswith("editptgroup:"))
async def edit_task_group_start(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    task_id = int(parts[1])
    origin_group_id = int(parts[2]) if len(parts) > 2 else 0
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if not can_edit_planned_tasks(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    task = await repo.get_task(ctx.family_id, task_id)
    if task is None:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    groups = await family_repo.list_groups(ctx.family_id)
    rows: list[list[InlineKeyboardButton]] = []
    for group in groups:
        marker = "✓ " if task["group_id"] is not None and int(task["group_id"]) == int(group["id"]) else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{marker}{group['name']}",
                    callback_data=f"setptgroup:{task_id}:{group['id']}:{origin_group_id}",
                )
            ]
        )
    no_group_marker = "✓ " if task["group_id"] is None else ""
    rows.append(
        [InlineKeyboardButton(text=f"{no_group_marker}Без группы", callback_data=f"setptgroup:{task_id}:none:{origin_group_id}")]
    )
    rows.append([InlineKeyboardButton(text="Назад к задаче", callback_data=f"editpt:{task_id}:{origin_group_id}")])
    try:
        await callback.message.edit_text(
            "Выберите группу для задачи:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    except TelegramBadRequest as exc:
        await callback.answer(f"Не удалось открыть выбор группы: {exc}", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("setptgroup:"))
async def edit_task_group_set(callback: CallbackQuery) -> None:
    _, task_id_raw, group_token, *rest = callback.data.split(":")
    task_id = int(task_id_raw)
    group_id = None if group_token == "none" else int(group_token)
    origin_group_id = int(rest[0]) if rest else 0
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if not can_edit_planned_tasks(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    updated = await repo.set_task_group(ctx.family_id, task_id, group_id)
    if not updated:
        await callback.answer("Не удалось обновить группу задачи", show_alert=True)
        return
    refreshed, _ = await _refresh_task_editor_message(
        callback.message,
        repo,
        ctx.family_id,
        task_id,
        origin_group_id,
        allow_send_fallback=False,
    )
    if not refreshed:
        await callback.answer("Группа обновлена, но не удалось изменить текущее сообщение", show_alert=True)
        return
    await callback.answer("Группа задачи обновлена")


@router.callback_query(F.data.startswith("editptdelask:"))
async def edit_task_delete_ask(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    task_id = int(parts[1])
    origin_group_id = int(parts[2]) if len(parts) > 2 else 0
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if not can_edit_planned_tasks(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    task = await repo.get_task(ctx.family_id, task_id)
    if task is None:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    history_count = await repo.count_task_history_actions(ctx.family_id, task_id)
    if history_count > 0:
        await callback.message.answer(
            f"Задачу удалять запрещено, по ней заведено {history_count} действий в истории"
        )
        await callback.answer()
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Нет", callback_data=f"editptdelno:{task_id}:{origin_group_id}"),
                InlineKeyboardButton(text="Да", callback_data=f"editptdelyes:{task_id}:{origin_group_id}"),
            ]
        ]
    )
    await callback.message.answer(
        f"Вы действительно хоите удалить задачу {task['title']}",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("editptdelno:"))
async def edit_task_delete_no(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    task_id = int(parts[1])
    origin_group_id = int(parts[2]) if len(parts) > 2 else 0
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if not can_edit_planned_tasks(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    shown = await _send_task_editor(callback.message, repo, ctx.family_id, task_id, origin_group_id)
    if not shown:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    await callback.answer("Удаление отменено")


@router.callback_query(F.data.startswith("editptdelyes:"))
async def edit_task_delete_yes(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":")[1])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if not can_edit_planned_tasks(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    task = await repo.get_task(ctx.family_id, task_id)
    if task is None:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    deleted, history_count = await repo.delete_task_if_no_history(ctx.family_id, task_id)
    if not deleted and history_count > 0:
        await callback.message.answer(
            f"Задачу удалять запрещено, по ней заведено {history_count} действий в истории"
        )
        await callback.answer()
        return
    if not deleted:
        await callback.answer("Не удалось удалить задачу", show_alert=True)
        return
    await callback.message.answer(f"Задача {task['title']} удалена.")
    await send_planned_tasks_edit_root(callback.message, ctx)
    await callback.answer()


@router.message(PlannedTaskStates.waiting_edit_title)
async def planned_task_edit_title_entered(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if len(title) < 2:
        await message.answer("Название слишком короткое.")
        return
    data = await state.get_data()
    task_id = int(data.get("edit_task_id", 0))
    if task_id <= 0:
        await state.clear()
        await message.answer("Не удалось определить задачу для переименования.")
        return
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if not can_edit_planned_tasks(ctx):
        await state.clear()
        await message.answer("Эта команда доступна только администраторам.")
        return
    repo = PlannedTaskRepository(db)
    updated = await repo.update_task_title(ctx.family_id, task_id, title)
    await state.clear()
    if not updated:
        await message.answer("Задача не найдена или недоступна для редактирования.")
        return
    await message.answer("Название задачи обновлено.")


@router.callback_query(F.data.startswith("grouptasks:"))
async def planned_tasks_group_view(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[1])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not can_edit_planned_tasks(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    if group_id == 0:
        root_kb = await _planned_tasks_edit_root_keyboard(repo, family_repo, ctx.family_id)
        try:
            await callback.message.edit_text(
                "Плановые задачи для редактирования:",
                reply_markup=root_kb,
            )
        except TelegramBadRequest as exc:
            await callback.answer(f"Не удалось обновить список: {exc}", show_alert=True)
            return
        await callback.answer()
        return

    group = await family_repo.get_group(ctx.family_id, group_id)
    if group is None:
        await callback.answer("Группа не найдена", show_alert=True)
        return
    tasks = await repo.list_tasks_by_group(ctx.family_id, group_id)
    task_rows = [
        [InlineKeyboardButton(text=_task_caption(task), callback_data=f"editpt:{task['id']}:{group_id}")]
        for task in tasks
    ]
    task_rows.append([InlineKeyboardButton(text="Назад", callback_data="grouptasks:0")])
    markup = InlineKeyboardMarkup(inline_keyboard=task_rows)
    if not tasks:
        text = f"В группе «{group['name']}» пока нет задач."
    else:
        text = f"Группа «{group['name']}» — выберите задачу для редактирования:"
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        await callback.answer(f"Не удалось открыть группу: {exc}", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data == "pteditback")
async def planned_tasks_edit_back_to_overview(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not can_edit_planned_tasks(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    text = await _planned_tasks_overview_text(repo, family_repo, ctx.family_id)
    try:
        await callback.message.edit_text(text, reply_markup=None)
    except TelegramBadRequest:
        await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.startswith("adddep:"))
async def add_dependency_choose_child(callback: CallbackQuery) -> None:
    parent_id = int(callback.data.split(":")[1])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    repo = PlannedTaskRepository(db)
    tasks = await repo.list_tasks(ctx.family_id)
    buttons = [
        [InlineKeyboardButton(text=str(task["title"]), callback_data=f"depchild:{parent_id}:{task['id']}")]
        for task in tasks
        if int(task["id"]) != parent_id
    ]
    await callback.message.answer("Выберите дочернюю задачу:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("depchild:"))
async def add_dependency_choose_required(callback: CallbackQuery) -> None:
    _, parent_id, child_id = callback.data.split(":")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Обязательная", callback_data=f"depreq:{parent_id}:{child_id}:1")],
            [InlineKeyboardButton(text="Опциональная", callback_data=f"depreq:{parent_id}:{child_id}:0")],
        ]
    )
    await callback.message.answer("Тип зависимости:", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("depreq:"))
async def add_dependency_choose_mode(callback: CallbackQuery) -> None:
    _, parent_id, child_id, required = callback.data.split(":")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Без отсрочки", callback_data=f"depmode:{parent_id}:{child_id}:{required}:none")],
            [InlineKeyboardButton(text="Фиксированная", callback_data=f"depmode:{parent_id}:{child_id}:{required}:fixed")],
            [InlineKeyboardButton(text="Настраиваемая", callback_data=f"depmode:{parent_id}:{child_id}:{required}:configurable")],
        ]
    )
    await callback.message.answer("Выберите тип отсрочки:", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("depmode:"))
async def add_dependency_finalize_or_ask(callback: CallbackQuery, state: FSMContext) -> None:
    _, parent_id, child_id, required, mode = callback.data.split(":")
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    repo = PlannedTaskRepository(db)
    if mode == "none":
        ok = await repo.add_dependency(ctx.family_id, int(parent_id), int(child_id), required == "1", mode, 0)
        await callback.answer("Сохранено" if ok else "Ошибка: цикл или неверная связь", show_alert=not ok)
        return
    await state.set_state(PlannedTaskStates.waiting_dependency_delay)
    await state.update_data(parent_id=int(parent_id), child_id=int(child_id), required=required == "1", mode=mode)
    await callback.message.answer("Введите отсрочку в минутах (целое число, например 15):")
    await callback.answer()


@router.callback_query(F.data.startswith("depdel:"))
async def delete_dependency_callback(callback: CallbackQuery) -> None:
    _, parent_id, child_id = callback.data.split(":")
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if not can_edit_planned_tasks(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    await repo.delete_dependency(ctx.family_id, int(parent_id), int(child_id))
    await callback.answer("Зависимость удалена")


@router.callback_query(F.data.startswith("depedit:"))
async def edit_dependency_callback(callback: CallbackQuery, state: FSMContext) -> None:
    _, parent_id, child_id = callback.data.split(":")
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if not can_edit_planned_tasks(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    dep = await repo.get_dependency(ctx.family_id, int(parent_id), int(child_id))
    if dep is None:
        await callback.answer("Зависимость не найдена", show_alert=True)
        return
    await state.set_state(PlannedTaskStates.waiting_dependency_delay)
    await state.update_data(
        parent_id=int(parent_id),
        child_id=int(child_id),
        required=bool(dep["is_required"]),
        mode=str(dep["delay_mode"]),
    )
    await callback.message.answer(
        f"Текущая отсрочка: {dep['default_delay_minutes']} мин. Введите новое значение минут:"
    )
    await callback.answer()


@router.message(PlannedTaskStates.waiting_dependency_delay)
async def add_dependency_wait_delay(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Введите число минут.")
        return
    delay = int(text)
    data = await state.get_data()
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    repo = PlannedTaskRepository(db)
    ok = await repo.add_dependency(
        ctx.family_id,
        int(data["parent_id"]),
        int(data["child_id"]),
        bool(data["required"]),
        str(data["mode"]),
        delay,
    )
    await state.clear()
    await message.answer("Зависимость сохранена." if ok else "Не удалось сохранить связь (проверьте цикл зависимостей).")


@router.callback_query(F.data.startswith("adddefault:"))
async def add_default_task(callback: CallbackQuery) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    repo = PlannedTaskRepository(db)
    default_id = int(callback.data.split(":")[1])
    created = await repo.create_from_default(ctx.family_id, default_id, ctx.user_id)
    if created is None:
        await callback.answer("Не удалось создать", show_alert=True)
    else:
        await callback.answer("Задача добавлена")


@router.callback_query(F.data.startswith("addexec:"))
async def add_execution_callback(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    scope = "root"
    scope_id = 0
    if len(parts) == 2:
        task_id = int(parts[1])
    elif len(parts) >= 4:
        scope = parts[1]
        if scope == "group":
            scope_id = int(parts[2])
            task_id = int(parts[3])
        else:
            task_id = int(parts[-1])
    else:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    await _clear_callback_inline_keyboard(callback)
    await state.set_state(RuntimeTaskStates.waiting_execution_time)
    await state.update_data(exec_task_id=task_id, exec_scope=scope, exec_scope_id=scope_id)
    await callback.message.answer("Введите время чч:мм или 'сейчас'.")
    await callback.answer()


@router.message(RuntimeTaskStates.waiting_execution_time)
async def execution_time_entered(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().lower()
    data = await state.get_data()
    task_id = int(data["exec_task_id"])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    repo = PlannedTaskRepository(db)
    task = await repo.get_task(ctx.family_id, task_id)
    if task is None:
        await state.clear()
        await message.answer("Задача не найдена.")
        return
    activation_text = "сейчас"
    activated_at_iso: str | None = None
    if text == "сейчас":
        pass
    else:
        if not is_valid_hhmm(text):
            await message.answer("Введите 'сейчас' или корректное время чч:мм.")
            return
        hh, mm = map(int, text.split(":"))
        tz_name = ctx.family_timezone or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        local_now = datetime.now(timezone.utc).astimezone(tz)
        local_dt = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        utc_dt = local_dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
        activated_at_iso = utc_dt.isoformat()
        activation_text = text
    await state.set_state(RuntimeTaskStates.waiting_execution_confirm)
    await state.update_data(exec_task_id=task_id, exec_activation_iso=activated_at_iso)
    confirm_text = (
        f"Вы действительно, хотите добавить выполненую вашу задачу {task['title']}"
        f" ({activation_text})?"
    )
    await message.answer(confirm_text, reply_markup=_add_execution_confirm_keyboard())


@router.callback_query(F.data.startswith("addexecconfirm:"))
async def add_execution_confirm_callback(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":", 1)[1]
    data = await state.get_data()
    task_id = int(data.get("exec_task_id", 0))
    scope = str(data.get("exec_scope", "root"))
    scope_id = int(data.get("exec_scope_id", 0))
    if task_id <= 0:
        await state.clear()
        await callback.answer("Сессия подтверждения устарела.", show_alert=True)
        return
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await state.clear()
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not can_add_to_execution(ctx):
        await state.clear()
        await callback.answer("Нет прав", show_alert=True)
        return
    runtime = TaskRuntimeRepository(db)
    notify_repo = NotificationRepository(db)
    repo = PlannedTaskRepository(db)
    task = await repo.get_task(ctx.family_id, task_id)
    if task is None:
        await state.clear()
        await callback.answer("Задача не найдена.", show_alert=True)
        return
    if action == "cancel":
        await state.clear()
        try:
            await callback.message.edit_text("Операция отменена.")
        except TelegramBadRequest:
            await callback.message.answer("Операция отменена.")
        await callback.answer()
        return
    if action == "back":
        await state.set_state(RuntimeTaskStates.waiting_execution_time)
        await _show_add_execution_level(
            callback,
            runtime,
            family_repo,
            family_id=ctx.family_id,
            scope=scope,
            scope_id=scope_id,
        )
        await callback.answer()
        return
    activated_at_iso = data.get("exec_activation_iso")
    activated_at: datetime | None = None
    if isinstance(activated_at_iso, str) and activated_at_iso:
        activated_at = datetime.fromisoformat(activated_at_iso)
    created = await runtime.create_instance(ctx.family_id, task_id, ctx.user_id, "manual", activated_at)
    if created is None:
        await state.clear()
        await callback.message.edit_text("Такая задача уже есть в активных/запланированных.")
        await callback.answer()
        return
    await notify_family(callback.message.bot, notify_repo, ctx.family_id, "Новая задача добавлена к выполнению.")
    success_text = f"Задача {task['title']} добавлена"
    if action == "addmore":
        await state.set_state(RuntimeTaskStates.waiting_execution_time)
        await callback.message.edit_text(success_text)
        payload = await _add_execution_level_payload(
            runtime,
            family_repo,
            family_id=ctx.family_id,
            scope=scope,
            scope_id=scope_id,
        )
        if payload is not None:
            level_text, level_kb = payload
            await callback.message.answer(level_text, reply_markup=level_kb)
    else:
        await state.clear()
        await callback.message.edit_text(success_text)
    await callback.answer()


@router.callback_query(F.data.startswith("done:"))
async def complete_current_task(callback: CallbackQuery) -> None:
    instance_id = int(callback.data.split(":")[1])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    runtime = TaskRuntimeRepository(db)
    notify_repo = NotificationRepository(db)
    result = await runtime.complete_instance(instance_id, ctx.user_id, "current")
    if result is None:
        await callback.answer("Задача уже закрыта или не найдена.", show_alert=True)
        return
    await _process_dependencies(
        callback.message.bot,
        runtime,
        notify_repo,
        int(result["family_id"]),
        int(result["planned_task_id"]),
        ctx.user_id,
        callback.from_user.id,
    )
    await callback.answer("Выполнено")


@router.callback_query(F.data.startswith("manualdone:"))
async def complete_manual_task(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    scope = "root"
    scope_id = 0
    if len(parts) == 3 and parts[1] == "root":
        planned_task_id = int(parts[2])
    elif len(parts) == 2:
        planned_task_id = int(parts[1])
    elif len(parts) >= 4:
        scope = parts[1]
        if scope == "group":
            scope_id = int(parts[2])
            planned_task_id = int(parts[3])
        else:
            planned_task_id = int(parts[-1])
    else:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    repo = PlannedTaskRepository(db)
    task = await repo.get_task(ctx.family_id, planned_task_id)
    if task is None:
        await callback.answer("Задача не найдена.", show_alert=True)
        return
    if bool(task["requires_comment"]):
        await state.update_data(
            manual_comment_leads_to_final_menu=True,
            manual_comment_task_id=planned_task_id,
            manual_comment_completed_by_user_id=ctx.user_id,
            manual_comment_actor_user_id=ctx.user_id,
            manual_comment_task_title=str(task["title"]),
            manual_comment_add_more=False,
            manual_comment_scope=scope,
            manual_comment_scope_id=scope_id,
            manual_comment_target_user_id=0,
            manual_comment_target_member_name="",
            manual_comment_final_msg_id=callback.message.message_id,
        )
        await state.set_state(RuntimeTaskStates.waiting_manual_comment)
        await callback.message.answer(
            f"Задача «{task['title']}» требует комментарий.\nВведите комментарий (или отправьте «Отмена»):"
        )
        await callback.answer()
        return
    await _manual_fin_seed_state(
        state,
        planned_task_id=planned_task_id,
        completed_by_user_id=ctx.user_id,
        actor_user_id=ctx.user_id,
        scope=scope,
        scope_id=scope_id,
        add_more=False,
        for_member=False,
        target_member_name="",
        task_requires_comment=False,
        initial_comment="",
        chat_id=callback.message.chat.id,
    )
    await state.update_data(m_fin_final_msg_id=callback.message.message_id)
    await _manual_fin_answer_final(callback.message, state, ctx, family_repo, task)
    await callback.answer()


@router.callback_query(F.data.startswith("manualfordone:"))
async def complete_manual_task_for_member(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    target_user_id = int(parts[1])
    scope = "root"
    scope_id = 0
    if len(parts) == 4 and parts[2] == "root":
        planned_task_id = int(parts[3])
    elif len(parts) == 3:
        planned_task_id = int(parts[2])
    elif len(parts) >= 5:
        scope = parts[2]
        if scope == "group":
            scope_id = int(parts[3])
            planned_task_id = int(parts[4])
        else:
            planned_task_id = int(parts[-1])
    else:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None:
        await callback.answer("Вы не состоите в семье.", show_alert=True)
        return
    if not can_add_to_execution(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    members = await family_repo.list_members_for_edit(ctx.family_id)
    member_name = _member_display_name(members, target_user_id)
    if member_name is None:
        await callback.answer("Участник не найден.", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    task = await repo.get_task(ctx.family_id, planned_task_id)
    if task is None:
        await callback.answer("Задача не найдена.", show_alert=True)
        return
    if bool(task["requires_comment"]):
        await state.update_data(
            manual_comment_leads_to_final_menu=True,
            manual_comment_task_id=planned_task_id,
            manual_comment_completed_by_user_id=target_user_id,
            manual_comment_actor_user_id=ctx.user_id,
            manual_comment_task_title=str(task["title"]),
            manual_comment_add_more=False,
            manual_comment_scope=scope,
            manual_comment_scope_id=scope_id,
            manual_comment_target_user_id=target_user_id,
            manual_comment_target_member_name=member_name,
            manual_comment_final_msg_id=callback.message.message_id,
        )
        await state.set_state(RuntimeTaskStates.waiting_manual_comment)
        await callback.message.answer(
            f"Исполнитель: {member_name}\n"
            f"Задача «{task['title']}» требует комментарий.\n"
            f"Введите комментарий (или отправьте «Отмена»):"
        )
        await callback.answer()
        return
    await _manual_fin_seed_state(
        state,
        planned_task_id=planned_task_id,
        completed_by_user_id=target_user_id,
        actor_user_id=ctx.user_id,
        scope=scope,
        scope_id=scope_id,
        add_more=False,
        for_member=True,
        target_member_name=member_name,
        task_requires_comment=False,
        initial_comment="",
        chat_id=callback.message.chat.id,
    )
    await state.update_data(m_fin_final_msg_id=callback.message.message_id)
    await _manual_fin_answer_final(callback.message, state, ctx, family_repo, task)
    await callback.answer()


@router.callback_query(F.data == "noop")
async def manual_inline_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("mcfin:"))
async def manual_completion_final_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer()
        return
    cur = await state.get_state()
    if cur is None or cur != RuntimeTaskStates.waiting_manual_completion_draft.state:
        await callback.answer()
        return
    action = callback.data.split(":", 1)[1]
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if await deny_if_no_family(callback.message, ctx):
        await state.clear()
        await callback.answer()
        return
    data = await state.get_data()
    chat_id = int(data.get("m_fin_chat_id") or callback.message.chat.id)
    runtime = TaskRuntimeRepository(db)
    notify_repo = NotificationRepository(db)
    repo = PlannedTaskRepository(db)
    planned_task_id = int(data.get("m_fin_planned_task_id", 0))
    task = await repo.get_task(ctx.family_id, planned_task_id) if planned_task_id > 0 else None
    if task is None:
        await state.clear()
        await callback.answer("Задача не найдена.", show_alert=True)
        return

    if action == "cancel":
        await _manual_fin_cleanup_submessages(callback.message.bot, data, chat_id)
        await state.clear()
        try:
            await callback.message.edit_text("Операция отменена.", reply_markup=None)
        except TelegramBadRequest:
            await callback.message.answer("Операция отменена.")
        await callback.answer()
        return

    if action == "back":
        await _manual_fin_cleanup_submessages(callback.message.bot, data, chat_id)
        scope = str(data.get("m_fin_scope", "root"))
        scope_id = int(data.get("m_fin_scope_id", 0))
        for_member = int(data.get("m_fin_for_member", 0)) == 1
        target_uid = int(data.get("m_fin_completed_by", 0))
        member_name = str(data.get("m_fin_target_name", "")).strip()
        await state.clear()
        if for_member:
            members = await family_repo.list_members_for_edit(ctx.family_id)
            if member_name == "" and target_uid > 0:
                member_name = _member_display_name(members, target_uid) or "Участник"
            await _show_manual_member_level(
                callback,
                runtime,
                family_repo,
                family_id=ctx.family_id,
                target_user_id=target_uid,
                member_name=member_name or "Участник",
                scope=scope,
                scope_id=scope_id,
            )
        else:
            await _show_manual_self_level(
                callback,
                runtime,
                family_repo,
                family_id=ctx.family_id,
                scope=scope,
                scope_id=scope_id,
            )
        await callback.answer()
        return

    if action == "exec":
        if not ctx.is_admin:
            await callback.answer("Нет прав.", show_alert=True)
            return
        members = await family_repo.list_members_for_edit(ctx.family_id)
        rows = [
            [InlineKeyboardButton(text=str(m["display_name"]), callback_data=f"mcexsel:{m['user_id']}")]
            for m in members
        ]
        rows.append([InlineKeyboardButton(text="Назад", callback_data="mcexsel:back")])
        prev_pick = data.get("m_fin_exec_pick_msg_id")
        await _manual_fin_try_delete_message(callback.message.bot, chat_id, prev_pick)
        sent = await callback.message.answer(
            "Выберите исполнителя:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await state.update_data(m_fin_exec_pick_msg_id=sent.message_id)
        await callback.answer()
        return

    if action == "dt":
        if not ctx.is_admin:
            await callback.answer("Нет прав.", show_alert=True)
            return
        tz = ctx.family_timezone or "UTC"
        baseline = str(data.get("m_fin_completed_at_utc") or "")
        await state.update_data(m_fin_dt_baseline_utc=baseline)
        prev_dt = data.get("m_fin_dt_ui_msg_id")
        await _manual_fin_try_delete_message(callback.message.bot, chat_id, prev_dt)
        preview = _manual_fin_local_display(baseline, tz)
        header = (
            f"Текущая дата/время (база): {_manual_fin_local_display(baseline, tz)}\n"
            f"Измените время кнопками ниже (новое значение на первой кнопке)."
        )
        sent = await callback.message.answer(
            header,
            reply_markup=_manual_completion_datetime_keyboard(preview),
        )
        await state.update_data(m_fin_dt_ui_msg_id=sent.message_id)
        await callback.answer()
        return

    if action == "comment":
        if int(data.get("m_fin_task_requires_comment", 0)) != 1:
            await callback.answer()
            return
        await state.update_data(manual_comment_redraft=True)
        await state.set_state(RuntimeTaskStates.waiting_manual_comment)
        await callback.message.answer("Введите новый комментарий (или «Отмена»):")
        await callback.answer()
        return

    if action in {"add", "addmore"}:
        req_c = int(data.get("m_fin_task_requires_comment", 0)) == 1
        comment = (data.get("m_fin_comment") or "").strip()
        if req_c and not comment:
            await callback.answer("Сначала введите комментарий.", show_alert=True)
            return
        completed_by = int(data.get("m_fin_completed_by", 0))
        actor_user_id = int(data.get("m_fin_actor_user_id", 0))
        completed_at_utc = str(data.get("m_fin_completed_at_utc") or "").strip() or None
        add_more = action == "addmore"
        await state.update_data(m_fin_add_more=add_more)
        await _finalize_manual_completion(
            callback.message.bot,
            runtime,
            notify_repo,
            family_id=ctx.family_id,
            planned_task_id=planned_task_id,
            completed_by_user_id=completed_by,
            actor_user_id=actor_user_id,
            actor_chat_id=callback.from_user.id,
            comment_text=comment if req_c else None,
            completed_at_utc=completed_at_utc,
        )
        await _manual_fin_success_finish(
            callback.message,
            state,
            runtime=runtime,
            family_repo=family_repo,
            ctx=ctx,
            task_title=str(task["title"]),
        )
        await callback.answer()
        return

    await callback.answer()


@router.callback_query(F.data.startswith("mcdt:"))
async def manual_completion_datetime_adjust(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer()
        return
    cur = await state.get_state()
    if cur is None or cur != RuntimeTaskStates.waiting_manual_completion_draft.state:
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) == 2 and parts[1] == "back":
        db, user_repo, family_repo = get_repositories()
        ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
        if await deny_if_no_family(callback.message, ctx):
            await state.clear()
            await callback.answer()
            return
        data = await state.get_data()
        chat_id = int(data.get("m_fin_chat_id") or callback.message.chat.id)
        await _manual_fin_try_delete_message(callback.message.bot, chat_id, data.get("m_fin_dt_ui_msg_id"))
        await state.update_data(m_fin_dt_ui_msg_id=None, m_fin_dt_baseline_utc=None)
        repo = PlannedTaskRepository(db)
        tid = int(data.get("m_fin_planned_task_id", 0))
        task = await repo.get_task(ctx.family_id, tid) if tid > 0 else None
        if task is not None:
            await _manual_fin_answer_final(callback.message, state, ctx, family_repo, task)
        await callback.answer()
        return
    if len(parts) != 3:
        await callback.answer()
        return
    _, sign, field = parts
    delta = 1 if sign == "+" else -1 if sign == "-" else 0
    if delta == 0 or field not in {"d", "M", "y", "h", "m"}:
        await callback.answer()
        return
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if await deny_if_no_family(callback.message, ctx):
        await state.clear()
        await callback.answer()
        return
    data = await state.get_data()
    cur_utc = str(data.get("m_fin_completed_at_utc") or "").strip()
    if not cur_utc:
        await callback.answer()
        return
    tz_name = ctx.family_timezone or "UTC"
    try:
        new_utc = _bump_manual_completion_local_datetime(cur_utc, tz_name, field, delta)
    except ValueError:
        await callback.answer("Некорректное время.", show_alert=True)
        return
    await state.update_data(m_fin_completed_at_utc=new_utc)
    preview = _manual_fin_local_display(new_utc, tz_name)
    baseline = str(data.get("m_fin_dt_baseline_utc") or cur_utc)
    header = (
        f"Текущая дата/время (база): {_manual_fin_local_display(baseline, tz_name)}\n"
        f"Измените время кнопками ниже (новое значение на первой кнопке)."
    )
    try:
        await callback.message.edit_text(
            header,
            reply_markup=_manual_completion_datetime_keyboard(preview),
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("mcexsel:"))
async def manual_completion_executor_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer()
        return
    cur = await state.get_state()
    if cur is None or cur != RuntimeTaskStates.waiting_manual_completion_draft.state:
        await callback.answer()
        return
    token = callback.data.split(":", 1)[1]
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if await deny_if_no_family(callback.message, ctx):
        await state.clear()
        await callback.answer()
        return
    data = await state.get_data()
    chat_id = int(data.get("m_fin_chat_id") or callback.message.chat.id)
    await _manual_fin_try_delete_message(callback.message.bot, chat_id, data.get("m_fin_exec_pick_msg_id"))
    await state.update_data(m_fin_exec_pick_msg_id=None)
    if token == "back":
        repo = PlannedTaskRepository(db)
        tid = int(data.get("m_fin_planned_task_id", 0))
        task = await repo.get_task(ctx.family_id, tid) if tid > 0 else None
        if task is not None:
            await _manual_fin_answer_final(callback.message, state, ctx, family_repo, task)
        await callback.answer()
        return
    if not token.isdigit():
        await callback.answer()
        return
    new_uid = int(token)
    await state.update_data(m_fin_completed_by=new_uid)
    repo = PlannedTaskRepository(db)
    tid = int(data.get("m_fin_planned_task_id", 0))
    task = await repo.get_task(ctx.family_id, tid) if tid > 0 else None
    if task is not None:
        await _manual_fin_answer_final(callback.message, state, ctx, family_repo, task)
    await callback.answer()


@router.message(RuntimeTaskStates.waiting_manual_comment)
async def manual_comment_entered(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.lower() in {"отмена", "назад"}:
        await state.clear()
        await message.answer("Добавление выполнения отменено.")
        return
    if not text:
        await message.answer("Комментарий не может быть пустым. Введите текст или «Отмена».")
        return
    data = await state.get_data()
    if data.get("manual_comment_redraft"):
        db, user_repo, family_repo = get_repositories()
        ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
        if await deny_if_no_family(message, ctx):
            await state.clear()
            return
        await state.update_data(manual_comment_redraft=False, m_fin_comment=text)
        await state.set_state(RuntimeTaskStates.waiting_manual_completion_draft)
        repo = PlannedTaskRepository(db)
        tid = int(data.get("m_fin_planned_task_id", 0))
        task = await repo.get_task(ctx.family_id, tid) if tid > 0 else None
        if task is None:
            await state.clear()
            await message.answer("Не удалось определить задачу.")
            return
        await _manual_fin_answer_final(message, state, ctx, family_repo, task)
        return

    if not data.get("manual_comment_leads_to_final_menu"):
        await state.clear()
        await message.answer("Сессия устарела. Начните добавление заново.")
        return

    planned_task_id = int(data.get("manual_comment_task_id", 0))
    completed_by_user_id = int(data.get("manual_comment_completed_by_user_id", 0))
    actor_user_id = int(data.get("manual_comment_actor_user_id", 0))
    add_more = bool(data.get("manual_comment_add_more", False))
    scope = str(data.get("manual_comment_scope", "root"))
    scope_id = int(data.get("manual_comment_scope_id", 0))
    target_user_id = int(data.get("manual_comment_target_user_id", 0))
    target_member_name = str(data.get("manual_comment_target_member_name", "")).strip()
    final_list_mid = data.get("manual_comment_final_msg_id")
    if planned_task_id <= 0 or completed_by_user_id <= 0 or actor_user_id <= 0:
        await state.clear()
        await message.answer("Не удалось определить задачу для комментария.")
        return
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        await state.clear()
        return
    repo = PlannedTaskRepository(db)
    task = await repo.get_task(ctx.family_id, planned_task_id)
    if task is None:
        await state.clear()
        await message.answer("Задача не найдена.")
        return
    await state.update_data(manual_comment_leads_to_final_menu=False)
    for_member = target_user_id > 0
    await _manual_fin_seed_state(
        state,
        planned_task_id=planned_task_id,
        completed_by_user_id=completed_by_user_id,
        actor_user_id=actor_user_id,
        scope=scope,
        scope_id=scope_id,
        add_more=add_more,
        for_member=for_member,
        target_member_name=target_member_name,
        task_requires_comment=True,
        initial_comment=text,
        chat_id=message.chat.id,
    )
    if final_list_mid is not None:
        await state.update_data(m_fin_final_msg_id=int(final_list_mid))
    await _manual_fin_answer_final(message, state, ctx, family_repo, task)


@router.message(F.text == "Отм. последнее выполнение")
async def undo_last_completion(message: Message) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        return
    runtime = TaskRuntimeRepository(db)
    completion = await runtime.get_last_undoable_completion(ctx.family_id, ctx.user_id)
    if completion is None:
        await message.answer("Нет действий для отмены.")
        return
    local_completed_at = _to_family_local_timestamp(
        str(completion["completed_at"]),
        ctx.family_timezone or "UTC",
    )
    task_title = str(completion["task_title"])
    await message.answer(
        f"Вы действительно хотите отменить выполнение {local_completed_at} {task_title}?",
        reply_markup=_undo_last_completion_confirm_keyboard(int(completion["completion_id"])),
    )


@router.callback_query(F.data.startswith("undolast:no:"))
async def undo_last_completion_no(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return
    completion_id = int(callback.data.split(":")[2])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if await deny_if_no_family(callback.message, ctx):
        return
    runtime = TaskRuntimeRepository(db)
    completion = await runtime.get_last_undoable_completion(ctx.family_id, ctx.user_id)
    if completion is None or int(completion["completion_id"]) != completion_id:
        await callback.answer("Нет действий для отмены.")
        return
    try:
        await callback.message.edit_text("Операция отменена.")
    except TelegramBadRequest:
        await callback.message.answer("Операция отменена.")
    await callback.answer()


@router.callback_query(F.data.startswith("undolast:yes:"))
async def undo_last_completion_yes(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return
    completion_id = int(callback.data.split(":")[2])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if await deny_if_no_family(callback.message, ctx):
        return
    runtime = TaskRuntimeRepository(db)
    completion = await runtime.get_last_undoable_completion(ctx.family_id, ctx.user_id)
    if completion is None or int(completion["completion_id"]) != completion_id:
        await callback.answer("Последнее выполнение уже изменилось. Повторите команду.", show_alert=True)
        return
    ok = await runtime.undo_last_completion(ctx.family_id, ctx.user_id)
    text = "Последнее выполнение отменено." if ok else "Нет действий для отмены."
    try:
        await callback.message.edit_text(text)
    except TelegramBadRequest:
        await callback.message.answer(text)
    await callback.answer()


async def _process_dependencies(
    bot,
    runtime: TaskRuntimeRepository,
    notify_repo: NotificationRepository,
    family_id: int,
    parent_task_id: int,
    user_id: int,
    actor_chat_id: int | None,
) -> None:
    deps = await runtime.get_dependencies(family_id, parent_task_id)
    for dep in deps:
        delay = 0
        if dep["delay_mode"] == "fixed":
            delay = int(dep["default_delay_minutes"])
        if dep["delay_mode"] == "configurable":
            if actor_chat_id is not None:
                if dep["is_required"]:
                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=f"Отсрочка ({dep['default_delay_minutes']} мин)",
                                    callback_data=f"cfgdep:{family_id}:{dep['child_task_id']}:required:default:{dep['default_delay_minutes']}",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="Отсрочка (настраиваемая)",
                                    callback_data=f"cfgdep:{family_id}:{dep['child_task_id']}:required:custom:0",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="Без отсрочки",
                                    callback_data=f"cfgdep:{family_id}:{dep['child_task_id']}:required:none:0",
                                )
                            ],
                        ]
                    )
                else:
                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=f"Добавить с отсрочкой ({dep['default_delay_minutes']} мин)",
                                    callback_data=f"cfgdep:{family_id}:{dep['child_task_id']}:optional:default:{dep['default_delay_minutes']}",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="Добавить с настраиваемой отсрочкой",
                                    callback_data=f"cfgdep:{family_id}:{dep['child_task_id']}:optional:custom:0",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="Добавить без отсрочки",
                                    callback_data=f"cfgdep:{family_id}:{dep['child_task_id']}:optional:none:0",
                                )
                            ],
                            [InlineKeyboardButton(text="Пропустить", callback_data="cfgdep:0:0:optional:skip:0")],
                        ]
                    )
                await bot.send_message(actor_chat_id, "Выберите вариант для настраиваемой отсрочки:", reply_markup=kb)
            continue
        if dep["is_required"]:
            created = await runtime.create_dependency_instance(
                family_id,
                int(dep["child_task_id"]),
                user_id,
                delay,
            )
            if created is not None and delay == 0:
                await notify_family(bot, notify_repo, family_id, "Новая обязательная зависимая задача добавлена.")
        elif actor_chat_id is not None:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text="Добавить",
                        callback_data=f"optdep:{dep['child_task_id']}:{delay}",
                    ),
                    InlineKeyboardButton(text="Пропустить", callback_data="optdep:skip:0"),
                ]]
            )
            await bot.send_message(actor_chat_id, "Есть опциональная зависимая задача. Добавить?", reply_markup=kb)


@router.callback_query(F.data.startswith("optdep:"))
async def optional_dependency_callback(callback: CallbackQuery) -> None:
    _, child_token, delay_token = callback.data.split(":")
    if child_token == "skip":
        await callback.answer("Пропущено")
        return
    child_task_id = int(child_token)
    delay = int(delay_token)
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    runtime = TaskRuntimeRepository(db)
    notify_repo = NotificationRepository(db)
    created = await runtime.create_dependency_instance(ctx.family_id, child_task_id, ctx.user_id, delay)
    if created is None:
        await callback.answer("Задача уже существует", show_alert=True)
        return
    await callback.answer("Добавлено")
    if delay == 0:
        await notify_family(callback.message.bot, notify_repo, ctx.family_id, "Добавлена опциональная зависимая задача.")


@router.callback_query(F.data.startswith("cfgdep:"))
async def configurable_dependency_callback(callback: CallbackQuery, state: FSMContext) -> None:
    _, family_id, child_task_id, required_token, action, default_delay = callback.data.split(":")
    if action == "skip":
        await callback.answer("Пропущено")
        return
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    runtime = TaskRuntimeRepository(db)
    notify_repo = NotificationRepository(db)
    if action == "custom":
        await state.set_state(RuntimeTaskStates.waiting_custom_delay)
        await state.update_data(
            cfg_family_id=int(family_id),
            cfg_child_task_id=int(child_task_id),
            cfg_required=(required_token == "required"),
        )
        await callback.message.answer("Введите отсрочку в минутах:")
        await callback.answer()
        return
    delay = 0 if action == "none" else int(default_delay)
    created = await runtime.create_dependency_instance(int(family_id), int(child_task_id), ctx.user_id, delay)
    if created is None:
        await callback.answer("Задача уже существует", show_alert=True)
        return
    await callback.answer("Создано")
    if delay == 0:
        await notify_family(callback.message.bot, notify_repo, ctx.family_id, "Добавлена зависимая задача.")


@router.message(RuntimeTaskStates.waiting_custom_delay)
async def configurable_dependency_custom_delay(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Введите число минут.")
        return
    delay = int(text)
    data = await state.get_data()
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    runtime = TaskRuntimeRepository(db)
    notify_repo = NotificationRepository(db)
    created = await runtime.create_dependency_instance(
        int(data["cfg_family_id"]),
        int(data["cfg_child_task_id"]),
        ctx.user_id,
        delay,
    )
    await state.clear()
    if created is None:
        await message.answer("Задача уже существует.")
        return
    if delay == 0:
        await notify_family(message.bot, notify_repo, ctx.family_id, "Добавлена зависимая задача.")
    await message.answer("Задача добавлена с настраиваемой отсрочкой.")
