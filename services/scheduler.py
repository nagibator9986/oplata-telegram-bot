"""Фоновый планировщик: рассылки по расписанию, дожим, донат-офферы.

Один asyncio-таск, тик каждые 30 секунд. Всё состояние — в SQLite, поэтому
рестарт процесса ничего не теряет.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from db import repo
from db.models import utcnow
from services import broadcaster, runtime, schedule_calc
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
            await repo.mark_followup(task.id, "sent" if ok else "cancelled")
            await repo.log_message(lead_id=lead.id, kind="followup",
                                   status="sent" if ok else "blocked")

    # 3. Донат-офферы и напоминания
    if schedule_calc.quiet_hours_ok(now):
        delay = int(await repo.get_setting("donation_delay_hours", "24") or 24)
        for lead in await repo.leads_due_donation_offer(delay):
            from handlers.donation import send_donation_offer  # локально — избегаем цикла
            await send_donation_offer(bot, lead)
        for lead in await repo.leads_due_donation_reminder():
            ok = await send_slot(bot, lead.telegram_id, "donation_reminder", lead)
            await repo.update_lead(lead.telegram_id, donation_reminded_at=now)
            if ok:
                await repo.log_message(lead_id=lead.id, kind="donation", status="reminded")
