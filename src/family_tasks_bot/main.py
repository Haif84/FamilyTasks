from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums.parse_mode import ParseMode
from aiohttp import web

from family_tasks_bot.config import settings
from family_tasks_bot.db.database import Database
from family_tasks_bot.db.migrations import run_migrations, seed_default_tasks
from family_tasks_bot.deps import install_deps, reset_deps
from family_tasks_bot.db.repository_modules import (
    FamilyRepository,
    NotificationRepository,
    PlannedTaskRepository,
    TaskRuntimeRepository,
    UserRepository,
)
from family_tasks_bot.handlers import setup_routers
from family_tasks_bot.scheduler import scheduler_loop
from family_tasks_bot.services.alice import handle_alice_webhook_payload


def _normalized_alice_path() -> str:
    raw = (settings.alice_webhook_path or "").strip()
    if not raw:
        return "/alice/webhook"
    if raw.startswith("/"):
        return raw
    return f"/{raw}"


def _build_alice_webhook_handler(conn, bot: Bot):
    async def _alice_webhook(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {
                    "response": {"text": "Некорректный формат запроса.", "end_session": False},
                    "version": "1.0",
                }
            )

        user_repo = UserRepository(conn)
        planned_repo = PlannedTaskRepository(conn)
        runtime_repo = TaskRuntimeRepository(conn)
        notify_repo = NotificationRepository(conn)
        response_body = await handle_alice_webhook_payload(
            payload,
            user_repo=user_repo,
            planned_repo=planned_repo,
            runtime_repo=runtime_repo,
            notify_repo=notify_repo,
            bot=bot,
        )
        return web.json_response(response_body)

    return _alice_webhook


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

    dp.include_router(setup_routers())
    asyncio.create_task(scheduler_loop(bot, conn))
    alice_runner: web.AppRunner | None = None
    if settings.alice_webhook_enabled:
        alice_app = web.Application()
        alice_app.router.add_post(_normalized_alice_path(), _build_alice_webhook_handler(conn, bot))
        alice_runner = web.AppRunner(alice_app)
        await alice_runner.setup()
        site = web.TCPSite(alice_runner, host=settings.alice_webhook_host, port=settings.alice_webhook_port)
        await site.start()
        logging.info(
            "Alice webhook started at http://%s:%s%s",
            settings.alice_webhook_host,
            settings.alice_webhook_port,
            _normalized_alice_path(),
        )

    token = install_deps(conn, UserRepository, FamilyRepository)
    try:
        await dp.start_polling(bot)
    finally:
        reset_deps(token)
        if alice_runner is not None:
            await alice_runner.cleanup()
        await conn.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
