from __future__ import annotations

from aiogram import Bot


def install_bot_data_mapping() -> None:
    if getattr(Bot, "_family_tasks_mapping_installed", False):
        return

    def __setitem__(self: Bot, key: str, value):  # type: ignore[no-untyped-def]
        data = getattr(self, "_family_tasks_data", None)
        if data is None:
            data = {}
            setattr(self, "_family_tasks_data", data)
        data[key] = value

    def __getitem__(self: Bot, key: str):  # type: ignore[no-untyped-def]
        data = getattr(self, "_family_tasks_data", None)
        if data is None or key not in data:
            raise KeyError(key)
        return data[key]

    Bot.__setitem__ = __setitem__  # type: ignore[attr-defined]
    Bot.__getitem__ = __getitem__  # type: ignore[attr-defined]
    Bot._family_tasks_mapping_installed = True  # type: ignore[attr-defined]
