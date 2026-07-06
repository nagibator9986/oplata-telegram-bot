"""Рантайм-значения, известные после старта (username бота для deep-link'ов),
и трекер фоновых задач."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

BOT_USERNAME: str = ""
STARTED_AT: datetime | None = None   # момент старта процесса (для аптайма в диагностике)
LAST_TICK: datetime | None = None    # последний тик планировщика (жив ли фон)

log = logging.getLogger(__name__)

# Сильные ссылки на fire-and-forget задачи: без них asyncio.create_task() может быть
# собран GC до завершения (см. предупреждение в документации asyncio).
_background_tasks: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    """Запустить корутину в фоне, удержав ссылку и залогировав необработанные ошибки."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.exception("фоновая задача упала", exc_info=t.exception())

    task.add_done_callback(_done)
    return task


def deep_link(payload: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start={payload}"
