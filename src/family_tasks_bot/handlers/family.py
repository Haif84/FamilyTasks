from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from family_tasks_bot.handlers.common import deny_if_no_family
from family_tasks_bot.keyboards.inline import member_actions_keyboard, members_edit_keyboard
from family_tasks_bot.keyboards.reply import family_menu
from family_tasks_bot.states import FamilyStates, NavStates
from family_tasks_bot.services.auth import can_edit_family
from family_tasks_bot.services.bootstrap import ensure_member_context
from family_tasks_bot.utils.validators import is_valid_username

router = Router(name="family")


@router.message(F.text == "Состав семьи")
async def open_family_menu(message: Message, state: FSMContext) -> None:
    db = message.bot["db_conn"]
    user_repo = message.bot["user_repo_factory"](db)
    family_repo = message.bot["family_repo_factory"](db)
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        return
    await state.set_state(NavStates.in_family_menu)
    await message.answer("Состав семьи", reply_markup=family_menu(is_admin=ctx.is_admin))


@router.message(NavStates.in_family_menu, F.text == "Список")
async def show_family_list(message: Message) -> None:
    db = message.bot["db_conn"]
    user_repo = message.bot["user_repo_factory"](db)
    family_repo = message.bot["family_repo_factory"](db)
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        return
    members = await family_repo.get_family_members(ctx.family_id)
    lines = ["Состав семьи:"]
    for member in members:
        role = "Родитель" if member["role_type"] == "parent" else "Ребенок"
        admin = " (админ)" if member["is_admin"] else ""
        username = f" @{member['username']}" if member["username"] else ""
        lines.append(f"- {member['display_name']}{username}: {role}{admin}")
    await message.answer("\n".join(lines))


@router.message(F.text == "Править")
async def family_edit_open(message: Message) -> None:
    db = message.bot["db_conn"]
    user_repo = message.bot["user_repo_factory"](db)
    family_repo = message.bot["family_repo_factory"](db)
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        return
    if not can_edit_family(ctx):
        await message.answer("Эта команда доступна только администраторам.")
        return
    members = await family_repo.list_members_for_edit(ctx.family_id)
    member_buttons = [
        {"id": str(member["id"]), "title": f"{member['display_name']} (@{member['username'] or '-'})"}
        for member in members
    ]
    await message.answer("Состав семьи", reply_markup=members_edit_keyboard(member_buttons))


@router.callback_query(F.data.startswith("member:"))
async def family_member_callback(callback: CallbackQuery) -> None:
    db = callback.message.bot["db_conn"]
    user_repo = callback.message.bot["user_repo_factory"](db)
    family_repo = callback.message.bot["family_repo_factory"](db)
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None or not can_edit_family(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    payload = callback.data.split(":")[1]
    if payload.isdigit():
        member = await family_repo.get_member(int(payload), ctx.family_id)
        if member is None:
            await callback.answer("Участник не найден", show_alert=True)
            return
        await callback.message.answer(
            f"Участник: {member['display_name']}\nВыберите действие:",
            reply_markup=member_actions_keyboard(
                member_id=int(payload),
                is_parent=member["role_type"] == "parent",
                is_admin=bool(member["is_admin"]),
            ),
        )
        await callback.answer()
        return


@router.callback_query(F.data.startswith("memberact:"))
async def family_member_action_callback(callback: CallbackQuery) -> None:
    db = callback.message.bot["db_conn"]
    user_repo = callback.message.bot["user_repo_factory"](db)
    family_repo = callback.message.bot["family_repo_factory"](db)
    ctx = await ensure_member_context(user_repo, family_repo, callback.from_user)
    if ctx.family_id is None or not can_edit_family(ctx):
        await callback.answer("Нет прав", show_alert=True)
        return
    _, member_id_raw, action = callback.data.split(":")
    member_id = int(member_id_raw)
    if action == "toggle_role":
        await family_repo.toggle_member_role(member_id, ctx.family_id)
        await callback.answer("Роль изменена")
    elif action == "toggle_admin":
        ok = await family_repo.toggle_member_admin(member_id, ctx.family_id)
        await callback.answer("Изменено" if ok else "Нельзя убрать последнего админа", show_alert=not ok)
    elif action == "delete":
        ok = await family_repo.delete_member(member_id, ctx.family_id)
        await callback.answer("Удалено" if ok else "Нельзя удалить последнего админа", show_alert=not ok)
    elif action == "rename":
        await callback.answer("Переименование будет добавлено следующим шагом")
    else:
        await callback.answer()


@router.message(F.text == "Добавить родителя")
async def add_parent_start(message: Message, state: FSMContext) -> None:
    db = message.bot["db_conn"]
    user_repo = message.bot["user_repo_factory"](db)
    family_repo = message.bot["family_repo_factory"](db)
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None or not can_edit_family(ctx):
        await message.answer("Эта команда доступна только администраторам.")
        return
    await state.set_state(FamilyStates.waiting_parent_username)
    await message.answer("Введите @username родителя:")


@router.message(F.text == "Добавить ребенка")
async def add_child_start(message: Message, state: FSMContext) -> None:
    db = message.bot["db_conn"]
    user_repo = message.bot["user_repo_factory"](db)
    family_repo = message.bot["family_repo_factory"](db)
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None or not can_edit_family(ctx):
        await message.answer("Эта команда доступна только администраторам.")
        return
    await state.set_state(FamilyStates.waiting_child_username)
    await message.answer("Введите @username ребенка:")


@router.message(FamilyStates.waiting_parent_username)
async def add_parent_finish(message: Message, state: FSMContext) -> None:
    await _save_invite(message, state, "parent")


@router.message(FamilyStates.waiting_child_username)
async def add_child_finish(message: Message, state: FSMContext) -> None:
    await _save_invite(message, state, "child")


async def _save_invite(message: Message, state: FSMContext, role_type: str) -> None:
    username = (message.text or "").strip().lower()
    if not is_valid_username(username):
        await message.answer("Некорректный @username. Пример: @family_member")
        return
    db = message.bot["db_conn"]
    user_repo = message.bot["user_repo_factory"](db)
    family_repo = message.bot["family_repo_factory"](db)
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        await state.clear()
        return
    await family_repo.add_invite(ctx.family_id, username, role_type, False, ctx.user_id)
    await state.clear()
    role_title = "родитель" if role_type == "parent" else "ребенок"
    await message.answer(
        f"Приглашение для {username} создано.\n"
        f"После /start пользователь будет добавлен как {role_title}.",
        reply_markup=family_menu(is_admin=ctx.is_admin),
    )
