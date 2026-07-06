"""Режим AI-ассистента: объясняет философию и методологию в переписке.

UX: быстрые вопросы кнопками при входе, под каждым ответом — «Завершить» и
«Написать человеку» (бесшовный хэндофф на живого админа).
"""
from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from db.models import Lead
from keyboards.common import kb
from services import assistant as ai
from services.content import send_slot
from states import AssistantStates

router = Router(name="assistant")

# Быстрые вопросы — снимают барьер «не знаю, что спросить»
QUICK_QS = [
    "Что такое Тенри-Равновесие?",
    "Как проходит разбор компании?",
    "Чем вы отличаетесь от консалтинга?",
]

ANSWER_KB = kb([("⏹ Завершить", "as:stop"), ("👤 Написать человеку", "go:human")])


def _greeting_kb():
    rows = [[(q, f"asq:{i}")] for i, q in enumerate(QUICK_QS)]
    rows.append([("⏹ Завершить диалог", "as:stop")])
    return kb(*rows)


@router.callback_query(F.data == "go:assistant")
async def cb_assistant(call: CallbackQuery, lead: Lead, bot: Bot, state: FSMContext) -> None:
    await call.answer()
    if not await ai.is_enabled():
        await call.message.answer("Ассистент сейчас выключен. Нажмите «Задать вопрос» → "
                                  "«Написать человеку» — команда ответит лично.")
        return
    await state.set_state(AssistantStates.chatting)
    await send_slot(bot, call.message.chat.id, "assistant_greeting", lead,
                    extra_kb=_greeting_kb())


@router.callback_query(F.data.startswith("asq:"))
async def cb_quick_question(call: CallbackQuery, lead: Lead, bot: Bot, state: FSMContext) -> None:
    await call.answer()
    try:
        question = QUICK_QS[int(call.data.split(":")[1])]
    except (ValueError, IndexError):
        return
    if not await ai.is_enabled():
        await call.message.answer("Ассистент сейчас выключен.")
        return
    await state.set_state(AssistantStates.chatting)
    await call.message.answer(f"<i>Вы: {escape(question)}</i>")
    await bot.send_chat_action(call.message.chat.id, "typing")
    answer = await ai.reply(lead, question)
    # parse_mode=None: ответ LLM недоверенный, символы </& под HTML дали бы 400 и потерю ответа
    await call.message.answer(answer, reply_markup=ANSWER_KB, disable_web_page_preview=True,
                              parse_mode=None)


@router.callback_query(F.data == "as:stop")
async def cb_stop(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await call.message.answer("Диалог с ассистентом завершён. Я всегда рядом — /start 🌿")


@router.message(AssistantStates.chatting, F.text)
async def msg_chat(message: Message, lead: Lead, bot: Bot) -> None:
    await bot.send_chat_action(message.chat.id, "typing")
    answer = await ai.reply(lead, message.text.strip())
    # parse_mode=None: ответ LLM недоверенный (символы </& под HTML → 400 и потеря ответа)
    await message.answer(answer, reply_markup=ANSWER_KB, disable_web_page_preview=True,
                         parse_mode=None)
