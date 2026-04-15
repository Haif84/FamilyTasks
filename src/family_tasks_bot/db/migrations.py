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
    await conn.commit()


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
