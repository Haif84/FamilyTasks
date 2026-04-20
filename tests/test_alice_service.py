from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, User

from family_tasks_bot.db.repositories import NotificationRepository, PlannedTaskRepository, TaskRuntimeRepository, UserRepository
from family_tasks_bot.services.alice import handle_alice_webhook_payload


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


async def _init_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    schema = Path("src/family_tasks_bot/db/schema.sql").read_text(encoding="utf-8")
    await conn.executescript(schema)
    await conn.execute("INSERT INTO users (id, tg_user_id, username, display_name) VALUES (1, 111, 'u1', 'User 1')")
    await conn.execute("INSERT INTO families (id, name, created_by_user_id) VALUES (1, 'F', 1)")
    await conn.execute(
        "INSERT INTO family_members (family_id, user_id, role_type, is_admin, is_active) VALUES (1, 1, 'parent', 1, 1)"
    )
    await conn.commit()
    return conn


def _payload(user_id: str, command: str) -> dict:
    return {
        "meta": {"locale": "ru-RU"},
        "request": {"command": command, "original_utterance": command, "type": "SimpleUtterance"},
        "session": {"new": False, "message_id": 1, "session_id": "s1", "skill_id": "skill", "user": {"user_id": user_id}},
        "version": "1.0",
    }


@pytest.mark.asyncio
async def test_alice_link_flow_requires_valid_code() -> None:
    conn = await _init_db()
    users = UserRepository(conn)
    planned = PlannedTaskRepository(conn)
    runtime = TaskRuntimeRepository(conn)
    notify = NotificationRepository(conn)
    bot = StubBot()
    code = await users.create_alice_link_code(1, 1, ttl_minutes=10)

    no_code = await handle_alice_webhook_payload(
        _payload("alice-u1", "привет"),
        user_repo=users,
        planned_repo=planned,
        runtime_repo=runtime,
        notify_repo=notify,
        bot=bot,
    )
    assert "код привязки" in no_code["response"]["text"].lower()

    ok = await handle_alice_webhook_payload(
        _payload("alice-u1", f"мой код {code}"),
        user_repo=users,
        planned_repo=planned,
        runtime_repo=runtime,
        notify_repo=notify,
        bot=bot,
    )
    assert "привязка выполнена" in ok["response"]["text"].lower()

    link = await users.get_alice_user_link("alice-u1")
    assert link is not None
    await conn.close()
    await bot.session.close()


@pytest.mark.asyncio
async def test_alice_marks_task_completed_after_linking() -> None:
    conn = await _init_db()
    users = UserRepository(conn)
    planned = PlannedTaskRepository(conn)
    runtime = TaskRuntimeRepository(conn)
    notify = NotificationRepository(conn)
    bot = StubBot()

    task_id = await planned.create_task(1, "Помыть посуду", 1)
    await users.upsert_alice_user_link("alice-u2", 1, 1)

    result = await handle_alice_webhook_payload(
        _payload("alice-u2", "отметь выполненной задачу помыть посуду"),
        user_repo=users,
        planned_repo=planned,
        runtime_repo=runtime,
        notify_repo=notify,
        bot=bot,
    )
    assert "отметила выполненной" in result["response"]["text"].lower()

    async with conn.execute(
        "SELECT COUNT(*) AS cnt FROM task_completions WHERE family_id = 1 AND planned_task_id = ?",
        (task_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert int(row["cnt"]) == 1
    assert any("через алису отмечена выполненной задача" in text.lower() for text in bot.sent_texts)
    await conn.close()
    await bot.session.close()
