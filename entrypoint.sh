#!/bin/sh
# Готовит директорию БД (в т.ч. смонтированный Railway Volume, который приходит owned by root),
# затем дропает привилегии и запускает бота от непривилегированного пользователя app.
set -e

# Путь к БД должен совпадать с config.py:_default_db_path:
# TENRI_DB_PATH / DB_PATH → RAILWAY_VOLUME_MOUNT_PATH (том Railway) → /app/data.
DB_PATH="${TENRI_DB_PATH:-${DB_PATH:-}}"
if [ -z "$DB_PATH" ] && [ -n "$RAILWAY_VOLUME_MOUNT_PATH" ]; then
    DB_PATH="${RAILWAY_VOLUME_MOUNT_PATH%/}/tenribot.db"
fi
DB_PATH="${DB_PATH:-/app/data/tenribot.db}"
DB_DIR="$(dirname "$DB_PATH")"

mkdir -p "$DB_DIR"
# том Railway монтируется от root — отдаём его app, чтобы SQLite (и WAL/SHM) могли писать
chown -R app:app "$DB_DIR" 2>/dev/null || true

exec gosu app "$@"
