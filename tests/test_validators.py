from family_tasks_bot.utils.validators import invite_row_username_for_tg_id, is_valid_timezone, parse_invite_input


def test_parse_invite_tg_id_digits_only() -> None:
    assert parse_invite_input("  123456789  ") == ("tg_id", 123456789)


def test_parse_invite_username_with_at() -> None:
    assert parse_invite_input("@Some_User") == ("username", "@some_user")


def test_parse_invite_username_without_at() -> None:
    assert parse_invite_input("Some_User") == ("username", "@some_user")


def test_parse_invite_invalid() -> None:
    assert parse_invite_input("") is None
    assert parse_invite_input("ab") is None


def test_invite_row_username_for_tg_id() -> None:
    assert invite_row_username_for_tg_id(42) == "tg:42"


def test_is_valid_timezone() -> None:
    assert is_valid_timezone("Europe/Moscow") is True
    assert is_valid_timezone("UTC") is True
    assert is_valid_timezone("Not/A_Real_Timezone") is False
