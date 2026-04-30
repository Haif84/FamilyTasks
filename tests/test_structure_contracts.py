from family_tasks_bot.db.repository_modules import (
    FamilyRepository as FamilyRepositorySplit,
    NotificationRepository as NotificationRepositorySplit,
    PlannedTaskRepository as PlannedTaskRepositorySplit,
    TaskRuntimeRepository as TaskRuntimeRepositorySplit,
    UserRepository as UserRepositorySplit,
)
from family_tasks_bot.db.repositories import (
    FamilyRepository,
    NotificationRepository,
    PlannedTaskRepository,
    TaskRuntimeRepository,
    UserRepository,
)
from family_tasks_bot.handlers.family import router as family_router
from family_tasks_bot.handlers.misc_feature import router as misc_router
from family_tasks_bot.handlers.start import router as start_router
from family_tasks_bot.handlers.tasks_feature import router as tasks_router


def test_repository_split_exports_keep_compatibility() -> None:
    assert UserRepositorySplit is UserRepository
    assert FamilyRepositorySplit is FamilyRepository
    assert PlannedTaskRepositorySplit is PlannedTaskRepository
    assert TaskRuntimeRepositorySplit is TaskRuntimeRepository
    assert NotificationRepositorySplit is NotificationRepository


def test_feature_router_exports_keep_contract_names() -> None:
    assert start_router.name == "start"
    assert misc_router.name == "misc"
    assert family_router.name == "family"
    assert tasks_router.name == "tasks"
