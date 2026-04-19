from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def members_edit_keyboard(members: list[dict[str, str]]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=member["title"], callback_data=f"member:{member['id']}")]
        for member in members
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons or [[InlineKeyboardButton(text="Пусто", callback_data="noop")]])


def member_actions_keyboard(member_id: int, is_parent: bool, is_admin: bool) -> InlineKeyboardMarkup:
    role_button = InlineKeyboardButton(
        text="Сделать ребенком" if is_parent else "Сделать родителем",
        callback_data=f"memberact:{member_id}:toggle_role",
    )
    admin_button = InlineKeyboardButton(
        text="Сделать обычным" if is_admin else "Сделать админом",
        callback_data=f"memberact:{member_id}:toggle_admin",
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отображаемое имя", callback_data=f"memberact:{member_id}:display_name")],
            [InlineKeyboardButton(text="Переименовать", callback_data=f"memberact:{member_id}:rename")],
            [InlineKeyboardButton(text="Удалить", callback_data=f"memberact:{member_id}:delete")],
            [role_button],
            [admin_button],
        ]
    )


def tasks_keyboard(tasks: list[dict[str, str]], prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=task["title"], callback_data=f"{prefix}:{task['id']}")]
        for task in tasks
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons or [[InlineKeyboardButton(text="Нет задач", callback_data="noop")]])
