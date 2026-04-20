from __future__ import annotations

import re
from typing import Any

from aiogram import Bot

from family_tasks_bot.db.repositories import NotificationRepository, PlannedTaskRepository, TaskRuntimeRepository, UserRepository
from family_tasks_bot.services.notifications import notify_family

CODE_RE = re.compile(r"\b([A-HJ-NP-Z2-9]{6})\b", re.IGNORECASE)


def _response(text: str, *, end_session: bool = False) -> dict[str, Any]:
    return {
        "response": {
            "text": text,
            "end_session": end_session,
        },
        "version": "1.0",
    }


def _normalize_command(payload: dict[str, Any]) -> str:
    request = payload.get("request") or {}
    command = request.get("command")
    if isinstance(command, str):
        return command.strip()
    return ""


def _extract_alice_user_id(payload: dict[str, Any]) -> str | None:
    session = payload.get("session") or {}
    user = session.get("user") or {}
    user_id = user.get("user_id")
    if isinstance(user_id, str) and user_id.strip():
        return user_id.strip()
    return None


def _extract_link_code(command: str) -> str | None:
    match = CODE_RE.search((command or "").upper())
    if match is None:
        return None
    return match.group(1).upper()


def _extract_task_phrase(command: str) -> str:
    normalized = (command or "").strip()
    lowered = normalized.lower()
    prefixes = (
        "отметь выполненной задачу",
        "отметить выполненной задачу",
        "добавь выполненную задачу",
        "добавить выполненную задачу",
        "выполнил задачу",
        "выполнена задача",
    )
    for prefix in prefixes:
        if lowered.startswith(prefix):
            return normalized[len(prefix) :].strip(" .,!?:;")
    return normalized.strip(" .,!?:;")


async def handle_alice_webhook_payload(
    payload: dict[str, Any],
    *,
    user_repo: UserRepository,
    planned_repo: PlannedTaskRepository,
    runtime_repo: TaskRuntimeRepository,
    notify_repo: NotificationRepository,
    bot: Bot,
) -> dict[str, Any]:
    alice_user_id = _extract_alice_user_id(payload)
    if alice_user_id is None:
        return _response("Не удалось определить пользователя Алисы. Попробуйте позже.")

    command = _normalize_command(payload)
    existing_link = await user_repo.get_alice_user_link(alice_user_id)
    if existing_link is None:
        link_code = _extract_link_code(command)
        if link_code is None:
            return _response("Сначала назовите код привязки из Telegram. Код состоит из 6 символов.")
        code_row = await user_repo.consume_alice_link_code(link_code)
        if code_row is None:
            return _response("Код недействителен или уже использован. Получите новый код в Telegram.")
        await user_repo.upsert_alice_user_link(
            alice_user_id=alice_user_id,
            family_id=int(code_row["family_id"]),
            user_id=int(code_row["user_id"]),
        )
        return _response("Привязка выполнена. Теперь скажите: отметь выполненной задачу и её название.")

    await user_repo.touch_alice_user_link(alice_user_id)
    task_phrase = _extract_task_phrase(command)
    if not task_phrase:
        return _response("Скажите команду: отметь выполненной задачу и её название.")

    family_id = int(existing_link["family_id"])
    completed_by_user_id = int(existing_link["user_id"])
    candidates = await planned_repo.search_active_tasks_by_phrase(family_id, task_phrase, limit=5)
    if not candidates:
        return _response("Не нашла задачу. Повторите название точнее.")

    lowered_phrase = task_phrase.lower()
    exact = [row for row in candidates if str(row["title"]).strip().lower() == lowered_phrase]
    if len(exact) == 1:
        selected = exact[0]
    elif len(candidates) == 1:
        selected = candidates[0]
    else:
        options = ", ".join(str(row["title"]) for row in candidates[:3])
        return _response(f"Нашла несколько задач: {options}. Повторите точнее.")

    await runtime_repo.add_manual_completion(
        family_id=family_id,
        planned_task_id=int(selected["id"]),
        completed_by_user_id=completed_by_user_id,
        actor_user_id=completed_by_user_id,
    )
    await notify_family(
        bot,
        notify_repo,
        family_id,
        f"Через Алису отмечена выполненной задача: {selected['title']}",
    )
    return _response(f"Отметила выполненной: {selected['title']}.")
