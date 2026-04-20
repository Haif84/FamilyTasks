from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def _rows_of_two(labels: list[str]) -> list[list[KeyboardButton]]:
    rows: list[list[KeyboardButton]] = []
    for i in range(0, len(labels), 2):
        pair = labels[i : i + 2]
        rows.append([KeyboardButton(text=label) for label in pair])
    return rows


def main_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    labels: list[str] = []
    if is_admin:
        labels.append("Текущие задачи")
    labels.extend(["Добавить выполненную", "Отм. последнее выполнение"])
    if is_admin:
        labels.append("Добавить к выполнению")
    labels.extend(["Статистика", "Прочее"])
    rows = _rows_of_two(labels)
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def back_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Назад")]],
        resize_keyboard=True,
    )


def misc_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    labels = [
        "Состав семьи",
        "Плановые задачи",
        "Группы",
        "Код для Алисы",
    ]
    if is_admin:
        labels.append("Добавить выполненную (за ...)")
    labels.extend(["О боте", "Назад"])
    rows = _rows_of_two(labels)
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
    )


def stats_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    labels = [
        "По члену семьи",
        "По задаче",
        "Текущая неделя",
    ]
    if is_admin:
        labels.append("Правка")
    labels.append("Назад")
    rows = _rows_of_two(labels)
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
    )


def groups_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    labels = ["Список"]
    if is_admin:
        labels.append("Правка групп")
    labels.append("Назад")
    rows = _rows_of_two(labels)
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
    )


def family_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    labels = ["Список", "Часовой пояс семьи"]
    if is_admin:
        labels.extend(
            [
                "Править состав семьи",
                "Добавить родителя",
                "Добавить ребенка",
            ]
        )
    labels.append("Назад")
    rows = _rows_of_two(labels)
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def planned_tasks_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    labels = ["Список"]
    if is_admin:
        labels.extend(["Править", "Добавить", "Добавить (по-умолчанию)"])
    labels.append("Назад")
    rows = _rows_of_two(labels)
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
