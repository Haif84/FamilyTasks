from aiogram.fsm.state import State, StatesGroup


class FamilyStates(StatesGroup):
    waiting_parent_username = State()
    waiting_child_username = State()
    waiting_member_display_name = State()
    waiting_family_timezone = State()


class PlannedTaskStates(StatesGroup):
    waiting_title = State()
    waiting_edit_title = State()
    waiting_schedule = State()
    waiting_default_schedule = State()
    waiting_dependency_delay = State()


class RuntimeTaskStates(StatesGroup):
    waiting_execution_time = State()
    waiting_execution_confirm = State()
    waiting_custom_delay = State()
    waiting_manual_comment = State()


class GroupStates(StatesGroup):
    waiting_group_name_create = State()
    waiting_group_name_rename = State()


class StatsStates(StatesGroup):
    waiting_history_executor = State()
    waiting_history_datetime = State()


class NavStates(StatesGroup):
    in_family_menu = State()
    in_planned_tasks_menu = State()
    in_stats_menu = State()
    in_groups_menu = State()
