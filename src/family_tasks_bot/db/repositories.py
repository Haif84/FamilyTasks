from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiosqlite


class UserRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    async def upsert_user(self, tg_user_id: int, username: str | None, display_name: str) -> int:
        await self.conn.execute(
            """
            INSERT INTO users (tg_user_id, username, display_name)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_user_id) DO UPDATE SET
                username = excluded.username,
                display_name = excluded.display_name
            """,
            (tg_user_id, username, display_name),
        )
        await self.conn.commit()
        async with self.conn.execute(
            "SELECT id FROM users WHERE tg_user_id = ?",
            (tg_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["id"])

    async def get_user_family_membership(self, user_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            """
            SELECT fm.*, f.name AS family_name, f.timezone AS family_timezone
            FROM family_members fm
            JOIN families f ON f.id = fm.family_id
            WHERE fm.user_id = ? AND fm.is_active = 1
            LIMIT 1
            """,
            (user_id,),
        ) as cursor:
            return await cursor.fetchone()

    async def find_pending_invite(self, username: str) -> aiosqlite.Row | None:
        async with self.conn.execute(
            """
            SELECT fi.*
            FROM family_invites fi
            WHERE lower(fi.username) = lower(?)
              AND fi.accepted_at IS NULL
            ORDER BY fi.id ASC
            LIMIT 1
            """,
            (username,),
        ) as cursor:
            return await cursor.fetchone()

    async def accept_invite(self, invite_id: int, user_id: int) -> None:
        async with self.conn.execute(
            "SELECT family_id, role_type, is_admin FROM family_invites WHERE id = ?",
            (invite_id,),
        ) as cursor:
            invite = await cursor.fetchone()
        if invite is None:
            return
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO family_members (id, family_id, user_id, role_type, is_admin, is_active, joined_at)
            VALUES (
                (SELECT id FROM family_members WHERE family_id = ? AND user_id = ?),
                ?, ?, ?, ?, 1, CURRENT_TIMESTAMP
            )
            """,
            (
                invite["family_id"],
                user_id,
                invite["family_id"],
                user_id,
                invite["role_type"],
                invite["is_admin"],
            ),
        )
        await self.conn.execute(
            "UPDATE family_invites SET accepted_at = CURRENT_TIMESTAMP WHERE id = ?",
            (invite_id,),
        )
        await self.conn.commit()

    async def is_first_user(self) -> bool:
        async with self.conn.execute("SELECT COUNT(*) AS cnt FROM users") as cursor:
            row = await cursor.fetchone()
        return int(row["cnt"]) == 0


class FamilyRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    async def create_initial_family(self, creator_user_id: int, family_name: str = "Моя семья") -> int:
        cur = await self.conn.execute(
            """
            INSERT INTO families (name, created_by_user_id)
            VALUES (?, ?)
            """,
            (family_name, creator_user_id),
        )
        family_id = cur.lastrowid
        await self.conn.execute(
            """
            INSERT INTO family_members (family_id, user_id, role_type, is_admin, is_active)
            VALUES (?, ?, 'parent', 1, 1)
            """,
            (family_id, creator_user_id),
        )
        await self.conn.commit()
        return int(family_id)

    async def get_family_members(self, family_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            """
            SELECT u.username, u.display_name, fm.role_type, fm.is_admin
            FROM family_members fm
            JOIN users u ON u.id = fm.user_id
            WHERE fm.family_id = ? AND fm.is_active = 1
            ORDER BY fm.is_admin DESC, fm.role_type DESC, u.display_name
            """,
            (family_id,),
        ) as cursor:
            return await cursor.fetchall()

    async def add_invite(
        self,
        family_id: int,
        username: str,
        role_type: str,
        is_admin: bool,
        created_by: int,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO family_invites (family_id, username, role_type, is_admin, created_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(family_id, username) DO UPDATE SET
                role_type = excluded.role_type,
                is_admin = excluded.is_admin,
                created_by = excluded.created_by,
                accepted_at = NULL,
                created_at = CURRENT_TIMESTAMP
            """,
            (family_id, username.lower(), role_type, int(is_admin), created_by),
        )
        await self.conn.commit()

    async def family_has_member_tg_id(self, family_id: int, tg_user_id: int) -> bool:
        async with self.conn.execute(
            """
            SELECT 1
            FROM family_members fm
            JOIN users u ON u.id = fm.user_id
            WHERE fm.family_id = ? AND fm.is_active = 1 AND u.tg_user_id = ?
            LIMIT 1
            """,
            (family_id, tg_user_id),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def list_members_for_edit(self, family_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            """
            SELECT fm.id, fm.user_id, fm.role_type, fm.is_admin, u.display_name, u.username
            FROM family_members fm
            JOIN users u ON u.id = fm.user_id
            WHERE fm.family_id = ? AND fm.is_active = 1
            ORDER BY fm.is_admin DESC, u.display_name
            """,
            (family_id,),
        ) as cursor:
            return await cursor.fetchall()

    async def list_pending_invites(self, family_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            """
            SELECT username, role_type, is_admin, created_at
            FROM family_invites
            WHERE family_id = ? AND accepted_at IS NULL
            ORDER BY created_at DESC
            """,
            (family_id,),
        ) as cursor:
            return await cursor.fetchall()

    async def get_member(self, member_id: int, family_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            """
            SELECT fm.*, u.display_name, u.username, u.tg_user_id
            FROM family_members fm
            JOIN users u ON u.id = fm.user_id
            WHERE fm.id = ? AND fm.family_id = ? AND fm.is_active = 1
            """,
            (member_id, family_id),
        ) as cursor:
            return await cursor.fetchone()

    async def update_member_display_name(self, member_id: int, family_id: int, display_name: str) -> bool:
        async with self.conn.execute(
            "SELECT user_id FROM family_members WHERE id = ? AND family_id = ? AND is_active = 1",
            (member_id, family_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return False
        await self.conn.execute(
            "UPDATE users SET display_name = ? WHERE id = ?",
            (display_name, int(row["user_id"])),
        )
        await self.conn.commit()
        return True

    async def admin_count(self, family_id: int) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM family_members WHERE family_id = ? AND is_active = 1 AND is_admin = 1",
            (family_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["cnt"])

    async def toggle_member_role(self, member_id: int, family_id: int) -> None:
        await self.conn.execute(
            """
            UPDATE family_members
            SET role_type = CASE role_type WHEN 'parent' THEN 'child' ELSE 'parent' END
            WHERE id = ? AND family_id = ?
            """,
            (member_id, family_id),
        )
        await self.conn.commit()

    async def toggle_member_admin(self, member_id: int, family_id: int) -> bool:
        member = await self.get_member(member_id, family_id)
        if member is None:
            return False
        if member["is_admin"] and await self.admin_count(family_id) <= 1:
            return False
        await self.conn.execute(
            "UPDATE family_members SET is_admin = CASE is_admin WHEN 1 THEN 0 ELSE 1 END WHERE id = ? AND family_id = ?",
            (member_id, family_id),
        )
        await self.conn.commit()
        return True

    async def delete_member(self, member_id: int, family_id: int) -> bool:
        member = await self.get_member(member_id, family_id)
        if member is None:
            return False
        if member["is_admin"] and await self.admin_count(family_id) <= 1:
            return False
        await self.conn.execute(
            "UPDATE family_members SET is_active = 0 WHERE id = ? AND family_id = ?",
            (member_id, family_id),
        )
        await self.conn.commit()
        return True


class PlannedTaskRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    async def list_tasks(self, family_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            """
            SELECT id, title, is_active, sort_order
            FROM planned_tasks
            WHERE family_id = ?
            ORDER BY sort_order, title, id
            """,
            (family_id,),
        ) as cursor:
            return await cursor.fetchall()

    async def get_task(self, family_id: int, task_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT id, title, sort_order, is_active FROM planned_tasks WHERE family_id = ? AND id = ?",
            (family_id, task_id),
        ) as cursor:
            return await cursor.fetchone()

    async def create_task(self, family_id: int, title: str, created_by: int) -> int:
        cur = await self.conn.execute(
            """
            INSERT INTO planned_tasks (family_id, title, sort_order, created_by)
            VALUES (
                ?, ?, COALESCE((SELECT MAX(sort_order) + 1 FROM planned_tasks WHERE family_id = ?), 1), ?
            )
            """,
            (family_id, title, family_id, created_by),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def update_task_title(self, family_id: int, task_id: int, title: str) -> bool:
        cur = await self.conn.execute(
            """
            UPDATE planned_tasks
            SET title = ?
            WHERE family_id = ? AND id = ? AND is_active = 1
            """,
            (title, family_id, task_id),
        )
        await self.conn.commit()
        return (cur.rowcount or 0) > 0

    async def set_task_active(self, family_id: int, task_id: int, is_active: bool) -> bool:
        cur = await self.conn.execute(
            """
            UPDATE planned_tasks
            SET is_active = ?
            WHERE family_id = ? AND id = ?
            """,
            (int(is_active), family_id, task_id),
        )
        await self.conn.commit()
        return (cur.rowcount or 0) > 0

    async def move_task_up(self, family_id: int, task_id: int) -> bool:
        return await self._swap_with_neighbor(family_id, task_id, direction="up")

    async def move_task_down(self, family_id: int, task_id: int) -> bool:
        return await self._swap_with_neighbor(family_id, task_id, direction="down")

    async def add_schedule(self, task_id: int, hhmm: str, day_of_week: int) -> None:
        await self.conn.execute(
            "INSERT INTO task_schedules (task_id, day_of_week, time_hhmm, is_active) VALUES (?, ?, ?, 1)",
            (task_id, day_of_week, hhmm),
        )
        await self.conn.commit()

    async def list_default_tasks(self) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT id, title, description FROM default_tasks WHERE is_active = 1 ORDER BY sort_order, title"
        ) as cursor:
            return await cursor.fetchall()

    async def create_from_default(self, family_id: int, default_task_id: int, created_by: int) -> int | None:
        async with self.conn.execute(
            "SELECT title FROM default_tasks WHERE id = ? AND is_active = 1",
            (default_task_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return await self.create_task(family_id, str(row["title"]), created_by)

    async def add_dependency(
        self,
        family_id: int,
        parent_task_id: int,
        child_task_id: int,
        is_required: bool,
        delay_mode: str,
        default_delay_minutes: int,
    ) -> bool:
        if parent_task_id == child_task_id:
            return False
        if await self._has_path(family_id, child_task_id, parent_task_id):
            return False
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO task_dependency_rules
            (id, family_id, parent_task_id, child_task_id, is_required, delay_mode, default_delay_minutes)
            VALUES (
                (SELECT id FROM task_dependency_rules WHERE parent_task_id = ? AND child_task_id = ?),
                ?, ?, ?, ?, ?, ?
            )
            """,
            (
                parent_task_id,
                child_task_id,
                family_id,
                parent_task_id,
                child_task_id,
                int(is_required),
                delay_mode,
                default_delay_minutes,
            ),
        )
        await self.conn.commit()
        return True

    async def list_dependencies(self, family_id: int, parent_task_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            """
            SELECT tdr.child_task_id, pt.title AS child_title, tdr.is_required, tdr.delay_mode, tdr.default_delay_minutes
            FROM task_dependency_rules tdr
            JOIN planned_tasks pt ON pt.id = tdr.child_task_id
            WHERE tdr.family_id = ? AND tdr.parent_task_id = ?
            ORDER BY pt.sort_order, pt.title, pt.id
            """,
            (family_id, parent_task_id),
        ) as cursor:
            return await cursor.fetchall()

    async def get_dependency(self, family_id: int, parent_task_id: int, child_task_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            """
            SELECT child_task_id, is_required, delay_mode, default_delay_minutes
            FROM task_dependency_rules
            WHERE family_id = ? AND parent_task_id = ? AND child_task_id = ?
            """,
            (family_id, parent_task_id, child_task_id),
        ) as cursor:
            return await cursor.fetchone()

    async def delete_dependency(self, family_id: int, parent_task_id: int, child_task_id: int) -> None:
        await self.conn.execute(
            """
            DELETE FROM task_dependency_rules
            WHERE family_id = ? AND parent_task_id = ? AND child_task_id = ?
            """,
            (family_id, parent_task_id, child_task_id),
        )
        await self.conn.commit()

    async def _has_path(self, family_id: int, source_task_id: int, target_task_id: int) -> bool:
        async with self.conn.execute(
            """
            SELECT parent_task_id, child_task_id
            FROM task_dependency_rules
            WHERE family_id = ?
            """,
            (family_id,),
        ) as cursor:
            edges = await cursor.fetchall()
        graph: dict[int, set[int]] = {}
        for edge in edges:
            parent = int(edge["parent_task_id"])
            child = int(edge["child_task_id"])
            graph.setdefault(parent, set()).add(child)
        stack = [source_task_id]
        visited: set[int] = set()
        while stack:
            node = stack.pop()
            if node == target_task_id:
                return True
            if node in visited:
                continue
            visited.add(node)
            stack.extend(graph.get(node, set()))
        return False

    async def _swap_with_neighbor(self, family_id: int, task_id: int, direction: str) -> bool:
        direction = direction.lower()
        if direction not in {"up", "down"}:
            raise ValueError(f"Unsupported direction: {direction}")

        comparator = "<" if direction == "up" else ">"
        sort_direction = "DESC" if direction == "up" else "ASC"
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            async with self.conn.execute(
                """
                SELECT id, sort_order
                FROM planned_tasks
                WHERE family_id = ? AND id = ? AND is_active = 1
                """,
                (family_id, task_id),
            ) as cursor:
                current = await cursor.fetchone()
            if current is None:
                await self.conn.rollback()
                return False

            async with self.conn.execute(
                f"""
                SELECT id, sort_order
                FROM planned_tasks
                WHERE family_id = ? AND is_active = 1 AND sort_order {comparator} ?
                ORDER BY sort_order {sort_direction}, id {sort_direction}
                LIMIT 1
                """,
                (family_id, int(current["sort_order"])),
            ) as cursor:
                neighbor = await cursor.fetchone()
            if neighbor is None:
                await self.conn.rollback()
                return False

            await self.conn.execute(
                "UPDATE planned_tasks SET sort_order = ? WHERE id = ? AND family_id = ?",
                (int(neighbor["sort_order"]), int(current["id"]), family_id),
            )
            await self.conn.execute(
                "UPDATE planned_tasks SET sort_order = ? WHERE id = ? AND family_id = ?",
                (int(current["sort_order"]), int(neighbor["id"]), family_id),
            )
            await self.conn.commit()
            return True
        except Exception:
            await self.conn.rollback()
            raise


class TaskRuntimeRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    async def list_active_instances(self, family_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            """
            SELECT ti.id, pt.title, ti.activated_at, ti.due_at
            FROM task_instances ti
            JOIN planned_tasks pt ON pt.id = ti.planned_task_id
            WHERE ti.family_id = ? AND ti.status = 'pending'
            ORDER BY COALESCE(ti.activated_at, ti.created_at)
            """,
            (family_id,),
        ) as cursor:
            return await cursor.fetchall()

    async def list_planned_tasks(self, family_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            """
            SELECT id, title
            FROM planned_tasks
            WHERE family_id = ? AND is_active = 1
            ORDER BY sort_order, title, id
            """,
            (family_id,),
        ) as cursor:
            return await cursor.fetchall()

    async def create_instance(
        self,
        family_id: int,
        planned_task_id: int,
        created_by: int | None,
        source_type: str,
        activated_at: datetime | None = None,
    ) -> int | None:
        # Dedup rule: one active/scheduled instance per planned task.
        async with self.conn.execute(
            """
            SELECT id FROM task_instances
            WHERE family_id = ? AND planned_task_id = ? AND status IN ('scheduled', 'pending')
            LIMIT 1
            """,
            (family_id, planned_task_id),
        ) as cursor:
            exists = await cursor.fetchone()
        if exists is not None:
            return None
        cur = await self.conn.execute(
            """
            INSERT INTO task_instances (family_id, planned_task_id, status, activated_at, created_by, source_type)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                family_id,
                planned_task_id,
                "pending" if activated_at is None else "scheduled",
                None if activated_at is None else activated_at.isoformat(),
                created_by,
                source_type,
            ),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def complete_instance(self, instance_id: int, user_id: int, mode: str) -> aiosqlite.Row | None:
        async with self.conn.execute(
            """
            SELECT id, family_id, planned_task_id FROM task_instances
            WHERE id = ? AND status = 'pending'
            """,
            (instance_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        await self.conn.execute(
            "UPDATE task_instances SET status = 'done' WHERE id = ?",
            (instance_id,),
        )
        cur = await self.conn.execute(
            """
            INSERT INTO task_completions (task_instance_id, family_id, planned_task_id, completed_by, completion_mode)
            VALUES (?, ?, ?, ?, ?)
            """,
            (instance_id, row["family_id"], row["planned_task_id"], user_id, mode),
        )
        completion_id = int(cur.lastrowid)
        await self.conn.execute(
            """
            INSERT INTO undo_log (family_id, user_id, action_type, action_ref_id, payload_json)
            VALUES (?, ?, 'completion', ?, ?)
            """,
            (row["family_id"], user_id, completion_id, json.dumps({"instance_id": instance_id})),
        )
        await self.conn.commit()
        return row

    async def add_manual_completion(self, family_id: int, planned_task_id: int, user_id: int) -> int:
        cur = await self.conn.execute(
            """
            INSERT INTO task_completions (task_instance_id, family_id, planned_task_id, completed_by, completion_mode)
            VALUES (NULL, ?, ?, ?, 'manual')
            """,
            (family_id, planned_task_id, user_id),
        )
        completion_id = int(cur.lastrowid)
        await self.conn.execute(
            """
            INSERT INTO undo_log (family_id, user_id, action_type, action_ref_id, payload_json)
            VALUES (?, ?, 'completion', ?, ?)
            """,
            (family_id, user_id, completion_id, json.dumps({"instance_id": None})),
        )
        await self.conn.commit()
        return completion_id

    async def get_dependencies(self, family_id: int, parent_task_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            """
            SELECT child_task_id, is_required, delay_mode, default_delay_minutes
            FROM task_dependency_rules
            WHERE family_id = ? AND parent_task_id = ?
            """,
            (family_id, parent_task_id),
        ) as cursor:
            return await cursor.fetchall()

    async def create_dependency_instance(
        self, family_id: int, child_task_id: int, user_id: int, delay_minutes: int
    ) -> int | None:
        activation = None
        if delay_minutes > 0:
            activation = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
        return await self.create_instance(family_id, child_task_id, user_id, "dependency", activation)

    async def undo_last_completion(self, family_id: int, user_id: int) -> bool:
        async with self.conn.execute(
            """
            SELECT id, action_ref_id, payload_json
            FROM undo_log
            WHERE family_id = ? AND user_id = ? AND action_type = 'completion' AND is_reverted = 0
            ORDER BY id DESC LIMIT 1
            """,
            (family_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return False
        completion_id = int(row["action_ref_id"])
        async with self.conn.execute(
            "SELECT task_instance_id FROM task_completions WHERE id = ?",
            (completion_id,),
        ) as cursor:
            completion = await cursor.fetchone()
        if completion is not None and completion["task_instance_id"] is not None:
            await self.conn.execute(
                "UPDATE task_instances SET status = 'pending' WHERE id = ?",
                (completion["task_instance_id"],),
            )
        await self.conn.execute("DELETE FROM task_completions WHERE id = ?", (completion_id,))
        await self.conn.execute("UPDATE undo_log SET is_reverted = 1 WHERE id = ?", (row["id"],))
        await self.conn.commit()
        return True

    async def stats_summary(self, family_id: int, period_days: int) -> tuple[list[aiosqlite.Row], int, int]:
        async with self.conn.execute(
            """
            SELECT u.display_name, COUNT(*) AS cnt
            FROM task_completions tc
            JOIN users u ON u.id = tc.completed_by
            WHERE tc.family_id = ?
              AND tc.completed_at >= datetime('now', ?)
            GROUP BY u.id, u.display_name
            ORDER BY cnt DESC
            """,
            (family_id, f"-{period_days} days"),
        ) as cursor:
            by_user = await cursor.fetchall()
        async with self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM task_instances WHERE family_id = ? AND status = 'pending'",
            (family_id,),
        ) as cursor:
            active = int((await cursor.fetchone())["cnt"])
        async with self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM task_instances WHERE family_id = ? AND status = 'scheduled'",
            (family_id,),
        ) as cursor:
            scheduled = int((await cursor.fetchone())["cnt"])
        return by_user, active, scheduled

    async def stats_by_task_type(self, family_id: int, period_days: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            """
            SELECT pt.title, COUNT(*) AS cnt
            FROM task_completions tc
            JOIN planned_tasks pt ON pt.id = tc.planned_task_id
            WHERE tc.family_id = ?
              AND tc.completed_at >= datetime('now', ?)
            GROUP BY pt.id, pt.title
            ORDER BY cnt DESC, pt.title
            """,
            (family_id, f"-{period_days} days"),
        ) as cursor:
            return await cursor.fetchall()

    async def list_recent_actions_by_member(
        self, family_id: int, user_id: int, limit: int, offset: int
    ) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            """
            SELECT tc.completed_at
            FROM task_completions tc
            WHERE tc.family_id = ? AND tc.completed_by = ?
            ORDER BY tc.completed_at DESC, tc.id DESC
            LIMIT ? OFFSET ?
            """,
            (family_id, user_id, limit, offset),
        ) as cursor:
            return await cursor.fetchall()

    async def list_recent_actions_by_task(
        self, family_id: int, task_id: int, limit: int, offset: int
    ) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            """
            SELECT tc.completed_at, u.display_name
            FROM task_completions tc
            JOIN users u ON u.id = tc.completed_by
            WHERE tc.family_id = ? AND tc.planned_task_id = ?
            ORDER BY tc.completed_at DESC, tc.id DESC
            LIMIT ? OFFSET ?
            """,
            (family_id, task_id, limit, offset),
        ) as cursor:
            return await cursor.fetchall()

    async def activate_due_scheduled(self) -> list[aiosqlite.Row]:
        now_iso = datetime.now(timezone.utc).isoformat()
        async with self.conn.execute(
            """
            SELECT ti.id, ti.family_id, ti.planned_task_id, pt.title
            FROM task_instances ti
            JOIN planned_tasks pt ON pt.id = ti.planned_task_id
            WHERE ti.status = 'scheduled' AND ti.activated_at <= ?
            """,
            (now_iso,),
        ) as cursor:
            due = await cursor.fetchall()
        if not due:
            return []
        ids = [int(row["id"]) for row in due]
        placeholders = ",".join(["?"] * len(ids))
        await self.conn.execute(
            f"UPDATE task_instances SET status = 'pending' WHERE id IN ({placeholders})",
            ids,
        )
        await self.conn.commit()
        return due

    async def scheduler_generate_for_now(self) -> list[tuple[int, str, int]]:
        async with self.conn.execute(
            """
            SELECT pt.family_id, pt.id, pt.title, f.timezone, ts.day_of_week, ts.time_hhmm
            FROM planned_tasks pt
            JOIN task_schedules ts ON ts.task_id = pt.id AND ts.is_active = 1
            JOIN families f ON f.id = pt.family_id
            WHERE pt.is_active = 1
            """,
        ) as cursor:
            rows = await cursor.fetchall()
        result: list[tuple[int, str, int]] = []
        now_utc = datetime.now(timezone.utc)
        for row in rows:
            tz_name = str(row["timezone"] or "UTC")
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = timezone.utc
            local = now_utc.astimezone(tz)
            if int(row["day_of_week"]) != local.weekday():
                continue
            if str(row["time_hhmm"]) != local.strftime("%H:%M"):
                continue
            created = await self.create_instance(int(row["family_id"]), int(row["id"]), None, "schedule")
            if created is not None:
                result.append((int(row["family_id"]), str(row["title"]), created))
        return result


class NotificationRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    async def family_recipients(self, family_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            """
            SELECT u.tg_user_id, u.id AS user_id
            FROM family_members fm
            JOIN users u ON u.id = fm.user_id
            WHERE fm.family_id = ? AND fm.is_active = 1 AND u.is_reachable = 1
            """,
            (family_id,),
        ) as cursor:
            return await cursor.fetchall()

    async def is_quiet_now(self, family_id: int, user_id: int) -> bool:
        async with self.conn.execute(
            "SELECT timezone FROM families WHERE id = ?",
            (family_id,),
        ) as cursor:
            row = await cursor.fetchone()
        tz_name = str(row["timezone"]) if row else "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        local = datetime.now(timezone.utc).astimezone(tz)
        day = local.weekday()
        hhmm = local.strftime("%H:%M")
        async with self.conn.execute(
            """
            SELECT 1
            FROM notification_quiet_hours
            WHERE family_id = ? AND user_id = ?
              AND (is_all_week = 1 OR day_of_week = ?)
              AND ? BETWEEN quiet_from AND quiet_to
            LIMIT 1
            """,
            (family_id, user_id, day, hhmm),
        ) as cursor:
            row = await cursor.fetchone()
        return row is not None

    async def set_quiet_interval(
        self, family_id: int, user_id: int, quiet_from: str, quiet_to: str, is_all_week: bool, day_of_week: int | None
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO notification_quiet_hours (family_id, user_id, day_of_week, quiet_from, quiet_to, is_all_week)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (family_id, user_id, day_of_week, quiet_from, quiet_to, int(is_all_week)),
        )
        await self.conn.commit()
