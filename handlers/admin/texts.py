"""Редактор текстов бота: список → предпросмотр → правка → подтверждение → откат."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db import repo
from keyboards.common import kb, kb_from_json
from states import AdminTextStates

router = Router(name="admin_texts")
PAGE_SIZE = 8


@router.callback_query(F.data.startswith("adm:texts:"))
async def cb_list(call: CallbackQuery) -> None:
    page = int(call.data.split(":")[2])
    texts = await repo.list_texts()
    chunk = texts[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    rows = [[InlineKeyboardButton(text=f"{t.title or t.key}", callback_data=f"adm:t:view:{t.key}")]
            for t in chunk]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm:texts:{page - 1}"))
    if (page + 1) * PAGE_SIZE < len(texts):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm:texts:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="adm:menu")])
    await call.message.answer(f"📝 <b>Тексты бота</b> ({len(texts)})",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data.startswith("adm:t:view:"))
async def cb_view(call: CallbackQuery, bot: Bot) -> None:
    key = call.data.split(":", 3)[3]
    bt = await repo.get_text(key)
    await call.answer()
    if bt is None:
        return
    # предпросмотр 1:1 как увидит клиент
    markup = kb_from_json(bt.buttons)
    if bt.media_file_id:
        await bot.send_photo(call.message.chat.id, bt.media_file_id, caption=bt.text,
                             reply_markup=markup)
    else:
        await bot.send_message(call.message.chat.id, bt.text, reply_markup=markup,
                               disable_web_page_preview=True)
    await call.message.answer(
        f"👆 Слот <code>{key}</code> — «{bt.title}»",
        reply_markup=kb([("✏️ Изменить", f"adm:t:edit:{key}")],
                        [("↩️ Откатить правку", f"adm:t:revert:{key}")],
                        [("📃 К списку", "adm:texts:0")]),
    )


@router.callback_query(F.data.startswith("adm:t:edit:"))
async def cb_edit(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.split(":", 3)[3]
    await state.set_state(AdminTextStates.waiting_text)
    await state.update_data(key=key)
    await call.message.answer(
        "Пришлите новый текст (можно с фото — тогда текст в подписи).\n"
        "Разметка: <b>&lt;b&gt;жирный&lt;/b&gt;</b>, <i>&lt;i&gt;курсив&lt;/i&gt;</i>.\n"
        "Плейсхолдеры: <code>{name}</code>, <code>{group_link}</code>.",
        reply_markup=kb([("↩️ Отмена", "adm:t:cancel")]),
    )
    await call.answer()


@router.message(AdminTextStates.waiting_text)
async def msg_new_text(message: Message, state: FSMContext, bot: Bot) -> None:
    new_media = ""
    if message.photo:
        new_media = message.photo[-1].file_id
        new_text = message.caption or ""
    else:
        new_text = message.html_text if message.text else ""
    if not new_text and not new_media:
        await message.answer("Пришлите текст или фото с подписью.")
        return
    await state.update_data(new_text=new_text, new_media=new_media)
    await state.set_state(AdminTextStates.confirm)
    # предпросмотр
    try:
        if new_media:
            await bot.send_photo(message.chat.id, new_media, caption=new_text)
        else:
            await bot.send_message(message.chat.id, new_text, disable_web_page_preview=True)
    except Exception:
        await state.set_state(AdminTextStates.waiting_text)
        await message.answer("⚠️ Telegram отверг разметку. Проверьте HTML-теги и пришлите ещё раз.")
        return
    await message.answer("Сохранить этот вариант?",
                         reply_markup=kb([("✅ Сохранить", "adm:t:save"),
                                          ("↩️ Отмена", "adm:t:cancel")]))


@router.callback_query(AdminTextStates.confirm, F.data == "adm:t:save")
async def cb_save(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await repo.save_text(data["key"], text=data.get("new_text"),
                         media_file_id=data.get("new_media"),
                         edited_by=str(call.from_user.id))
    await state.clear()
    await call.message.answer(f"✅ Слот <code>{data['key']}</code> обновлён.",
                              reply_markup=kb([("📃 К списку", "adm:texts:0")]))
    await call.answer()


@router.callback_query(F.data == "adm:t:cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.answer("Правка отменена.", reply_markup=kb([("📃 К списку", "adm:texts:0")]))
    await call.answer()


@router.callback_query(F.data.startswith("adm:t:revert:"))
async def cb_revert(call: CallbackQuery) -> None:
    key = call.data.split(":", 3)[3]
    ok = await repo.revert_text(key)
    await call.answer("Откатил ✅" if ok else "Нет сохранённых правок", show_alert=True)
