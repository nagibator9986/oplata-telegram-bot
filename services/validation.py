"""Проверка осмысленности текстовых ответов анкеты.

Ловит явный мусор («...», «ааааа», «фывфывфыв», «asdasd»), но НЕ мешает
коротким законным ответам («Да», «Нет», «ИП», «Алматы», «5 лет») и ссылкам
(вопросы про сайт и профили).
"""
from __future__ import annotations

import re

# Гласные RU/KZ/EN — для оценки «похоже ли на слова»
_VOWELS = set("аеёиоуыэюяәіөүaeiouy")

# Ссылки/@handle не прогоняем через эвристики: у них своя «нечеловеческая» статистика
_LINK_RE = re.compile(r"https?://|www\.|@\w|[\w-]+\.(?:com|kz|ru|io|me|net|org|co|kz)\b",
                      re.IGNORECASE)

TOO_SHORT = ("Это не похоже на ответ 🙏 Напишите, пожалуйста, по сути вопроса — "
             "хотя бы пару слов.")
GIBBERISH = ("Похоже на случайный набор символов 🙏 Ответьте, пожалуйста, по сути вопроса — "
             "это нужно эксперту для разбора.")


def check_answer(text: str) -> str | None:
    """None — ответ принят; строка — понятный текст ошибки для клиента."""
    t = (text or "").strip()
    if not t:
        return TOO_SHORT
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
