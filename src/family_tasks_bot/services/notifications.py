from __future__ import annotations

from aiogram import Bot

from family_tasks_bot.db.repositories import NotificationRepository


async def notify_family(bot: Bot, repo: NotificationRepository, family_id: int, text: str) -> None:
    recipients = await repo.family_recipients(family_id)
    for recipient in recipients:
        silent = await repo.is_quiet_now(family_id, int(recipient["user_id"]))
        try:
            await bot.send_message(
                int(recipient["tg_user_id"]),
                text,
                disable_notification=silent,
            )
        except Exception:
            # Soft-fail for MVP to avoid breaking business flow.
            continue
