from __future__ import annotations

from datetime import datetime
from pathlib import Path

import aiosqlite
import pytest

from family_tasks_bot.db.repositories import PlannedTaskRepository, TaskRuntimeRepository, UserRepository


async def _init_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    schema = Path("src/family_tasks_bot/db/schema.sql").read_text(encoding="utf-8")
    await conn.executescript(schema)
    await conn.execute("INSERT INTO users (tg_user_id, username, display_name) VALUES (1, 'u1', 'User 1')")
    await conn.execute("INSERT INTO users (tg_user_id, username, display_name) VALUES (2, 'u2', 'User 2')")
    await conn.execute("INSERT INTO families (name, created_by_user_id) VALUES ('F', 1)")
    await conn.execute(
        "INSERT INTO family_members (family_id, user_id, role_type, is_admin, is_active) VALUES (1, 1, 'parent', 1, 1)"
    )
    await conn.commit()
    return conn


@pytest.mark.asyncio
async def test_dependency_cycle_protection() -> None:
    conn = await _init_db()
    repo = PlannedTaskRepository(conn)
    t1 = await repo.create_task(1, "T1", 1)
    t2 = await repo.create_task(1, "T2", 1)
    t3 = await repo.create_task(1, "T3", 1)
    assert await repo.add_dependency(1, t1, t2, True, "none", 0) is True
    assert await repo.add_dependency(1, t2, t3, True, "none", 0) is True
    assert await repo.add_dependency(1, t3, t1, True, "none", 0) is False
    await conn.close()


@pytest.mark.asyncio
async def test_instance_dedup_and_undo() -> None:
    conn = await _init_db()
    planned = PlannedTaskRepository(conn)
    runtime = TaskRuntimeRepository(conn)
    t1 = await planned.create_task(1, "Dishwasher", 1)
    first = await runtime.create_instance(1, t1, 1, "manual")
    second = await runtime.create_instance(1, t1, 1, "manual")
    assert first is not None
    assert second is None
    completed = await runtime.complete_instance(first, 1, "current")
    assert completed is not None
    ok = await runtime.undo_last_completion(1, 1)
    assert ok is True
    rows = await runtime.list_active_instances(1)
    assert len(rows) == 1
    await conn.close()


@pytest.mark.asyncio
async def test_stats_by_task_type() -> None:
    conn = await _init_db()
    planned = PlannedTaskRepository(conn)
    runtime = TaskRuntimeRepository(conn)
    t1 = await planned.create_task(1, "Feed dogs", 1)
    t2 = await planned.create_task(1, "Walk dogs", 1)
    i1 = await runtime.create_instance(1, t1, 1, "manual")
    i2 = await runtime.create_instance(1, t2, 1, "manual")
    await runtime.complete_instance(i1, 1, "current")
    await runtime.complete_instance(i2, 1, "current")
    by_task = await runtime.stats_by_task_type(1, 7)
    titles = {row["title"] for row in by_task}
    assert "Feed dogs" in titles
    assert "Walk dogs" in titles
    await conn.close()


@pytest.mark.asyncio
async def test_stats_timezone_boundary_respected() -> None:
    conn = await _init_db()
    planned = PlannedTaskRepository(conn)
    runtime = TaskRuntimeRepository(conn)
    t1 = await planned.create_task(1, "Laundry", 1)
    instance_id = await runtime.create_instance(1, t1, 1, "manual")
    await runtime.complete_instance(instance_id, 1, "current")
    utc_since = datetime.strptime(runtime._stats_since_utc(1, "UTC"), "%Y-%m-%d %H:%M:%S")
    moscow_since = datetime.strptime(runtime._stats_since_utc(1, "Europe/Moscow"), "%Y-%m-%d %H:%M:%S")
    assert moscow_since < utc_since
    midpoint = moscow_since + (utc_since - moscow_since) / 2
    completion_at = midpoint.strftime("%Y-%m-%d %H:%M:%S")
    await conn.execute("UPDATE task_completions SET completed_at = ? WHERE family_id = 1", (completion_at,))
    await conn.commit()

    by_user_utc, _, _ = await runtime.stats_summary(1, 1, "UTC")
    assert len(by_user_utc) == 0

    by_user_moscow, _, _ = await runtime.stats_summary(1, 1, "Europe/Moscow")
    assert len(by_user_moscow) == 1
    assert int(by_user_moscow[0]["cnt"]) == 1
    await conn.close()


@pytest.mark.asyncio
async def test_upsert_user_preserves_custom_display_name() -> None:
    conn = await _init_db()
    users = UserRepository(conn)
    user_id = await users.upsert_user(999001, "user_one", "First Name")
    assert user_id > 0

    await conn.execute("UPDATE users SET display_name = ? WHERE id = ?", ("Custom Name", user_id))
    await conn.commit()

    await users.upsert_user(999001, "user_one_new", "Telegram Name")
    async with conn.execute("SELECT username, display_name FROM users WHERE id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert row["username"] == "user_one_new"
    assert row["display_name"] == "Custom Name"
    await conn.close()


@pytest.mark.asyncio
async def test_history_fields_and_edit_methods() -> None:
    conn = await _init_db()
    await conn.execute("INSERT INTO users (id, tg_user_id, username, display_name) VALUES (3, 1003, 'u3', 'User 3')")
    await conn.execute(
        "INSERT INTO family_members (family_id, user_id, role_type, is_admin, is_active) VALUES (1, 3, 'child', 0, 1)"
    )
    await conn.commit()

    planned = PlannedTaskRepository(conn)
    runtime = TaskRuntimeRepository(conn)
    task_id = await planned.create_task(1, "Laundry", 1)
    await runtime.add_manual_completion(1, task_id, 1)

    rows = await runtime.list_recent_actions(1, 10, 0)
    assert len(rows) == 1
    entry = rows[0]
    assert entry["added_at"] is not None
    assert entry["history_updated_at"] is not None

    completion_id = int(entry["completion_id"])
    updated_executor = await runtime.update_completion_executor(1, completion_id, 3)
    assert updated_executor is True

    updated_time = await runtime.update_completion_datetime(1, completion_id, "2026-04-20 10:30:00")
    assert updated_time is True

    saved = await runtime.get_completion_entry(1, completion_id)
    assert saved is not None
    assert int(saved["member_user_id"]) == 3
    assert str(saved["completed_at"]) == "2026-04-20 10:30:00"
    assert saved["added_at"] is not None
    assert saved["history_updated_at"] is not None
    await conn.close()
