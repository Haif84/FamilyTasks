from __future__ import annotations

from pathlib import Path

import aiosqlite


async def run_migrations(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    schema_path = Path(__file__).with_name("schema.sql")
    sql = schema_path.read_text(encoding="utf-8")
    migrations: list[tuple[str, str]] = [
        ("001_base_schema", sql),
    ]
    for migration_id, migration_sql in migrations:
        async with conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = ? LIMIT 1",
            (migration_id,),
        ) as cursor:
            exists = await cursor.fetchone()
        if exists is not None:
            continue
        await conn.executescript(migration_sql)
        await conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (?)",
            (migration_id,),
        )
    await _migrate_planned_tasks_sort_order(conn)
    await _migrate_rooms_and_task_room(conn)
    await _migrate_groups_and_task_group(conn)
    await _migrate_groups_sort_order(conn)
    await _migrate_task_completions_history_fields(conn)
    await conn.commit()


async def _migrate_planned_tasks_sort_order(conn: aiosqlite.Connection) -> None:
    migration_id = "003_planned_tasks_sort_order"
    async with conn.execute(
        "SELECT 1 FROM schema_migrations WHERE id = ? LIMIT 1",
        (migration_id,),
    ) as cursor:
        exists = await cursor.fetchone()
    if exists is not None:
        return

    async with conn.execute("PRAGMA table_info(planned_tasks)") as cursor:
        columns = await cursor.fetchall()
    has_sort_order = any(str(col["name"]) == "sort_order" for col in columns)
    if not has_sort_order:
        await conn.execute("ALTER TABLE planned_tasks ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")

    await conn.execute(
        """
        UPDATE planned_tasks AS p
        SET sort_order = (
            SELECT COUNT(*)
            FROM planned_tasks AS p2
            WHERE p2.family_id = p.family_id
              AND (
                  p2.title < p.title
                  OR (p2.title = p.title AND p2.id <= p.id)
              )
        )
        WHERE COALESCE(p.sort_order, 0) = 0
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_planned_tasks_family_sort_order ON planned_tasks(family_id, sort_order)"
    )
    await conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (migration_id,))


async def _migrate_rooms_and_task_room(conn: aiosqlite.Connection) -> None:
    migration_id = "004_rooms_and_planned_task_room"
    async with conn.execute(
        "SELECT 1 FROM schema_migrations WHERE id = ? LIMIT 1",
        (migration_id,),
    ) as cursor:
        exists = await cursor.fetchone()
    if exists is not None:
        return

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            family_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (family_id, name),
            FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE CASCADE
        )
        """
    )

    async with conn.execute("PRAGMA table_info(planned_tasks)") as cursor:
        columns = await cursor.fetchall()
    has_room_id = any(str(col["name"]) == "room_id" for col in columns)
    if not has_room_id:
        await conn.execute("ALTER TABLE planned_tasks ADD COLUMN room_id INTEGER")

    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rooms_family_name ON rooms(family_id, name)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_planned_tasks_family_room_sort ON planned_tasks(family_id, room_id, sort_order)"
    )
    await conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (migration_id,))


async def _migrate_task_completions_history_fields(conn: aiosqlite.Connection) -> None:
    migration_id = "005_task_completions_history_fields"
    async with conn.execute(
        "SELECT 1 FROM schema_migrations WHERE id = ? LIMIT 1",
        (migration_id,),
    ) as cursor:
        exists = await cursor.fetchone()
    if exists is not None:
        return

    async with conn.execute("PRAGMA table_info(task_completions)") as cursor:
        columns = await cursor.fetchall()
    col_names = {str(col["name"]) for col in columns}

    if "added_at" not in col_names:
        await conn.execute("ALTER TABLE task_completions ADD COLUMN added_at TEXT")
    if "history_updated_at" not in col_names:
        await conn.execute("ALTER TABLE task_completions ADD COLUMN history_updated_at TEXT")

    await conn.execute(
        """
        UPDATE task_completions
        SET added_at = COALESCE(NULLIF(added_at, ''), completed_at, CURRENT_TIMESTAMP)
        """
    )
    await conn.execute(
        """
        UPDATE task_completions
        SET history_updated_at = COALESCE(NULLIF(history_updated_at, ''), CURRENT_TIMESTAMP)
        """
    )
    await conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (migration_id,))


async def _migrate_groups_and_task_group(conn: aiosqlite.Connection) -> None:
    migration_id = "006_groups_and_planned_task_group"
    async with conn.execute(
        "SELECT 1 FROM schema_migrations WHERE id = ? LIMIT 1",
        (migration_id,),
    ) as cursor:
        exists = await cursor.fetchone()
    if exists is not None:
        return

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            family_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (family_id, name),
            FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE CASCADE
        )
        """
    )

    async with conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rooms' LIMIT 1"
    ) as cursor:
        has_rooms_table = await cursor.fetchone() is not None
    if has_rooms_table:
        await conn.execute(
            """
            INSERT OR IGNORE INTO groups (id, family_id, name, sort_order, created_at, updated_at)
            SELECT
                r.id,
                r.family_id,
                r.name,
                (
                    SELECT COUNT(*)
                    FROM rooms r2
                    WHERE r2.family_id = r.family_id
                      AND (
                          r2.name < r.name
                          OR (r2.name = r.name AND r2.id <= r.id)
                      )
                ),
                r.created_at,
                r.updated_at
            FROM rooms r
            """
        )

    async with conn.execute("PRAGMA table_info(planned_tasks)") as cursor:
        columns = await cursor.fetchall()
    col_names = {str(col["name"]) for col in columns}
    has_group_id = "group_id" in col_names
    if not has_group_id:
        await conn.execute("ALTER TABLE planned_tasks ADD COLUMN group_id INTEGER")

    has_room_id = "room_id" in col_names
    if has_room_id:
        await conn.execute(
            """
            UPDATE planned_tasks
            SET group_id = room_id
            WHERE group_id IS NULL AND room_id IS NOT NULL
            """
        )

    await conn.execute("DROP INDEX IF EXISTS idx_planned_tasks_family_room_sort")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_groups_family_name ON groups(family_id, name)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_planned_tasks_family_group_sort ON planned_tasks(family_id, group_id, sort_order)"
    )
    await conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (migration_id,))


async def _migrate_groups_sort_order(conn: aiosqlite.Connection) -> None:
    migration_id = "007_groups_sort_order"
    async with conn.execute(
        "SELECT 1 FROM schema_migrations WHERE id = ? LIMIT 1",
        (migration_id,),
    ) as cursor:
        exists = await cursor.fetchone()
    if exists is not None:
        return

    async with conn.execute("PRAGMA table_info(groups)") as cursor:
        columns = await cursor.fetchall()
    col_names = {str(col["name"]) for col in columns}
    if "sort_order" not in col_names:
        await conn.execute("ALTER TABLE groups ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")

    await conn.execute(
        """
        UPDATE groups AS g
        SET sort_order = (
            SELECT COUNT(*)
            FROM groups AS g2
            WHERE g2.family_id = g.family_id
              AND (
                  g2.name < g.name
                  OR (g2.name = g.name AND g2.id <= g.id)
              )
        )
        WHERE COALESCE(g.sort_order, 0) = 0
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_groups_family_sort_order ON groups(family_id, sort_order)"
    )
    await conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (migration_id,))


async def seed_default_tasks(conn: aiosqlite.Connection) -> None:
    seed_id = "002_seed_default_tasks"
    async with conn.execute(
        "SELECT 1 FROM schema_migrations WHERE id = ? LIMIT 1",
        (seed_id,),
    ) as cursor:
        seeded = await cursor.fetchone()
    if seeded is not None:
        return
    defaults = [
        ("Кормление собак", "Базовая задача для ухода за собаками", 10),
        ("Загрузка посудомойки", "Старт цикла посудомойки", 20),
        ("Разгрузка посудомойки", "Завершение цикла посудомойки", 30),
    ]
    for title, description, sort_order in defaults:
        await conn.execute(
            """
            INSERT INTO default_tasks (title, description, sort_order, is_active)
            SELECT ?, ?, ?, 1
            WHERE NOT EXISTS (
                SELECT 1 FROM default_tasks WHERE title = ?
            )
            """,
            (title, description, sort_order, title),
        )
    await conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (seed_id,))
    await conn.commit()
