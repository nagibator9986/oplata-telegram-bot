"""Слой LLM-провайдеров: Gemini (основной) + OpenAI (резерв), единый интерфейс.

- Gemini — через REST (httpx), без дополнительных зависимостей.
- Автоматический фолбэк: если предпочтительный провайдер упал (429/5xx/сеть/пустой
  ответ safety-фильтра) — пробуем второй настроенный.
- Возвращаем (text, tokens, provider_name); наружу — единственное исключение AIProviderError.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from config import get_settings

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class AIProviderError(Exception):
    """Ни один провайдер не смог ответить."""


def provider_chain() -> list[str]:
    """Порядок опроса провайдеров: предпочтительный из конфига, потом остальные с ключами."""
    s = get_settings()
    have = {"gemini": bool(s.gemini_api_key), "openai": bool(s.openai_api_key)}
    pref = s.ai_provider if s.ai_provider in ("gemini", "openai") else "gemini"
    order = [pref] + [p for p in ("gemini", "openai") if p != pref]
    return [p for p in order if have[p]]


async def _gemini(system: str, messages: list[dict], temperature: float,
                  max_tokens: int) -> tuple[str, int]:
    s = get_settings()
    # Gemini v1beta требует, чтобы первый элемент contents имел role 'user'. После
    # неудачного прошлого хода в истории может остаться «висячий» user без ответа —
    # в окне контекста он выталкивает assistant('model') в начало → HTTP 400. Срезаем
    # ведущие не-user ходы, чтобы запрос всегда начинался с пользователя.
    hist = list(messages)
    while hist and hist[0]["role"] != "user":
        hist.pop(0)
    contents = [
        {"role": "user" if m["role"] == "user" else "model",
         "parts": [{"text": m["content"]}]}
        for m in hist
    ]
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    data = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                r = await client.post(
                    GEMINI_URL.format(model=s.gemini_model),
                    json=body,
                    headers={"x-goog-api-key": s.gemini_api_key},
                )
            if r.status_code in (429, 500, 502, 503) and attempt == 1:
                await asyncio.sleep(2)  # перегрузка — один повтор
                continue
            r.raise_for_status()
            data = r.json()
            break
        except httpx.HTTPError as exc:
            if attempt == 2:
                raise AIProviderError(f"gemini: {exc}") from exc
            await asyncio.sleep(2)
    if data is None:
        raise AIProviderError("gemini: нет ответа")

    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, TypeError):
        # пустой кандидат = сработал safety-фильтр или обрезка
        raise AIProviderError(f"gemini: пустой ответ ({data.get('promptFeedback')})")
    if not text:
        raise AIProviderError("gemini: пустой текст")
    tokens = int((data.get("usageMetadata") or {}).get("totalTokenCount", 0))
    return text, tokens


async def _openai(system: str, messages: list[dict], temperature: float,
                  max_tokens: int) -> tuple[str, int]:
    s = get_settings()
    try:
        from openai import AsyncOpenAI

        # async with — иначе httpx-пул клиента (сокеты/fd) течёт: клиент создаётся на
        # каждый вызов и без явного закрытия освобождается только сборщиком мусора.
        async with AsyncOpenAI(api_key=s.openai_api_key) as client:
            resp = await client.chat.completions.create(
                model=s.openai_model,
                messages=[{"role": "system", "content": system}]
                + [{"role": m["role"], "content": m["content"]} for m in messages],
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=45,
            )
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                raise AIProviderError("openai: пустой текст")
            return text, (resp.usage.total_tokens if resp.usage else 0)
    except AIProviderError:
        raise
    except Exception as exc:
        raise AIProviderError(f"openai: {exc}") from exc


_PROVIDERS = {"gemini": _gemini, "openai": _openai}


async def complete(system: str, messages: list[dict], *, temperature: float = 0.6,
                   max_tokens: int = 600) -> tuple[str, int, str]:
    """Опросить провайдеров по цепочке. messages: [{"role": "user"|"assistant", "content": str}]."""
    last: Exception | None = None
    for name in provider_chain():
        try:
            text, tokens = await _PROVIDERS[name](system, messages, temperature, max_tokens)
            return text, tokens, name
        except Exception as exc:  # noqa: BLE001 — ЛЮБАЯ ошибка провайдера не должна ронять бота
            log.warning("провайдер %s не ответил: %s — пробуем следующий", name, exc)
            last = exc
    raise AIProviderError(str(last) if last else "нет настроенных AI-провайдеров")
