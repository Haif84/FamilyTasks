from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def family_tzinfo(timezone_name: str) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return timezone.utc


def parse_completed_at_utc_sql(value: str) -> datetime:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty")
    if "T" not in raw and " " in raw:
        raw = raw.replace(" ", "T", 1)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def bump_local_datetime(completed_at_utc_sql: str, tz_name: str, field: str, delta: int) -> str:
    utc = parse_completed_at_utc_sql(completed_at_utc_sql)
    local = utc.astimezone(family_tzinfo(tz_name))
    y, m, d = local.year, local.month, local.day
    if field == "m":
        local2 = local + timedelta(minutes=delta)
    elif field == "h":
        local2 = local + timedelta(hours=delta)
    elif field == "d":
        local2 = local + timedelta(days=delta)
    elif field == "M":
        total = y * 12 + (m - 1) + delta
        y2, m0 = divmod(total, 12)
        m2 = m0 + 1
        d2 = min(d, monthrange(y2, m2)[1])
        local2 = local.replace(year=y2, month=m2, day=d2)
    elif field == "y":
        y2 = y + delta
        d2 = min(d, monthrange(y2, m)[1])
        local2 = local.replace(year=y2, day=d2)
    else:
        local2 = local
    return local2.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
