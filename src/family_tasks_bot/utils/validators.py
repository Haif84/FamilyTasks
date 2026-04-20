import re
from typing import Literal
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{4,31}$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
# Telegram user ids are positive; allow up to 15 digits (fits int64).
TG_USER_ID_RE = re.compile(r"^\d{1,15}$")

InviteInputKind = Literal["username", "tg_id"]
FALLBACK_TIMEZONES = {
    "UTC": timezone.utc,
    "Etc/UTC": timezone.utc,
    "Europe/Moscow": timezone(timedelta(hours=3)),
}


def is_valid_username(value: str) -> bool:
    return bool(USERNAME_RE.fullmatch(value))


def invite_row_username_for_tg_id(tg_user_id: int) -> str:
    """Stored in family_invites.username for invites created by numeric Telegram id."""
    return f"tg:{tg_user_id}"


def parse_invite_input(value: str) -> tuple[InviteInputKind, str | int] | None:
    """
    Accept @username (or username without @) or numeric Telegram user id (digits only).
    Numeric form is only matched when the string is all digits (no @), so @123456 is still a username.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    if TG_USER_ID_RE.fullmatch(raw):
        tid = int(raw)
        if tid <= 0:
            return None
        return ("tg_id", tid)
    normalized = raw if raw.startswith("@") else f"@{raw}"
    normalized = normalized.lower()
    if is_valid_username(normalized):
        return ("username", normalized)
    return None


def is_valid_hhmm(value: str) -> bool:
    return bool(TIME_RE.fullmatch(value))


def is_valid_timezone(value: str) -> bool:
    raw = (value or "").strip()
    if not raw:
        return False
    if raw in FALLBACK_TIMEZONES:
        return True
    try:
        ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        return False
    except Exception:
        return False
    return True
