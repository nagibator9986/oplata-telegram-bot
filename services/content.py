"""Рендер и отправка контент-слотов (BotText) с плейсхолдерами и медиа."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

from db import repo
from db.models import BotText, Lead
from keyboards.common import kb_from_json

log = logging.getLogger(__name__)

TG_TEXT_LIMIT = 4096
TG_CAPTION_LIMIT = 1024


async def _send_text_chunks(bot: Bot, chat_id: int, text: str, markup) -> None:
    """Отправить текст, разбив по лимиту 4096; клавиатура — на последнем сообщении."""
    from services.notifier import split_long
    parts = split_long(text, TG_TEXT_LIMIT - 200)
    for i, part in enumerate(parts):
        await bot.send_message(chat_id, part,
                               reply_markup=markup if i == len(parts) - 1 else None,
                               disable_web_page_preview=True)


def render_text(bt: BotText, lead: Lead | None) -> str:
    out = bt.text
    if lead is not None:
        # first_name — из профиля Telegram (может содержать «<»/«&»); экранируем под HTML
        out = out.replace("{name}", escape(lead.first_name or "друг"))
        out = out.replace("{group_link}", lead.invite_link or "{group_link}")
    return out


async def send_slot(bot: Bot, chat_id: int, key: str, lead: Lead | None = None,
                    extra_kb=None) -> bool:
    """Отправить слот контента. Возвращает False, если пользователь заблокировал бота."""
    bt = await repo.get_text(key)
    if bt is None:
        log.warning("BotText '%s' не найден", key)
        return True
    text = render_text(bt, lead)
    if "{group_link}" in text:  # у лида нет персональной ссылки — запасная статическая
        static = await repo.get_setting("static_group_link", "")
        text = text.replace("{group_link}", static or "(ссылка в группу появится чуть позже — "
                                                       "напишите /start)")
    markup = extra_kb or kb_from_json(bt.buttons)
    try:
        if bt.media_file_id:
            # подпись к фото ограничена 1024: длинный текст шлём отдельным сообщением
            if len(text) <= TG_CAPTION_LIMIT:
                await bot.send_photo(chat_id, bt.media_file_id, caption=text, reply_markup=markup)
            else:
                await bot.send_photo(chat_id, bt.media_file_id)
                await _send_text_chunks(bot, chat_id, text, markup)
        else:
            await _send_text_chunks(bot, chat_id, text, markup)
        return True
    except TelegramForbiddenError:
        if lead is not None:
            await repo.update_lead(lead.telegram_id, is_blocked=True)
        return False
    except Exception:
        log.exception("send_slot('%s') → chat %s failed", key, chat_id)
        return True
