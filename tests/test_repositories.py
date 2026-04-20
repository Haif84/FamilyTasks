from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from family_tasks_bot.db.repositories import PlannedTaskRepository, TaskRuntimeRepository


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
    await conn.execute(
        "UPDATE task_completions SET completed_at = datetime('now', '-2 hours') WHERE family_id = 1"
    )
    await conn.commit()

    by_user_utc, _, _ = await runtime.stats_summary(1, 1, "UTC")
    assert len(by_user_utc) == 0

    by_user_moscow, _, _ = await runtime.stats_summary(1, 1, "Europe/Moscow")
    assert len(by_user_moscow) == 1
    assert int(by_user_moscow[0]["cnt"]) == 1
    await conn.close()
