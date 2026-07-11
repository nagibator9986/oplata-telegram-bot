"""Фоновый планировщик: рассылки по расписанию, дожим, донат-офферы.

Один asyncio-таск, тик каждые 30 секунд. Всё состояние — в SQLite, поэтому
рестарт процесса ничего не теряет.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from aiogram.exceptions import TelegramForbiddenError

from db import repo
from db.models import utcnow
from services import broadcaster, closer, runtime, schedule_calc
from services.content import send_slot

log = logging.getLogger(__name__)

TICK_SECONDS = 30


async def scheduler_loop(bot: Bot) -> None:
    await asyncio.sleep(5)  # дать боту стартовать
    log.info("планировщик запущен")
    while True:
        try:
            await _tick(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("тик планировщика упал (продолжаем)")
        await asyncio.sleep(TICK_SECONDS)


async def _tick(bot: Bot) -> None:
    now = utcnow()
    runtime.LAST_TICK = now  # признак живого фона для админской диагностики
    schedule_calc.set_quiet_cache(await repo.get_setting("quiet_hours", "9-21"))

    # 1. Рассылки по расписанию
    for sch in await repo.schedules_due(now):
        # сначала bump — защита от повторного запуска на следующем тике
        await repo.bump_schedule(sch.id, schedule_calc.next_run_for(sch))
        runtime.spawn(broadcaster.run_broadcast(bot, sch.broadcast_id))

    # 2. Дожим (только в «громкие» часы)
    if (await repo.get_setting("followups_enabled", "1")) == "1":
        quiet_ok = schedule_calc.quiet_hours_ok(now)
        for task, rule, lead in await repo.followup_tasks_due(now):
            if lead.is_blocked or lead.do_not_disturb:
                await repo.mark_followup(task.id, "cancelled")
                continue
            if lead.funnel_state != rule.trigger_state:
                await repo.mark_followup(task.id, "skipped")  # лид уже ушёл дальше
                continue
            if not quiet_ok:
                await repo.mark_followup(task.id, "pending", due_at=schedule_calc.next_quiet_ok(now))
                continue
            ok = await send_slot(bot, lead.telegram_id, rule.text_key, lead)
            if ok is None:
                continue  # временный сбой — задача остаётся pending, повторим на след. тике
            await repo.mark_followup(task.id, "sent" if ok else "cancelled")
            await repo.log_message(lead_id=lead.id, kind="followup",
                                   status="sent" if ok else "blocked")

    # 4. Проактивный AI-опенер: продажник сам пишет «замолчавшему» лиду с рекламы
    #    (стадия WELCOMED, нет активности N минут). Ведёт к бесплатному аудиту.
    if (schedule_calc.quiet_hours_ok(now)
            and (await repo.get_setting("closer_enabled", "1")) == "1"
            and (await repo.get_setting("closer_proactive", "1")) == "1"):
        delay_min = int(await repo.get_setting("closer_proactive_delay_min", "10") or 10)
        for lead in await repo.leads_due_proactive_opener(delay_min):
            sent = await _send_proactive_opener(bot, lead)
            if sent is None:
                continue  # временный сбой — повторим на след. тике (opener_at не ставим)
            await repo.update_lead(lead.telegram_id, proactive_opener_at=now)
            if sent:
                await repo.log_message(lead_id=lead.id, kind="closer_opener", status="sent")

    # 3. Донат-офферы и напоминания
    if schedule_calc.quiet_hours_ok(now):
        delay = int(await repo.get_setting("donation_delay_hours", "24") or 24)
        for lead in await repo.leads_due_donation_offer(delay):
            from handlers.donation import send_donation_offer  # локально — избегаем цикла
            await send_donation_offer(bot, lead)
        for lead in await repo.leads_due_donation_reminder():
            ok = await send_slot(bot, lead.telegram_id, "donation_reminder", lead)
            if ok is None:
                continue  # временный сбой — не проставляем reminded_at, повторим позже
            await repo.update_lead(lead.telegram_id, donation_reminded_at=now)
            if ok:
                await repo.log_message(lead_id=lead.id, kind="donation", status="reminded")


async def _send_proactive_opener(bot: Bot, lead) -> bool | None:
    """Отправить проактивный опенер. True=доставлено, False=не доставлено (не ретраим),
    None=временный сбой статики (можно повторить).

    AI-текст (Gemini) с CTA-кнопкой; если AI недоступен/бюджет — статический слот.
    Историю (и учёт токенов) пишем ТОЛЬКО после успешной доставки — сбой отправки не
    плодит фантомные записи и не заставляет генерировать заново (жечь токены)."""
    gen = await closer.generate_opener(lead)
    if gen is None:  # AI недоступен/бюджет — статический опенер (без токенов, ретрай ок)
        return await send_slot(bot, lead.telegram_id, "closer_opener_static", lead)
    text, tokens = gen
    try:
        await bot.send_message(lead.telegram_id, text, reply_markup=closer.cta_keyboard(),
                               disable_web_page_preview=True, parse_mode=None)
    except TelegramForbiddenError:
        await repo.update_lead(lead.telegram_id, is_blocked=True)
        return False  # заблокирован — opener_at ставим, LLM не ретраим
    except Exception:
        log.exception("проактивный опенер лиду %s не отправлен", lead.telegram_id)
        return False  # токены уже потрачены — не ретраим генерацию
    await repo.add_closer_msg(lead.id, "assistant", text, tokens=tokens)  # контекст диалога
    return True
