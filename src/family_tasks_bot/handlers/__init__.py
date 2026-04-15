from aiogram import Router

from family_tasks_bot.handlers.family import router as family_router
from family_tasks_bot.handlers.misc import router as misc_router
from family_tasks_bot.handlers.start import router as start_router
from family_tasks_bot.handlers.tasks import router as tasks_router
from family_tasks_bot.utils.bot_data import install_bot_data_mapping


def setup_routers() -> Router:
    install_bot_data_mapping()
    root = Router(name="root")
    root.include_router(start_router)
    root.include_router(misc_router)
    root.include_router(family_router)
    root.include_router(tasks_router)
    return root
