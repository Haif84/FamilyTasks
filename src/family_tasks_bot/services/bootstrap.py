from __future__ import annotations

from aiogram.types import User as TgUser

from family_tasks_bot.db.repositories import FamilyRepository, UserRepository
from family_tasks_bot.services.auth import AccessContext
from family_tasks_bot.utils.validators import invite_row_username_for_tg_id


async def ensure_member_context(
    user_repo: UserRepository,
    family_repo: FamilyRepository,
    tg_user: TgUser,
) -> AccessContext:
    was_empty = await user_repo.is_first_user()
    display_name = tg_user.full_name or tg_user.username or str(tg_user.id)
    user_id = await user_repo.upsert_user(tg_user.id, tg_user.username, display_name)

    invite = await user_repo.find_pending_invite(invite_row_username_for_tg_id(tg_user.id))
    if invite is not None:
        await user_repo.accept_invite(int(invite["id"]), user_id)
    elif tg_user.username:
        invite = await user_repo.find_pending_invite(f"@{tg_user.username.lower()}")
        if invite is not None:
            await user_repo.accept_invite(int(invite["id"]), user_id)

    membership = await user_repo.get_user_family_membership(user_id)

    if membership is None and was_empty:
        await family_repo.create_initial_family(user_id)
        membership = await user_repo.get_user_family_membership(user_id)

    if membership is None:
        return AccessContext(
            user_id=user_id,
            family_id=None,
            role_type=None,
            is_admin=False,
            family_name=None,
            family_timezone=None,
        )

    return AccessContext(
        user_id=user_id,
        family_id=int(membership["family_id"]),
        role_type=str(membership["role_type"]),
        is_admin=bool(membership["is_admin"]),
        family_name=str(membership["family_name"]),
        family_timezone=str(membership["family_timezone"]),
    )
