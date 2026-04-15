# Первый push в новый репозиторий GitHub (FamilyTasks)

Авторизация выполняется **у вас локально** (браузер / SSH). Агент в песочнице не может войти в ваш GitHub-аккаунт.

## Вариант A: GitHub CLI (удобно создать репозиторий и запушить)

1. Установите [Git for Windows](https://git-scm.com/download/win) и [GitHub CLI](https://cli.github.com/).
2. В терминале в корне проекта:

```powershell
cd E:\ZeroCoder\FamilyTasks
gh auth login
```

3. Создайте репозиторий и отправьте код:

```powershell
.\scripts\first-push-github.ps1 -RepoName FamilyTasks -Visibility public
```

Скрипт сам выставит **локально** для этого репозитория `user.name` / `user.email` из вашего аккаунта GitHub (формат `id+login@users.noreply.github.com`), если глобальная конфигурация Git ещё не задана. При желании задайте глобально один раз:

```powershell
git config --global user.email "you@example.com"
git config --global user.name "Your Name"
```

Для приватного репозитория: `-Visibility private`.

## Вариант B: Репозиторий уже создан на github.com

1. Создайте пустой репозиторий `FamilyTasks` (без README, если уже есть файлы локально).
2. В корне проекта:

```powershell
cd E:\ZeroCoder\FamilyTasks
git init
git add .
git commit -m "Initial commit: Family Tasks bot MVP"
git branch -M main
git remote add origin https://github.com/<ВАШ_ЛОГИН>/FamilyTasks.git
git push -u origin main
```

Для SSH замените URL на `git@github.com:<ВАШ_ЛОГИН>/FamilyTasks.git`.

## После push

- Проверьте, что в [Settings → Secrets → Actions](https://github.com/<USER>/FamilyTasks/settings/secrets/actions) заданы секреты для деплоя (см. [DEPLOY.md](DEPLOY.md)).
