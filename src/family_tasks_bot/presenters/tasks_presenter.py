from __future__ import annotations


def truncate_preview(text: str, *, limit: int = 64) -> str:
    return text if len(text) <= limit else f"{text[: limit - 3]}..."
