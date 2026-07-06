"""Выполнение рассылок: группа и личка, лимиты Telegram, ретраи, журнал."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from config import get_settings
from db import repo
from db.models import Broadcast
from keyboards.common import kb_from_json
from services.notifier import notify_admins

log = logging.getLogger(__name__)

DM_DELAY = 0.05  # ~20 сообщений/сек — с запасом от лимита Telegram (30/с)

# Сериализация по broadcast_id: run_broadcast запускается fire-and-forget из планировщика,
# кнопки «Отправить сейчас» и мастера. Без лока два параллельных запуска читают один runs,
# получают одинаковый run_no → идемпотентность ломается (дубли DM, порча stats). Процесс один
# (polling), поэтому in-process Lock достаточно.
_locks: dict[int, asyncio.Lock] = {}


def _lock_for(broadcast_id: int) -> asyncio.Lock:
    return _locks.setdefault(broadcast_id, asyncio.Lock())


async def _send_payload(bot: Bot, chat_id: int, b: Broadcast) -> None:
    markup = kb_from_json(b.buttons)
    if b.media_file_id:
        await bot.send_photo(chat_id, b.media_file_id, caption=b.text, reply_markup=markup)
    else:
        await bot.send_message(chat_id, b.text, reply_markup=markup,
                               disable_web_page_preview=True)


async def run_broadcast(bot: Bot, broadcast_id: int) -> None:
    async with _lock_for(broadcast_id):
        await _run_broadcast_locked(bot, broadcast_id)


async def _run_broadcast_locked(bot: Bot, broadcast_id: int) -> None:
    b = await repo.get_broadcast(broadcast_id)
    if b is None:
        return
    settings = get_settings()
    test_mode = (await repo.get_setting("test_mode", "0")) == "1"
    stats = dict(b.stats or {})
    run_no = stats.get("runs", 0) + 1
    stats["runs"] = run_no
    await repo.update_broadcast(b.id, status="sending", stats=stats)
    sent = failed = blocked = 0

    try:
        if b.target == Broadcast.TARGET_GROUP and not test_mode:
            try:
                await _send_payload(bot, settings.group_id, b)
                sent += 1
                # журналируем групповую отправку (для наблюдаемости; lead_id=0 — сентинел)
                await repo.log_message(broadcast_id=b.id, lead_id=0, run_no=run_no,
                                       kind="broadcast", status="sent")
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                await _send_payload(bot, settings.group_id, b)
                sent += 1
            except Exception:
                log.exception("рассылка #%s в группу не ушла", b.id)
                failed += 1
        else:
            if test_mode:
                leads = [await repo.get_lead_by_tg(a) for a in settings.admins]
                leads = [l for l in leads if l]
            else:
                leads = await repo.leads_for_dm(b.target, b.segment)
            for lead in leads:
                if await repo.already_sent(b.id, lead.id, run_no):
                    continue  # повторный запуск после сбоя — не дублируем
                try:
                    await _send_payload(bot, lead.telegram_id, b)
                    sent += 1
                    await repo.log_message(broadcast_id=b.id, lead_id=lead.id, run_no=run_no,
                                           kind="broadcast", status="sent")
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    try:
                        await _send_payload(bot, lead.telegram_id, b)
                        sent += 1
                    except Exception:
                        failed += 1
                except TelegramForbiddenError:
                    blocked += 1
                    await repo.update_lead(lead.telegram_id, is_blocked=True)
                except Exception:
                    failed += 1
                    log.exception("рассылка #%s лиду %s", b.id, lead.telegram_id)
                await asyncio.sleep(DM_DELAY)
    finally:
        stats["sent"] = stats.get("sent", 0) + sent
        stats["failed"] = stats.get("failed", 0) + failed
        stats["blocked"] = stats.get("blocked", 0) + blocked
        has_schedule = any(s.is_active for s in await repo.schedules_for(b.id))
        await repo.update_broadcast(b.id, status="active" if has_schedule else "done", stats=stats)
        if failed:
            await notify_admins(bot, f"⚠️ Рассылка «{b.title or b.id}»: ошибок {failed}, "
                                     f"отправлено {sent}, блокировок {blocked}.")
        log.info("broadcast #%s run %s: sent=%s failed=%s blocked=%s",
                 b.id, run_no, sent, failed, blocked)
