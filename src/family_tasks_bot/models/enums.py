from enum import StrEnum


class RoleType(StrEnum):
    PARENT = "parent"
    CHILD = "child"


class TaskStatus(StrEnum):
    SCHEDULED = "scheduled"
    PENDING = "pending"
    DONE = "done"
    CANCELLED = "cancelled"


class DelayMode(StrEnum):
    NONE = "none"
    FIXED = "fixed"
    CONFIGURABLE = "configurable"
