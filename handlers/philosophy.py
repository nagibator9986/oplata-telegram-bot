"""Карусель философии Тенри-Равновесия (слоты philosophy_1..N)."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from db import repo
from db.models import Funnel, Lead
from services.content import send_slot
from services.funnel import advance

router = Router(name="philosophy")


@router.callback_query(F.data == "go:phil")
async def cb_philosophy(call: CallbackQuery, lead: Lead, bot: Bot) -> None:
    await send_slot(bot, call.message.chat.id, "philosophy_1", lead)
    await advance(lead, Funnel.PHILOSOPHY)
    await call.answer()


@router.callback_query(F.data.startswith("ph:"))
async def cb_philosophy_next(call: CallbackQuery, lead: Lead, bot: Bot) -> None:
    try:
        n = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    if await repo.get_text(f"philosophy_{n}"):
        await send_slot(bot, call.message.chat.id, f"philosophy_{n}", lead)
    await call.answer()
