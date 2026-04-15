from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from family_tasks_bot.keyboards.reply import main_menu
from family_tasks_bot.services.bootstrap import ensure_member_context
from family_tasks_bot.handlers.common import role_title

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    db = message.bot["db_conn"]
    user_repo = message.bot["user_repo_factory"](db)
    family_repo = message.bot["family_repo_factory"](db)
    ctx = await ensure_member_context(user_repo, family_repo, message.from_user)

    if ctx.family_id is None:
        await message.answer(
            "Вы зарегистрированы в системе, но пока не состоите в семье.\n"
            "Когда администратор добавит вас, нажмите /start снова."
        )
        return

    await message.answer(
        f"Вы добавлены в семью \"{ctx.family_name}\".\n"
        f"Ваша роль: {role_title(ctx)}.\n"
        "Выберите действие в меню ниже.",
        reply_markup=main_menu(is_parent=ctx.is_parent),
    )
