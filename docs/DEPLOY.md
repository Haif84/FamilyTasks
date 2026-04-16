# Развёртывание на VPS (Docker + GitHub Actions)

Краткий путь: репозиторий на GitHub → CI на каждый PR/push → CD по SSH на VPS с `docker compose`.

## 1. Требования на VPS

- Ubuntu 22.04+ (или другой Linux с Docker).
- [Docker Engine](https://docs.docker.com/engine/install/) и Docker Compose v2 plugin (`docker compose`).
- Открытый SSH (порт 22 или свой).
- Git установлен на сервере.

Пример установки Docker на Ubuntu:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
# перелогиньтесь, затем:
docker compose version
```

## 2. Первичная настройка на сервере

1. Создайте пользователя для деплоя (рекомендуется не `root`):

```bash
sudo adduser deploy
sudo usermod -aG docker deploy
```

2. Клонируйте репозиторий (HTTPS или SSH-ключ для чтения GitHub):

```bash
sudo mkdir -p /opt/family-tasks && sudo chown deploy:deploy /opt/family-tasks
cd /opt/family-tasks
git clone https://github.com/<ORG_OR_USER>/FamilyTasks.git .
# VPS_DEPLOY_PATH тогда: /opt/family-tasks
```

3. Создайте файл окружения **только на сервере** (не коммитьте):

```bash
cp .env.example .env
nano .env   # BOT_TOKEN, при необходимости DB_PATH, LOG_LEVEL
```

Для контейнера по умолчанию `DB_PATH=/app/data/family_tasks.sqlite3` (см. `docker-compose.yml`); база попадёт в Docker volume `bot_data`.

4. Первый запуск вручную:

```bash
docker compose up -d --build
docker compose logs -f bot
```

## 3. SSH-ключ для GitHub Actions

На **локальной машине** или на VPS сгенерируйте пару ключей **только для деплоя**:

```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ./gha_deploy_key -N ""
```

- Содержимое `gha_deploy_key` → секрет репозитория **`VPS_SSH_PRIVATE_KEY`** (весь PEM, включая `BEGIN`/`END`).
- Строку из `gha_deploy_key.pub` добавьте в `~/.ssh/authorized_keys` пользователя на VPS (того же, что в **`VPS_USER`**).

Проверка с вашего ПК:

```bash
ssh -i ./gha_deploy_key <VPS_USER>@<VPS_HOST> "echo ok"
```

## 4. Секреты GitHub (Settings → Secrets and variables → Actions)

| Секрет | Описание |
|--------|----------|
| `VPS_HOST` | IP или DNS VPS |
| `VPS_USER` | SSH-пользователь (например `deploy`) |
| `VPS_SSH_PRIVATE_KEY` | Приватный ключ для деплоя |
| `VPS_DEPLOY_PATH` | Абсолютный путь к **корню** клона репозитория на сервере (например `/opt/family-tasks`, если клонировали в эту папку) |

Опционально: в GitHub создайте environment **`production`** с required reviewers и раскомментируйте `environment: production` в [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) — тогда job `deploy` будет ждать ручного подтверждения.

## 5. CI и CD

### CI (`.github/workflows/ci.yml`)

Запускается на `push` и `pull_request` в ветки `main` / `master`: установка зависимостей и `pytest`.

### CD (`.github/workflows/deploy.yml`)

Триггеры:

- **Ручной запуск** (Actions → Deploy to VPS → Run workflow), поле `ref` — ветка или тег.
- **Push тега** вида `v*` (например `v1.0.0`).

На сервере workflow выполняет:

1. `cd $VPS_DEPLOY_PATH`
2. `git fetch`, checkout на указанный ref (или `main`)
3. `docker compose build --pull` и `docker compose up -d`

Убедитесь, что пользователь из `VPS_USER` может выполнять `docker compose` в каталоге деплоя (группа `docker` и права на каталог).

## 5.1 Деплой с локального ПК (`.env` уже в папке проекта)

Скрипт [`scripts/deploy-vps.ps1`](../scripts/deploy-vps.ps1) копирует **локальный** `.env` на сервер и выполняет `git pull` + `docker compose`.

Первый раз на VPS создайте каталог и клонируйте репозиторий (подставьте свой URL и путь):

```bash
ssh root@YOUR_VPS_IP "mkdir -p /opt/family-tasks && git clone https://github.com/YOUR_USER/FamilyTasks.git /opt/family-tasks/FamilyTasks"
```

Дальше с **Windows** из корня проекта (где лежит `.env`):

```powershell
cd E:\ZeroCoder\FamilyTasks
.\scripts\deploy-vps.ps1 -VpsHost YOUR_VPS_IP -User root -RemotePath /opt/family-tasks/FamilyTasks
```

Потребуется ввод пароля или настроенный SSH‑ключ. Токен бота только в `.env`, в репозиторий не коммитьте.

## 6. Обновление бота после релиза

Локально:

```bash
git tag v1.0.1
git push origin v1.0.1
```

Либо через **Run workflow** с нужной веткой.

## 7. Полезные команды на VPS

```bash
cd <VPS_DEPLOY_PATH>
docker compose logs -f --tail=200 bot
docker compose restart bot
docker compose down
```

## 8. Резервное копирование SQLite

Том `bot_data` хранит файл БД. Бэкап:

```bash
docker run --rm -v family-tasks_bot_data:/data -v $(pwd):/backup alpine \
  tar czf /backup/bot-data-$(date +%F).tgz -C /data .
```

Имя тома может отличаться — смотрите `docker volume ls`.
