from __future__ import annotations

import os
from datetime import datetime, timezone


def _fallback_version() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}.{now.month:02d}.{now.day:02d}.0"


def get_app_version() -> str:
    raw = os.getenv("APP_VERSION", "").strip()
    if raw:
        return raw
    return _fallback_version()


APP_VERSION = get_app_version()
