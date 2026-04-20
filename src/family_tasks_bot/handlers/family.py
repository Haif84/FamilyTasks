from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from family_tasks_bot.deps import get_repositories
from family_tasks_bot.handlers.common import deny_if_no_family
from family_tasks_bot.keyboards.inline import member_actions_keyboard, members_edit_keyboard
from family_tasks_bot.keyboards.reply import family_menu
from family_tasks_bot.states import FamilyStates, NavStates
from family_tasks_bot.services.auth import can_edit_family
from family_tasks_bot.services.bootstrap import ensure_member_context
from family_tasks_bot.utils.validators import invite_row_username_for_tg_id, parse_invite_input

router = Router(name="family")


def _format_pending_invite(invite: dict) -> str:
    raw_username = str(invite["username"])
    if raw_username.startswith("tg:"):
        label = f"Telegram ID {raw_username.split(':', 1)[1]}"
    else:
        label = raw_username if raw_username.startswith("@") else f"@{raw_username}"
    role = "Родитель" if invite["role_type"] == "parent" else "Ребенок"
    admin = " (админ)" if bool(invite["is_admin"]) else ""
    return f"- {label}: {role}{admin}"


def _member_card_text(member: dict) -> str:
    telegram_id = member["tg_user_id"]
    return (
        f"Участник: {member['display_name']} (Id: {telegram_id})\n"
        f"Отображаемое имя: {member['display_name']}\n"
        "Выберите действие:"
    )


@router.message(F.text == "Состав семьи")
async def open_family_menu(message: Message, state: FSMContext) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        return
    await state.set_state(NavStates.in_family_menu)
    await message.answer("Состав семьи", reply_markup=family_menu(is_admin=ctx.is_admin))


@router.message(NavStates.in_family_menu, F.text == "Список")
async def show_family_list(message: Message) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        return
    members = await family_repo.get_family_members(ctx.family_id)
    lines = ["Состав семьи:"]
    for member in members:
        role = "Родитель" if member["role_type"] == "parent" else "Ребенок"
        admin = " (админ)" if member["is_admin"] else ""
        lines.append(f"- {member['display_name']}: {role}{admin}")
    await message.answer("\n".join(lines))


@router.message(NavStates.in_family_menu, F.text == "Править состав семьи")
async def family_edit_open(message: Message) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        return
    if not can_edit_family(ctx):
        await message.answer("Эта команда доступна только администраторам.")
        return
    members = await family_repo.list_members_for_edit(ctx.family_id)
    member_buttons = [
        {"id": str(member["id"]), "title": f"{member['display_name']}"}
        for member in members
    ]
    await message.answer("Состав семьи", reply_markup=members_edit_keyboard(member_buttons))
    pending = await family_repo.list_pending_invites(ctx.family_id)
    if pending:
        lines = ["Ожидают авторизацию в боте:"]
        lines.extend(_format_pending_invite(invite) for invite in pending)
        await message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("member:"))
async def family_member_callback(callback: CallbackQuery) -> None:
    db, user_repo, family_repo = get_repositories()
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
            _member_card_text(member),
            reply_markup=member_actions_keyboard(
                member_id=int(payload),
                is_parent=member["role_type"] == "parent",
                is_admin=bool(member["is_admin"]),
            ),
        )
        await callback.answer()
        return


@router.callback_query(F.data.startswith("memberact:"))
async def family_member_action_callback(callback: CallbackQuery, state: FSMContext) -> None:
    db, user_repo, family_repo = get_repositories()
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
    elif action == "display_name":
        await state.set_state(FamilyStates.waiting_member_display_name)
        await state.update_data(member_id=member_id)
        await callback.message.answer("Введите новое отображаемое имя (2..64 символа):")
        await callback.answer()
    elif action == "rename":
        await callback.answer("Переименование будет добавлено следующим шагом")
    else:
        await callback.answer()


@router.message(FamilyStates.waiting_member_display_name)
async def change_member_display_name(message: Message, state: FSMContext) -> None:
    new_name = (message.text or "").strip()
    if len(new_name) < 2 or len(new_name) > 64:
        await message.answer("Отображаемое имя должно быть длиной от 2 до 64 символов.")
        return
    data = await state.get_data()
    member_id = int(data.get("member_id", 0))
    if member_id <= 0:
        await state.clear()
        await message.answer("Не удалось определить участника для редактирования.")
        return
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None or not can_edit_family(ctx):
        await state.clear()
        await message.answer("Эта команда доступна только администраторам.")
        return
    updated = await family_repo.update_member_display_name(member_id, ctx.family_id, new_name)
    if not updated:
        await state.clear()
        await message.answer("Участник не найден.")
        return
    member = await family_repo.get_member(member_id, ctx.family_id)
    await state.clear()
    await message.answer("Отображаемое имя обновлено.")
    if member is not None:
        await message.answer(
            _member_card_text(member),
            reply_markup=member_actions_keyboard(
                member_id=member_id,
                is_parent=member["role_type"] == "parent",
                is_admin=bool(member["is_admin"]),
            ),
        )


@router.message(F.text == "Добавить родителя")
async def add_parent_start(message: Message, state: FSMContext) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None or not can_edit_family(ctx):
        await message.answer("Эта команда доступна только администраторам.")
        return
    await state.set_state(FamilyStates.waiting_parent_username)
    await message.answer(
        "Введите @username родителя или только числовой Telegram ID пользователя "
        "(узнать можно через @userinfobot и др.; без @, только цифры)."
    )


@router.message(F.text == "Добавить ребенка")
async def add_child_start(message: Message, state: FSMContext) -> None:
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if ctx.family_id is None or not can_edit_family(ctx):
        await message.answer("Эта команда доступна только администраторам.")
        return
    await state.set_state(FamilyStates.waiting_child_username)
    await message.answer(
        "Введите @username ребенка или только числовой Telegram ID пользователя "
        "(без @, только цифры)."
    )


@router.message(FamilyStates.waiting_parent_username)
async def add_parent_finish(message: Message, state: FSMContext) -> None:
    await _save_invite(message, state, "parent")


@router.message(FamilyStates.waiting_child_username)
async def add_child_finish(message: Message, state: FSMContext) -> None:
    await _save_invite(message, state, "child")


async def _save_invite(message: Message, state: FSMContext, role_type: str) -> None:
    parsed = parse_invite_input(message.text or "")
    if parsed is None:
        await message.answer(
            "Некорректный ввод. Укажите @username (например, @family_member) "
            "или только числовой Telegram ID (например, 123456789), без пробелов."
        )
        return
    db, user_repo, family_repo = get_repositories()
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)
    if await deny_if_no_family(message, ctx):
        await state.clear()
        return

    kind, value = parsed
    if kind == "tg_id":
        tg_target = int(value)
        if tg_target == message.from_user.id:
            await message.answer("Нельзя пригласить самого себя по ID.")
            return
        if await family_repo.family_has_member_tg_id(ctx.family_id, tg_target):
            await message.answer("Этот пользователь уже состоит в вашей семье.")
            return
        storage_key = invite_row_username_for_tg_id(tg_target)
        label = f"Telegram ID {tg_target}"
    else:
        uname = str(value)
        if message.from_user.username and uname == f"@{message.from_user.username.lower()}":
            await message.answer("Нельзя пригласить самого себя.")
            return
        storage_key = uname
        label = uname

    await family_repo.add_invite(ctx.family_id, storage_key, role_type, False, ctx.user_id)
    await state.clear()
    role_title = "родитель" if role_type == "parent" else "ребенок"
    await message.answer(
        f"Приглашение для {label} создано.\n"
        f"После /start пользователь будет добавлен как {role_title}.",
        reply_markup=family_menu(is_admin=ctx.is_admin),
    )
