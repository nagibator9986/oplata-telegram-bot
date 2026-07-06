"""Уведомления админам в личку + форматирование карточек."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot

from config import get_settings
from db.models import Funnel, Lead, SurveyResponse
from keyboards.common import kb

log = logging.getLogger(__name__)


async def notify_admins(bot: Bot, text: str, reply_markup=None) -> None:
    for admin_id in get_settings().admins:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup,
                                   disable_web_page_preview=True)
        except Exception:
            log.exception("не доставлено админу %s", admin_id)


def split_long(text: str, limit: int = 3800) -> list[str]:
    """Разбить текст на части ≤ limit по границам строк (лимит Telegram — 4096)."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # одна строка длиннее лимита — режем жёстко
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


async def send_long(bot: Bot, chat_id: int, text: str, reply_markup=None) -> None:
    """Отправить длинный текст частями; клавиатура — на последней части."""
    parts = split_long(text)
    for i, part in enumerate(parts):
        await bot.send_message(chat_id, part,
                               reply_markup=reply_markup if i == len(parts) - 1 else None,
                               disable_web_page_preview=True)


async def notify_admins_long(bot: Bot, text: str, reply_markup=None) -> None:
    """Уведомление админам с разбивкой длинного текста (карточки анкет из 30+ ответов)."""
    for admin_id in get_settings().admins:
        try:
            await send_long(bot, admin_id, text, reply_markup=reply_markup)
        except Exception:
            log.exception("не доставлено админу %s", admin_id)


def lead_card(lead: Lead) -> str:
    lines = [
        f"👤 <b>{escape(lead.display_name)}</b>",
        f"id: <code>{lead.telegram_id}</code>",
        f"Телефон: {escape(lead.phone) or '—'}",
        f"Источник: {escape(lead.source) or '—'}",
        f"Воронка: {Funnel.LABELS.get(lead.funnel_state, lead.funnel_state)}",
        f"В группе: {'да' if lead.in_group else 'нет'}",
        f"Донат: {'да' if lead.donated_at else 'нет'}",
    ]
    if lead.company_name:
        lines.insert(1, f"Компания: {escape(lead.company_name)}")
    if lead.deep_audit:
        lines.append("Аудит: 🔬 углублённый")
    if lead.notes:
        lines.append(f"Заметки: {escape(lead.notes)}")
    return "\n".join(lines)


def lead_admin_kb(lead: Lead):
    rows = [
        [("📋 Ответы анкеты", f"adm:lead:answers:{lead.id}")],
        [("✉️ Написать", f"adm:lead:msg:{lead.id}"), ("📝 Заметка", f"adm:lead:note:{lead.id}")],
        [("🚫 Пометить lost", f"adm:lead:lost:{lead.id}")],
    ]
    if lead.deep_audit:
        rows.insert(1, [("🔬 Аудит команды", f"adm:aud:{lead.id}")])
    return kb(*rows)


def response_card(lead: Lead, resp: SurveyResponse) -> str:
    lines = [f"📋 <b>Анкета: {escape(lead.display_name)}</b>",
             f"Телефон: {escape(lead.phone) or '—'}", ""]
    for i, entry in enumerate(resp.answers or [], start=1):
        # номер в <b>, текст вопроса — вне тега: q.text бывает многострочным, а split_long
        # режет по \n и разорвал бы <b>…</b> (Telegram отверг бы сообщение с 400)
        lines.append(f"<b>{i}.</b> {escape(str(entry.get('q', '')))}")
        lines.append(escape(str(entry.get("a", "—"))))
    return "\n".join(lines)
