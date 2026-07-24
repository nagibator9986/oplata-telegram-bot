"""Проверка осмысленности текстовых ответов анкеты.

Двухуровневая:
1. Быстрая эвристика (check_answer) — ловит явный мусор («...», «ааааа») бесплатно.
2. AI-проверка (check_answer_ai) — Gemini решает «осмысленно / бред» для тонких
   случаев вроде «asdaa», «выфвыфв», «фывфыв» (набор клавиш с гласными, который
   эвристика пропускает). С фолбэком (AI недоступен → принимаем) и учётом бюджета.

Оба уровня НЕ мешают коротким законным ответам («Да», «Нет», «ИП», «Алматы»,
«5 лет») и ссылкам/@handle.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Гласные RU/KZ/EN — для оценки «похоже ли на слова»
_VOWELS = set("аеёиоуыэюяәіөүaeiouy")

# Ссылки/@handle не прогоняем через эвристики: у них своя «нечеловеческая» статистика
_LINK_RE = re.compile(r"https?://|www\.|@\w|[\w-]+\.(?:com|kz|ru|io|me|net|org|co|kz)\b",
                      re.IGNORECASE)

TOO_SHORT = ("Это не похоже на ответ 🙏 Напишите, пожалуйста, по сути вопроса — "
             "хотя бы пару слов.")
GIBBERISH = ("Похоже на случайный набор символов 🙏 Ответьте, пожалуйста, по сути вопроса — "
             "это нужно эксперту для разбора.")
REJECT = ("Это не похоже на ответ по сути вопроса 🙏 Напишите, пожалуйста, конкретный "
          "ответ именно на заданный вопрос — это нужно эксперту для разбора.")

# Явный мат (по границам слов — латиница) — ловим мгновенно, без AI.
_PROFANITY_RE = re.compile(r"\b(?:fuck|shit|bitch|cunt|asshole|dick|bastard)\b", re.IGNORECASE)
# Отписки/оскорбления — блокируем ТОЛЬКО при точном совпадении со всем ответом,
# чтобы не задеть законные фразы, где эти слова — часть предложения.
_REFUSAL_EXACT = {
    "мне все равно", "мне всё равно", "все равно", "всё равно", "пофиг", "по фиг",
    "не важно", "неважно", "какая разница", "без разницы", "не скажу", "не твое дело",
    "не твоё дело", "отстань", "отвали", "i dont care", "i don't care", "idc",
    "fuck you", "fuck", "shit", "нет ответа", "не хочу отвечать",
}


def check_answer(text: str) -> str | None:
    """None — ответ принят; строка — понятный текст ошибки для клиента."""
    t = (text or "").strip()
    if not t:
        return TOO_SHORT
    low = t.lower()
    if low in _REFUSAL_EXACT or _PROFANITY_RE.search(t):
        return REJECT
    if _LINK_RE.search(t):
        return None

    letters = [c for c in t if c.isalpha()]
    has_digit = any(c.isdigit() for c in t)

    # 1) только знаки препинания/символы: «...», «???», «—», «!!!»
    if not letters and not has_digit:
        return TOO_SHORT
    # 2) одна буква и никаких цифр: «а», «х»
    if len(letters) < 2 and not has_digit:
        return TOO_SHORT
    # 3) длинный повтор одного символа: «ааааа», «.....», «11111»
    if re.search(r"(.)\1{4,}", t):
        return GIBBERISH
    # 4) весь ответ — повтор короткого блока: «фывфывфыв», «asdasdasd», «абабаб»
    if re.fullmatch(r"(.{1,4}?)\1{2,}", t.lower().replace(" ", "")):
        return GIBBERISH
    # 5) для длинных буквенных строк — доля уникальных букв и гласных
    if len(letters) >= 6:
        low = [c.lower() for c in letters]
        if len(set(low)) / len(low) < 0.25:
            return GIBBERISH
        vowel_ratio = sum(1 for c in low if c in _VOWELS) / len(low)
        if vowel_ratio < 0.12 or vowel_ratio > 0.88:
            return GIBBERISH
    return None


# ── AI-уровень: Gemini определяет «осмысленно / бред» ────────────────────────

_AI_SYSTEM = (
    "Ты — валидатор ответов на вопрос анкеты бизнес-аудита. Тебе дают ВОПРОС и ОТВЕТ "
    "человека. Реши, является ли ответ добросовестной попыткой ответить ИМЕННО на этот "
    "вопрос.\n\n"
    "OK — ответ по теме вопроса и осмыслен: реальные слова, название компании/бренда, "
    "город, число, ссылка, короткий ответ по сути, «да», «нет»; а также «не знаю»/«нет "
    "такого», если это уместный ответ на данный вопрос.\n\n"
    "BRED — если ответ:\n"
    "• случайный набор символов / набор клавиш («asdasdasd», «выфвыфв», «...», «джждж»);\n"
    "• оскорбление, мат, троллинг («fuck you», «иди на…», «дурак»);\n"
    "• отмашка/отказ вместо ответа («мне всё равно», «i dont care», «не важно», «отстань», "
    "«какая разница»);\n"
    "• явно НЕ на тему вопроса (например на вопрос про сферу деятельности — «i dont care»).\n\n"
    "Если сомневаешься между OK и BRED — выбирай OK. Ответь РОВНО одним словом: OK или BRED."
)


async def check_answer_ai(lead_id: int, question: str, answer: str) -> str | None:
    """None — принять; строка — текст ошибки. Сначала эвристика, затем Gemini.

    При недоступности AI / выключенной проверке / исчерпанном бюджете — принимаем
    (эвристика уже отсекла явный мусор)."""
    err = check_answer(answer)
    if err:
        return err

    # локальные импорты — модуль без AI-зависимостей на импорте
    from config import get_settings
    from db import repo
    from services.ai_providers import AIProviderError, complete, provider_chain

    if not provider_chain():
        return None
    if (await repo.get_setting("answer_ai_check", "1")) != "1":
        return None
    s = get_settings()
    if s.ai_daily_token_budget and await repo.ai_tokens_today() >= s.ai_daily_token_budget:
        return None

    try:
        text, tokens, _ = await complete(
            _AI_SYSTEM,
            [{"role": "user", "content": f"Вопрос: {question[:300]}\nОтвет: {answer[:300]}"}],
            temperature=0.0, max_tokens=8)
    except AIProviderError:
        return None  # AI недоступен — не блокируем прохождение анкеты
    await repo.record_ai_tokens(tokens)
    if "BRED" in text.upper():
        log.info("анкета: AI отклонил ответ %r → бред/не по теме", answer[:60])
        return REJECT
    return None
