from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def manual_completion_datetime_keyboard(time_preview: str) -> InlineKeyboardMarkup:
    fields = ["d", "M", "y", "h", "m5", "m"]
    labels_up = ["День+", "Мес+", "Год+", "Час+", "5мин+", "Мин+"]
    labels_dn = ["День−", "Мес−", "Год−", "Час−", "5мин−", "Мин−"]
    row_up = [InlineKeyboardButton(text=labels_up[i], callback_data=f"mcdt:+:{fields[i]}") for i in range(6)]
    row_dn = [InlineKeyboardButton(text=labels_dn[i], callback_data=f"mcdt:-:{fields[i]}") for i in range(6)]
    preview = time_preview if len(time_preview) <= 64 else f"{time_preview[:61]}..."
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=preview, callback_data="noop")],
            row_up,
            row_dn,
            [InlineKeyboardButton(text="Назад", callback_data="mcdt:back")],
        ]
    )
