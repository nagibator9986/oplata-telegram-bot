"""Лиды: поиск, карточка, ответы анкеты, сообщение лиду, заметки, ответы на вопросы."""
from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db import repo
from db.models import Funnel
from keyboards.common import kb
from services.funnel import advance
from services.notifier import lead_admin_kb, lead_card, response_card
from states import AdminReply, LeadAdmin

router = Router(name="admin_leads")


@router.callback_query(F.data == "adm:leads")
async def cb_leads(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(LeadAdmin.searching)
    await call.message.answer(
        "Пришлите имя, @username или телефон для поиска.\n"
        "Или отправьте <code>last</code> — последние 10 лидов.",
        reply_markup=kb([("📤 Экспорт всех в CSV", "adm:leads:export")],
                        [("🏠 Меню", "adm:menu")]))
    await call.answer()


@router.callback_query(F.data == "adm:leads:export")
async def cb_export(call: CallbackQuery) -> None:
    """Выгрузка всех лидов в CSV (Excel-совместимый: ; и utf-8-sig)."""
    import csv
    import io

    from aiogram.types import BufferedInputFile
    from db.models import Funnel as Fn

    leads = await repo.all_leads()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "telegram_id", "username", "имя", "фамилия", "телефон",
                "компания", "этап воронки", "источник", "в группе", "анкета",
                "углублённый аудит", "донат", "заблокировал", "не беспокоить",
                "создан", "заметки"])
    for l in leads:
        w.writerow([
            l.id, l.telegram_id, l.username, l.first_name, l.last_name, l.phone,
            l.company_name, Fn.LABELS.get(l.funnel_state, l.funnel_state), l.source,
            "да" if l.in_group else "нет",
            l.survey_completed_at.strftime("%d.%m.%Y") if l.survey_completed_at else "",
            "да" if l.deep_audit else "",
            l.donated_at.strftime("%d.%m.%Y") if l.donated_at else "",
            "да" if l.is_blocked else "", "да" if l.do_not_disturb else "",
            l.created_at.strftime("%d.%m.%Y %H:%M"), l.notes.replace("\n", " "),
        ])
    data = buf.getvalue().encode("utf-8-sig")
    await call.answer()
    await call.message.answer_document(
        BufferedInputFile(data, filename="tenri-leads.csv"),
        caption=f"📤 Экспорт: {len(leads)} лидов")


@router.message(LeadAdmin.searching)
async def msg_search(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    leads = await (repo.recent_leads() if query.lower() == "last" else repo.search_leads(query))
    if not leads:
        await message.answer("Никого не нашёл. Попробуйте иначе (или <code>last</code>).")
        return
    await state.clear()
    rows = [[InlineKeyboardButton(
        text=f"{l.display_name} · {Funnel.LABELS.get(l.funnel_state, '')}",
        callback_data=f"adm:lead:{l.id}")] for l in leads]
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="adm:menu")])
    await message.answer("Результаты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^adm:lead:\d+$"))
async def cb_lead_card(call: CallbackQuery) -> None:
    lead = await repo.get_lead(int(call.data.split(":")[2]))
    await call.answer()
    if lead is None:
        return
    await call.message.answer(lead_card(lead), reply_markup=lead_admin_kb(lead))


@router.callback_query(F.data.startswith("adm:lead:answers:"))
async def cb_answers(call: CallbackQuery, bot: Bot) -> None:
    from services.notifier import send_long

    lead = await repo.get_lead(int(call.data.split(":")[3]))
    await call.answer()
    if lead is None:
        return
    resp = await repo.latest_completed_response(lead.id) or await repo.get_open_response(lead.id)
    if resp is None or not resp.answers:
        await call.message.answer("Анкета ещё не начата.")
        return
    suffix = "" if resp.status == "completed" else "\n\n<i>⚠️ Анкета не завершена.</i>"
    # Карточка из 42 вопросов превышает лимит Telegram 4096 — шлём частями
    await send_long(bot, call.message.chat.id, response_card(lead, resp) + suffix)


@router.callback_query(F.data.startswith("adm:lead:msg:"))
async def cb_write(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(LeadAdmin.writing)
    await state.update_data(lead_id=int(call.data.split(":")[3]))
    await call.message.answer("Пришлите текст — я отправлю его лиду от имени бота.")
    await call.answer()


@router.message(LeadAdmin.writing)
async def msg_write(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    lead = await repo.get_lead(data["lead_id"])
    if lead is None:
        return
    try:
        await bot.send_message(lead.telegram_id,
                               f"✉️ <b>Сообщение от команды:</b>\n\n{escape(message.text or '')}")
        await message.answer("Доставлено ✅")
    except Exception:
        await message.answer("⚠️ Не доставлено (лид заблокировал бота?)")


@router.callback_query(F.data.startswith("adm:lead:note:"))
async def cb_note(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(LeadAdmin.noting)
    await state.update_data(lead_id=int(call.data.split(":")[3]))
    await call.message.answer("Пришлите текст заметки:")
    await call.answer()


@router.message(LeadAdmin.noting)
async def msg_note(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    lead = await repo.get_lead(data["lead_id"])
    if lead is None:
        return
    note = (lead.notes + "\n" if lead.notes else "") + (message.text or "")
    await repo.update_lead(lead.telegram_id, notes=note.strip())
    await message.answer("Заметка сохранена 📝")


@router.callback_query(F.data.startswith("adm:lead:lost:"))
async def cb_lost(call: CallbackQuery) -> None:
    lead = await repo.get_lead(int(call.data.split(":")[3]))
    if lead:
        await advance(lead, Funnel.LOST)
    await call.answer("Помечен как lost 🚫", show_alert=True)


# --- Углублённый аудит: панель команды лида ---

async def _audit_panel(message: Message, lead_id: int) -> None:
    from handlers.deep_audit import TEAM_LINKS_MAX, links_block

    lead = await repo.get_lead(lead_id)
    if lead is None:
        return
    parts = await repo.participants_for(lead_id)
    done = sum(1 for p in parts if p.status == "completed")
    header = (f"🔬 <b>Аудит команды: {escape(lead.company_name or lead.display_name)}</b>\n"
              f"Завершено анкет: <b>{done} из {len(parts)}</b>\n\n")
    body = links_block(parts) if parts else "Ссылки ещё не созданы."
    rows = [[InlineKeyboardButton(text=f"📄 Ответы: {p.label} · {p.role_label}",
                                  callback_data=f"adm:aud:resp:{p.id}")]
            for p in parts if p.status == "completed"]
    rows.append([InlineKeyboardButton(text="📨 Прислать ссылки лиду",
                                      callback_data=f"adm:aud:send:{lead_id}")])
    if len(parts) < TEAM_LINKS_MAX:
        rows.append([InlineKeyboardButton(text="➕ Ещё ссылка",
                                          callback_data=f"adm:aud:add:{lead_id}")])
    rows.append([InlineKeyboardButton(text="◀️ К карточке", callback_data=f"adm:lead:{lead_id}")])
    await message.answer(header + body,
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                         disable_web_page_preview=True)


@router.callback_query(F.data.regexp(r"^adm:aud:\d+$"))
async def cb_audit_panel(call: CallbackQuery) -> None:
    await call.answer()
    await _audit_panel(call.message, int(call.data.split(":")[2]))


@router.callback_query(F.data.startswith("adm:aud:send:"))
async def cb_audit_send(call: CallbackQuery, bot: Bot) -> None:
    """Создать (если нет) и отправить ссылки владельцу в личку."""
    from handlers.deep_audit import send_team_links

    lead = await repo.get_lead(int(call.data.split(":")[3]))
    if lead is None:
        await call.answer()
        return
    try:
        await send_team_links(bot, lead.telegram_id, lead)
        await call.answer("Ссылки отправлены лиду ✅", show_alert=True)
    except Exception:
        await call.answer("⚠️ Не доставлено (лид заблокировал бота?)", show_alert=True)
    await _audit_panel(call.message, lead.id)


@router.callback_query(F.data.startswith("adm:aud:add:"))
async def cb_audit_add(call: CallbackQuery) -> None:
    from handlers.deep_audit import TEAM_LINKS_MAX

    lead_id = int(call.data.split(":")[3])
    parts = await repo.participants_for(lead_id)
    if len(parts) >= TEAM_LINKS_MAX:
        await call.answer(f"Максимум {TEAM_LINKS_MAX} ссылок", show_alert=True)
        return
    await call.answer("Ссылка добавлена ➕")  # гасим кнопку сразу — меньше шанс двойного клика
    await repo.create_participants(lead_id, 1)
    await _audit_panel(call.message, lead_id)


@router.callback_query(F.data.startswith("adm:aud:resp:"))
async def cb_audit_resp(call: CallbackQuery, bot: Bot) -> None:
    """Ответы участника углублённого аудита (повторный просмотр)."""
    from handlers.deep_audit import participant_card
    from services.notifier import send_long

    p = await repo.get_participant(int(call.data.split(":")[3]))
    await call.answer()
    if p is None or not p.response_id or not p.participant_lead_id:
        await call.message.answer("Ответы не найдены.")
        return
    resp = await repo.get_response(p.response_id)
    participant = await repo.get_lead(p.participant_lead_id)
    owner = await repo.get_lead(p.owner_lead_id)
    if resp is None or participant is None or owner is None:
        await call.message.answer("Ответы не найдены.")
        return
    await send_long(bot, call.message.chat.id, participant_card(p, participant, owner, resp))


# --- Ответ на вопрос клиента (кнопка под уведомлением «Вопрос от …») ---

@router.callback_query(F.data.regexp(r"^reply:\d+$"))
async def cb_reply(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminReply.replying)
    await state.update_data(lead_id=int(call.data.split(":")[1]))
    await call.message.answer("Пришлите ответ — я передам его клиенту.")
    await call.answer()


@router.message(AdminReply.replying)
async def msg_reply(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    lead = await repo.get_lead(data["lead_id"])
    if lead is None:
        return
    try:
        await bot.send_message(lead.telegram_id,
                               f"💬 <b>Ответ команды:</b>\n\n{escape(message.text or '')}")
        await message.answer("Ответ доставлен ✅")
    except Exception:
        await message.answer("⚠️ Не доставлено (лид заблокировал бота?)")
