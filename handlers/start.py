"""/start, deep-links, «Не беспокоить», /delete_me."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from db import repo
from db.models import Funnel, Lead
from services import platform
from services.content import send_slot
from services.funnel import advance

router = Router(name="start")

MAIN_MENU_KB_ROWS = (
    [("🧭 Философия Равновесия", "go:phil")],
    [("🔒 Закрытая группа", "go:group")],
    [("📋 Пройти диагностику", "go:survey")],
    [("💬 Задать вопрос", "go:ask")],
)


def main_menu_kb():
    from keyboards.common import kb
    return kb(*MAIN_MENU_KB_ROWS)


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext,
                    lead: Lead, bot: Bot) -> None:
    await state.clear()
    payload = (command.args or "").strip().lower()

    # вернулся после «не беспокоить» — снимаем флаг
    if lead.do_not_disturb:
        lead = await repo.update_lead(lead.telegram_id, do_not_disturb=False) or lead

    # персональная ссылка участника углублённого аудита (токены — lowercase hex)
    if payload.startswith("aud_"):
        from handlers.deep_audit import start_participant
        await start_participant(bot, message.chat.id, lead, state, payload[4:])
        return

    if payload == "survey":
        from handlers.survey import begin_survey
        await begin_survey(bot, message.chat.id, lead, state)
        return
    if payload == "contact":
        from handlers.contact import start_human_contact
        await start_human_contact(message, state, lead, bot)
        return
    if payload == "donate":
        from handlers.donation import send_donation_offer
        await send_donation_offer(bot, lead)
        return

    # метка источника (реклама/кнопка в группе) — только при первом контакте
    if payload and not lead.source:
        lead = await repo.update_lead(lead.telegram_id, source=payload) or lead

    # у пользователя есть незавершённая анкета участника аудита — предлагаем вернуться
    participation = await repo.open_participation(lead.id)
    if participation is not None:
        from keyboards.common import kb
        await message.answer(
            "У вас есть незавершённая анкета аудита компании. Продолжим?",
            reply_markup=kb([("▶️ Продолжить анкету аудита", "da:resume")],
                            [("🏠 Главное меню", "go:menu")]))
        return

    is_new = lead.funnel_state == Funnel.NEW
    await send_slot(bot, message.chat.id, "welcome", lead)
    # LOST-лид вернулся сам → реактивируем воронку (force). UNQUALIFIED оставляем
    # терминальным: иначе снялась бы защита от повторного прохождения фильтра-аудита.
    lead = await advance(lead, Funnel.WELCOMED, force=(lead.funnel_state == Funnel.LOST))
    if is_new:
        platform.sync_lead(lead, "new_lead")


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Выберите, что вам интересно 👇", reply_markup=main_menu_kb())


@router.callback_query(F.data == "go:menu")
async def cb_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.answer("Выберите, что вам интересно 👇", reply_markup=main_menu_kb())
    await call.answer()


@router.message(Command("help"))
async def cmd_help(message: Message, lead: Lead, bot: Bot, state: FSMContext) -> None:
    await state.clear()
    await send_slot(bot, message.chat.id, "help", lead)


@router.callback_query(F.data == "go:later")
async def cb_later(call: CallbackQuery) -> None:
    await call.answer("Хорошо! Я на связи 🙌", show_alert=False)


@router.callback_query(F.data == "dnd")
async def cb_dnd(call: CallbackQuery, lead: Lead, bot: Bot) -> None:
    lead = await repo.update_lead(lead.telegram_id, do_not_disturb=True) or lead
    await advance(lead, Funnel.LOST)
    await send_slot(bot, call.message.chat.id, "dnd_ok", lead)
    await call.answer()


@router.message(Command("delete_me"))
async def cmd_delete_me(message: Message, lead: Lead, state: FSMContext) -> None:
    """Право на забвение: анонимизация лида."""
    await state.clear()
    await repo.update_lead(lead.telegram_id, first_name="", last_name="", username="",
                           phone="", notes="", do_not_disturb=True, funnel_state=Funnel.LOST)
    await message.answer("Ваши данные анонимизированы, напоминаний больше не будет. "
                         "Если вернётесь — просто напишите /start.")
