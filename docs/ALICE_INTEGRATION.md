# Интеграция с Яндекс.Алисой (MVP)

Документ описывает подключение и настройку интеграции Алисы для команды:

- `отметь выполненной задачу ...`

В MVP используется одноразовая привязка пользователя Алисы к участнику семьи из Telegram-бота.

## 1) Что уже реализовано в коде

- Таблицы привязки и кодов:
  - `alice_user_links`
  - `alice_link_codes`
- Telegram-side:
  - кнопка `Код для Алисы` в меню `Прочее`
  - команда `/alice_link`
- Webhook endpoint в приложении:
  - ответ в формате Алисы (`version`, `response.text`, `end_session`)
  - логика привязки по коду
  - логика отметки выполнения задачи

## 2) Настройка `.env`

Минимальный набор:

```env
BOT_TOKEN=...
LOG_LEVEL=INFO
ALICE_WEBHOOK_ENABLED=true
ALICE_WEBHOOK_HOST=0.0.0.0
ALICE_WEBHOOK_PORT=8080
ALICE_WEBHOOK_PATH=/alice/webhook
```

Примечания:

- `ALICE_WEBHOOK_ENABLED=true` обязательно, иначе endpoint не поднимется.
- `ALICE_WEBHOOK_PATH` должен совпадать с путем, который вы укажете в настройках навыка.
- При Docker-развертывании обычно слушаем `0.0.0.0:8080` внутри контейнера.

## 3) Публичный HTTPS URL для webhook

Алиса требует публичный HTTPS endpoint.

Рекомендуемая схема:

1. Поднять бот как есть (polling + встроенный webhook сервер).
2. Перед ботом поставить reverse proxy (Nginx/Caddy/Traefik).
3. Выдать TLS-сертификат (Let's Encrypt).
4. Проксировать `https://<your-domain>/alice/webhook` на `http://127.0.0.1:8080/alice/webhook`.

Пример для Nginx:

```nginx
server {
    listen 443 ssl;
    server_name bot.example.com;

    ssl_certificate /etc/letsencrypt/live/bot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bot.example.com/privkey.pem;

    location /alice/webhook {
        proxy_pass http://127.0.0.1:8080/alice/webhook;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## 4) Настройка навыка Алисы

В кабинете навыка:

1. Укажите webhook URL:
   - `https://bot.example.com/alice/webhook`
2. Проверьте, что навык шлет JSON в ваш endpoint.
3. Опубликуйте/активируйте навык в нужном окружении (test/prod).

Важно: путь и домен должны совпадать с тем, что реально доступно снаружи.

## 5) Сценарий привязки пользователя

1. Пользователь открывает Telegram-бот:
   - `Прочее` -> `Код для Алисы` или `/alice_link`.
2. Бот выдает одноразовый код (TTL 10 минут).
3. Пользователь в Алисе произносит/вводит этот код.
4. Алиса подтверждает привязку.
5. Далее пользователь может говорить:
   - `отметь выполненной задачу ...`

## 6) Команда "добавить выполненную" в Алисе

Логика MVP:

- Если привязки нет -> запросить код привязки.
- Если задача не найдена -> попросить уточнить название.
- Если совпало несколько задач -> попросить уточнить (вывести 2-3 варианта).
- Если найдена ровно одна задача -> создать ручное выполнение и отправить уведомление семье.

## 7) Быстрый чек-лист деплоя

- [ ] В `.env` включен `ALICE_WEBHOOK_ENABLED=true`.
- [ ] Бот запущен и слушает `ALICE_WEBHOOK_HOST:ALICE_WEBHOOK_PORT`.
- [ ] Настроен reverse proxy с TLS (Let's Encrypt).
- [ ] Публичный URL `https://.../alice/webhook` доступен извне.
- [ ] В настройках навыка указан этот URL.
- [ ] Проверен end-to-end сценарий:
  - [ ] получить код в Telegram
  - [ ] привязать в Алисе
  - [ ] отметить задачу выполненной голосом
  - [ ] увидеть completion в истории/уведомлениях

## 8) Диагностика

Если не работает:

- Проверьте логи контейнера/процесса бота.
- Убедитесь, что `ALICE_WEBHOOK_ENABLED=true`.
- Проверьте совпадение `ALICE_WEBHOOK_PATH` и пути в навыке.
- Проверьте, что TLS-сертификат валидный и не self-signed.
- Убедитесь, что код привязки не просрочен и не использован ранее.
