"""AI-ассистент философии Тенри-Равновесия.

Провайдеры — services/ai_providers.py (Gemini основной, OpenAI резерв).
Защита расходов: лимит сообщений на лида/день, глобальный дневной бюджет токенов,
антифлуд-кулдаун, окно контекста. Промпт — редактируемый BotText 'assistant_prompt'.
"""
from __future__ import annotations

import logging
import time

from config import get_settings
from db import repo
from db.models import Lead
from services.ai_providers import AIProviderError, complete, provider_chain

log = logging.getLogger(__name__)

FALLBACK = ("Ассистент сейчас недоступен. Нажмите «Задать вопрос» → «Написать человеку» — "
            "команда ответит вам лично.")
LIMIT_MSG = ("На сегодня лимит вопросов ассистенту исчерпан 🙏 Возвращайтесь завтра "
             "или напишите человеку через «Задать вопрос».")
BUDGET_MSG = ("Ассистент сегодня отдыхает — дневной лимит проекта исчерпан 🌿 "
              "Напишите человеку через «Задать вопрос», вам обязательно ответят.")
COOLDOWN_MSG = "Секунду, я ещё думаю над прошлым вопросом 🙏"

_COOLDOWN_SEC = 2.0
_last_call: dict[int, float] = {}  # lead_id → monotonic time (антифлуд)


async def is_enabled() -> bool:
    return bool(provider_chain()) and (await repo.get_setting("assistant_enabled", "1")) == "1"


async def reply(lead: Lead, user_text: str) -> str:
    settings = get_settings()

    # антифлуд: не чаще одного запроса к LLM в 2 секунды на лида
    now = time.monotonic()
    if now - _last_call.get(lead.id, 0.0) < _COOLDOWN_SEC:
        return COOLDOWN_MSG
    _last_call[lead.id] = now

    # лимит на лида в сутки
    if await repo.assistant_msgs_today(lead.id) >= settings.assistant_daily_limit:
        return LIMIT_MSG
    # глобальный дневной бюджет токенов проекта
    if settings.ai_daily_token_budget and \
            await repo.ai_tokens_today() >= settings.ai_daily_token_budget:
        log.warning("глобальный AI-бюджет исчерпан")
        return BUDGET_MSG

    prompt_bt = await repo.get_text("assistant_prompt")
    system = prompt_bt.text if prompt_bt else "Ты — ассистент проекта Тенри-Равновесие."
    if lead.first_name:  # только имя — никаких недоверенных полей в системный промпт
        system += f"\n\nСобеседника зовут {lead.first_name[:64]}."

    history = await repo.assistant_history(lead.id, settings.assistant_history_window)
    messages = [{"role": m.role, "content": m.content} for m in history]
    messages.append({"role": "user", "content": user_text})

    await repo.add_assistant_msg(lead.id, "user", user_text)
    try:
        text, tokens, provider = await complete(system, messages)
        await repo.add_assistant_msg(lead.id, "assistant", text, tokens=tokens)
        log.info("ассистент ответил лиду %s через %s (%s ток.)", lead.telegram_id, provider, tokens)
        return text
    except AIProviderError as exc:
        log.error("все AI-провайдеры недоступны: %s", exc)
        return FALLBACK
