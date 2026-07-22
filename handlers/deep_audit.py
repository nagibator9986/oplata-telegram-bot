"""Углублённый аудит (Блок 4 ТЗ): анкеты команды по персональным ссылкам.

Флоу: владелец выбирает «Углублённый» в своей анкете → после завершения своей
части получает набор персональных ссылок → раздаёт их сотрудникам (2–6 человек).
Участник по ссылке выбирает уровень ответственности: Совладелец / Топ-менеджер —
получает блок владельца (Блок 3), Менеджер — блок менеджеров (Блок 4).

Участники НЕ входят в маркетинговую воронку: funnel не двигается, дожим и
донат-офферы их не касаются. Ответы хранятся в SurveyResponse (kind=participant).
"""
from __future__ import annotations

import logging
import math
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from db import repo
from db.models import AuditParticipant, Lead, utcnow
from services import runtime
from services.content import render_text, send_slot
from services.notifier import notify_admins_long
from services.validation import check_answer
from states import DeepAuditStates

log = logging.getLogger(__name__)
router = Router(name="deep_audit")

TEAM_LINKS_DEFAULT = 6   # по ТЗ: 2–6 участников; лишние ссылки просто не используются
TEAM_LINKS_MAX = 12      # предохранитель от бесконечной генерации из админки

PAUSE_BTN = InlineKeyboardButton(text="⏸ Продолжить позже", callback_data="da:pause")

STATUS_ICONS = {"created": "▫️", "claimed": "👀", "in_progress": "✍️", "completed": "✅"}


def _participant_stage(role: str) -> str:
    """Совладелец и топ-менеджер отвечают на блок владельца, менеджер — на свой."""
    return "manager" if role == "manager" else "main"


def links_block(parts: list[AuditParticipant]) -> str:
    lines = []
    for p in parts:
        status = AuditParticipant.STATUS_LABELS.get(p.status, p.status)
        extra = f" · {p.role_label}" if p.role else ""
        lines.append(f"{STATUS_ICONS.get(p.status, '▫️')} <b>{escape(p.label)}</b> "
                     f"({status}{extra})\n{runtime.deep_link('aud_' + p.token)}")
    return "\n".join(lines)


async def send_team_links(bot: Bot, chat_id: int, owner: Lead) -> None:
    """Прислать владельцу ссылки для команды (создаются при первом обращении)."""
    parts = await repo.participants_for(owner.id)
    if not parts:
        parts = await repo.create_participants(owner.id, TEAM_LINKS_DEFAULT)
    bt = await repo.get_text("deep_links_intro")
    intro = render_text(bt, owner) if bt else "🔗 <b>Ссылки для Вашей команды</b>"
    await bot.send_message(
        chat_id, intro + "\n\n" + links_block(parts),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статус команды", callback_data="da:status")]]),
        disable_web_page_preview=True)


# ================================================================ вход участника

async def start_participant(bot: Bot, chat_id: int, lead: Lead, state: FSMContext,
                            token: str) -> None:
    """Обработка deep-link ?start=aud_<token>."""
    p = await repo.participant_by_token(token)
    if p is None:
        await bot.send_message(chat_id, "Ссылка недействительна или устарела. "
                                        "Попросите руководителя прислать новую.")
        return

    owner = await repo.get_lead(p.owner_lead_id)
    if owner is None:
        await bot.send_message(chat_id, "Ссылка недействительна или устарела.")
        return

    if lead.id == p.owner_lead_id:
        parts = await repo.participants_for(owner.id)
        await bot.send_message(
            chat_id, "Это ссылка для сотрудника — перешлите её участнику команды. "
                     "Текущий статус аудита:\n\n" + links_block(parts),
            disable_web_page_preview=True)
        return

    if p.status == "completed":
        await bot.send_message(chat_id, "По этой ссылке анкета уже пройдена. Спасибо! 🙏")
        return

    if p.participant_lead_id and p.participant_lead_id != lead.id:
        await bot.send_message(chat_id, "Эта ссылка уже используется другим участником. "
                                        "Попросите руководителя прислать вам отдельную ссылку.")
        return

    # тот же человек открыл вторую ссылку — продолжаем уже начатую анкету
    other = await repo.open_participation(lead.id)
    if other is not None and other.id != p.id:
        await bot.send_message(chat_id, "Вы уже участвуете в аудите — продолжаем вашу анкету 👇")
        p = other

    if not p.participant_lead_id:
        await repo.update_participant(p.id, participant_lead_id=lead.id,
                                      status="claimed", claimed_at=utcnow())

    if p.status == "in_progress" and p.response_id:
        qstage = _participant_stage(p.role)
        await state.set_state(DeepAuditStates.answering)
        await state.update_data(pid=p.id, resp_id=p.response_id, qstage=qstage, multi=[])
        await bot.send_message(chat_id, "С возвращением! Продолжаем с места остановки 👇")
        if not await _ask_current(bot, chat_id, p.response_id, qstage):
            # анкету «подрезали» (админ скрыл вопросы) — завершаем, не зависаем
            await _finish(bot, chat_id, state, lead, p.id, p.response_id)
        return

    await _send_intro_and_role(bot, chat_id, lead, owner, p, state)


async def _send_intro_and_role(bot: Bot, chat_id: int, lead: Lead, owner: Lead,
                               p: AuditParticipant, state: FSMContext) -> None:
    bt = await repo.get_text("deep_participant_intro")
    company = owner.company_name or "Вашей организации"
    if bt is not None:
        await bot.send_message(chat_id, render_text(bt, lead).replace("{company}", escape(company)),
                               disable_web_page_preview=True)
    await state.set_state(DeepAuditStates.choosing_role)
    await state.update_data(pid=p.id)
    await bot.send_message(
        chat_id, "<b>Ваш уровень ответственности в компании:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Совладелец", callback_data="da:role:co_owner")],
            [InlineKeyboardButton(text="Топ-менеджер", callback_data="da:role:top")],
            [InlineKeyboardButton(text="Менеджер", callback_data="da:role:manager")],
        ]))


@router.callback_query(DeepAuditStates.choosing_role, F.data.startswith("da:role:"))
async def cb_role(call: CallbackQuery, lead: Lead, bot: Bot, state: FSMContext) -> None:
    role = call.data.split(":")[2]
    if role not in AuditParticipant.ROLE_LABELS:
        await call.answer()
        return
    data = await state.get_data()
    p = await repo.get_participant(data.get("pid", 0))
    await call.answer()
    if p is None or p.participant_lead_id != lead.id:
        await state.clear()
        return
    # Защита от двойного тапа: если роль уже выбрана и анкета создана — не плодим
    # осиротевшие SurveyResponse, а продолжаем существующую.
    if p.status == "in_progress" and p.response_id:
        qstage = _participant_stage(p.role)
        await state.set_state(DeepAuditStates.answering)
        await state.update_data(pid=p.id, resp_id=p.response_id, qstage=qstage, multi=[])
        return
    qstage = _participant_stage(role)
    questions = await repo.questions_by_stage(qstage)
    if not questions:
        await call.message.answer("Анкета для вашей роли пока готовится — мы напишем вам. 🙏")
        await state.clear()
        return
    resp = await repo.create_response(lead.id, kind="participant")
    await repo.update_participant(p.id, role=role, response_id=resp.id, status="in_progress")
    await state.set_state(DeepAuditStates.answering)
    await state.update_data(pid=p.id, resp_id=resp.id, qstage=qstage, multi=[])
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _ask_current(bot, call.message.chat.id, resp.id, qstage)


# ================================================================ вопросы

def _multi_kb(options: list, selected: list[int]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=("✅ " if i in selected else "▫️ ") + opt, callback_data=f"da:mt:{i}")]
        for i, opt in enumerate(options)]
    rows.append([InlineKeyboardButton(text="✔️ Готово", callback_data="da:mdone")])
    rows.append([PAUSE_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _ask_current(bot: Bot, chat_id: int, resp_id: int, qstage: str) -> bool:
    """Показать текущий вопрос. Возвращает False, если вопросов не осталось (idx за границей)."""
    questions = await repo.questions_by_stage(qstage)
    resp = await repo.get_response(resp_id)
    if resp is None:
        return False
    idx = resp.current_index
    if idx >= len(questions):
        return False  # список «подрезали» — вызывающий должен завершить анкету
    q = questions[idx]
    # escape(q.text): текст вопроса редактируется админом и может содержать «<»/«&»
    header = f"<b>Вопрос {idx + 1} из {len(questions)}</b>\n\n{escape(q.text)}"

    if q.field_type == "choice":
        rows = [[InlineKeyboardButton(text=opt, callback_data=f"da:opt:{i}")]
                for i, opt in enumerate(q.options or [])]
        rows.append([PAUSE_BTN])
        await bot.send_message(chat_id, header,
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    elif q.field_type == "multichoice":
        await bot.send_message(chat_id, header + "\n\n<i>Можно выбрать несколько вариантов.</i>",
                               reply_markup=_multi_kb(q.options or [], []))
    elif q.field_type == "contact":
        buttons: list[list[KeyboardButton]] = [[KeyboardButton(text="📱 Поделиться контактом",
                                                               request_contact=True)]]
        if not q.required:
            buttons.append([KeyboardButton(text="Пропустить")])
        await bot.send_message(
            chat_id, header + "\n\n<i>Нажмите кнопку ниже или введите номер вручную.</i>",
            reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True,
                                             one_time_keyboard=True))
    else:  # text | number
        rows = [[PAUSE_BTN]]
        if not q.required:  # индекс в callback — защита от «слепого» пропуска чужого вопроса
            rows.insert(0, [InlineKeyboardButton(text="Пропустить →",
                                                 callback_data=f"da:skip:{idx}")])
        await bot.send_message(chat_id, header,
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    return True


async def _record(bot: Bot, chat_id: int, state: FSMContext, lead: Lead, value: str) -> None:
    data = await state.get_data()
    resp_id, qstage = data.get("resp_id"), data.get("qstage", "manager")
    questions = await repo.questions_by_stage(qstage)
    resp = await repo.get_response(resp_id) if resp_id else None
    if resp is None or resp.current_index >= len(questions):
        await state.clear()
        return
    q = questions[resp.current_index]
    next_idx = resp.current_index + 1
    await repo.save_answer(resp_id, {"q": q.text, "a": value}, next_idx)
    await state.update_data(multi=[])

    if next_idx >= len(questions):
        await _finish(bot, chat_id, state, lead, data.get("pid", 0), resp_id)
    else:
        await _ask_current(bot, chat_id, resp_id, qstage)


async def _finish(bot: Bot, chat_id: int, state: FSMContext, lead: Lead,
                  pid: int, resp_id: int) -> None:
    resp = await repo.complete_response(resp_id)
    await repo.update_participant(pid, status="completed", completed_at=utcnow())
    await state.clear()
    await bot.send_message(chat_id, "✅ Анкета завершена!", reply_markup=ReplyKeyboardRemove())
    await send_slot(bot, chat_id, "deep_participant_thanks", lead)

    p = await repo.get_participant(pid)
    owner = await repo.get_lead(p.owner_lead_id) if p else None
    if p is None or owner is None:
        return
    parts = await repo.participants_for(owner.id)
    done = sum(1 for x in parts if x.status == "completed")

    await notify_admins_long(bot, participant_card(p, lead, owner, resp))
    try:
        await bot.send_message(
            owner.telegram_id,
            f"✅ Участник Вашей команды ({p.role_label}) завершил анкету аудита.\n"
            f"Прогресс команды: <b>{done} из {len(parts)}</b> ссылок использовано.",
        )
    except Exception:
        log.warning("владелец %s недоступен для уведомления", owner.telegram_id)


def participant_card(p: AuditParticipant, participant: Lead, owner: Lead, resp) -> str:
    lines = [
        "🔬 <b>Углублённый аудит · анкета участника</b>",
        f"Компания: {escape(owner.company_name) or '—'}",
        f"Владелец: {escape(owner.display_name)}",
        f"Участник: {escape(participant.display_name)} · {p.role_label}", "",
    ]
    for i, entry in enumerate(resp.answers or [], start=1):
        # номер в <b>, текст вопроса — вне тега: q.text может быть многострочным,
        # а split_long режет по \n и разорвал бы <b>…</b> (Telegram отверг бы сообщение)
        lines.append(f"<b>{i}.</b> {escape(str(entry.get('q', '')))}")
        lines.append(escape(str(entry.get("a", "—"))))
    return "\n".join(lines)


# ================================================================ ответы участника

@router.callback_query(DeepAuditStates.answering, F.data == "da:pause")
async def cb_pause(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.answer("Прогресс сохранён 👌 Вернуться можно по вашей ссылке "
                              "или командой /start.")
    await call.answer()


@router.callback_query(DeepAuditStates.answering, F.data.startswith("da:skip:"))
async def cb_skip(call: CallbackQuery, lead: Lead, bot: Bot, state: FSMContext) -> None:
    await call.answer()
    # Индекс в callback должен совпасть с текущим вопросом — иначе устаревшая/повторная кнопка
    try:
        idx = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        return
    resp = await repo.get_response((await state.get_data()).get("resp_id", 0))
    if resp is None or resp.current_index != idx:
        return
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _record(bot, call.message.chat.id, state, lead, "—")


@router.callback_query(DeepAuditStates.answering, F.data.startswith("da:opt:"))
async def cb_choice(call: CallbackQuery, lead: Lead, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    questions = await repo.questions_by_stage(data.get("qstage", "manager"))
    resp = await repo.get_response(data.get("resp_id", 0))
    await call.answer()
    if resp is None or resp.current_index >= len(questions):
        return
    q = questions[resp.current_index]
    try:
        value = (q.options or [])[int(call.data.split(":")[2])]
    except (ValueError, IndexError):
        return
    await call.message.edit_reply_markup(reply_markup=None)
    await _record(bot, call.message.chat.id, state, lead, value)


@router.callback_query(DeepAuditStates.answering, F.data.startswith("da:mt:"))
async def cb_multi_toggle(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    questions = await repo.questions_by_stage(data.get("qstage", "manager"))
    resp = await repo.get_response(data.get("resp_id", 0))
    await call.answer()
    if resp is None or resp.current_index >= len(questions):
        return
    q = questions[resp.current_index]
    try:
        i = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        return
    selected = list(data.get("multi", []))
    selected = [x for x in selected if x != i] if i in selected else selected + [i]
    await state.update_data(multi=selected)
    try:
        await call.message.edit_reply_markup(reply_markup=_multi_kb(q.options or [], selected))
    except Exception:
        pass  # markup не изменился


@router.callback_query(DeepAuditStates.answering, F.data == "da:mdone")
async def cb_multi_done(call: CallbackQuery, lead: Lead, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    questions = await repo.questions_by_stage(data.get("qstage", "manager"))
    resp = await repo.get_response(data.get("resp_id", 0))
    if resp is None or resp.current_index >= len(questions):
        await call.answer()
        return
    q = questions[resp.current_index]
    selected = list(data.get("multi", []))
    if q.required and not selected:
        await call.answer("Выберите хотя бы один вариант", show_alert=True)
        return
    value = ", ".join((q.options or [])[i] for i in sorted(selected)
                      if i < len(q.options or [])) or "—"
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer()
    await _record(bot, call.message.chat.id, state, lead, value)


@router.message(DeepAuditStates.answering)
async def msg_answer(message: Message, lead: Lead, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    questions = await repo.questions_by_stage(data.get("qstage", "manager"))
    resp = await repo.get_response(data.get("resp_id", 0))
    if resp is None or resp.current_index >= len(questions):
        await state.clear()
        return
    q = questions[resp.current_index]

    if q.field_type == "contact":
        if message.contact:
            phone = message.contact.phone_number
            await repo.update_lead(lead.telegram_id, phone=phone)
            await _record(bot, message.chat.id, state, lead, phone)
        elif message.text and message.text.strip().lower() == "пропустить" and not q.required:
            await _record(bot, message.chat.id, state, lead, "—")
        elif message.text and any(ch.isdigit() for ch in message.text):
            phone = message.text.strip()
            await repo.update_lead(lead.telegram_id, phone=phone)
            await _record(bot, message.chat.id, state, lead, phone)
        else:
            await message.answer("Нажмите «📱 Поделиться контактом» или введите номер телефона.")
        return

    if q.field_type in ("choice", "multichoice"):
        await message.answer("Пожалуйста, используйте кнопки под вопросом 🙏")
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Отправьте, пожалуйста, текстовый ответ.")
        return
    if q.field_type == "number":
        normalized = text.replace(",", ".").replace(" ", "")
        try:
            val = float(normalized)
            if not math.isfinite(val):  # float() пропускает inf/-inf/nan/1e999 — отсекаем
                raise ValueError
        except ValueError:
            await message.answer("Нужно число — например: 12 или 3.5")
            return
        text = normalized
    else:
        # Отсекаем мусорные ответы («...», «ааааа», «фывфыв») — вопрос переспрашивается
        err = check_answer(text)
        if err:
            await message.answer(err)
            return
    await _record(bot, message.chat.id, state, lead, text)


# ================================================================ статус и возврат

@router.callback_query(F.data == "da:status")
async def cb_status(call: CallbackQuery, lead: Lead) -> None:
    """Владелец смотрит прогресс своей команды (кнопка под сообщением со ссылками)."""
    parts = await repo.participants_for(lead.id)
    await call.answer()
    if not parts:
        await call.message.answer("Ссылок для команды пока нет.")
        return
    done = sum(1 for x in parts if x.status == "completed")
    await call.message.answer(
        f"📊 <b>Аудит команды: {done} из {len(parts)} анкет завершено</b>\n\n"
        + links_block(parts),
        disable_web_page_preview=True)


@router.callback_query(F.data == "da:resume")
async def cb_resume(call: CallbackQuery, lead: Lead, bot: Bot, state: FSMContext) -> None:
    """Возврат к незавершённой анкете участника (кнопка из /start)."""
    p = await repo.open_participation(lead.id)
    await call.answer()
    if p is None:
        await call.message.answer("Незавершённых анкет аудита не нашёл.")
        return
    await start_participant(bot, call.message.chat.id, lead, state, p.token)
