from __future__ import annotations

from family_tasks_bot.handlers.tasks import (
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
