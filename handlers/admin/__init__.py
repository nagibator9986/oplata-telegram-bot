"""Админ-роутер: доступ только для telegram_id из TENRI_ADMIN_IDS."""
from __future__ import annotations

from aiogram import F, Router

from config import get_settings

from . import broadcasts, leads, menu, settings_admin, survey_editor, texts

admin_router = Router(name="admin")
_admins = set(get_settings().admins)
admin_router.message.filter(F.from_user.id.in_(_admins))
admin_router.callback_query.filter(F.from_user.id.in_(_admins))

admin_router.include_router(menu.router)
admin_router.include_router(texts.router)
admin_router.include_router(broadcasts.router)
admin_router.include_router(survey_editor.router)
admin_router.include_router(leads.router)
admin_router.include_router(settings_admin.router)
