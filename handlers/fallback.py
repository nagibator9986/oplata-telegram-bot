"""Fallback: сообщение вне сценария → быстрое меню (только личка, вне FSM-состояний)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import Message

from handlers.start import main_menu_kb

router = Router(name="fallback")


@router.message(StateFilter(None), F.chat.type == "private")
async def any_message(message: Message) -> None:
    if message.text and message.text.startswith("/"):
        return  # неизвестная команда — молчим
    await message.answer("Выберите, что вам интересно 👇", reply_markup=main_menu_kb())
