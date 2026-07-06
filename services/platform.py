"""Интеграция с платформой Baqsy (Django на VPS).

Принципы:
- Бот работает автономно: если сайт недоступен/не настроен — всё продолжает работать.
- Отправка событий — fire-and-forget с одним ретраем, ошибки только в лог.
- Auth — заголовок X-Bot-Token (на бэкенде IsBotAuthenticated, константное сравнение).

События: new_lead, joined_group, left_group, survey_completed (+answers), donated.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from config import get_settings
from db.models import Lead

log = logging.getLogger(__name__)


def enabled() -> bool:
    s = get_settings()
    return bool(s.platform_url and s.platform_token)


def _lead_payload(lead: Lead, event: str, answers: list | None = None) -> dict:
    payload = {
        "telegram_id": lead.telegram_id,
        "username": lead.username,
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "phone": lead.phone,
        "funnel_state": lead.funnel_state,
        "source": lead.source,
        "in_group": lead.in_group,
        "survey_completed_at": lead.survey_completed_at.isoformat() + "Z"
        if lead.survey_completed_at else None,
        "donated_at": lead.donated_at.isoformat() + "Z" if lead.donated_at else None,
        "event": event,
    }
    if answers:
        payload["answers"] = answers
    return payload


async def _post_sync(payload: dict) -> None:
    s = get_settings()
    url = s.platform_url.rstrip("/") + "/api/v1/tenri/leads/sync/"
    headers = {"X-Bot-Token": s.platform_token}
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url, json=payload, headers=headers)
                r.raise_for_status()
                return
        except Exception as exc:
            log.warning("platform sync attempt %s failed: %s", attempt, exc)
            await asyncio.sleep(2)
    log.error("platform sync failed: lead %s event %s (сайт недоступен?)",
              payload.get("telegram_id"), payload.get("event"))


def sync_lead(lead: Lead, event: str, answers: list | None = None) -> None:
    """Фоновая отправка события на сайт (не блокирует диалог с клиентом)."""
    if not enabled():
        return
    from services import runtime
    runtime.spawn(_post_sync(_lead_payload(lead, event, answers)))


async def fetch_donation_requisites() -> str | None:
    """Реквизиты доната с сайта (ContentBlock donation_*) — единый источник с лендингом."""
    if not enabled():
        return None
    s = get_settings()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(s.platform_url.rstrip("/") + "/api/v1/content/")
            r.raise_for_status()
            blocks: dict = r.json()
    except Exception as exc:
        log.warning("не удалось получить контент с сайта: %s", exc)
        return None
    card = (blocks.get("donation_card_number") or "").strip()
    if not card:
        return None
    lines = ["<b>Реквизиты для перевода:</b>", f"Карта: <code>{card}</code>"]
    if blocks.get("donation_card_holder"):
        lines.append(f"Получатель: {blocks['donation_card_holder']}")
    if blocks.get("donation_card_bank"):
        lines.append(f"Банк: {blocks['donation_card_bank']}")
    lines.append("\nПосле перевода нажмите «Я перевёл(а)» — мы скажем спасибо лично.")
    return "\n".join(lines)
