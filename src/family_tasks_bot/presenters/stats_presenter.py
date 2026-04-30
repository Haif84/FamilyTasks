from __future__ import annotations

from datetime import timedelta

WEEKDAY_SHORT_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def build_day_pages(entries: list, to_local_datetime, *, reverse_input: bool = True) -> list[dict]:
    ordered_entries = list(reversed(entries)) if reverse_input else list(entries)
    pages_asc: list[dict] = []
    current_page: dict | None = None
    current_day_key: str | None = None

    def _make_page(day_key: str, weekday_short: str, header: str) -> dict:
        return {
            "day_key": day_key,
            "weekday_short": weekday_short,
            "weekday_cap": weekday_short.capitalize(),
            "header": header,
            "items": [],
        }

    for entry in ordered_entries:
        raw_completed_at = str(entry["completed_at"])
        local_dt = to_local_datetime(raw_completed_at)
        if local_dt is None:
            day_key = f"raw:{raw_completed_at}"
            weekday_short = "?"
            day_header = "дата неизвестна:"
            time_part = raw_completed_at
        else:
            date_part = local_dt.strftime("%Y-%m-%d")
            weekday_short = WEEKDAY_SHORT_RU[local_dt.weekday()]
            day_key = date_part
            day_header = f"{weekday_short} ({date_part}):"
            time_part = local_dt.strftime("%H:%M")
        if day_key != current_day_key:
            current_page = _make_page(day_key, weekday_short, day_header)
            pages_asc.append(current_page)
            current_day_key = day_key
        current_page["items"].append((time_part, entry))

    return list(reversed(pages_asc))


def build_week_pages(entries: list, to_local_datetime, *, reverse_input: bool = True) -> list[dict]:
    ordered_entries = list(reversed(entries)) if reverse_input else list(entries)
    pages_asc: list[dict] = []
    current_page: dict | None = None
    current_week_key: str | None = None

    for entry in ordered_entries:
        raw_completed_at = str(entry["completed_at"])
        local_dt = to_local_datetime(raw_completed_at)
        if local_dt is None:
            week_start = raw_completed_at
            day_part = raw_completed_at
        else:
            ws = (local_dt - timedelta(days=local_dt.weekday())).date()
            week_start = ws.strftime("%Y-%m-%d")
            day_part = local_dt.strftime("%Y-%m-%d")
        if week_start != current_week_key:
            current_page = {
                "day_key": week_start,
                "weekday_cap": "Неделя",
                "header": f"Неделя ({week_start}):",
                "items": [],
            }
            pages_asc.append(current_page)
            current_week_key = week_start
        current_page["items"].append((day_part, entry))
    return list(reversed(pages_asc))


def render_day_page_lines(
    title: str,
    day_pages: list[dict],
    day_index: int,
    entry_tail_builder,
    empty_text: str,
) -> tuple[list[str], int]:
    if not day_pages:
        return ([title, empty_text], 0)
    normalized_day_index = max(0, min(day_index, len(day_pages) - 1))
    page = day_pages[normalized_day_index]
    lines = [title, page["header"]]
    for time_part, entry in page["items"]:
        lines.append(f"- {time_part} {entry_tail_builder(entry)}")
    return (lines, normalized_day_index)
