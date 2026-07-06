"""FSM воронки: переходы только вперёд + управление дожим-задачами."""
from __future__ import annotations

import logging

from db import repo
from db.models import Funnel, Lead

log = logging.getLogger(__name__)


TERMINAL = (Funnel.LOST, Funnel.UNQUALIFIED)


async def advance(lead: Lead, new_state: str, *, force: bool = False, **extra_fields) -> Lead:
    """Перевести лида в новое состояние (только вперёд; LOST — из любого).

    При переходе: отменяются дожим-задачи, у которых new_state в stop_states,
    и создаются задачи правил с trigger_state == new_state.

    LOST/UNQUALIFIED — терминальны: обычные кнопки воронки НЕ реактивируют лида
    (иначе «Не беспокоить» и мягкий отказ обходятся). Реактивация — только явным
    действием пользователя (/start передаёт force=True).
    """
    current = lead.funnel_state
    if current in TERMINAL and new_state not in TERMINAL and not force:
        if extra_fields:
            lead = await repo.update_lead(lead.telegram_id, **extra_fields) or lead
        return lead

    if new_state != Funnel.LOST and current in Funnel.ORDER and new_state in Funnel.ORDER:
        if Funnel.ORDER.index(new_state) <= Funnel.ORDER.index(current):
            if extra_fields:
                lead = await repo.update_lead(lead.telegram_id, **extra_fields) or lead
            return lead  # назад не ходим

    lead = await repo.update_lead(lead.telegram_id, funnel_state=new_state, **extra_fields) or lead
    await repo.cancel_followups_on(lead.id, new_state)
    if new_state != Funnel.LOST:
        followups_on = await repo.get_setting("followups_enabled", "1")
        if followups_on == "1":
            await repo.create_followup_tasks(lead.id, new_state)
    log.info("lead %s: %s → %s", lead.telegram_id, current, new_state)
    return lead
