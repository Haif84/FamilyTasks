from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from family_tasks_bot.db.repositories import NotificationRepository, PlannedTaskRepository


async def _db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    schema = Path("src/family_tasks_bot/db/schema.sql").read_text(encoding="utf-8")
    await conn.executescript(schema)
    await conn.execute("INSERT INTO users (tg_user_id, username, display_name) VALUES (1, 'admin', 'Admin')")
    await conn.execute("INSERT INTO families (name, timezone, created_by_user_id) VALUES ('Fam', 'Europe/Moscow', 1)")
    await conn.execute(
        "INSERT INTO family_members (family_id, user_id, role_type, is_admin, is_active) VALUES (1, 1, 'parent', 1, 1)"
    )
    await conn.execute("INSERT INTO planned_tasks (family_id, title, created_by) VALUES (1, 'A', 1)")
    await conn.execute("INSERT INTO planned_tasks (family_id, title, created_by) VALUES (1, 'B', 1)")
    await conn.commit()
    return conn


@pytest.mark.asyncio
async def test_dependency_delete() -> None:
    conn = await _db()
    repo = PlannedTaskRepository(conn)
    ok = await repo.add_dependency(1, 1, 2, True, "fixed", 10)
    assert ok is True
    dep = await repo.get_dependency(1, 1, 2)
    assert dep is not None
    await repo.delete_dependency(1, 1, 2)
    dep2 = await repo.get_dependency(1, 1, 2)
    assert dep2 is None
    await conn.close()


@pytest.mark.asyncio
async def test_quiet_mode_persists_and_reads() -> None:
    conn = await _db()
    repo = NotificationRepository(conn)
    await repo.set_quiet_interval(1, 1, "00:00", "23:59", True, None)
    quiet = await repo.is_quiet_now(1, 1)
    assert quiet is True
    await conn.close()
