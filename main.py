"""Точка входа tenri-bot: aiogram 3 + SQLite + фоновый планировщик."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand, ErrorEvent

from config import get_settings
from db.base import init_db
from db.models import utcnow
from db.seed import seed_defaults
from handlers import admin_routers, client_routers
from middlewares.lead import LeadMiddleware
from services import runtime
from services.scheduler import scheduler_loop

log = logging.getLogger("tenribot")


def _start_health_server() -> None:
    """Лёгкий health-эндпоинт на $PORT (нужен, если на Railway включён healthcheck/домен).

    Polling-боту порт не обязателен; сервер поднимается только когда PORT задан.
    """
    port = os.environ.get("PORT")
    if not port:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):  # тишина в логах
            pass

    try:
        srv = HTTPServer(("0.0.0.0", int(port)), Handler)
    except OSError as exc:
        log.warning("health-эндпоинт не поднялся на :%s — %s", port, exc)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("health-эндпоинт слушает :%s", port)


async def _maybe_reset_db(settings) -> bool:
    """Одноразовый сброс БД по метке TENRI_RESET_DB.

    Дропает и пересоздаёт все таблицы, если метка задана и отличается от последней
    применённой (хранится в settings._reset_tag). С той же меткой повторно не срабатывает,
    поэтому переменную можно безопасно оставить включённой в Railway. Возвращает True,
    если база была сброшена (тогда вызывающий сидирует заново и запоминает метку).
    """
    from db import repo
    from db.base import Base, engine

    tag = (settings.reset_db_tag or "").strip()
    if not tag:
        return False
    if await repo.get_setting("_reset_tag", "") == tag:
        return False  # этой меткой уже сбрасывали
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    log.critical("⚠️ БАЗА ДАННЫХ ПОЛНОСТЬЮ СБРОШЕНА (TENRI_RESET_DB=%r). Все лиды/тексты/"
                 "рассылки удалены и пересоздаются из сидов. Повторно с этой меткой не сработает.",
                 tag)
    return True


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.admins:
        # Без админов вся админ-панель безхозна (фильтр пропускает по пустому множеству),
        # рассылки/тексты/лиды недоступны — падаем явно, а не тихо стартуем «поломанными».
        raise SystemExit("TENRI_ADMIN_IDS пуст — задайте telegram_id администраторов "
                         "(через запятую), иначе админ-панель недоступна.")

    # Относительный путь = база во ВРЕМЕННОМ слое контейнера: на Railway её стирает
    # КАЖДЫЙ редеплой (лиды, ответы, прогресс анкет). Кричим в лог, чтобы это нельзя
    # было не заметить — прогресс анкет «сбрасывался в начало» именно из-за этого.
    if not os.path.isabs(settings.db_path):
        log.critical(
            "⚠️ TENRI_DB_PATH=%r — ОТНОСИТЕЛЬНЫЙ путь. База лежит во временном слое "
            "контейнера и будет СТЁРТА при следующем редеплое: пропадут все лиды, "
            "ответы и прогресс анкет. Подключите Volume на /data и задайте "
            "TENRI_DB_PATH=/data/tenribot.db (см. docs/DEPLOY_RAILWAY.md).",
            settings.db_path)

    _start_health_server()
    await init_db()
    reset_done = await _maybe_reset_db(settings)
    await seed_defaults()
    if reset_done:
        from db import repo
        await repo.set_setting("_reset_tag", settings.reset_db_tag.strip())

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    runtime.BOT_USERNAME = me.username or ""
    runtime.STARTED_AT = utcnow()
    log.info("бот @%s запускается (группа: %s)", me.username, settings.group_id)

    await bot.set_my_commands([
        BotCommand(command="start", description="Начать / главное меню"),
        BotCommand(command="menu", description="Меню действий"),
        BotCommand(command="help", description="Что умеет бот"),
    ])

    # events_isolation: апдейты одного пользователя обрабатываются строго последовательно —
    # это устраняет весь класс гонок «двойной тап»/read-modify-write (ответы анкеты, счётчики,
    # бюджет ассистента), не влияя на параллелизм между разными пользователями.
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dp.message.middleware(LeadMiddleware())
    dp.callback_query.middleware(LeadMiddleware())

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        """Ни одна ошибка обработчика (в т.ч. TelegramBadRequest от parse HTML) не должна
        ронять поллинг. Логируем и продолжаем."""
        if isinstance(event.exception, TelegramForbiddenError):
            return True  # пользователь заблокировал бота — это ожидаемо, шумом не логируем
        log.exception("ошибка при обработке апдейта: %s", event.exception)
        return True

    for router in admin_routers:   # админ раньше клиента
        dp.include_router(router)
    for router in client_routers:
        dp.include_router(router)

    scheduler_task = asyncio.create_task(scheduler_loop(bot))
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        # chat_member ОБЯЗАТЕЛЕН в allowed_updates — иначе не увидим вступления в группу.
        # start_polling(handle_signals=True по умолчанию) корректно завершится по SIGTERM
        # (redeploy на Railway) — управление вернётся в finally.
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"],
        )
    finally:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
        await bot.session.close()
        log.info("бот остановлен")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(main())
