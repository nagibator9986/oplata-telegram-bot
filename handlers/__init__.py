"""Сборка роутеров. Порядок важен: админ → сценарии → fallback."""
from handlers import (
    assistant,
    closer,
    contact,
    deep_audit,
    donation,
    fallback,
    group,
    philosophy,
    start,
    survey,
)
from handlers.admin import admin_router

client_routers = [
    start.router,
    philosophy.router,
    group.router,
    survey.router,
    deep_audit.router,
    donation.router,
    assistant.router,
    closer.router,
    contact.router,
    fallback.router,  # всегда последним
]

admin_routers = [admin_router]
