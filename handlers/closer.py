"""AI-продажник в личке: по кнопке «💬 Обсудить разбор» ведёт продающий диалог и
дожимает квалифицированного лида до заказа полного разбора.

Под каждым ответом — CTA-клавиатура с кнопкой оплаты (её строит код, т.к. вывод
LLM недоверенный, parse_mode=None). Сигнал «готов купить» → мгновенный хэндофф
менеджеру. Персона и тексты редактируются владельцем через админку (BotText).
"""
from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from db import repo
from db.models import Funnel, Lead
from keyboards.common import kb_from_json
from services import closer
from services.content import send_slot
from services.notifier import lead_admin_kb, lead_card, notify_admins
from states import CloserStates

router = Router(name="closer")

# Быстрые «боли» на входе — снимают барьер «не знаю, с чего начать».
CLOSER_QUICK = [
    "Теряю деньги, но не вижу где",
    "Всё держится на мне одном",
    "Рост остановился",
]

# Дедуп уведомлений менеджеру о «горячем» лиде: один раз за активный диалог.
_hot_notified: set[int] = set()


def _cta_kb(lead: Lead):
    """CTA-клавиатура, зависящая от того, прошёл ли лид аудит (до/после)."""
    return closer.cta_keyboard(lead.survey_completed_at is not None)


def _greeting_kb():
    rows = [[{"text": q, "cb": f"clq:{i}"}] for i, q in enumerate(CLOSER_QUICK)]
    rows.append([{"text": "⏹ Не сейчас", "cb": "cl:stop"}])
    return kb_from_json(rows)


async def _respond(bot: Bot, chat_id: int, lead: Lead, user_text: str) -> None:
    answer, hot = await closer.reply(lead, user_text)
    if hot and lead.id not in _hot_notified:
        _hot_notified.add(lead.id)
        await notify_admins(
            bot,
            "🔥 <b>Горячий лид</b> — интересуется ценой / готов к заказу разбора:\n\n"
            + lead_card(lead),
            reply_markup=lead_admin_kb(lead),
        )
    # parse_mode=None: ответ LLM недоверенный (символы </& под HTML → 400 и потеря ответа)
    await bot.send_message(chat_id, answer, reply_markup=_cta_kb(lead),
                           disable_web_page_preview=True, parse_mode=None)


@router.callback_query(F.data == "go:closer")
async def cb_closer(call: CallbackQuery, lead: Lead, bot: Bot, state: FSMContext) -> None:
    await call.answer()
    _hot_notified.discard(lead.id)  # новый диалог — сбрасываем дедуп
    if not await closer.is_enabled():
        # даже без AI не теряем оффер — CTA-кнопка остаётся
        await call.message.answer(closer.FALLBACK, reply_markup=_cta_kb(lead), parse_mode=None)
        return
    await state.set_state(CloserStates.chatting)
    await send_slot(bot, call.message.chat.id, "closer_greeting", lead,
                    extra_kb=_greeting_kb())


@router.callback_query(F.data.startswith("clq:"))
async def cb_quick(call: CallbackQuery, lead: Lead, bot: Bot, state: FSMContext) -> None:
    await call.answer()
    try:
        text = CLOSER_QUICK[int(call.data.split(":")[1])]
    except (ValueError, IndexError):
        return
    if not await closer.is_enabled():
        await call.message.answer(closer.FALLBACK, reply_markup=_cta_kb(lead), parse_mode=None)
        return
    await state.set_state(CloserStates.chatting)
    await call.message.answer(f"<i>Вы: {escape(text)}</i>")
    await bot.send_chat_action(call.message.chat.id, "typing")
    await _respond(bot, call.message.chat.id, lead, text)


@router.message(CloserStates.chatting, F.text)
async def msg_closer(message: Message, lead: Lead, bot: Bot, state: FSMContext) -> None:
    # Тумблер closer_enabled должен действовать и на уже открытые диалоги.
    if not await closer.is_enabled():
        await state.clear()
        await message.answer(closer.FALLBACK, reply_markup=_cta_kb(lead), parse_mode=None)
        return
    await bot.send_chat_action(message.chat.id, "typing")
    await _respond(bot, message.chat.id, lead, message.text.strip())


@router.callback_query(F.data == "cl:stop")
async def cb_stop(call: CallbackQuery, lead: Lead, state: FSMContext) -> None:
    await state.clear()
    _hot_notified.discard(lead.id)
    await call.answer()
    await call.message.answer(
        "Хорошо, не тороплю 🌿 Когда будете готовы обсудить разбор — я на связи. "
        "Просто напишите /start.")


@router.message(StateFilter(None), F.chat.type == "private", F.text)
async def msg_idle(message: Message, lead: Lead, bot: Bot) -> None:
    """Свободный текст лида вне сценария → AI-консультант (разговор «без кнопок»).

    Раньше это сообщение уходило в fallback-меню. Теперь продажник-консультант
    сам отвечает: до анкеты объясняет и ведёт к бесплатному аудиту, после — продаёт
    полный разбор. Отсеянным фильтром (UNQUALIFIED) и при выключенном AI — меню."""
    text = (message.text or "").strip()
    if text.startswith("/"):
        return  # неизвестная команда — молчим (как в fallback)
    if lead.is_blocked:
        return
    # Продажник не для отсеянных фильтром и не для участников чужого аудита (сотрудников):
    # им — обычное меню. Проверку участника делаем последней (короткое замыкание).
    if (not await closer.is_enabled()) or lead.funnel_state == Funnel.UNQUALIFIED \
            or await repo.is_participant(lead.id):
        from handlers.start import main_menu_kb
        await message.answer("Выберите, что вам интересно 👇", reply_markup=main_menu_kb())
        return
    await bot.send_chat_action(message.chat.id, "typing")
    await _respond(bot, message.chat.id, lead, text)
