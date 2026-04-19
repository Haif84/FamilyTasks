from __future__ import annotations

from datetime import datetime, timezone
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


async def send_planned_tasks_overview(message: Message, ctx: AccessContext) -> None:
    db, _, _ = get_repositories()
    repo = PlannedTaskRepository(db)
    tasks = await repo.list_tasks(ctx.family_id)
    if not tasks:
        await message.answer("Список плановых задач пуст.")
        return
    if can_edit_planned_tasks(ctx):
        buttons = []
        for task in tasks:
            suffix = " (неактивна)" if not bool(task["is_active"]) else ""
            buttons.append({"id": str(task["id"]), "title": f"{task['sort_order']}. {task['title']}{suffix}"})
        await message.answer(
            "Плановые задачи — выберите задачу для редактирования:",
            reply_markup=tasks_keyboard(buttons, "editpt"),
        )
        return
    lines = ["Список плановых задач:"]
    for task in tasks:
        suffix = " (неактивна)" if not bool(task["is_active"]) else ""
        lines.append(f"- {task['sort_order']}. #{task['id']} {task['title']}{suffix}")
    await message.answer("\n".join(lines))


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
    tasks = await runtime.list_planned_tasks(ctx.family_id)
    buttons = [{"id": str(task["id"]), "title": str(task["title"])} for task in tasks]
    await message.answer(
        "Выберите выполненную задачу.\n"
        "После выбора будут созданы зависимые обязательные задачи.",
    )
    await message.answer(
        "Плановые задачи:",
        reply_markup=tasks_keyboard(buttons, "manualdone"),
    )
    await message.answer(
        "Для возврата:",
        reply_markup=back_menu(),
    )


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
    tasks = await runtime.list_planned_tasks(ctx.family_id)
    buttons = [{"id": str(task["id"]), "title": str(task["title"])} for task in tasks]
    await message.answer(
        "Выберите задачу к выполнению:",
        reply_markup=tasks_keyboard(buttons, "addexec"),
    )
    await message.answer(
        "После выбора можно добавить сейчас или ввести время чч:мм.",
        reply_markup=back_menu(),
    )
    await state.clear()


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
        await send_planned_tasks_overview(message, ctx)
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
    repo: PlannedTaskRepository, family_id: int, task_id: int
) -> tuple[str, InlineKeyboardMarkup, int] | None:
    task = await repo.get_task(family_id, task_id)
    if task is None:
        return None
    deps = await repo.list_dependencies(family_id, task_id)
    is_active = bool(task["is_active"])
    state_text = "Активна" if is_active else "Неактивна"
    lines = [f"Задача #{task_id}: {task['title']}", f"Позиция в списке: {task['sort_order']}", f"Статус: {state_text}", "Зависимости:"]
    if deps:
        for dep in deps:
            req = "обязательная" if dep["is_required"] else "опциональная"
            lines.append(
                f"- {dep['child_title']} [{req}, {dep['delay_mode']}, {dep['default_delay_minutes']} мин]"
            )
    else:
        lines.append("- нет")

    dep_buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="Изменить имя", callback_data=f"editpttitle:{task_id}")],
        [
            InlineKeyboardButton(
                text="Деактивировать" if is_active else "Активировать",
                callback_data=f"editptactive:{task_id}:{0 if is_active else 1}",
            )
        ],
        [
            InlineKeyboardButton(text="Вверх", callback_data=f"editptmove:{task_id}:up"),
            InlineKeyboardButton(text="Вниз", callback_data=f"editptmove:{task_id}:down"),
        ],
        [InlineKeyboardButton(text="Добавить зависимость", callback_data=f"adddep:{task_id}")],
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


async def _send_task_editor(message: Message, repo: PlannedTaskRepository, family_id: int, task_id: int) -> bool:
    payload = await _build_task_editor_payload(repo, family_id, task_id)
    if payload is None:
        await message.answer("Задача не найдена.")
        return False
    text, kb, _ = payload
    await message.answer(text, reply_markup=kb)
    return True


async def _refresh_task_editor_message(
    message: Message, repo: PlannedTaskRepository, family_id: int, task_id: int
) -> tuple[bool, int | None]:
    payload = await _build_task_editor_payload(repo, family_id, task_id)
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
        elif "message can't be edited" in err or "message to edit not found" in err or "there is no text in the message" in err:
            await message.answer(text, reply_markup=kb)
        else:
            raise
    return (True, sort_order)


@router.callback_query(F.data.startswith("editpt:"))
async def edit_task_entry(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":")[1])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if not can_edit_planned_tasks(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    repo = PlannedTaskRepository(db)
    shown = await _send_task_editor(callback.message, repo, ctx.family_id, task_id)
    if not shown:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("editptmove:"))
async def edit_task_move(callback: CallbackQuery) -> None:
    _, task_id_raw, direction = callback.data.split(":")
    task_id = int(task_id_raw)
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
    updated, sort_order = await _refresh_task_editor_message(callback.message, repo, ctx.family_id, task_id)
    if not updated:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    if sort_order is None:
        await callback.answer("Порядок обновлён")
        return
    await callback.answer(f"Порядок обновлён. Текущая позиция: {sort_order}")


@router.callback_query(F.data.startswith("editptactive:"))
async def edit_task_active_toggle(callback: CallbackQuery) -> None:
    _, task_id_raw, active_raw = callback.data.split(":")
    task_id = int(task_id_raw)
    target_active = active_raw == "1"
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
    refreshed, _ = await _refresh_task_editor_message(callback.message, repo, ctx.family_id, task_id)
    if not refreshed:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    await callback.answer("Задача активирована" if target_active else "Задача деактивирована")


@router.callback_query(F.data.startswith("editpttitle:"))
async def edit_task_title_start(callback: CallbackQuery, state: FSMContext) -> None:
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
    await state.set_state(PlannedTaskStates.waiting_edit_title)
    await state.update_data(edit_task_id=task_id)
    await callback.message.answer(f"Текущее имя: {task['title']}\nВведите новое название задачи:")
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
    task_id = int(callback.data.split(":")[1])
    await state.set_state(RuntimeTaskStates.waiting_execution_time)
    await state.update_data(exec_task_id=task_id)
    await callback.message.answer("Введите время чч:мм или 'сейчас'.")
    await callback.answer()


@router.message(RuntimeTaskStates.waiting_execution_time)
async def execution_time_entered(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().lower()
    data = await state.get_data()
    task_id = int(data["exec_task_id"])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    runtime = TaskRuntimeRepository(db)
    notify_repo = NotificationRepository(db)
    if text == "сейчас":
        created = await runtime.create_instance(ctx.family_id, task_id, ctx.user_id, "manual")
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
        utc_dt = local_dt.astimezone(timezone.utc)
        created = await runtime.create_instance(ctx.family_id, task_id, ctx.user_id, "manual", utc_dt)
    await state.clear()
    if created is None:
        await message.answer("Такая задача уже есть в активных/запланированных.")
        return
    await notify_family(message.bot, notify_repo, ctx.family_id, "Новая задача добавлена к выполнению.")
    await message.answer("Задача добавлена.")


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
async def complete_manual_task(callback: CallbackQuery) -> None:
    planned_task_id = int(callback.data.split(":")[1])
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    runtime = TaskRuntimeRepository(db)
    notify_repo = NotificationRepository(db)
    await runtime.add_manual_completion(ctx.family_id, planned_task_id, ctx.user_id)
    await _process_dependencies(
        callback.message.bot,
        runtime,
        notify_repo,
        ctx.family_id,
        planned_task_id,
        ctx.user_id,
        callback.from_user.id,
    )
    await callback.answer("Выполнение добавлено")


@router.message(F.text == "Отменить последнее выполнение")
async def undo_last_completion(message: Message) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        return
    runtime = TaskRuntimeRepository(db)
    ok = await runtime.undo_last_completion(ctx.family_id, ctx.user_id)
    await message.answer("Последнее выполнение отменено." if ok else "Нет действий для отмены.")


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
