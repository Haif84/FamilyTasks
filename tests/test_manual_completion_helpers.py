from __future__ import annotations

from family_tasks_bot.handlers.tasks import (
    _bump_manual_completion_local_datetime,
    _manual_completion_datetime_keyboard,
    _manual_fin_carried_settings,
    _manual_fin_offset_display,
)


def test_manual_completion_offset_display() -> None:
    assert _manual_fin_offset_display(0) == "0 мин"
    assert _manual_fin_offset_display(-180) == "-3 ч"
    assert _manual_fin_offset_display(25) == "+25 мин"
    assert _manual_fin_offset_display(1505) == "+1 д 1 ч 5 мин"


def test_manual_completion_carried_settings_are_family_scoped() -> None:
    data = {
        "m_fin_carry_family_id": 7,
        "m_fin_carry_completed_by": 42,
        "m_fin_carry_dt_offset_minutes": -180,
        "m_fin_carry_dt_offset_is_set": 1,
    }

    assert _manual_fin_carried_settings(data, 7, 11) == (42, -180, True)
    assert _manual_fin_carried_settings(data, 8, 11) == (11, 0, False)


def test_manual_completion_datetime_keyboard_has_plusminus_five_min() -> None:
    kb = _manual_completion_datetime_keyboard("2026-04-29 20:00")
    rows = kb.inline_keyboard
    assert [btn.callback_data for btn in rows[1]] == [
        "mcdt:+:d",
        "mcdt:+:M",
        "mcdt:+:y",
        "mcdt:+:h",
        "mcdt:+:m5",
        "mcdt:+:m",
    ]
    assert [btn.callback_data for btn in rows[2]] == [
        "mcdt:-:d",
        "mcdt:-:M",
        "mcdt:-:y",
        "mcdt:-:h",
        "mcdt:-:m5",
        "mcdt:-:m",
    ]


def test_manual_completion_bump_five_minutes() -> None:
    bumped = _bump_manual_completion_local_datetime("2026-04-29 20:00:00", "UTC", "m", 5)
    assert bumped.startswith("2026-04-29 20:05")
