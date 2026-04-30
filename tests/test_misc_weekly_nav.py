from family_tasks_bot.handlers.misc import _build_day_nav_markup, _monthly_nav_keyboard, _weekly_nav_keyboard
from family_tasks_bot.keyboards.reply import misc_menu, stats_menu


def test_weekly_nav_keyboard_states() -> None:
    kb_current = _weekly_nav_keyboard(
        current_week_offset=0,
        current_week_start="2026-04-28",
        next_week_start="2026-05-05",
        prev_week_start="2026-04-21",
        left_enabled=False,
    )
    row_current = kb_current.inline_keyboard[0]
    assert row_current[0].text == " "
    assert row_current[0].callback_data == "statsnoop"
    assert row_current[1].callback_data == "statsback:global"
    assert row_current[2].text == " "
    assert row_current[2].callback_data == "statsnoop"

    kb_prev = _weekly_nav_keyboard(
        current_week_offset=-1,
        current_week_start="2026-04-21",
        next_week_start="2026-04-28",
        prev_week_start="2026-04-14",
        left_enabled=True,
    )
    row_prev = kb_prev.inline_keyboard[0]
    assert row_prev[0].text == "< (04-14)"
    assert row_prev[0].callback_data == "statsw:-2"
    assert row_prev[1].callback_data == "statsback:global"
    assert row_prev[2].text == "(04-28) >"
    assert row_prev[2].callback_data == "statsw:0"


def test_monthly_nav_keyboard_states() -> None:
    kb_current = _monthly_nav_keyboard(
        current_month_offset=0,
        current_month_start="2026-04-01",
        prev_month_start="2026-03-01",
        left_enabled=False,
    )
    row_current = kb_current.inline_keyboard[0]
    assert row_current[0].text == " "
    assert row_current[0].callback_data == "statsnoop"
    assert row_current[1].callback_data == "statsback:global"
    assert row_current[2].text == " "
    assert row_current[2].callback_data == "statsnoop"

    kb_prev = _monthly_nav_keyboard(
        current_month_offset=-1,
        current_month_start="2026-03-01",
        prev_month_start="2026-02-01",
        left_enabled=True,
    )
    row_prev = kb_prev.inline_keyboard[0]
    assert row_prev[0].text == "< (фев-26)"
    assert row_prev[0].callback_data == "statsmth:-2"
    assert row_prev[1].callback_data == "statsback:global"
    assert row_prev[2].text == "(мар-26) >"
    assert row_prev[2].callback_data == "statsmth:0"


def test_task_week_nav_labels_use_mm_dd() -> None:
    week_pages = [
        {"day_key": "2026-04-21", "weekday_cap": "Неделя"},
        {"day_key": "2026-04-14", "weekday_cap": "Неделя"},
    ]
    kb_last_week = _build_day_nav_markup(
        week_pages,
        0,
        lambda idx: f"statst:1:{idx}:root",
        "statsback:task:root",
    )
    assert kb_last_week is not None
    row_last_week = kb_last_week.inline_keyboard[0]
    assert row_last_week[0].text == "< (04-14)"
    assert row_last_week[0].callback_data == "statst:1:1:root"
    assert row_last_week[1].callback_data == "statsback:task:root"
    assert row_last_week[2].text == " "
    assert row_last_week[2].callback_data == "statsnoop"


def test_stats_menu_layout_rows() -> None:
    admin = stats_menu(True).keyboard
    user = stats_menu(False).keyboard
    assert [btn.text for btn in admin[0]] == ["По члену семьи", "По задаче"]
    assert [btn.text for btn in admin[1]] == ["Текущая неделя", "Текущий месяц"]
    assert [btn.text for btn in admin[2]] == ["Назад", "Правка"]
    assert [btn.text for btn in user[0]] == ["По члену семьи", "По задаче"]
    assert [btn.text for btn in user[1]] == ["Текущая неделя", "Текущий месяц"]
    assert [btn.text for btn in user[2]] == ["Назад"]


def test_misc_menu_places_back_left_and_about_right() -> None:
    kb = misc_menu(True).keyboard
    assert [btn.text for btn in kb[-1]] == ["Назад", "О боте"]
