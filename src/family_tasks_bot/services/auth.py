from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AccessContext:
    user_id: int
    family_id: int | None
    role_type: str | None
    is_admin: bool
    family_name: str | None
    family_timezone: str | None

    @property
    def is_parent(self) -> bool:
        return self.role_type == "parent"

    @property
    def is_child(self) -> bool:
        return self.role_type == "child"


def can_edit_family(ctx: AccessContext) -> bool:
    return ctx.is_admin


def can_edit_planned_tasks(ctx: AccessContext) -> bool:
    return ctx.is_admin


def can_add_to_execution(ctx: AccessContext) -> bool:
    return ctx.is_parent
