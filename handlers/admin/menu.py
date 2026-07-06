"""Главное админ-меню, статистика с конверсиями, бэкап БД."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from datetime import date

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from config import get_settings
from db import repo
from db.models import Funnel, utcnow
from keyboards.common import kb

router = Router(name="admin_menu")

MENU_KB = kb(
    [("📊 Статистика", "adm:stats"), ("📣 Рассылки", "adm:bc:list")],
    [("📝 Тексты бота", "adm:texts:0"), ("📋 Анкета", "adm:sv:list")],
    [("👥 Лиды", "adm:leads"), ("⚙️ Настройки", "adm:set")],
    [("🩺 Диагностика", "adm:diag"), ("💾 Бэкап БД", "adm:backup")],
)


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🛠 <b>Панель администратора</b>", reply_markup=MENU_KB)


@router.callback_query(F.data == "adm:menu")
async def cb_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.answer("🛠 <b>Панель администратора</b>", reply_markup=MENU_KB)
    await call.answer()


def _bar(value: int, total: int, width: int = 10) -> str:
    filled = round(width * value / total) if total else 0
    return "▇" * filled + "·" * (width - filled)


def _reached(funnel: dict, state: str) -> int:
    """Сколько лидов дошло до состояния или дальше (воронка — только вперёд)."""
    idx = Funnel.ORDER.index(state)
    return sum(c for st, c in funnel.items()
               if st in Funnel.ORDER and Funnel.ORDER.index(st) >= idx)


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "—"


@router.callback_query(F.data == "adm:stats")
async def cb_stats(call: CallbackQuery) -> None:
    s = await repo.stats_summary()
    funnel = s["funnel"]
    funnel_lines = []
    for st in Funnel.ORDER + [Funnel.UNQUALIFIED, Funnel.LOST]:
        n = funnel.get(st, 0)
        if n:
            funnel_lines.append(f"{_bar(n, s['total'])} {n} — {Funnel.LABELS[st]}")

    invited = _reached(funnel, Funnel.INVITED)
    joined = _reached(funnel, Funnel.JOINED)
    surveyed = _reached(funnel, Funnel.SURVEY_DONE)
    donated = _reached(funnel, Funnel.DONATED)
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"Всего лидов: <b>{s['total']}</b>\n"
        f"Новых за 24ч: {s['today']} · за 7 дней: {s['week']}\n"
        f"В группе: {s['in_group']} · Донаты: {s['donated']}\n"
        f"Анкет завершено: {s['surveys_done']} · Заблокировали бота: {s['blocked']}\n"
        f"Углублённых аудитов: {s['deep_leads']} · анкет команды: {s['deep_participants_done']}\n"
        f"AI-токенов сегодня: {s['ai_tokens_today']}\n\n"
        "<b>Конверсии:</b>\n"
        f"приглашение → группа: {_pct(joined, invited)}\n"
        f"группа → анкета: {_pct(surveyed, joined)}\n"
        f"анкета → донат: {_pct(donated, surveyed)}\n\n"
        "<b>Воронка:</b>\n" + ("\n".join(funnel_lines) or "пока пусто")
    )
    await call.message.answer(text, reply_markup=kb([("◀️ Меню", "adm:menu")]))
    await call.answer()


def _ago(ts) -> str:
    """Человекочитаемое «сколько назад» для наивного UTC-времени."""
    if ts is None:
        return "—"
    delta = (utcnow() - ts).total_seconds()
    if delta < 90:
        return f"{int(delta)} сек назад"
    if delta < 5400:
        return f"{int(delta // 60)} мин назад"
    if delta < 172800:
        return f"{int(delta // 3600)} ч назад"
    return f"{int(delta // 86400)} дн назад"


@router.callback_query(F.data == "adm:diag")
async def cb_diag(call: CallbackQuery) -> None:
    """Диагностика деплоя: жив ли фон, провайдеры AI, БД, интеграция с сайтом."""
    from services import platform, runtime
    from services.ai_providers import provider_chain

    s = get_settings()
    ops = await repo.ops_counts()

    # планировщик тикает каждые 30с — свежий тик (<120с) означает живой фон
    tick_fresh = runtime.LAST_TICK is not None and \
        (utcnow() - runtime.LAST_TICK).total_seconds() < 120
    sched_icon = "🟢" if tick_fresh else "🔴"

    chain = provider_chain()
    ai_enabled = (await repo.get_setting("assistant_enabled", "1")) == "1"
    ai_line = (f"{'🟢' if chain else '🟡'} AI-провайдеры: "
               f"{' → '.join(chain) if chain else 'нет ключей (ассистент офлайн)'}")

    db_size = "—"
    try:
        db_size = f"{os.path.getsize(s.db_path) / 1024:.0f} КБ"
    except OSError:
        pass

    fu_on = (await repo.get_setting("followups_enabled", "1")) == "1"
    test_mode = (await repo.get_setting("test_mode", "0")) == "1"

    lines = [
        "🩺 <b>Диагностика</b>",
        f"Бот: @{runtime.BOT_USERNAME or '—'}",
        f"Старт: {runtime.STARTED_AT.strftime('%d.%m %H:%M') if runtime.STARTED_AT else '—'} UTC",
        f"{sched_icon} Планировщик: тик {_ago(runtime.LAST_TICK)}",
        ai_line,
        f"AI-ассистент: {'включён' if ai_enabled else 'выключен'}",
        f"Дожим: {'вкл' if fu_on else 'выкл'} · Тест-режим рассылок: "
        f"{'ВКЛ ⚠️' if test_mode else 'выкл'}",
        "",
        f"📥 Очередь дожима: {ops['pending_followups']} · активных расписаний: "
        f"{ops['active_schedules']}",
        f"📋 Вопросов в анкете: {ops['questions']}",
        f"🗄 БД: <code>{s.db_path}</code> ({db_size})",
        f"🔗 Группа: <code>{s.group_id}</code>",
        f"🌐 Интеграция с сайтом: {'включена' if platform.enabled() else 'выключена (автономно)'}",
        f"👤 Админов: {len(s.admins)}",
    ]
    await call.message.answer("\n".join(lines), reply_markup=kb([("◀️ Меню", "adm:menu")]))
    await call.answer()


@router.callback_query(F.data == "adm:backup")
async def cb_backup(call: CallbackQuery) -> None:
    """Консистентная копия SQLite (sqlite3 backup API работает и при WAL) → файлом в чат."""
    await call.answer("Готовлю бэкап…")
    src = get_settings().db_path
    dst = os.path.join(tempfile.gettempdir(), f"tenribot-{date.today():%Y%m%d}.db")

    def _do_backup() -> None:
        with sqlite3.connect(src) as con, sqlite3.connect(dst) as out:
            con.backup(out)

    try:
        await asyncio.to_thread(_do_backup)
        await call.message.answer_document(
            FSInputFile(dst, filename=os.path.basename(dst)),
            caption="💾 Бэкап базы. Храните в надёжном месте — внутри персональные данные.")
    except Exception as exc:
        await call.message.answer(f"⚠️ Бэкап не удался: {exc}")
    finally:
        try:
            os.remove(dst)
        except OSError:
            pass
