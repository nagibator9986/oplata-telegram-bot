# Деплой tenri-bot на Railway

Бот работает в режиме **long polling** — публичный домен и вебхук не нужны. Единственное
обязательное условие продакшена — **Volume для SQLite**, иначе база (лиды, анкеты, тексты,
рассылки) сотрётся при каждом redeploy.

## 1. Создать проект
1. https://railway.app → **New Project** → **Deploy from GitHub repo** (или `railway init` в CLI),
   выбрать репозиторий с ботом. Railway сам увидит `Dockerfile` и `railway.json`.

## 2. Подключить Volume (ОБЯЗАТЕЛЬНО)
1. В сервисе → вкладка **Variables/Settings** → **Volumes** → **New Volume**.
2. Mount path: **`/data`**.
3. Добавить переменную `TENRI_DB_PATH=/data/tenribot.db`.

> Без тома контейнер пишет БД в эфемерный слой, который очищается на каждом деплое.
> `entrypoint.sh` сам создаёт `/data` и выдаёт права процессу — доп. действий не нужно.

## 3. Переменные окружения
Обязательные:
```
TENRI_BOT_TOKEN=<токен @BotFather>
TENRI_GROUP_ID=-100xxxxxxxxxx
TENRI_ADMIN_IDS=111111111,222222222
TENRI_DB_PATH=/data/tenribot.db
```
Рекомендуемые:
```
TENRI_TIMEZONE=Asia/Almaty
TENRI_GEMINI_API_KEY=<ключ aistudio.google.com/apikey>
```
Остальные — по `.env.example` (все опциональны, есть дефолты).

## 4. Деплой
Railway соберёт образ по `Dockerfile` и запустит `python main.py`. В логах должно появиться:
```
бот @<username> запускается (группа: -100…)
планировщик запущен
```

## Что уже настроено в репозитории
- **`railway.json`** — сборка из Dockerfile, `numReplicas: 1` (критично: два polling-инстанса
  дают `409 Conflict` в Telegram), рестарт при падении, App Sleep выключен.
- **`Dockerfile`** — ставит `tzdata` (иначе `ZoneInfo` падает в slim-образе), запускается от
  непривилегированного пользователя, БД-директория готовится в `entrypoint.sh`.
- **health-эндпоинт** — поднимается на `$PORT`, если Railway его задаёт (для healthcheck/домена);
  polling-боту порт не требуется.
- **graceful shutdown** — по SIGTERM (redeploy) polling и планировщик останавливаются штатно.

## Обновление
`git push` в отслеживаемую ветку → Railway пересоберёт и передеплоит. Данные в Volume сохраняются.
Резервная копия: в боте `/admin → 💾 Бэкап БД` пришлёт файл SQLite в чат.

## Частые проблемы
| Симптом | Причина / решение |
|---|---|
| Данные пропадают после деплоя | Не подключён Volume или `TENRI_DB_PATH` не на `/data` |
| `TelegramConflictError: terminated by other getUpdates` | Запущено >1 реплики или второй инстанс бота с тем же токеном; держите `numReplicas: 1` |
| `ZoneInfoNotFoundError` | Старый образ без `tzdata` — пересоберите с текущим Dockerfile |
| Бот молчит, в логах `Unauthorized` | Неверный `TENRI_BOT_TOKEN` |
| Не видит вступления в группу | Бот не админ группы, либо нет `chat_member` в allowed_updates (уже включён) |
