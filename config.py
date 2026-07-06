"""Конфигурация tenri-bot. Все параметры — через env-переменные TENRI_* (или .env)."""
from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TENRI_", env_file=".env", extra="ignore")

    bot_token: str
    group_id: int = 0                 # id закрытой группы (-100...)
    admin_ids: str = ""               # "111,222"
    db_path: str = "data/tenribot.db"
    timezone: str = "Asia/Almaty"
    site_url: str = "https://baqsy.tnriazun.com"

    # Интеграция с платформой Baqsy (Django на VPS). Пусто = автономный режим.
    platform_url: str = ""            # https://baqsy.tnriazun.com (базовый URL сайта)
    platform_token: str = ""          # = BOT_API_SECRET из .env бэкенда

    # AI-ассистент. Основной провайдер — Google Gemini, OpenAI — опциональный резерв.
    ai_provider: str = "gemini"           # gemini | openai (предпочтительный; второй — фолбэк)
    gemini_api_key: str = ""              # https://aistudio.google.com/apikey
    gemini_model: str = "gemini-2.0-flash"
    openai_api_key: str = ""              # резервный провайдер (не обязателен)
    openai_model: str = "gpt-4o-mini"
    assistant_daily_limit: int = 30       # сообщений ассистенту на лида в сутки
    assistant_history_window: int = 10    # сколько последних сообщений слать в контекст
    ai_daily_token_budget: int = 200000   # глобальный дневной лимит токенов (0 = без лимита)

    @property
    def admins(self) -> list[int]:
        return [int(x) for x in self.admin_ids.replace(" ", "").split(",") if x]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
