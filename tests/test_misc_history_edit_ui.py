from family_tasks_bot.handlers.misc import (
    _history_bump_local_datetime,
    _history_datetime_keyboard,
    _history_entry_actions_keyboard,
)


def test_history_entry_actions_keyboard_layout() -> None:
    kb = _history_entry_actions_keyboard(77)
    rows = kb.inline_keyboard
    assert len(rows) == 4
    assert [btn.text for btn in rows[0]] == ["Исполнитель", "Дата/Время"]
    assert [btn.text for btn in rows[1]] == ["Комментарий"]
    assert [btn.text for btn in rows[2]] == ["Удалить", "Обновить"]
    assert [btn.text for btn in rows[3]] == ["Назад", "Отмена"]


def test_history_datetime_keyboard_layout() -> None:
    kb = _history_datetime_keyboard("2026-04-29 20:00")
    rows = kb.inline_keyboard
    assert len(rows) == 4
    assert rows[0][0].text == "2026-04-29 20:00"
    assert [btn.callback_data for btn in rows[1]] == [
        "histdt:+:d",
        "histdt:+:M",
        "histdt:+:y",
        "histdt:+:h",
        "histdt:+:m",
    ]
    assert [btn.callback_data for btn in rows[2]] == [
        "histdt:-:d",
        "histdt:-:M",
        "histdt:-:y",
        "histdt:-:h",
        "histdt:-:m",
    ]
    assert rows[3][0].callback_data == "histdt:back"


def test_history_bump_without_offset_label_in_preview() -> None:
    bumped = _history_bump_local_datetime("2026-04-29 20:00:00", "UTC", "h", -3)
    assert bumped.startswith("2026-04-29 17:00")
    kb = _history_datetime_keyboard("2026-04-29 17:00")
    preview = kb.inline_keyboard[0][0].text
    assert "Относительное смещение" not in preview
