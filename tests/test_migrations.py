from __future__ import annotations

import aiosqlite
import pytest

from family_tasks_bot.db.migrations import run_migrations, seed_default_tasks


@pytest.mark.asyncio
async def test_schema_migrations_idempotent() -> None:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row

    await run_migrations(conn)
    await seed_default_tasks(conn)
    await run_migrations(conn)
    await seed_default_tasks(conn)

    async with conn.execute("SELECT COUNT(*) AS cnt FROM schema_migrations") as cursor:
        row = await cursor.fetchone()
    assert int(row["cnt"]) == 10

    async with conn.execute("SELECT COUNT(*) AS cnt FROM default_tasks") as cursor:
        row = await cursor.fetchone()
    assert int(row["cnt"]) >= 3

    await conn.close()
