"""Редактор анкеты: полный CRUD вопросов из бота.

Возможности: список с типами, карточка вопроса, изменение текста / вариантов /
обязательности, предпросмотр «как увидит клиент», порядок ↑↓, удаление с
подтверждением (soft-delete — прошлые ответы сохраняются), восстановление скрытых.
"""
from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db import repo
from keyboards.common import kb
from services.notifier import send_long
from states import SurveyEditor

router = Router(name="admin_survey")

TYPE_LABELS = {"text": "✍️ текст", "number": "🔢 число", "choice": "🔘 выбор",
               "multichoice": "☑️ мультивыбор", "contact": "📱 контакт"}
STAGE_LABELS = {"qualify": "🔎 фильтр", "passport": "📇 паспорт",
                "audit_choice": "⚙️ выбор аудита", "main": "📝 основная",
                "manager": "👔 менеджерская (углублённый аудит)"}


# ================================================================ список

@router.callback_query(F.data == "adm:sv:list")
async def cb_list(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    main_qs = await repo.active_questions()
    mgr_qs = await repo.questions_by_stage("manager")
    hidden = await repo.hidden_questions()

    def fmt(i: int, q) -> str:
        return (f"{i}. {escape(q.text[:60])} — {TYPE_LABELS.get(q.field_type, q.field_type)}"
                f"{'' if q.required else ' (необяз.)'}")

    lines = [fmt(i + 1, q) for i, q in enumerate(main_qs)]
    if mgr_qs:
        lines.append("\n👔 <b>Анкета менеджеров (углублённый аудит)</b>")
        lines += [fmt(len(main_qs) + i + 1, q) for i, q in enumerate(mgr_qs)]

    rows, row = [], []
    for i, q in enumerate(main_qs + mgr_qs):
        row.append(InlineKeyboardButton(text=str(i + 1), callback_data=f"adm:sv:q:{q.id}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="➕ Добавить вопрос", callback_data="adm:sv:add")])
    if hidden:
        rows.append([InlineKeyboardButton(text=f"🗂 Скрытые вопросы ({len(hidden)})",
                                          callback_data="adm:sv:hidden")])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="adm:menu")])
    await send_long(  # 60+ вопросов не влезают в один месседж — клавиатура на последней части
        bot, call.message.chat.id,
        "📋 <b>Анкета</b> — нажмите номер вопроса, чтобы открыть его.\n"
        "<i>Правки применяются сразу; начатые прохождения продолжатся по новому списку.</i>\n\n"
        + ("\n".join(lines) or "Вопросов нет — добавьте первый."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


# ================================================================ карточка вопроса

async def _question_card(message: Message, qid: int) -> None:
    q = await repo.get_question(qid)
    if q is None or q.is_deleted:
        await message.answer("Вопрос не найден (возможно, скрыт).",
                             reply_markup=kb([("📃 К анкете", "adm:sv:list")]))
        return
    opts = ("\n<b>Варианты:</b> " + ", ".join(escape(o) for o in q.options)) if q.options else ""
    stage = f"\n<b>Стадия:</b> {STAGE_LABELS.get(q.stage, q.stage)}" if getattr(q, "stage", "main") != "main" else ""
    disq = (f"\n⚠️ <b>Мягкий отказ при ответе:</b> {', '.join(q.disqualify_if)}"
            if getattr(q, "disqualify_if", None) else "")
    intro = f"\nℹ️ <i>Перед вопросом показывается текст-вступление ({q.intro_key})</i>" \
        if getattr(q, "intro_key", "") else ""
    edit_row = [("✏️ Текст", f"adm:sv:etext:{qid}")]
    if q.field_type in ("choice", "multichoice"):
        edit_row.append(("🔀 Варианты", f"adm:sv:eopts:{qid}"))
    rows = [
        edit_row,
        [("✳️ Сделать " + ("необязательным" if q.required else "обязательным"),
          f"adm:sv:req:{qid}")],
    ]
    # Для фильтра (qualify) — управление дисквалифицирующими вариантами прямо в карточке
    if q.stage == "qualify" and q.field_type == "choice":
        rows.append([("⚠️ Дисквалифицирующие варианты", f"adm:sv:disq:{qid}")])
    rows += [
        [("👁 Предпросмотр", f"adm:sv:prev:{qid}")],
        [("⬆️ Выше", f"adm:sv:up:{qid}"), ("⬇️ Ниже", f"adm:sv:down:{qid}")],
        [("🗑 Удалить вопрос", f"adm:sv:del:{qid}")],
        [("📃 К анкете", "adm:sv:list")],
    ]
    await message.answer(
        f"<b>Вопрос:</b> {escape(q.text)}\n"
        f"<b>Тип:</b> {TYPE_LABELS.get(q.field_type)}{opts}\n"
        f"<b>Обязательный:</b> {'да' if q.required else 'нет'}{stage}{disq}{intro}",
        reply_markup=kb(*rows))


@router.callback_query(F.data.regexp(r"^adm:sv:q:\d+$"))
async def cb_question(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await _question_card(call.message, int(call.data.split(":")[3]))


# ================================================================ правка

@router.callback_query(F.data.startswith("adm:sv:etext:"))
async def cb_edit_text(call: CallbackQuery, state: FSMContext) -> None:
    qid = int(call.data.split(":")[3])
    q = await repo.get_question(qid)
    await call.answer()
    if q is None:
        return
    await state.set_state(SurveyEditor.edit_text)
    await state.update_data(qid=qid)
    await call.message.answer(
        f"Текущий текст:\n<i>{escape(q.text)}</i>\n\nПришлите новый текст вопроса:",
        reply_markup=kb([("↩️ Отмена", f"adm:sv:q:{qid}")]))


@router.message(SurveyEditor.edit_text)
async def msg_edit_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст вопроса.")
        return
    data = await state.get_data()
    await state.clear()
    await repo.update_question(data["qid"], text=text)
    await message.answer("✅ Текст обновлён.")
    await _question_card(message, data["qid"])


@router.callback_query(F.data.startswith("adm:sv:eopts:"))
async def cb_edit_options(call: CallbackQuery, state: FSMContext) -> None:
    qid = int(call.data.split(":")[3])
    q = await repo.get_question(qid)
    await call.answer()
    if q is None:
        return
    await state.set_state(SurveyEditor.edit_options)
    await state.update_data(qid=qid)
    current = "\n".join(q.options or [])
    await call.message.answer(
        f"Текущие варианты:\n<i>{escape(current)}</i>\n\n"
        "Пришлите новый список — каждый вариант с новой строки (минимум 2):",
        reply_markup=kb([("↩️ Отмена", f"adm:sv:q:{qid}")]))


@router.message(SurveyEditor.edit_options)
async def msg_edit_options(message: Message, state: FSMContext) -> None:
    options = [line.strip() for line in (message.text or "").splitlines() if line.strip()]
    if len(options) < 2:
        await message.answer("Нужно минимум два варианта, каждый с новой строки.")
        return
    data = await state.get_data()
    await state.clear()
    qid = data["qid"]
    q = await repo.get_question(qid)
    fields = {"options": options}
    note = ""
    # Дисквалифицирующие варианты фильтра хранятся строками — при правьке пере-привязываем
    # их ПО ПОЗИЦИИ, иначе переименование варианта молча убило бы фильтр масштаба.
    if q is not None and getattr(q, "disqualify_if", None):
        old_opts = q.options or []
        disq_idx = [i for i, o in enumerate(old_opts) if o in q.disqualify_if]
        new_disq = [options[i] for i in disq_idx if i < len(options)]
        fields["disqualify_if"] = new_disq
        if len(new_disq) != len(q.disqualify_if):
            note = ("\n⚠️ Часть дисквалифицирующих вариантов исчезла (список стал короче) — "
                    "проверьте их в «⚠️ Дисквалифицирующие варианты».")
    await repo.update_question(qid, **fields)
    await message.answer("✅ Варианты обновлены." + note)
    await _question_card(message, qid)


# ---- Дисквалифицирующие варианты (фильтр масштаба) ----

def _disq_kb(q) -> InlineKeyboardMarkup:
    disq = set(q.disqualify_if or [])
    rows = [[InlineKeyboardButton(
        text=("⚠️ " if o in disq else "▫️ ") + o[:50],
        callback_data=f"adm:sv:disqt:{q.id}:{i}")] for i, o in enumerate(q.options or [])]
    rows.append([InlineKeyboardButton(text="◀️ К вопросу", callback_data=f"adm:sv:q:{q.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("adm:sv:disq:"))
async def cb_disq(call: CallbackQuery) -> None:
    qid = int(call.data.split(":")[3])
    q = await repo.get_question(qid)
    await call.answer()
    if q is None:
        return
    await call.message.answer(
        "⚠️ Отметьте варианты, при выборе которых лид получает мягкий отказ "
        "(не проходит по масштабу). Нажмите вариант, чтобы включить/выключить:",
        reply_markup=_disq_kb(q))


@router.callback_query(F.data.startswith("adm:sv:disqt:"))
async def cb_disq_toggle(call: CallbackQuery) -> None:
    _, _, _, qid_s, idx_s = call.data.split(":")
    q = await repo.get_question(int(qid_s))
    if q is None:
        await call.answer()
        return
    opts = q.options or []
    try:
        opt = opts[int(idx_s)]
    except (ValueError, IndexError):
        await call.answer()
        return
    disq = list(q.disqualify_if or [])
    disq = [x for x in disq if x != opt] if opt in disq else disq + [opt]
    await repo.update_question(q.id, disqualify_if=disq)
    await call.answer("Обновлено")
    q = await repo.get_question(q.id)
    try:
        await call.message.edit_reply_markup(reply_markup=_disq_kb(q))
    except Exception:
        pass


@router.callback_query(F.data.regexp(r"^adm:sv:req:\d+$"))
async def cb_toggle_required(call: CallbackQuery) -> None:
    qid = int(call.data.split(":")[3])
    q = await repo.get_question(qid)
    if q is None:
        await call.answer()
        return
    await repo.update_question(qid, required=not q.required)
    await call.answer("Обязательный ✅" if not q.required else "Теперь необязательный")
    await _question_card(call.message, qid)


# ================================================================ предпросмотр

@router.callback_query(F.data.startswith("adm:sv:prev:"))
async def cb_preview(call: CallbackQuery) -> None:
    qid = int(call.data.split(":")[3])
    q = await repo.get_question(qid)
    await call.answer()
    if q is None:
        return
    # позиция считается внутри своего потока: менеджерский блок нумеруется отдельно
    questions = await (repo.questions_by_stage("manager") if q.stage == "manager"
                       else repo.active_questions())
    pos = next((i for i, x in enumerate(questions) if x.id == qid), 0)
    header = f"<b>Вопрос {pos + 1} из {len(questions)}</b>\n\n{escape(q.text)}"
    if q.field_type in ("choice", "multichoice"):
        prefix = "▫️ " if q.field_type == "multichoice" else ""
        rows = [[InlineKeyboardButton(text=prefix + opt, callback_data="adm:sv:noop")]
                for opt in (q.options or [])]
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
        await call.message.answer(header, reply_markup=markup)
    elif q.field_type == "contact":
        await call.message.answer(header + "\n\n<i>(клиент увидит кнопку "
                                           "«📱 Поделиться контактом»)</i>")
    else:
        await call.message.answer(header)
    await call.message.answer("👆 Так увидит клиент.",
                              reply_markup=kb([("◀️ К вопросу", f"adm:sv:q:{qid}")]))


@router.callback_query(F.data == "adm:sv:noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer("Это предпросмотр 🙂")


# ================================================================ порядок

@router.callback_query(F.data.startswith("adm:sv:up:"))
async def cb_up(call: CallbackQuery) -> None:
    qid = int(call.data.split(":")[3])
    moved = await repo.move_question(qid, -1)
    await call.answer("Поднял ⬆️" if moved else "Это граница блока — выше нельзя",
                      show_alert=not moved)
    if moved:
        await _question_card(call.message, qid)


@router.callback_query(F.data.startswith("adm:sv:down:"))
async def cb_down(call: CallbackQuery) -> None:
    qid = int(call.data.split(":")[3])
    moved = await repo.move_question(qid, +1)
    await call.answer("Опустил ⬇️" if moved else "Это граница блока — ниже нельзя",
                      show_alert=not moved)
    if moved:
        await _question_card(call.message, qid)


# ================================================================ удаление / восстановление

@router.callback_query(F.data.regexp(r"^adm:sv:del:\d+$"))
async def cb_delete_ask(call: CallbackQuery) -> None:
    qid = int(call.data.split(":")[3])
    q = await repo.get_question(qid)
    await call.answer()
    if q is None:
        return
    await call.message.answer(
        f"Удалить вопрос?\n<i>{escape(q.text[:100])}</i>\n\n"
        "Вопрос будет скрыт из анкеты, но прошлые ответы сохранятся — "
        "его можно восстановить из «🗂 Скрытых».",
        reply_markup=kb([("🗑 Да, удалить", f"adm:sv:delok:{qid}")],
                        [("↩️ Отмена", f"adm:sv:q:{qid}")]))


@router.callback_query(F.data.startswith("adm:sv:delok:"))
async def cb_delete_confirm(call: CallbackQuery) -> None:
    qid = int(call.data.split(":")[3])
    await repo.update_question(qid, is_deleted=True)
    await call.answer("Вопрос удалён 🗑", show_alert=True)
    await call.message.answer("Вопрос скрыт из анкеты.",
                              reply_markup=kb([("📃 К анкете", "adm:sv:list")]))


@router.callback_query(F.data == "adm:sv:hidden")
async def cb_hidden(call: CallbackQuery) -> None:
    hidden = await repo.hidden_questions()
    await call.answer()
    if not hidden:
        await call.message.answer("Скрытых вопросов нет.",
                                  reply_markup=kb([("📃 К анкете", "adm:sv:list")]))
        return
    rows = [[InlineKeyboardButton(text=f"♻️ {q.text[:40]}",
                                  callback_data=f"adm:sv:rest:{q.id}")] for q in hidden]
    rows.append([InlineKeyboardButton(text="📃 К анкете", callback_data="adm:sv:list")])
    await call.message.answer("🗂 <b>Скрытые вопросы</b> — нажмите, чтобы восстановить:",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("adm:sv:rest:"))
async def cb_restore(call: CallbackQuery) -> None:
    qid = int(call.data.split(":")[3])
    await repo.update_question(qid, is_deleted=False)
    await call.answer("Восстановлен ♻️ (встанет на прежнюю позицию)", show_alert=True)
    await _question_card(call.message, qid)


# ================================================================ добавление

@router.callback_query(F.data == "adm:sv:add")
async def cb_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SurveyEditor.q_text)
    await state.update_data(q_stage="main")
    await call.message.answer("В какую анкету добавить вопрос?", reply_markup=kb(
        [("📝 Основная (владелец / ТОП)", "svw:stage:main")],
        [("👔 Менеджерская (углублённый аудит)", "svw:stage:manager")],
        [("↩️ Отмена", "adm:sv:list")]))
    await call.answer()


@router.callback_query(SurveyEditor.q_text, F.data.startswith("svw:stage:"))
async def q_stage(call: CallbackQuery, state: FSMContext) -> None:
    stage = call.data.split(":")[2]
    if stage not in ("main", "manager"):
        await call.answer()
        return
    await state.update_data(q_stage=stage)
    await call.answer()
    await call.message.answer("Пришлите текст вопроса:",
                              reply_markup=kb([("↩️ Отмена", "adm:sv:list")]))


@router.message(SurveyEditor.q_text)
async def q_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст вопроса.")
        return
    await state.update_data(q_text=text)
    await message.answer("Тип ответа:", reply_markup=kb(
        [("✍️ Текст", "svw:type:text"), ("🔢 Число", "svw:type:number")],
        [("🔘 Один вариант", "svw:type:choice"), ("☑️ Несколько", "svw:type:multichoice")],
        [("📱 Контакт", "svw:type:contact")],
    ))


@router.callback_query(SurveyEditor.q_text, F.data.startswith("svw:type:"))
async def q_type(call: CallbackQuery, state: FSMContext) -> None:
    qtype = call.data.split(":")[2]
    await state.update_data(q_type=qtype)
    await call.answer()
    if qtype in ("choice", "multichoice"):
        await state.set_state(SurveyEditor.q_options)
        await call.message.answer("Пришлите варианты ответа — каждый с новой строки:")
    else:
        await _ask_required(call.message)


@router.message(SurveyEditor.q_options)
async def q_options(message: Message, state: FSMContext) -> None:
    options = [line.strip() for line in (message.text or "").splitlines() if line.strip()]
    if len(options) < 2:
        await message.answer("Нужно минимум два варианта, каждый с новой строки.")
        return
    await state.update_data(q_options=options)
    await _ask_required(message)


async def _ask_required(message: Message) -> None:
    await message.answer("Вопрос обязательный?", reply_markup=kb(
        [("Да", "svw:req:1"), ("Нет, можно пропустить", "svw:req:0")]))


@router.callback_query(F.data.startswith("svw:req:"))
async def q_required(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("q_text"):
        await call.answer()
        return
    q = await repo.add_question(
        text=data["q_text"],
        field_type=data.get("q_type", "text"),
        options=data.get("q_options", []),
        required=call.data.endswith(":1"),
        stage=data.get("q_stage", "main"),
    )
    await state.clear()
    await call.answer("Вопрос добавлен ✅")
    await call.message.answer("✅ Вопрос добавлен в конец "
                              + ("менеджерской анкеты." if q.stage == "manager"
                                 else "основной анкеты."))
    await _question_card(call.message, q.id)
