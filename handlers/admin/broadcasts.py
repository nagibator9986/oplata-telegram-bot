"""Мастер рассылок: текст → кнопки → цель → расписание → предпросмотр → запуск."""
from __future__ import annotations

from datetime import timezone

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import get_settings
from db import repo
from db.models import Broadcast, Schedule
from keyboards.common import kb, kb_from_json
from services import runtime, schedule_calc
from services.broadcaster import run_broadcast
from states import BroadcastWizard

router = Router(name="admin_broadcasts")

WEEKDAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
TARGET_LABELS = {
    "group": "🔒 В группу", "dm_all": "👤 Всем в личку",
    "seg_in_group": "Сегмент: в группе", "seg_not_in_group": "Сегмент: не в группе",
    "seg_survey_done": "Сегмент: прошли анкету", "seg_donated": "Сегмент: донатеры",
}


# --------------------------------------------------------------- список

@router.callback_query(F.data == "adm:bc:list")
async def cb_list(call: CallbackQuery) -> None:
    items = await repo.list_broadcasts()
    rows = [[InlineKeyboardButton(text=f"#{b.id} · {b.title[:28]} · {b.status}",
                                  callback_data=f"adm:bc:view:{b.id}")] for b in items]
    rows.insert(0, [InlineKeyboardButton(text="➕ Новая рассылка", callback_data="adm:bc:new")])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="adm:menu")])
    await call.message.answer("📣 <b>Рассылки</b>",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data.startswith("adm:bc:view:"))
async def cb_view(call: CallbackQuery) -> None:
    bid = int(call.data.split(":")[3])
    b = await repo.get_broadcast(bid)
    await call.answer()
    if b is None:
        return
    schedules = await repo.schedules_for(bid)
    tz = get_settings().tz
    sch_lines = []
    for s in schedules:
        state = "▶️" if s.is_active else "⏸"
        nxt = (s.next_run_at.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%d.%m %H:%M")
               if s.next_run_at else "—")
        detail = {"once": "разово", "daily": f"ежедневно {s.time_of_day}",
                  "weekly": f"по {','.join(WEEKDAY_NAMES[d] for d in s.weekdays)} {s.time_of_day}",
                  "monthly": f"{s.day_of_month} числа {s.time_of_day}"}.get(s.kind, s.kind)
        sch_lines.append(f"{state} {detail} · след.: {nxt}")
    st = b.stats or {}
    text = (f"📣 <b>Рассылка #{b.id}</b> · {b.status}\n"
            f"Цель: {TARGET_LABELS.get(b.target if b.target != 'dm_segment' else 'seg_' + (b.segment or {}).get('preset', ''), b.target)}\n"
            f"Расписания:\n" + ("\n".join(sch_lines) or "нет") + "\n"
            f"Отправлено: {st.get('sent', 0)} · Ошибок: {st.get('failed', 0)} · "
            f"Блокировок: {st.get('blocked', 0)}\n\n"
            f"<i>Текст:</i>\n{b.text[:500]}")
    await call.message.answer(text, reply_markup=kb(
        [("🚀 Отправить сейчас", f"adm:bc:now:{bid}")],
        [("⏯ Пауза/возобновить", f"adm:bc:toggle:{bid}"), ("🗑 Удалить", f"adm:bc:del:{bid}")],
        [("📃 К списку", "adm:bc:list")],
    ))


@router.callback_query(F.data.startswith("adm:bc:now:"))
async def cb_send_now(call: CallbackQuery, bot: Bot) -> None:
    bid = int(call.data.split(":")[3])
    b = await repo.get_broadcast(bid)
    if b is None:
        await call.answer("Рассылка не найдена", show_alert=True)
        return
    if b.status == "sending":  # дебаунс: уже идёт (двойной тап / параллельный тик)
        await call.answer("Рассылка уже отправляется ⏳", show_alert=True)
        return
    # Закрываем окно двойного тапа: помечаем sending СИНХРОННО (в хендлере), ДО спавна
    # фоновой задачи. Раньше статус ставился внутри run_broadcast после нескольких await —
    # второй апдейт того же админа успевал прочитать не-sending и спавнил второй прогон,
    # который слал всей аудитории повторно (run_no у каждого прогона новый → already_sent
    # не спасал). Апдейты одного пользователя сериализованы (SimpleEventIsolation), поэтому
    # синхронной пометки достаточно.
    await repo.update_broadcast(bid, status="sending")
    runtime.spawn(run_broadcast(bot, bid))
    await call.answer("Запустил 🚀", show_alert=True)


@router.callback_query(F.data.startswith("adm:bc:toggle:"))
async def cb_toggle(call: CallbackQuery) -> None:
    bid = int(call.data.split(":")[3])
    new_state = await repo.toggle_broadcast_schedules(bid)
    await call.answer("Расписания включены ▶️" if new_state else "Расписания на паузе ⏸",
                      show_alert=True)


@router.callback_query(F.data.startswith("adm:bc:del:"))
async def cb_delete(call: CallbackQuery) -> None:
    bid = int(call.data.split(":")[3])
    await repo.delete_broadcast(bid)
    await call.answer("Удалено 🗑", show_alert=True)


# --------------------------------------------------------------- мастер

@router.callback_query(F.data == "adm:bc:new")
async def cb_new(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BroadcastWizard.text)
    await call.message.answer("Шаг 1/4. Пришлите <b>текст рассылки</b> (можно с фото).",
                              reply_markup=kb([("↩️ Отмена", "bcw:cancel")]))
    await call.answer()


@router.message(BroadcastWizard.text)
async def w_text(message: Message, state: FSMContext) -> None:
    media = message.photo[-1].file_id if message.photo else ""
    text = (message.caption or "") if media else (message.html_text if message.text else "")
    if not text and not media:
        await message.answer("Пришлите текст или фото с подписью.")
        return
    await state.update_data(text=text, media=media)
    await state.set_state(BroadcastWizard.buttons)
    await message.answer(
        "Шаг 2/4. Кнопки под сообщением?\nПришлите строки вида:\n"
        "<code>Текст кнопки | https://ссылка</code>\n(каждая кнопка — новая строка)",
        reply_markup=kb([("Без кнопок →", "bcw:skipbtn")], [("↩️ Отмена", "bcw:cancel")]))


def _parse_buttons(raw: str) -> list | None:
    rows = []
    for line in raw.strip().splitlines():
        if "|" not in line:
            return None
        text, url = (p.strip() for p in line.split("|", 1))
        if not text or not (url.startswith("http://") or url.startswith("https://")):
            return None
        rows.append([{"text": text, "url": url}])
    return rows


TARGET_KB = kb(
    [("🔒 В группу", "bcw:target:group")],
    [("👤 Всем в личку", "bcw:target:dm_all")],
    [("В группе", "bcw:target:seg_in_group"), ("Не в группе", "bcw:target:seg_not_in_group")],
    [("Прошли анкету", "bcw:target:seg_survey_done"), ("Донатеры", "bcw:target:seg_donated")],
    [("↩️ Отмена", "bcw:cancel")],
)


@router.message(BroadcastWizard.buttons)
async def w_buttons(message: Message, state: FSMContext) -> None:
    buttons = _parse_buttons(message.text or "")
    if buttons is None:
        await message.answer("Формат: <code>Текст | https://ссылка</code>, каждая кнопка с новой строки.")
        return
    await state.update_data(buttons=buttons)
    await state.set_state(BroadcastWizard.target)
    await message.answer("Шаг 3/4. Кому отправлять?", reply_markup=TARGET_KB)


@router.callback_query(BroadcastWizard.buttons, F.data == "bcw:skipbtn")
async def w_skip_buttons(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(buttons=[])
    await state.set_state(BroadcastWizard.target)
    await call.message.answer("Шаг 3/4. Кому отправлять?", reply_markup=TARGET_KB)
    await call.answer()


WHEN_KB = kb(
    [("🚀 Сейчас", "bcw:when:now"), ("🕐 Один раз", "bcw:when:once")],
    [("📅 Ежедневно", "bcw:when:daily"), ("📅 Еженедельно", "bcw:when:weekly")],
    [("📅 Ежемесячно", "bcw:when:monthly")],
    [("↩️ Отмена", "bcw:cancel")],
)


@router.callback_query(BroadcastWizard.target, F.data.startswith("bcw:target:"))
async def w_target(call: CallbackQuery, state: FSMContext) -> None:
    raw = call.data.split(":")[2]
    if raw == "group":
        await state.update_data(target=Broadcast.TARGET_GROUP, segment={})
    elif raw == "dm_all":
        await state.update_data(target=Broadcast.TARGET_DM_ALL, segment={})
    else:
        await state.update_data(target=Broadcast.TARGET_DM_SEGMENT,
                                segment={"preset": raw.removeprefix("seg_")})
    await state.set_state(BroadcastWizard.when)
    await call.message.answer("Шаг 4/4. Когда отправлять?", reply_markup=WHEN_KB)
    await call.answer()


@router.callback_query(BroadcastWizard.when, F.data.startswith("bcw:when:"))
async def w_when(call: CallbackQuery, state: FSMContext) -> None:
    kind = call.data.split(":")[2]
    await state.update_data(kind=kind)
    await call.answer()
    if kind == "now":
        await _show_confirm(call.message, state)
    elif kind == "once":
        await state.set_state(BroadcastWizard.once_dt)
        await call.message.answer("Дата и время (локальные): <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>")
    elif kind == "weekly":
        await state.update_data(weekdays=[])
        await state.set_state(BroadcastWizard.weekdays)
        await call.message.answer("Выберите дни недели:", reply_markup=_weekdays_kb([]))
    elif kind == "monthly":
        await state.set_state(BroadcastWizard.day_of_month)
        await call.message.answer("Число месяца (1–28):")
    else:  # daily
        await state.set_state(BroadcastWizard.time_of_day)
        await call.message.answer("Время отправки: <code>ЧЧ:ММ</code>")


@router.message(BroadcastWizard.once_dt)
async def w_once_dt(message: Message, state: FSMContext) -> None:
    dt = schedule_calc.parse_local_dt(message.text or "")
    if dt is None:
        await message.answer("Не понял. Формат: <code>25.12.2026 18:00</code>")
        return
    await state.update_data(run_at=dt.isoformat())
    await _show_confirm(message, state)


def _weekdays_kb(selected: list[int]) -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(
        text=("✅" if i in selected else "") + WEEKDAY_NAMES[i], callback_data=f"bcw:wd:{i}")
        for i in range(7)]
    return InlineKeyboardMarkup(inline_keyboard=[
        row1[:4], row1[4:],
        [InlineKeyboardButton(text="✔️ Готово", callback_data="bcw:wddone")],
    ])


@router.callback_query(BroadcastWizard.weekdays, F.data.startswith("bcw:wd:"))
async def w_weekday_toggle(call: CallbackQuery, state: FSMContext) -> None:
    i = int(call.data.split(":")[2])
    data = await state.get_data()
    sel = list(data.get("weekdays", []))
    sel = [x for x in sel if x != i] if i in sel else sel + [i]
    await state.update_data(weekdays=sel)
    try:
        await call.message.edit_reply_markup(reply_markup=_weekdays_kb(sel))
    except Exception:
        pass
    await call.answer()


@router.callback_query(BroadcastWizard.weekdays, F.data == "bcw:wddone")
async def w_weekdays_done(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("weekdays"):
        await call.answer("Выберите хотя бы один день", show_alert=True)
        return
    await state.set_state(BroadcastWizard.time_of_day)
    await call.message.answer("Время отправки: <code>ЧЧ:ММ</code>")
    await call.answer()


@router.message(BroadcastWizard.day_of_month)
async def w_day_of_month(message: Message, state: FSMContext) -> None:
    try:
        day = int((message.text or "").strip())
        assert 1 <= day <= 28
    except (ValueError, AssertionError):
        await message.answer("Введите число от 1 до 28.")
        return
    await state.update_data(day_of_month=day)
    await state.set_state(BroadcastWizard.time_of_day)
    await message.answer("Время отправки: <code>ЧЧ:ММ</code>")


@router.message(BroadcastWizard.time_of_day)
async def w_time(message: Message, state: FSMContext) -> None:
    if schedule_calc.parse_hhmm(message.text or "") is None:
        await message.answer("Формат: <code>09:30</code>")
        return
    await state.update_data(time_of_day=message.text.strip())
    await _show_confirm(message, state)


async def _show_confirm(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(BroadcastWizard.confirm)
    # предпросмотр 1:1
    markup = kb_from_json(data.get("buttons"))
    if data.get("media"):
        await message.answer_photo(data["media"], caption=data.get("text", ""), reply_markup=markup)
    else:
        await message.answer(data.get("text", ""), reply_markup=markup,
                             disable_web_page_preview=True)
    kind = data.get("kind", "now")
    # run_at хранится в наивном UTC (parse_local_dt уже перевёл локальное время админа).
    # В подтверждении показываем обратно в локальной TZ, иначе админ видит время на
    # величину сдвига раньше введённого (для Asia/Almaty — на 5 часов).
    once_label = ""
    if data.get("run_at"):
        from datetime import datetime
        _local = (datetime.fromisoformat(data["run_at"]).replace(tzinfo=timezone.utc)
                  .astimezone(get_settings().tz))
        once_label = _local.strftime("%d.%m.%Y %H:%M")
    when = {"now": "сейчас", "once": f"разово ({once_label})",
            "daily": f"ежедневно в {data.get('time_of_day')}",
            "weekly": f"по {','.join(WEEKDAY_NAMES[d] for d in data.get('weekdays', []))} "
                      f"в {data.get('time_of_day')}",
            "monthly": f"{data.get('day_of_month')} числа в {data.get('time_of_day')}"}[kind]
    await message.answer(f"👆 Так увидят получатели.\nОтправка: <b>{when}</b>. Подтвердить?",
                         reply_markup=kb([("🚀 Подтвердить", "bcw:ok"),
                                          ("↩️ Отмена", "bcw:cancel")]))


@router.callback_query(BroadcastWizard.confirm, F.data == "bcw:ok")
async def w_confirm(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    from datetime import datetime

    data = await state.get_data()
    await state.clear()
    b = await repo.create_broadcast(
        title=(data.get("text", "") or "media")[:60].replace("\n", " "),
        target=data.get("target", Broadcast.TARGET_GROUP),
        segment=data.get("segment", {}),
        text=data.get("text", ""),
        media_file_id=data.get("media", ""),
        buttons=data.get("buttons", []),
        status="active",
        created_by=call.from_user.id,
    )
    kind = data.get("kind", "now")
    if kind == "now":
        runtime.spawn(run_broadcast(bot, b.id))
        await call.message.answer(f"🚀 Рассылка #{b.id} запущена.",
                                  reply_markup=kb([("📃 К списку", "adm:bc:list")]))
    else:
        run_at = datetime.fromisoformat(data["run_at"]) if data.get("run_at") else None
        next_at = (run_at if kind == "once" else schedule_calc.next_run(
            kind, time_of_day=data.get("time_of_day", ""),
            weekdays=data.get("weekdays", []), day_of_month=data.get("day_of_month", 0)))
        await repo.add_schedule(
            b.id, {"once": Schedule.KIND_ONCE, "daily": Schedule.KIND_DAILY,
                   "weekly": Schedule.KIND_WEEKLY, "monthly": Schedule.KIND_MONTHLY}[kind],
            run_at=run_at, time_of_day=data.get("time_of_day", ""),
            weekdays=data.get("weekdays", []), day_of_month=data.get("day_of_month", 0),
            next_run_at=next_at)
        await call.message.answer(f"✅ Рассылка #{b.id} запланирована.",
                                  reply_markup=kb([("📃 К списку", "adm:bc:list")]))
    await call.answer()


@router.callback_query(F.data == "bcw:cancel")
async def w_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.answer("Мастер рассылки закрыт.",
                              reply_markup=kb([("📃 К списку", "adm:bc:list")]))
    await call.answer()
