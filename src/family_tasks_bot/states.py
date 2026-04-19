from aiogram.fsm.state import State, StatesGroup


class FamilyStates(StatesGroup):
    waiting_parent_username = State()
    waiting_child_username = State()


class PlannedTaskStates(StatesGroup):
    waiting_title = State()
    waiting_edit_title = State()
    waiting_schedule = State()
    waiting_default_schedule = State()
    waiting_dependency_delay = State()


class RuntimeTaskStates(StatesGroup):
    waiting_execution_time = State()
    waiting_custom_delay = State()


class NavStates(StatesGroup):
    in_family_menu = State()
    in_planned_tasks_menu = State()
