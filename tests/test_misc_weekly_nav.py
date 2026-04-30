from family_tasks_bot.handlers.misc import _weekly_nav_keyboard


def test_weekly_nav_keyboard_states() -> None:
    kb_current = _weekly_nav_keyboard(
        current_week_offset=0,
        current_week_start="2026-04-28",
        prev_week_start="2026-04-21",
        left_enabled=False,
    )
    row_current = kb_current.inline_keyboard[0]
    assert row_current[0].callback_data == "statsnoop"
    assert row_current[1].callback_data == "statsback:global"
    assert row_current[2].callback_data == "statsnoop"

    kb_prev = _weekly_nav_keyboard(
        current_week_offset=-1,
        current_week_start="2026-04-21",
        prev_week_start="2026-04-14",
        left_enabled=True,
    )
    row_prev = kb_prev.inline_keyboard[0]
    assert row_prev[0].callback_data == "statsw:-2"
    assert row_prev[1].callback_data == "statsback:global"
    assert row_prev[2].callback_data == "statsw:0"
