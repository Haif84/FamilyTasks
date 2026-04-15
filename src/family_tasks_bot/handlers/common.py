from __future__ import annotations

from aiogram.types import Message

from family_tasks_bot.services.auth import AccessContext


def role_title(ctx: AccessContext) -> str:
    if ctx.role_type == "parent" and ctx.is_admin:
        return "Родитель-администратор"
    if ctx.role_type == "parent":
        return "Родитель"
    if ctx.role_type == "child" and ctx.is_admin:
        return "Ребенок-администратор"
    if ctx.role_type == "child":
        return "Ребенок"
    return "Не назначена"


async def deny_if_no_family(message: Message, ctx: AccessContext) -> bool:
    if ctx.family_id is not None:
        return False
    await message.answer(
        "Вы пока не добавлены в семью. Попросите администратора семьи добавить ваш @username."
    )
    return True
