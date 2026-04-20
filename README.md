# Family Tasks Bot

Telegram bot on `aiogram 3.x` + `SQLite` for family household task tracking.

## Quick start

1. Create and activate virtual environment.
2. Install dependencies:
   - `pip install -e .`
3. Copy `.env.example` to `.env` and set `BOT_TOKEN`.
4. Run:
   - `python -m family_tasks_bot.main`

### Alice webhook (.env example)

```env
ALICE_WEBHOOK_ENABLED=true
ALICE_WEBHOOK_HOST=0.0.0.0
ALICE_WEBHOOK_PORT=8080
ALICE_WEBHOOK_PATH=/alice/webhook
```

Полная инструкция по подключению и настройке: [docs/ALICE_INTEGRATION.md](docs/ALICE_INTEGRATION.md).

## MVP commands and flows

- `/start` - onboarding and role-aware menu.
- `Состав семьи` -> `Добавить родителя/ребенка` - invite by `@username` or numeric Telegram user id (digits only, no `@`).
- `Плановые задачи` -> `Добавить` - create task and weekly schedule (`06:30,20:00`).
- `Плановые задачи` -> `Править` - open task editor and add dependencies via inline constructor.
- В редакторе задачи доступны изменение и удаление зависимостей.
- `Плановые задачи` -> `Добавить (по-умолчанию)` - clone task from defaults.
- `Текущие задачи` - mark pending tasks complete.
- `Добавить выполненную` - manual completion from planned task list.
- `Добавить к выполнению` - add now or at `чч:мм` (parent only).
- `Отменить последнее выполнение` - undo last completion by current user.
- `/quiet HH:MM-HH:MM [all|0..6]` - set quiet notifications for yourself.
- `/stats [day|week|month]` - extended statistics.

## Production polish included

- Family timezone is respected for schedule generation and quiet-mode evaluation.
- Configurable dependency delays are handled through inline choice buttons.
- Dependency graph is cycle-protected on save.
- Test suite includes repository-level production checks.

## Project structure

- `src/family_tasks_bot/config.py` - runtime settings.
- `src/family_tasks_bot/db/schema.sql` - full SQL DDL.
- `src/family_tasks_bot/db/migrations.py` - idempotent schema bootstrap.
- `src/family_tasks_bot/handlers/` - menu and role-based handlers.
- `src/family_tasks_bot/keyboards/` - reply and inline keyboards.

## Release: Docker и CI/CD

### Локально (Makefile)

- `make dev` — зависимости для разработки и тестов.
- `make test` — `pytest`.
- `make docker-build` / `make docker-up` — сборка и запуск через Docker Compose.

### GitHub

- **CI**: на push и pull request в `main`/`master` запускаются тесты (см. [`.github/workflows/ci.yml`](.github/workflows/ci.yml)).
- **CD**: деплой на VPS по SSH (см. [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)):
  - вручную: Actions → **Deploy to VPS** → Run workflow;
  - по push тега вида `v*` (например `v1.0.0`).

Пошаговая инструкция по VPS, секретам и первому запуску: [docs/DEPLOY.md](docs/DEPLOY.md).

Интеграция с Яндекс.Алисой (включая чек-лист публичного HTTPS webhook): [docs/ALICE_INTEGRATION.md](docs/ALICE_INTEGRATION.md).

Первый коммит и создание репозитория **FamilyTasks** на GitHub (локально, с вашей авторизацией): [docs/GITHUB_FIRST_PUSH.md](docs/GITHUB_FIRST_PUSH.md) и скрипт [`scripts/first-push-github.ps1`](scripts/first-push-github.ps1).

### Файлы образа

- [`Dockerfile`](Dockerfile) — production-образ на `python:3.11-slim`, непривилегированный пользователь, том для SQLite.
- [`docker-compose.yml`](docker-compose.yml) — сервис `bot`, volume `bot_data`, `restart: unless-stopped`.
