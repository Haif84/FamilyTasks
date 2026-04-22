from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import pytest

from family_tasks_bot.db.repositories import FamilyRepository, PlannedTaskRepository, TaskRuntimeRepository, UserRepository


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
async def test_stats_timezone_boundary_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = await _init_db()
    planned = PlannedTaskRepository(conn)
    runtime = TaskRuntimeRepository(conn)
    t1 = await planned.create_task(1, "Laundry", 1)
    instance_id = await runtime.create_instance(1, t1, 1, "manual")
    await runtime.complete_instance(instance_id, 1, "current")

    def _fixed_stats_since(self: TaskRuntimeRepository, period_days: int, timezone_name: str) -> str:
        if period_days != 1:
            return TaskRuntimeRepository._stats_since_utc(self, period_days, timezone_name)
        if timezone_name == "UTC":
            return "2026-06-15 00:00:00"
        if timezone_name == "Europe/Moscow":
            return "2026-06-14 21:00:00"
        return TaskRuntimeRepository._stats_since_utc(self, period_days, timezone_name)

    monkeypatch.setattr(TaskRuntimeRepository, "_stats_since_utc", _fixed_stats_since)

    utc_since = datetime.strptime("2026-06-15 00:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    moscow_since = datetime.strptime("2026-06-14 21:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
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

    deleted = await runtime.delete_completion_entry(1, completion_id)
    assert deleted is True
    missing = await runtime.get_completion_entry(1, completion_id)
    assert missing is None
    await conn.close()


@pytest.mark.asyncio
async def test_planned_task_delete_blocked_when_history_exists() -> None:
    conn = await _init_db()
    planned = PlannedTaskRepository(conn)
    runtime = TaskRuntimeRepository(conn)
    task_id = await planned.create_task(1, "Laundry", 1)
    await runtime.add_manual_completion(1, task_id, 1)

    deleted, history_count = await planned.delete_task_if_no_history(1, task_id)
    assert deleted is False
    assert history_count == 1
    task = await planned.get_task(1, task_id)
    assert task is not None
    await conn.close()


@pytest.mark.asyncio
async def test_planned_task_delete_when_no_history() -> None:
    conn = await _init_db()
    planned = PlannedTaskRepository(conn)
    task_id = await planned.create_task(1, "Laundry", 1)

    deleted, history_count = await planned.delete_task_if_no_history(1, task_id)
    assert deleted is True
    assert history_count == 0
    task = await planned.get_task(1, task_id)
    assert task is None
    await conn.close()


@pytest.mark.asyncio
async def test_runtime_tasks_grouped_by_group_for_manual_completion() -> None:
    conn = await _init_db()
    await conn.execute("INSERT INTO groups (id, family_id, name) VALUES (1, 1, 'Kitchen')")
    await conn.execute("INSERT INTO groups (id, family_id, name) VALUES (2, 1, 'Hall')")
    await conn.commit()

    planned = PlannedTaskRepository(conn)
    runtime = TaskRuntimeRepository(conn)
    t1 = await planned.create_task(1, "No group task", 1)
    t2 = await planned.create_task(1, "Kitchen task", 1)
    t3 = await planned.create_task(1, "Hall task", 1)
    await planned.set_task_group(1, t2, 1)
    await planned.set_task_group(1, t3, 2)

    without_group = await runtime.list_planned_tasks_without_group(1)
    kitchen = await runtime.list_planned_tasks_by_group(1, 1)
    hall = await runtime.list_planned_tasks_by_group(1, 2)

    assert {int(row["id"]) for row in without_group} == {t1}
    assert {int(row["id"]) for row in kitchen} == {t2}
    assert {int(row["id"]) for row in hall} == {t3}
    await conn.close()


@pytest.mark.asyncio
async def test_group_sort_order_create_and_move() -> None:
    conn = await _init_db()
    family = FamilyRepository(conn)
    g1 = await family.create_group(1, "Kitchen")
    g2 = await family.create_group(1, "Hall")
    g3 = await family.create_group(1, "Balcony")

    groups = await family.list_groups(1)
    assert [(int(row["id"]), int(row["sort_order"])) for row in groups] == [(g1, 1), (g2, 2), (g3, 3)]

    moved_up = await family.move_group_up(1, g3)
    assert moved_up is True
    groups_after_up = await family.list_groups(1)
    assert [int(row["id"]) for row in groups_after_up] == [g1, g3, g2]

    moved_down = await family.move_group_down(1, g1)
    assert moved_down is True
    groups_after_down = await family.list_groups(1)
    assert [int(row["id"]) for row in groups_after_down] == [g3, g1, g2]
    await conn.close()


@pytest.mark.asyncio
async def test_task_requires_comment_toggle_and_manual_comment_saved() -> None:
    conn = await _init_db()
    planned = PlannedTaskRepository(conn)
    runtime = TaskRuntimeRepository(conn)
    task_id = await planned.create_task(1, "Commented task", 1)

    toggled = await planned.set_task_requires_comment(1, task_id, True)
    assert toggled is True
    task = await planned.get_task(1, task_id)
    assert task is not None
    assert int(task["requires_comment"]) == 1

    completion_id = await runtime.add_manual_completion(
        1,
        task_id,
        2,
        comment_text="Done with details",
        actor_user_id=1,
    )
    async with conn.execute(
        "SELECT completed_by, comment_text FROM task_completions WHERE id = ?",
        (completion_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert int(row["completed_by"]) == 2
    assert str(row["comment_text"]) == "Done with details"

    async with conn.execute(
        "SELECT user_id FROM undo_log WHERE action_ref_id = ? AND action_type = 'completion'",
        (completion_id,),
    ) as cursor:
        undo_row = await cursor.fetchone()
    assert undo_row is not None
    assert int(undo_row["user_id"]) == 1
    await conn.close()


@pytest.mark.asyncio
async def test_add_manual_completion_explicit_completed_at() -> None:
    conn = await _init_db()
    planned = PlannedTaskRepository(conn)
    runtime = TaskRuntimeRepository(conn)
    task_id = await planned.create_task(1, "Timed task", 1)
    at = "2024-06-15 12:30:45"
    completion_id = await runtime.add_manual_completion(
        1,
        task_id,
        1,
        comment_text=None,
        actor_user_id=1,
        completed_at_utc=at,
    )
    async with conn.execute(
        "SELECT datetime(completed_at) AS ca FROM task_completions WHERE id = ?",
        (completion_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert str(row["ca"]).startswith("2024-06-15")
    await conn.close()


@pytest.mark.asyncio
async def test_alice_link_code_can_be_consumed_once() -> None:
    conn = await _init_db()
    users = UserRepository(conn)
    code = await users.create_alice_link_code(1, 1, ttl_minutes=10)
    assert len(code) == 6

    consumed = await users.consume_alice_link_code(code)
    assert consumed is not None
    assert int(consumed["family_id"]) == 1
    assert int(consumed["user_id"]) == 1

    consumed_again = await users.consume_alice_link_code(code)
    assert consumed_again is None
    await conn.close()


@pytest.mark.asyncio
async def test_alice_link_and_task_search_by_phrase() -> None:
    conn = await _init_db()
    users = UserRepository(conn)
    planned = PlannedTaskRepository(conn)
    task_id = await planned.create_task(1, "Помыть посуду", 1)
    await users.upsert_alice_user_link("alice-user-1", 1, 1)

    link = await users.get_alice_user_link("alice-user-1")
    assert link is not None
    assert int(link["family_id"]) == 1
    assert int(link["user_id"]) == 1

    matches = await planned.search_active_tasks_by_phrase(1, "посуд")
    assert len(matches) >= 1
    assert any(int(row["id"]) == task_id for row in matches)
    await conn.close()
