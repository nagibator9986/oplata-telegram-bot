"""Настройки: тихие часы, донаты, ассистент, дожим, тест-режим, статическая ссылка."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from db import repo
from keyboards.common import kb
from states import AdminSettings

router = Router(name="admin_settings")


async def _settings_view() -> tuple[str, object]:
    qh = await repo.get_setting("quiet_hours", "9-21")
    dd = await repo.get_setting("donation_delay_hours", "24")
    tm = (await repo.get_setting("test_mode", "0")) == "1"
    ai = (await repo.get_setting("assistant_enabled", "1")) == "1"
    fu = (await repo.get_setting("followups_enabled", "1")) == "1"
    gl = await repo.get_setting("static_group_link", "") or "—"
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"🕘 Тихие часы (дожим не шлём вне): <b>{qh}</b>\n"
        f"⏱ Донат-оффер после анкеты через: <b>{dd} ч</b>\n"
        f"🤖 Ассистент: <b>{'вкл' if ai else 'выкл'}</b>\n"
        f"📨 Дожим-серии: <b>{'вкл' if fu else 'выкл'}</b>\n"
        f"🧪 Тестовый режим (рассылки только админам): <b>{'вкл' if tm else 'выкл'}</b>\n"
        f"🔗 Статическая ссылка группы: {gl}"
    )
    markup = kb(
        [("🕘 Тихие часы", "adm:set:qh"), ("⏱ Задержка доната", "adm:set:dd")],
        [("🤖 Ассистент вкл/выкл", "adm:set:as"), ("📨 Дожим вкл/выкл", "adm:set:fu")],
        [("🧪 Тест-режим вкл/выкл", "adm:set:tm"), ("🔗 Ссылка группы", "adm:set:gl")],
        [("🏠 Меню", "adm:menu")],
    )
    return text, markup


@router.callback_query(F.data == "adm:set")
async def cb_settings(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, markup = await _settings_view()
    await call.message.answer(text, reply_markup=markup)
    await call.answer()


async def _toggle(call: CallbackQuery, key: str, label: str) -> None:
    new = "0" if (await repo.get_setting(key, "1")) == "1" else "1"
    await repo.set_setting(key, new)
    await call.answer(f"{label}: {'вкл' if new == '1' else 'выкл'}", show_alert=True)
    text, markup = await _settings_view()
    await call.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "adm:set:as")
async def cb_toggle_ai(call: CallbackQuery) -> None:
    await _toggle(call, "assistant_enabled", "Ассистент")


@router.callback_query(F.data == "adm:set:fu")
async def cb_toggle_fu(call: CallbackQuery) -> None:
    await _toggle(call, "followups_enabled", "Дожим")


@router.callback_query(F.data == "adm:set:tm")
async def cb_toggle_tm(call: CallbackQuery) -> None:
    new = "0" if (await repo.get_setting("test_mode", "0")) == "1" else "1"
    await repo.set_setting("test_mode", new)
    await call.answer(f"Тестовый режим: {'вкл' if new == '1' else 'выкл'}", show_alert=True)


@router.callback_query(F.data == "adm:set:qh")
async def cb_quiet(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSettings.quiet_hours)
    await call.message.answer("Тихие часы в формате <code>9-21</code> (слать можно с 9 до 21):")
    await call.answer()


@router.message(AdminSettings.quiet_hours)
async def msg_quiet(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    try:
        start, end = (int(x) for x in raw.split("-"))
        assert 0 <= start < end <= 24
    except (ValueError, AssertionError):
        await message.answer("Формат: <code>9-21</code>")
        return
    await repo.set_setting("quiet_hours", raw)
    await state.clear()
    await message.answer("Сохранено ✅", reply_markup=kb([("⚙️ Настройки", "adm:set")]))


@router.callback_query(F.data == "adm:set:dd")
async def cb_delay(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSettings.donation_delay)
    await call.message.answer("Через сколько <b>часов</b> после анкеты предлагать донат? (число)")
    await call.answer()


@router.message(AdminSettings.donation_delay)
async def msg_delay(message: Message, state: FSMContext) -> None:
    try:
        hours = int((message.text or "").strip())
        assert 0 <= hours <= 720
    except (ValueError, AssertionError):
        await message.answer("Введите число часов, например 24.")
        return
    await repo.set_setting("donation_delay_hours", str(hours))
    await state.clear()
    await message.answer("Сохранено ✅", reply_markup=kb([("⚙️ Настройки", "adm:set")]))


@router.callback_query(F.data == "adm:set:gl")
async def cb_link(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSettings.static_link)
    await call.message.answer("Пришлите статическую ссылку-приглашение в группу "
                              "(запасной вариант, если бот не сможет создавать персональные), "
                              "или <code>-</code> чтобы очистить:")
    await call.answer()


@router.message(AdminSettings.static_link)
async def msg_link(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    await repo.set_setting("static_group_link", "" if raw == "-" else raw)
    await state.clear()
    await message.answer("Сохранено ✅", reply_markup=kb([("⚙️ Настройки", "adm:set")]))
