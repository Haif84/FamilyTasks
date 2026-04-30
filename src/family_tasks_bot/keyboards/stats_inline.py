from __future__ import annotations

import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

MONTH_SHORT_RU = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def build_day_nav_markup(
    day_pages: list[dict],
    day_index: int,
    day_callback_builder,
    back_callback_data: str,
) -> InlineKeyboardMarkup | None:
    left_button = InlineKeyboardButton(text=" ", callback_data="statsnoop")
    right_button = InlineKeyboardButton(text=" ", callback_data="statsnoop")

    def _day_nav_label(page: dict) -> str:
        weekday_cap = str(page.get("weekday_cap") or "").strip()
        if weekday_cap == "Неделя":
            day_key = str(page.get("day_key") or "").strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}$", day_key):
                return day_key[5:]
            return day_key
        return weekday_cap

    if day_index + 1 < len(day_pages):
        target = _day_nav_label(day_pages[day_index + 1])
        left_button = InlineKeyboardButton(
            text=f"< ({target})",
            callback_data=day_callback_builder(day_index + 1),
        )
    if day_index > 0:
        target = _day_nav_label(day_pages[day_index - 1])
        right_button = InlineKeyboardButton(
            text=f"({target}) >",
            callback_data=day_callback_builder(day_index - 1),
        )
    middle_button = InlineKeyboardButton(text="Назад", callback_data=back_callback_data)
    return InlineKeyboardMarkup(inline_keyboard=[[left_button, middle_button, right_button]])


def weekly_nav_keyboard(
    *,
    current_week_offset: int,
    current_week_start: str,
    next_week_start: str,
    prev_week_start: str,
    left_enabled: bool,
) -> InlineKeyboardMarkup:
    next_week_label = next_week_start[5:] if re.match(r"^\d{4}-\d{2}-\d{2}$", next_week_start) else next_week_start
    prev_week_label = prev_week_start[5:] if re.match(r"^\d{4}-\d{2}-\d{2}$", prev_week_start) else prev_week_start

    left_button = InlineKeyboardButton(text=" ", callback_data="statsnoop")
    if left_enabled:
        left_button = InlineKeyboardButton(
            text=f"< ({prev_week_label})",
            callback_data=f"statsw:{current_week_offset - 1}",
        )
    right_button = InlineKeyboardButton(text=" ", callback_data="statsnoop")
    if current_week_offset < 0:
        right_button = InlineKeyboardButton(
            text=f"({next_week_label}) >",
            callback_data=f"statsw:{current_week_offset + 1}",
        )
    middle_button = InlineKeyboardButton(text="Назад", callback_data="statsback:global")
    return InlineKeyboardMarkup(inline_keyboard=[[left_button, middle_button, right_button]])


def monthly_nav_keyboard(
    *,
    current_month_offset: int,
    current_month_start: str,
    prev_month_start: str,
    left_enabled: bool,
) -> InlineKeyboardMarkup:
    def _month_label(date_str: str) -> str:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            year = int(date_str[:4])
            month = int(date_str[5:7])
            return f"{MONTH_SHORT_RU[month - 1]}-{year % 100:02d}"
        return date_str

    current_month_label = _month_label(current_month_start)
    prev_month_label = _month_label(prev_month_start)
    left_button = InlineKeyboardButton(text=" ", callback_data="statsnoop")
    if left_enabled:
        left_button = InlineKeyboardButton(
            text=f"< ({prev_month_label})",
            callback_data=f"statsmth:{current_month_offset - 1}",
        )
    right_button = InlineKeyboardButton(text=" ", callback_data="statsnoop")
    if current_month_offset < 0:
        right_button = InlineKeyboardButton(
            text=f"({current_month_label}) >",
            callback_data=f"statsmth:{current_month_offset + 1}",
        )
    middle_button = InlineKeyboardButton(text="Назад", callback_data="statsback:global")
    return InlineKeyboardMarkup(inline_keyboard=[[left_button, middle_button, right_button]])


def history_datetime_keyboard(time_preview: str) -> InlineKeyboardMarkup:
    fields = ["d", "M", "y", "h", "m5", "m"]
    labels_up = ["День+", "Мес+", "Год+", "Час+", "5мин+", "Мин+"]
    labels_dn = ["День−", "Мес−", "Год−", "Час−", "5мин−", "Мин−"]
    row_up = [InlineKeyboardButton(text=labels_up[i], callback_data=f"histdt:+:{fields[i]}") for i in range(6)]
    row_dn = [InlineKeyboardButton(text=labels_dn[i], callback_data=f"histdt:-:{fields[i]}") for i in range(6)]
    preview = time_preview if len(time_preview) <= 64 else f"{time_preview[:61]}..."
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=preview, callback_data="statsnoop")],
            row_up,
            row_dn,
            [InlineKeyboardButton(text="Назад", callback_data="histdt:back")],
        ]
    )
