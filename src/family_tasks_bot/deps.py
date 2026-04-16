from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

_ctx: ContextVar[dict[str, Any] | None] = ContextVar("family_tasks_bot_deps", default=None)


def install_deps(db_conn: Any, user_repo_factory: Any, family_repo_factory: Any) -> Token:
    return _ctx.set(
        {
            "db_conn": db_conn,
            "user_repo_factory": user_repo_factory,
            "family_repo_factory": family_repo_factory,
        }
    )


def reset_deps(token: Token) -> None:
    _ctx.reset(token)


def get_repositories() -> tuple[Any, Any, Any]:
    d = _ctx.get()
    if not d:
        raise RuntimeError("Bot dependencies not installed (install_deps before polling)")
    db = d["db_conn"]
    return db, d["user_repo_factory"](db), d["family_repo_factory"](db)
