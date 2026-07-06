"""Сборка inline-клавиатур из JSON-описания кнопок BotText/Broadcast."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from services import runtime


def kb_from_json(rows: list | None) -> InlineKeyboardMarkup | None:
    """rows: [[{"text": "...", "url"|"cb"|"start": "..."}]] → клавиатура (или None)."""
    if not rows:
        return None
    keyboard: list[list[InlineKeyboardButton]] = []
    for row in rows:
        btn_row = []
        for b in row:
            text = b.get("text", "")
            if not text:
                continue
            if b.get("url"):
                btn_row.append(InlineKeyboardButton(text=text, url=b["url"]))
            elif b.get("start"):  # deep-link в личку бота (для кнопок в группе)
                btn_row.append(InlineKeyboardButton(text=text, url=runtime.deep_link(b["start"])))
            elif b.get("cb"):
                btn_row.append(InlineKeyboardButton(text=text, callback_data=b["cb"]))
        if btn_row:
            keyboard.append(btn_row)
    return InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Быстрая сборка: kb([("Текст", "callback")], [...])."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=c) for t, c in row] for row in rows
    ])
