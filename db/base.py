"""Движок SQLite (aiosqlite) + фабрика сессий."""
from __future__ import annotations

import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
os.makedirs(os.path.dirname(_settings.db_path) or ".", exist_ok=True)

engine = create_async_engine(f"sqlite+aiosqlite:///{_settings.db_path}")
Session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    from db import models  # noqa: F401 — регистрация моделей в metadata

    async with engine.begin() as conn:
        # WAL: читатели не блокируют писателя — важно для планировщика + хендлеров
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA foreign_keys=ON"))
        await conn.run_sync(Base.metadata.create_all)
        # Лёгкая миграция: добавить недостающие колонки в существующих БД
        # (create_all не делает ALTER для уже созданных таблиц).
        await _ensure_columns(conn, "survey_questions", {
            "stage": "VARCHAR(16) DEFAULT 'main'",
            "disqualify_if": "JSON DEFAULT '[]'",
            "intro_key": "VARCHAR(64) DEFAULT ''",
        })
        await _ensure_columns(conn, "leads", {
            "deep_audit": "BOOLEAN DEFAULT 0",
            "company_name": "VARCHAR(200) DEFAULT ''",
        })
        await _ensure_columns(conn, "survey_responses", {
            "kind": "VARCHAR(16) DEFAULT 'lead'",
        })


async def _ensure_columns(conn, table: str, columns: dict[str, str]) -> None:
    rows = await conn.execute(text(f"PRAGMA table_info({table})"))
    existing = {r[1] for r in rows.fetchall()}
    for name, ddl in columns.items():
        if name not in existing:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
