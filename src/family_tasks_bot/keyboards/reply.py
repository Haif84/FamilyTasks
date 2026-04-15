from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def main_menu(is_parent: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="Текущие задачи")],
        [KeyboardButton(text="Добавить выполненную")],
        [KeyboardButton(text="Отменить последнее выполнение")],
    ]
    if is_parent:
        rows.append([KeyboardButton(text="Добавить к выполнению")])
    rows.append([KeyboardButton(text="Прочее")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def back_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Назад")]],
        resize_keyboard=True,
    )


def misc_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Статистика")],
            [KeyboardButton(text="Состав семьи")],
            [KeyboardButton(text="Плановые задачи")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
    )


def family_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text="Список")]]
    if is_admin:
        rows.extend(
            [
                [KeyboardButton(text="Править")],
                [KeyboardButton(text="Добавить родителя")],
                [KeyboardButton(text="Добавить ребенка")],
            ]
        )
    rows.append([KeyboardButton(text="Назад")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def planned_tasks_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text="Список")]]
    if is_admin:
        rows.extend(
            [
                [KeyboardButton(text="Править")],
                [KeyboardButton(text="Добавить")],
                [KeyboardButton(text="Добавить (по-умолчанию)")],
            ]
        )
    rows.append([KeyboardButton(text="Назад")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
