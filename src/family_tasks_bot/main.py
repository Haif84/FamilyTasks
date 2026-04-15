from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums.parse_mode import ParseMode

from family_tasks_bot.config import settings
from family_tasks_bot.db.database import Database
from family_tasks_bot.db.migrations import run_migrations, seed_default_tasks
from family_tasks_bot.db.repositories import FamilyRepository, UserRepository
from family_tasks_bot.handlers import setup_routers
from family_tasks_bot.scheduler import scheduler_loop


async def main() -> None:
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    db = Database(settings.db_path)
    conn = await db.connect()
    await run_migrations(conn)
    await seed_default_tasks(conn)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    bot["db_conn"] = conn
    bot["user_repo_factory"] = UserRepository
    bot["family_repo_factory"] = FamilyRepository

    dp.include_router(setup_routers())
    asyncio.create_task(scheduler_loop(bot, conn))

    try:
        await dp.start_polling(bot)
    finally:
        await conn.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
