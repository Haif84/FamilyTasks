import re


USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{4,31}$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def is_valid_username(value: str) -> bool:
    return bool(USERNAME_RE.fullmatch(value))


def is_valid_hhmm(value: str) -> bool:
    return bool(TIME_RE.fullmatch(value))
