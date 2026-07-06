"""Апсерт лида на каждый апдейт из лички + прокидывание lead в хендлеры."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from db import repo


class LeadMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        if user and not user.is_bot and (chat is None or chat.type == "private"):
            data["lead"] = await repo.upsert_lead(user)
        return await handler(event, data)
