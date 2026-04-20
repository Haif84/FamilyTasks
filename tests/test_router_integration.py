from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User

from family_tasks_bot.deps import install_deps, reset_deps
from family_tasks_bot.db.repositories import FamilyRepository, UserRepository
from family_tasks_bot.handlers import setup_routers
from family_tasks_bot.utils.validators import invite_row_username_for_tg_id

DP = Dispatcher()
DP.include_router(setup_routers())


async def _bootstrap_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    schema = Path("src/family_tasks_bot/db/schema.sql").read_text(encoding="utf-8")
    await conn.executescript(schema)
    return conn


def _make_message_update(update_id: int, user_id: int, text: str, username: str | None = None) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="Test", username=username),
            text=text,
        ),
    )


class StubBot(Bot):
    def __init__(self) -> None:
        super().__init__("123456:TEST")
        self.sent_texts: list[str] = []

    async def __call__(self, method, request_timeout=None):  # type: ignore[override]
        if isinstance(method, SendMessage):
            self.sent_texts.append(str(method.text))
            return Message(
                message_id=1,
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"),
                from_user=User(id=999999, is_bot=True, first_name="Bot"),
                text=str(method.text),
            )
        return True


@pytest.mark.asyncio
async def test_start_creates_initial_family_and_replies() -> None:
    conn = await _bootstrap_db()
    bot = StubBot()
    token = install_deps(conn, UserRepository, FamilyRepository)
    try:
        await DP.feed_update(bot, _make_message_update(1, 101, "/start", "first_admin"))
    finally:
        reset_deps(token)
    assert any("Вы добавлены в семью" in msg for msg in bot.sent_texts)

    async with conn.execute("SELECT COUNT(*) AS cnt FROM families") as cursor:
        row = await cursor.fetchone()
    assert int(row["cnt"]) == 1
    await conn.close()
    await bot.session.close()


@pytest.mark.asyncio
async def test_child_cannot_add_to_execution() -> None:
    conn = await _bootstrap_db()
    await conn.execute("INSERT INTO users (id, tg_user_id, username, display_name) VALUES (1, 1001, 'admin', 'Admin')")
    await conn.execute("INSERT INTO users (id, tg_user_id, username, display_name) VALUES (2, 1002, 'kid', 'Kid')")
    await conn.execute("INSERT INTO families (id, name, created_by_user_id) VALUES (1, 'F', 1)")
    await conn.execute(
        "INSERT INTO family_members (family_id, user_id, role_type, is_admin, is_active) VALUES (1, 1, 'parent', 1, 1)"
    )
    await conn.execute(
        "INSERT INTO family_members (family_id, user_id, role_type, is_admin, is_active) VALUES (1, 2, 'child', 0, 1)"
    )
    await conn.commit()

    bot = StubBot()
    token = install_deps(conn, UserRepository, FamilyRepository)
    try:
        await DP.feed_update(bot, _make_message_update(2, 1002, "Добавить к выполнению", "kid"))
    finally:
        reset_deps(token)
    assert any("только администраторам" in msg for msg in bot.sent_texts)
    await conn.close()
    await bot.session.close()


@pytest.mark.asyncio
async def test_invite_by_telegram_id_accepted_on_start() -> None:
    conn = await _bootstrap_db()
    await conn.execute(
        "INSERT INTO users (id, tg_user_id, username, display_name) VALUES (1, 1000, 'admin', 'Admin')"
    )
    await conn.execute("INSERT INTO families (id, name, created_by_user_id) VALUES (1, 'F', 1)")
    await conn.execute(
        "INSERT INTO family_members (family_id, user_id, role_type, is_admin, is_active) "
        "VALUES (1, 1, 'parent', 1, 1)"
    )
    await conn.commit()

    fam = FamilyRepository(conn)
    await fam.add_invite(1, invite_row_username_for_tg_id(8888), "child", False, 1)

    bot = StubBot()
    token = install_deps(conn, UserRepository, FamilyRepository)
    try:
        await DP.feed_update(bot, _make_message_update(3, 8888, "/start", None))
    finally:
        reset_deps(token)

    assert any("Вы добавлены в семью" in msg for msg in bot.sent_texts)
    async with conn.execute(
        "SELECT COUNT(*) AS cnt FROM family_members WHERE family_id = 1 AND is_active = 1"
    ) as cursor:
        row = await cursor.fetchone()
    assert int(row["cnt"]) == 2

    await conn.close()
    await bot.session.close()


@pytest.mark.asyncio
async def test_alice_link_command_returns_code() -> None:
    conn = await _bootstrap_db()
    bot = StubBot()
    token = install_deps(conn, UserRepository, FamilyRepository)
    try:
        await DP.feed_update(bot, _make_message_update(4, 5001, "/start", "alice_link_user"))
        await DP.feed_update(bot, _make_message_update(5, 5001, "/alice_link", "alice_link_user"))
    finally:
        reset_deps(token)
    assert any("Код привязки Алисы:" in msg for msg in bot.sent_texts)
    await conn.close()
    await bot.session.close()
