"""Аудит по Коду Вечного Иля: фильтр → паспорт → выбор аудита → 32 вопроса.

Особенности:
* стадия qualify — мягкий отказ, если ответ входит в disqualify_if (зовём в клуб);
* intro_key — вводный текст блока показывается перед вопросом;
* стадия audit_choice — «Углублённый» ставит флаг, после завершения своей части
  владелец получает персональные ссылки для команды (handlers/deep_audit.py);
* ответ сохраняется после каждого вопроса, продолжение с места остановки.
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
from db.models import Funnel, Lead, utcnow
from services import platform
from services.content import send_slot
from services.funnel import advance
from services.notifier import lead_admin_kb, notify_admins, notify_admins_long, response_card
from services.validation import check_answer
from states import SurveyStates

log = logging.getLogger(__name__)
router = Router(name="survey")

PAUSE_BTN = InlineKeyboardButton(text="⏸ Продолжить позже", callback_data="sv:pause")


def _choice_kb(options: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=opt, callback_data=f"sv:opt:{i}")]
            for i, opt in enumerate(options)]
    rows.append([PAUSE_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _qualify_confirm_kb(qidx: int, opt_idx: int) -> InlineKeyboardMarkup:
    """Подтверждение выбора фильтр-вопроса — защита от случайного тапа."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, всё верно", callback_data=f"sv:qy:{qidx}:{opt_idx}")],
        [InlineKeyboardButton(text="◀️ Изменить", callback_data=f"sv:qn:{qidx}")],
    ])


async def begin_survey(bot: Bot, chat_id: int, lead: Lead, state: FSMContext) -> None:
    # Отсеянного фильтром лида не пускаем обратно в анкету — только вежливый отказ
    if lead.funnel_state == Funnel.UNQUALIFIED:
        await send_slot(bot, chat_id, "survey_reject", lead)
        return
    questions = await repo.active_questions()
    if not questions:
        await bot.send_message(chat_id, "Аудит пока готовится — я напишу, когда он откроется 🙏")
        return
    resp = await repo.get_open_response(lead.id)
    if resp is None:
        if lead.survey_completed_at:
            await bot.send_message(chat_id, "Вы уже прошли аудит — ответы у аналитика 🙌")
            return
        resp = await repo.create_response(lead.id)
    elif resp.current_index >= len(questions):
        # Анкету «подрезали» (админ скрыл вопросы) — досрочно завершаем, не зависаем
        await _finalize(bot, chat_id, state, lead, resp.id)
        return
    lead = await advance(lead, Funnel.SURVEYING)
    await state.set_state(SurveyStates.answering)
    await state.update_data(resp_id=resp.id, multi=[])
    await _ask_current(bot, chat_id, resp.id)


async def _ask_current(bot: Bot, chat_id: int, resp_id: int) -> None:
    questions = await repo.active_questions()
    resp = await _get_resp(resp_id)
    idx = resp.current_index
    if idx >= len(questions):
        return
    q = questions[idx]

    # Вводный текст блока — перед вопросом (Блок 2 / инструкция перед основной частью)
    if q.intro_key:
        lead = await repo.get_lead(resp.lead_id)
        await send_slot(bot, chat_id, q.intro_key, lead)

    # escape(q.text): текст вопроса редактируется админом и может содержать «<»/«&»
    header = f"<b>Вопрос {idx + 1} из {len(questions)}</b>\n\n{escape(q.text)}"

    if q.field_type == "choice":
        await bot.send_message(chat_id, header, reply_markup=_choice_kb(q.options or []))
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
                                                 callback_data=f"sv:skip:{idx}")])
        await bot.send_message(chat_id, header, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


def _multi_kb(options: list, selected: list[int]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=("✅ " if i in selected else "▫️ ") + opt, callback_data=f"sv:mt:{i}")]
        for i, opt in enumerate(options)]
    rows.append([InlineKeyboardButton(text="✔️ Готово", callback_data="sv:mdone")])
    rows.append([PAUSE_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _get_resp(resp_id: int):
    from db.base import Session
    from db.models import SurveyResponse
    async with Session() as s:
        return await s.get(SurveyResponse, resp_id)


async def _reject(bot: Bot, chat_id: int, state: FSMContext, lead: Lead, resp_id: int) -> None:
    """Мягкий отказ по фильтру: аудит не запускаем, зовём в закрытый клуб."""
    await state.clear()
    # Закрываем открытую анкету, иначе её можно возобновить кнопкой и обойти фильтр
    await repo.reject_response(resp_id)
    lead = await repo.update_lead(lead.telegram_id, funnel_state=Funnel.UNQUALIFIED) or lead
    await repo.cancel_followups_on(lead.id, Funnel.UNQUALIFIED)
    await bot.send_message(chat_id, "Спасибо за ответы 🙏", reply_markup=ReplyKeyboardRemove())
    await send_slot(bot, chat_id, "survey_reject", lead)
    await notify_admins(bot, f"ℹ️ Лид не прошёл фильтр масштаба: {escape(lead.display_name)} "
                             f"(группа и аудит не выданы).")


async def _record(bot: Bot, chat_id: int, state: FSMContext, lead: Lead, value: str) -> None:
    data = await state.get_data()
    resp_id = data.get("resp_id")
    questions = await repo.active_questions()
    resp = await _get_resp(resp_id)
    if resp is None or resp.current_index >= len(questions):
        await state.clear()
        return
    idx = resp.current_index
    q = questions[idx]
    next_idx = idx + 1
    await repo.save_answer(resp_id, {"q": q.text, "a": value}, next_idx)
    await state.update_data(multi=[])

    # Фильтр масштаба (qualify): отказ / следующий фильтр-вопрос / пауза с оффером
    if q.stage == "qualify":
        if value in (q.disqualify_if or []):
            await _reject(bot, chat_id, state, lead, resp_id)
            return
        last_qualify = next_idx >= len(questions) or questions[next_idx].stage != "qualify"
        if last_qualify:
            # Прошёл фильтр целиком → флаг qualified + пауза: аудит и группа предлагаются
            # кнопками (сначала фильтрация, потом уже аудит и закрытая группа).
            lead = await repo.update_lead(lead.telegram_id, qualified=True) or lead
            await state.clear()
            await send_slot(bot, chat_id, "qualified_hub", lead)
            await notify_admins(
                bot, f"✅ Лид прошёл фильтр масштаба: {escape(lead.display_name)}.")
            return
        await _ask_current(bot, chat_id, resp_id)  # ещё есть фильтр-вопросы
        return

    # Паспорт: название компании берём с ПЕРВОГО вопроса-паспорта (по позиции, не по тексту —
    # чтобы правка формулировки в редакторе не ломала захват).
    first_passport = next((i for i, x in enumerate(questions) if x.stage == "passport"), None)
    if q.stage == "passport" and idx == first_passport and not lead.company_name:
        lead = await repo.update_lead(lead.telegram_id, company_name=value[:200]) or lead

    # Выбор аудита: углублённый = выбран не первый вариант (по индексу, не по тексту).
    if q.stage == "audit_choice":
        opts = q.options or []
        is_deep = value in opts and opts.index(value) >= 1
        if is_deep:
            lead = await repo.update_lead(lead.telegram_id, deep_audit=True) or lead
            await send_slot(bot, chat_id, "survey_deep_selected", lead)
            await notify_admins(
                bot, f"🔬 Лид выбрал <b>углублённый аудит</b>: {escape(lead.display_name)} "
                     f"({escape(lead.company_name) or 'компания не указана'}). Ссылки для "
                     f"команды будут выданы автоматически после его части анкеты.")

    if next_idx >= len(questions):
        await _finalize(bot, chat_id, state, lead, resp_id)
    else:
        await _ask_current(bot, chat_id, resp_id)


async def _finalize(bot: Bot, chat_id: int, state: FSMContext, lead: Lead, resp_id: int) -> None:
    """Завершение анкеты: статус, воронка, уведомления, ссылки команде (если углублённый)."""
    resp = await repo.complete_response(resp_id)
    await state.clear()
    lead = await advance(lead, Funnel.SURVEY_DONE, survey_completed_at=utcnow())
    await bot.send_message(chat_id, "✅ Аудит завершён!", reply_markup=ReplyKeyboardRemove())
    await send_slot(bot, chat_id, "survey_thanks", lead)

    # СНАЧАЛА обязательное: ссылки команде (углублённый), уведомление админам, синк CRM —
    # это не должно зависеть от AI-продажника.
    if lead.deep_audit:  # углублённый: сразу выдаём владельцу ссылки для команды
        from handlers.deep_audit import send_team_links
        await send_team_links(bot, chat_id, lead)
    await notify_admins_long(bot, response_card(lead, resp), reply_markup=lead_admin_kb(lead))
    platform.sync_lead(lead, "survey_completed", answers=resp.answers)

    # ПОТОМ (не критично) AI-продажник: диагноз проблемных зон + оффер калибровки.
    # Оборачиваем — сбой AI/отправки не должен ломать завершение анкеты.
    from services import closer
    try:
        if await closer.is_enabled():
            await bot.send_chat_action(chat_id, "typing")
            pitch = await closer.post_survey_pitch(lead)
            if pitch:  # parse_mode=None: вывод LLM недоверенный
                await bot.send_message(chat_id, pitch,
                                       reply_markup=closer.cta_keyboard(surveyed=True),
                                       disable_web_page_preview=True, parse_mode=None)
    except Exception:
        log.exception("пост-анкетный питч не отправлен (не критично)")


# --- Входные точки ---

@router.callback_query(F.data == "go:survey")
async def cb_go_survey(call: CallbackQuery, lead: Lead, bot: Bot, state: FSMContext) -> None:
    await call.answer()
    await begin_survey(bot, call.message.chat.id, lead, state)


@router.callback_query(SurveyStates.answering, F.data == "sv:pause")
async def cb_pause(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.answer("Прогресс сохранён 👌 Вернуться можно кнопкой «Пройти аудит» "
                              "или командой /start.")
    await call.answer()


@router.callback_query(SurveyStates.answering, F.data.startswith("sv:skip:"))
async def cb_skip(call: CallbackQuery, lead: Lead, bot: Bot, state: FSMContext) -> None:
    await call.answer()
    # Индекс в callback должен совпасть с текущим вопросом — иначе это устаревшая/повторная
    # кнопка (двойной тап, клик по старому сообщению): молча игнорируем.
    try:
        idx = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        return
    resp = await _get_resp((await state.get_data()).get("resp_id"))
    if resp is None or resp.current_index != idx:
        return
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _record(bot, call.message.chat.id, state, lead, "—")


@router.callback_query(SurveyStates.answering, F.data.startswith("sv:opt:"))
async def cb_choice(call: CallbackQuery, lead: Lead, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    questions = await repo.active_questions()
    resp = await _get_resp(data.get("resp_id"))
    await call.answer()
    if resp is None or resp.current_index >= len(questions):
        return
    qidx = resp.current_index
    q = questions[qidx]
    try:
        opt_idx = int(call.data.split(":")[2])
        value = (q.options or [])[opt_idx]
    except (ValueError, IndexError):
        return
    # Фильтр-вопросы решают судьбу лида → переспрашиваем (защита от случайного тапа)
    if q.stage == "qualify":
        try:
            await call.message.edit_text(
                f"<b>Вопрос {qidx + 1} из {len(questions)}</b>\n\n{escape(q.text)}\n\n"
                f"Вы выбрали: <b>{escape(value)}</b>. Всё верно?",
                reply_markup=_qualify_confirm_kb(qidx, opt_idx))
        except Exception:
            pass
        return
    await call.message.edit_reply_markup(reply_markup=None)
    await _record(bot, call.message.chat.id, state, lead, value)


@router.callback_query(SurveyStates.answering, F.data.startswith("sv:qy:"))
async def cb_qualify_confirm(call: CallbackQuery, lead: Lead, bot: Bot, state: FSMContext) -> None:
    """Подтверждение фильтр-ответа → записываем."""
    await call.answer()
    parts = call.data.split(":")
    try:
        qidx, opt_idx = int(parts[2]), int(parts[3])
    except (ValueError, IndexError):
        return
    questions = await repo.active_questions()
    resp = await _get_resp((await state.get_data()).get("resp_id"))
    if resp is None or qidx >= len(questions) or resp.current_index != qidx:
        return  # устаревший тап — вопрос уже сменился
    try:
        value = (questions[qidx].options or [])[opt_idx]
    except IndexError:
        return
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _record(bot, call.message.chat.id, state, lead, value)


@router.callback_query(SurveyStates.answering, F.data.startswith("sv:qn:"))
async def cb_qualify_change(call: CallbackQuery, state: FSMContext) -> None:
    """«Изменить» — возвращаем варианты фильтр-вопроса."""
    await call.answer()
    try:
        qidx = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        return
    questions = await repo.active_questions()
    resp = await _get_resp((await state.get_data()).get("resp_id"))
    if resp is None or qidx >= len(questions) or resp.current_index != qidx:
        return
    q = questions[qidx]
    try:
        await call.message.edit_text(
            f"<b>Вопрос {qidx + 1} из {len(questions)}</b>\n\n{escape(q.text)}",
            reply_markup=_choice_kb(q.options or []))
    except Exception:
        pass


@router.callback_query(SurveyStates.answering, F.data.startswith("sv:mt:"))
async def cb_multi_toggle(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    questions = await repo.active_questions()
    resp = await _get_resp(data.get("resp_id"))
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


@router.callback_query(SurveyStates.answering, F.data == "sv:mdone")
async def cb_multi_done(call: CallbackQuery, lead: Lead, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    questions = await repo.active_questions()
    resp = await _get_resp(data.get("resp_id"))
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


@router.message(SurveyStates.answering)
async def msg_answer(message: Message, lead: Lead, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    questions = await repo.active_questions()
    resp = await _get_resp(data.get("resp_id"))
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
