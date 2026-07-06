"""Донаты на развитие проекта: оффер, реквизиты, подтверждение перевода."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from db import repo
from db.models import Funnel, Lead, utcnow
from services import platform
from services.content import send_slot
from services.funnel import advance
from services.notifier import lead_card, notify_admins

router = Router(name="donation")

PAID_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Я перевёл(а)", callback_data="don:paid")]])


async def _send_requisites(bot: Bot, chat_id: int, lead: Lead) -> None:
    """Реквизиты: приоритет — с сайта (единый источник с лендингом), fallback — локальный слот."""
    site_text = await platform.fetch_donation_requisites()
    if site_text:
        await bot.send_message(chat_id, site_text, reply_markup=PAID_KB)
    else:
        await send_slot(bot, chat_id, "donation_requisites", lead, extra_kb=PAID_KB)


async def _offer_kb() -> InlineKeyboardMarkup:
    presets = [p.strip() for p in
               (await repo.get_setting("donation_presets", "5000,10000,25000")).split(",") if p.strip()]
    rows = [[InlineKeyboardButton(text=f"{p} ₸", callback_data=f"don:amt:{p}") for p in presets[:3]]]
    rows.append([InlineKeyboardButton(text="💳 Реквизиты", callback_data="don:req")])
    rows.append([InlineKeyboardButton(text="✅ Я перевёл(а)", callback_data="don:paid")])
    rows.append([InlineKeyboardButton(text="Не сейчас", callback_data="don:later")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_donation_offer(bot: Bot, lead: Lead) -> None:
    ok = await send_slot(bot, lead.telegram_id, "donation_offer", lead, extra_kb=await _offer_kb())
    if ok:
        await advance(lead, Funnel.DON_OFFERED, donation_offered_at=utcnow())
        await repo.log_message(lead_id=lead.id, kind="donation", status="offered")


@router.callback_query(F.data.startswith("don:amt:"))
async def cb_amount(call: CallbackQuery, lead: Lead, bot: Bot) -> None:
    amount = call.data.split(":")[2]
    await call.answer()
    await call.message.answer(f"Сумма: <b>{amount} ₸</b> 🙏")
    await _send_requisites(bot, call.message.chat.id, lead)


@router.callback_query(F.data == "don:req")
async def cb_requisites(call: CallbackQuery, lead: Lead, bot: Bot) -> None:
    await call.answer()
    await _send_requisites(bot, call.message.chat.id, lead)


@router.callback_query(F.data == "don:paid")
async def cb_paid(call: CallbackQuery, lead: Lead, bot: Bot) -> None:
    lead = await advance(lead, Funnel.DONATED, donated_at=utcnow())
    platform.sync_lead(lead, "donated")
    await call.answer()
    await send_slot(bot, call.message.chat.id, "donation_thanks", lead)
    await notify_admins(bot, f"💚 <b>Донат! Клиент нажал «Я перевёл(а)»</b> — проверьте поступление.\n\n"
                             f"{lead_card(lead)}")


@router.callback_query(F.data == "don:later")
async def cb_don_later(call: CallbackQuery) -> None:
    await call.answer("Хорошо! Спасибо, что вы с нами 🌿", show_alert=False)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
