"""Конфигурация tenri-bot.

Параметры читаются из переменных окружения. Имена принимаются в ДВУХ вариантах —
с префиксом `TENRI_` и без него, — чтобы деплой не падал из-за того, что в панели
Railway переменную назвали `BOT_TOKEN` вместо `TENRI_BOT_TOKEN` (частая ошибка).
"""
from __future__ import annotations

import sys
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


def _alias(*names: str) -> AliasChoices:
    """Принимать имя и с префиксом TENRI_, и без него (регистр не важен)."""
    out: list[str] = []
    for n in names:
        out += [f"TENRI_{n}", n]
    return AliasChoices(*out)


class Settings(BaseSettings):
    # env_prefix оставляем для полей без явного alias; case_sensitive=False — TENRI_/tenri_
    model_config = SettingsConfigDict(env_prefix="TENRI_", env_file=".env",
                                      case_sensitive=False, extra="ignore")

    # Обязательное. Принимается как TENRI_BOT_TOKEN, так и BOT_TOKEN.
    bot_token: str = Field(validation_alias=_alias("BOT_TOKEN"))
    group_id: int = Field(0, validation_alias=_alias("GROUP_ID"))
    admin_ids: str = Field("", validation_alias=_alias("ADMIN_IDS"))
    db_path: str = Field("data/tenribot.db", validation_alias=_alias("DB_PATH"))
    # Только TENRI_TIMEZONE/TIMEZONE. Раньше сюда был алиасом привязан POSIX TZ — а его
    # операторы штатно ставят для системного времени/логов контейнера; лишний TZ=UTC молча
    # сдвигал бы тихие часы и расписания. Бизнес-таймзону задаём отдельным именем.
    timezone: str = Field("Asia/Almaty", validation_alias=_alias("TIMEZONE"))
    site_url: str = "https://baqsy.tnriazun.com"

    # Интеграция с платформой Baqsy (Django на VPS). Пусто = автономный режим.
    platform_url: str = ""            # https://baqsy.tnriazun.com (базовый URL сайта)
    platform_token: str = ""          # = BOT_API_SECRET из .env бэкенда

    # AI-ассистент. Основной провайдер — Google Gemini, OpenAI — опциональный резерв.
    ai_provider: str = "gemini"           # gemini | openai (предпочтительный; второй — фолбэк)
    gemini_api_key: str = Field("", validation_alias=_alias("GEMINI_API_KEY"))
    gemini_model: str = "gemini-2.0-flash"
    openai_api_key: str = Field("", validation_alias=_alias("OPENAI_API_KEY"))
    openai_model: str = "gpt-4o-mini"
    assistant_daily_limit: int = 30       # сообщений ассистенту на лида в сутки
    assistant_history_window: int = 10    # сколько последних сообщений слать в контекст
    ai_daily_token_budget: int = 200000   # глобальный дневной лимит токенов (0 = без лимита)

    @property
    def admins(self) -> list[int]:
        out: list[int] = []
        for x in self.admin_ids.replace(" ", "").split(","):
            if not x:
                continue
            try:
                out.append(int(x))
            except ValueError:
                # admins читается на импорте (handlers/admin), поэтому «сырой» ValueError
                # уронил бы старт трейсбеком. Даём понятное сообщение и fail-fast.
                raise SystemExit(
                    "\n" + "=" * 64 + "\n"
                    f"[config] TENRI_ADMIN_IDS содержит нечисловое значение: {x!r}\n"
                    "Укажите только telegram id администраторов через запятую, "
                    "напр. 123456789,987654321\n"
                    + "=" * 64 + "\n"
                ) from None
        return out

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        # Понятное сообщение вместо стены pydantic-трейсбеков в логах Railway.
        def _norm(name: str) -> str:
            name = name.upper()
            return name if name.startswith("TENRI_") else f"TENRI_{name}"

        missing = [_norm(str(e["loc"][0])) for e in exc.errors() if e.get("type") == "missing"]
        names = ", ".join(dict.fromkeys(missing)) or "TENRI_BOT_TOKEN"
        msg = (
            "\n" + "=" * 64 + "\n"
            "[config] НЕ ЗАДАНЫ обязательные переменные окружения: " + names + "\n"
            "Задайте их в Railway -> вкладка Variables и передеплойте:\n"
            "  TENRI_BOT_TOKEN   - токен от @BotFather (ОБЯЗАТЕЛЬНО)\n"
            "  TENRI_GROUP_ID    - id закрытой группы, напр. -1001234567890\n"
            "  TENRI_ADMIN_IDS   - ваш telegram id (напр. 123456789)\n"
            "  TENRI_DB_PATH     - /data/tenribot.db (при подключённом Volume)\n"
            "Имена можно и без префикса TENRI_. Подробнее - docs/DEPLOY_RAILWAY.md\n"
            + "=" * 64 + "\n")
        try:
            sys.stderr.write(msg)
        except UnicodeEncodeError:  # на случай не-UTF-8 локали в контейнере
            sys.stderr.write(msg.encode("utf-8", "backslashreplace").decode("ascii", "ignore"))
        raise SystemExit(1) from None
