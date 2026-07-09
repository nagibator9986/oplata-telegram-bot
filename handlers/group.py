"""Закрытая группа: персональные invite-ссылки, вступления/выходы."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import CallbackQuery, ChatMemberUpdated

from config import get_settings
from db import repo
from db.models import Funnel, Lead, utcnow
from keyboards.common import kb
from services import platform
from services.content import send_slot
from services.funnel import advance
from services.notifier import lead_card, notify_admins

log = logging.getLogger(__name__)
router = Router(name="group")


async def _ensure_invite_link(bot: Bot, lead: Lead) -> tuple[Lead, str]:
    """Персональная одноразовая ссылка (атрибуция по name='lead:{id}'). Fallback — статическая."""
    settings = get_settings()
    if lead.invite_link:
        return lead, lead.invite_link
    try:
        link = await bot.create_chat_invite_link(
            settings.group_id,
            name=f"lead:{lead.id}"[:32],
            member_limit=1,
            # 30 дней: дожим-серия идёт до 7-го дня — ссылка не должна протухнуть раньше
            expire_date=datetime.now(timezone.utc) + timedelta(days=30),
        )
        lead = await repo.update_lead(lead.telegram_id, invite_link=link.invite_link) or lead
        return lead, link.invite_link
    except Exception:
        log.exception("не удалось создать invite-ссылку (бот админ в группе?)")
        static = await repo.get_setting("static_group_link", "")
        if not static:
            await notify_admins(bot, "⚠️ Не могу создать ссылку в группу: проверьте, что бот — "
                                     "админ группы, либо задайте статическую ссылку в ⚙️ Настройках.")
        return lead, static


@router.callback_query(F.data == "go:group")
async def cb_group(call: CallbackQuery, lead: Lead, bot: Bot) -> None:
    # Группа — только для прошедших фильтр масштаба (в группу нельзя всех желающих).
    if lead.funnel_state == Funnel.UNQUALIFIED:
        await call.answer()
        await send_slot(bot, call.message.chat.id, "survey_reject", lead)  # вежливый отказ, без ссылки
        return
    if not lead.qualified:
        await call.answer()
        await send_slot(bot, call.message.chat.id, "group_needs_filter", lead)
        return
    lead, link = await _ensure_invite_link(bot, lead)
    if not link:
        await call.answer("Ссылка временно недоступна, попробуйте позже 🙏", show_alert=True)
        return
    if not lead.invite_link:
        lead = await repo.update_lead(lead.telegram_id, invite_link=link) or lead
    await send_slot(bot, call.message.chat.id, "group_invite", lead)
    await advance(lead, Funnel.INVITED)
    await call.answer()


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_group_join(event: ChatMemberUpdated, bot: Bot) -> None:
    if event.chat.id != get_settings().group_id:
        return
    user = event.new_chat_member.user
    if user.is_bot:
        return
    lead = await repo.upsert_lead(user)  # мог прийти по пересланной ссылке без /start
    lead = await repo.update_lead(lead.telegram_id, in_group=True,
                                  group_joined_at=utcnow()) or lead
    lead = await advance(lead, Funnel.JOINED)

    via = event.invite_link.name if event.invite_link and event.invite_link.name else "общая ссылка"
    await notify_admins(
        bot,
        f"🟢 <b>Вступил в группу</b> (по: {via})\n\n{lead_card(lead)}",
        reply_markup=kb([("👤 Открыть карточку", f"adm:lead:{lead.id}")]),
    )
    platform.sync_lead(lead, "joined_group")
    # приветствие в личку (если человек не открывал бота — просто не доставится)
    await send_slot(bot, user.id, "after_join", lead)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def on_group_leave(event: ChatMemberUpdated, bot: Bot) -> None:
    if event.chat.id != get_settings().group_id:
        return
    user = event.new_chat_member.user
    if user.is_bot:
        return
    lead = await repo.get_lead_by_tg(user.id)
    if lead is None:
        return
    lead = await repo.update_lead(lead.telegram_id, in_group=False,
                                  group_left_at=utcnow()) or lead
    platform.sync_lead(lead, "left_group")
    await notify_admins(bot, f"🔴 <b>Покинул группу</b>\n\n{lead_card(lead)}",
                        reply_markup=kb([("👤 Открыть карточку", f"adm:lead:{lead.id}")]))
