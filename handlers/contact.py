"""«Задать вопрос»: выбор ассистент/человек + мост клиент → админ."""
from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from db.models import Lead
from keyboards.common import kb
from services.content import send_slot
from services.notifier import notify_admins
from states import ContactStates

router = Router(name="contact")


@router.callback_query(F.data == "go:ask")
async def cb_ask(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.answer(
        "Как вам удобнее?",
        reply_markup=kb([("🤖 Спросить ассистента", "go:assistant")],
                        [("👤 Написать человеку", "go:human")]),
    )


async def start_human_contact(message: Message, state: FSMContext, lead: Lead, bot: Bot) -> None:
    await state.set_state(ContactStates.waiting_message)
    await send_slot(bot, message.chat.id, "human_intro", lead)


@router.callback_query(F.data == "go:human")
async def cb_human(call: CallbackQuery, lead: Lead, bot: Bot, state: FSMContext) -> None:
    await call.answer()
    await start_human_contact(call.message, state, lead, bot)


@router.message(ContactStates.waiting_message, F.text)
async def msg_question(message: Message, lead: Lead, bot: Bot, state: FSMContext) -> None:
    await state.clear()
    await notify_admins(
        bot,
        f"✉️ <b>Вопрос от {escape(lead.display_name)}</b>\n\n{escape(message.text)}",
        reply_markup=kb([("↩️ Ответить", f"reply:{lead.id}")],
                        [("👤 Карточка", f"adm:lead:{lead.id}")]),
    )
    await send_slot(bot, message.chat.id, "human_sent", lead)
