from __future__ import annotations

import asyncio
import logging

import aiosqlite
from aiogram import Bot

from family_tasks_bot.db.repositories import NotificationRepository, TaskRuntimeRepository
from family_tasks_bot.services.notifications import notify_family

logger = logging.getLogger(__name__)


async def scheduler_loop(bot: Bot, conn: aiosqlite.Connection, poll_seconds: int = 60) -> None:
    runtime = TaskRuntimeRepository(conn)
    notifications = NotificationRepository(conn)
    while True:
        try:
            created = await runtime.scheduler_generate_for_now()
            for family_id, title, _ in created:
                await notify_family(bot, notifications, family_id, f"Новая плановая задача: {title}")

            activated = await runtime.activate_due_scheduled()
            for row in activated:
                await notify_family(bot, notifications, int(row["family_id"]), f"Активирована задача: {row['title']}")
        except Exception:  # pragma: no cover
            logger.exception("Scheduler iteration failed")
        await asyncio.sleep(poll_seconds)
