#!/usr/bin/env bash
# Создаёт пользователя для деплоя бота на Linux (Ubuntu/Debian и аналоги).
# Запуск на VPS от root:
#   curl -fsSL ... | sudo bash
# или из корня репозитория:
#   sudo bash scripts/setup-deploy-user.sh
#
# Переменные окружения (опционально):
#   DEPLOY_USER       — имя пользователя (по умолчанию: deploy)
#   DEPLOY_REPO_PATH  — каталог для клона репозитория (создаётся, владелец — DEPLOY_USER)
#   DEPLOY_PUBKEY     — одна строка публичного SSH-ключа (добавится в authorized_keys)

set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-deploy}"
DEPLOY_REPO_PATH="${DEPLOY_REPO_PATH:-}"
DEPLOY_PUBKEY="${DEPLOY_PUBKEY:-}"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Запустите от root: sudo bash $0" >&2
  exit 1
fi

if id "$DEPLOY_USER" &>/dev/null; then
  echo "Пользователь '$DEPLOY_USER' уже существует."
else
  useradd --create-home --shell /bin/bash "$DEPLOY_USER"
  echo "Создан пользователь '$DEPLOY_USER' с домашним каталогом."
fi

if getent group docker >/dev/null 2>&1; then
  usermod -aG docker "$DEPLOY_USER"
  echo "Пользователь '$DEPLOY_USER' добавлен в группу 'docker'."
else
  echo "Внимание: группа 'docker' не найдена. Установите Docker Engine, затем выполните:" >&2
  echo "  sudo usermod -aG docker $DEPLOY_USER" >&2
fi

if [[ -n "${DEPLOY_REPO_PATH}" ]]; then
  mkdir -p "${DEPLOY_REPO_PATH}"
  chown "${DEPLOY_USER}:${DEPLOY_USER}" "${DEPLOY_REPO_PATH}"
  echo "Каталог ${DEPLOY_REPO_PATH} создан, владелец: ${DEPLOY_USER}."
fi

if [[ -n "${DEPLOY_PUBKEY}" ]]; then
  uhome=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
  install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "${uhome}/.ssh"
  auth="${uhome}/.ssh/authorized_keys"
  if [[ -f "${auth}" ]] && grep -Fxq "${DEPLOY_PUBKEY}" "${auth}" 2>/dev/null; then
    echo "SSH-ключ уже есть в ${auth}."
  else
    printf '%s\n' "${DEPLOY_PUBKEY}" >> "${auth}"
    chown "${DEPLOY_USER}:${DEPLOY_USER}" "${auth}"
    chmod 600 "${auth}"
    echo "Публичный ключ добавлен в ${auth}."
  fi
fi

echo ""
echo "Дальше:"
echo "  1) Перезайдите под ${DEPLOY_USER} или выполните: newgrp docker"
echo "     (иначе docker compose может требовать sudo)."
echo "  2) Клонируйте репозиторий в DEPLOY_REPO_PATH или другой каталог от имени ${DEPLOY_USER}."
echo "  3) Скопируйте .env на сервер и запустите: docker compose up -d --build"
echo "     (см. docs/DEPLOY.md)."
