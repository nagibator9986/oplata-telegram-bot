"""Репозиторий: весь доступ к данным. Каждая функция — своя сессия (низкая нагрузка, SQLite)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select, update

from db.base import Session
from db.models import (
    AssistantMessage,
    AuditParticipant,
    BotText,
    Broadcast,
    CloserMessage,
    FollowUpRule,
    FollowUpTask,
    Funnel,
    Lead,
    MessageLog,
    Schedule,
    Setting,
    SurveyQuestion,
    SurveyResponse,
    TextRevision,
    utcnow,
)

# ---------------------------------------------------------------- Leads


async def upsert_lead(user, source: str = "") -> Lead:
    """Апсерт лида по telegram-пользователю. Обновляет username/имя при каждом апдейте."""
    async with Session() as s:
        lead = (await s.execute(select(Lead).where(Lead.telegram_id == user.id))).scalar_one_or_none()
        if lead is None:
            lead = Lead(
                telegram_id=user.id,
                username=user.username or "",
                first_name=user.first_name or "",
                last_name=user.last_name or "",
                source=source,
            )
            s.add(lead)
        else:
            lead.username = user.username or ""
            lead.first_name = user.first_name or ""
            lead.last_name = user.last_name or ""
            if lead.is_blocked:  # написал снова — значит разблокировал
                lead.is_blocked = False
        await s.commit()
        return lead


async def get_lead_by_tg(telegram_id: int) -> Lead | None:
    async with Session() as s:
        return (await s.execute(select(Lead).where(Lead.telegram_id == telegram_id))).scalar_one_or_none()


async def get_lead(lead_id: int) -> Lead | None:
    async with Session() as s:
        return await s.get(Lead, lead_id)


async def update_lead(telegram_id: int, **fields) -> Lead | None:
    async with Session() as s:
        lead = (await s.execute(select(Lead).where(Lead.telegram_id == telegram_id))).scalar_one_or_none()
        if lead is None:
            return None
        for k, v in fields.items():
            setattr(lead, k, v)
        await s.commit()
        return lead


async def search_leads(query: str, limit: int = 10) -> list[Lead]:
    q = f"%{query.lstrip('@')}%"
    async with Session() as s:
        rows = await s.execute(
            select(Lead).where(or_(
                Lead.username.ilike(q), Lead.first_name.ilike(q),
                Lead.last_name.ilike(q), Lead.phone.ilike(q),
            )).order_by(Lead.updated_at.desc()).limit(limit)
        )
        return list(rows.scalars())


async def all_leads() -> list[Lead]:
    """Все лиды для экспорта CSV."""
    async with Session() as s:
        return list((await s.execute(select(Lead).order_by(Lead.id))).scalars())


async def recent_leads(limit: int = 10) -> list[Lead]:
    async with Session() as s:
        rows = await s.execute(select(Lead).order_by(Lead.created_at.desc()).limit(limit))
        return list(rows.scalars())


def _participant_lead_ids_subq():
    """Подзапрос: id лидов, которые пришли только как участники чужого аудита."""
    return select(AuditParticipant.participant_lead_id).where(
        AuditParticipant.participant_lead_id.is_not(None))


async def leads_for_dm(target: str, segment: dict) -> list[Lead]:
    """Получатели DM-рассылки. Blocked, do_not_disturb и участники аудита исключаются всегда."""
    async with Session() as s:
        stmt = select(Lead).where(
            Lead.is_blocked.is_(False), Lead.do_not_disturb.is_(False),
            Lead.id.not_in(_participant_lead_ids_subq()))
        preset = (segment or {}).get("preset", "")
        if target == Broadcast.TARGET_DM_SEGMENT:
            if preset == "in_group":
                stmt = stmt.where(Lead.in_group.is_(True))
            elif preset == "not_in_group":
                stmt = stmt.where(Lead.in_group.is_(False))
            elif preset == "survey_done":
                stmt = stmt.where(Lead.survey_completed_at.is_not(None))
            elif preset == "donated":
                stmt = stmt.where(Lead.donated_at.is_not(None))
        return list((await s.execute(stmt)).scalars())


async def stats_summary() -> dict:
    async with Session() as s:
        now = utcnow()

        # Участники чужого аудита не в воронке — их не считаем как лидов проекта
        not_participant = Lead.id.not_in(_participant_lead_ids_subq())

        async def cnt(*where) -> int:
            return (await s.execute(
                select(func.count(Lead.id)).where(not_participant, *where))).scalar() or 0

        funnel_rows = await s.execute(
            select(Lead.funnel_state, func.count(Lead.id))
            .where(not_participant).group_by(Lead.funnel_state))
        surveys_done = (await s.execute(
            select(func.count(SurveyResponse.id)).where(SurveyResponse.status == "completed",
                                                        SurveyResponse.kind == "lead")
        )).scalar() or 0
        deep_participants_done = (await s.execute(
            select(func.count(AuditParticipant.id))
            .where(AuditParticipant.status == "completed"))).scalar() or 0
        _day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # Токены за сегодня — ассистент + продажник (общий дневной бюджет, ср. ai_tokens_today).
        tokens_today = ((await s.execute(
            select(func.coalesce(func.sum(AssistantMessage.tokens), 0))
            .where(AssistantMessage.created_at >= _day0))).scalar() or 0) + ((await s.execute(
            select(func.coalesce(func.sum(CloserMessage.tokens), 0))
            .where(CloserMessage.created_at >= _day0))).scalar() or 0)
        return {
            "total": await cnt(),
            "today": await cnt(Lead.created_at >= now - timedelta(days=1)),
            "week": await cnt(Lead.created_at >= now - timedelta(days=7)),
            "in_group": await cnt(Lead.in_group.is_(True)),
            "blocked": await cnt(Lead.is_blocked.is_(True)),
            "donated": await cnt(Lead.donated_at.is_not(None)),
            "surveys_done": surveys_done,
            "deep_leads": await cnt(Lead.deep_audit.is_(True)),
            "deep_participants_done": deep_participants_done,
            "funnel": dict(funnel_rows.all()),
            "ai_tokens_today": tokens_today,
        }

# ---------------------------------------------------------------- BotText


async def get_text(key: str) -> BotText | None:
    async with Session() as s:
        return await s.get(BotText, key)


async def list_texts() -> list[BotText]:
    async with Session() as s:
        return list((await s.execute(select(BotText).order_by(BotText.key))).scalars())


async def save_text(key: str, *, text: str | None = None, media_file_id: str | None = None,
                    edited_by: str = "") -> BotText | None:
    async with Session() as s:
        bt = await s.get(BotText, key)
        if bt is None:
            return None
        s.add(TextRevision(key=key, old_text=bt.text, old_media_file_id=bt.media_file_id,
                           edited_by=edited_by))
        if text is not None:
            bt.text = text
        if media_file_id is not None:
            bt.media_file_id = media_file_id
        bt.updated_by = edited_by
        await s.commit()
        return bt


async def revert_text(key: str) -> bool:
    async with Session() as s:
        rev = (await s.execute(
            select(TextRevision).where(TextRevision.key == key)
            .order_by(TextRevision.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        if rev is None:
            return False
        bt = await s.get(BotText, key)
        bt.text, bt.media_file_id = rev.old_text, rev.old_media_file_id
        await s.execute(delete(TextRevision).where(TextRevision.id == rev.id))
        await s.commit()
        return True

# ---------------------------------------------------------------- Settings


async def get_setting(key: str, default: str = "") -> str:
    async with Session() as s:
        row = await s.get(Setting, key)
        return row.value if row else default


async def set_setting(key: str, value: str) -> None:
    async with Session() as s:
        row = await s.get(Setting, key)
        if row:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))
        await s.commit()

# ---------------------------------------------------------------- Broadcasts & schedules


async def create_broadcast(**kw) -> Broadcast:
    async with Session() as s:
        b = Broadcast(**kw)
        s.add(b)
        await s.commit()
        return b


async def get_broadcast(broadcast_id: int) -> Broadcast | None:
    async with Session() as s:
        return await s.get(Broadcast, broadcast_id)


async def list_broadcasts(limit: int = 10) -> list[Broadcast]:
    async with Session() as s:
        rows = await s.execute(select(Broadcast).order_by(Broadcast.id.desc()).limit(limit))
        return list(rows.scalars())


async def update_broadcast(broadcast_id: int, **fields) -> None:
    async with Session() as s:
        await s.execute(update(Broadcast).where(Broadcast.id == broadcast_id).values(**fields))
        await s.commit()


async def delete_broadcast(broadcast_id: int) -> None:
    async with Session() as s:
        await s.execute(delete(Schedule).where(Schedule.broadcast_id == broadcast_id))
        await s.execute(delete(Broadcast).where(Broadcast.id == broadcast_id))
        await s.commit()


async def add_schedule(broadcast_id: int, kind: str, *, run_at=None, time_of_day: str = "",
                       weekdays: list | None = None, day_of_month: int = 0,
                       next_run_at=None) -> Schedule:
    async with Session() as s:
        sch = Schedule(broadcast_id=broadcast_id, kind=kind, run_at=run_at,
                       time_of_day=time_of_day, weekdays=weekdays or [],
                       day_of_month=day_of_month, next_run_at=next_run_at)
        s.add(sch)
        await s.commit()
        return sch


async def schedules_for(broadcast_id: int) -> list[Schedule]:
    async with Session() as s:
        rows = await s.execute(select(Schedule).where(Schedule.broadcast_id == broadcast_id))
        return list(rows.scalars())


async def schedules_due(now: datetime) -> list[Schedule]:
    async with Session() as s:
        rows = await s.execute(select(Schedule).where(
            Schedule.is_active.is_(True), Schedule.next_run_at.is_not(None),
            Schedule.next_run_at <= now,
        ))
        return list(rows.scalars())


async def bump_schedule(schedule_id: int, next_run_at: datetime | None) -> None:
    """Отметить запуск: once — деактивируется, периодические — получают новый next_run_at."""
    async with Session() as s:
        sch = await s.get(Schedule, schedule_id)
        if sch is None:
            return
        sch.last_run_at = utcnow()
        sch.next_run_at = next_run_at
        if next_run_at is None:
            sch.is_active = False
        await s.commit()


async def toggle_broadcast_schedules(broadcast_id: int) -> bool:
    """Пауза/возобновление всех расписаний рассылки. Возвращает новое состояние is_active."""
    async with Session() as s:
        schedules = list((await s.execute(
            select(Schedule).where(Schedule.broadcast_id == broadcast_id))).scalars())
        if not schedules:
            return False
        new_state = not any(sch.is_active for sch in schedules)
        for sch in schedules:
            sch.is_active = new_state
        await s.commit()
        return new_state

# ---------------------------------------------------------------- Follow-ups


async def followup_rules(trigger_state: str | None = None) -> list[FollowUpRule]:
    async with Session() as s:
        stmt = select(FollowUpRule).where(FollowUpRule.is_active.is_(True))
        if trigger_state:
            stmt = stmt.where(FollowUpRule.trigger_state == trigger_state)
        return list((await s.execute(stmt.order_by(FollowUpRule.step_order))).scalars())


async def create_followup_tasks(lead_id: int, state: str) -> None:
    """При входе лида в состояние — заготовить все шаги серии (идемпотентно по unique)."""
    async with Session() as s:
        rules = list((await s.execute(
            select(FollowUpRule).where(FollowUpRule.is_active.is_(True),
                                       FollowUpRule.trigger_state == state))).scalars())
        existing = {r[0] for r in (await s.execute(
            select(FollowUpTask.rule_id).where(FollowUpTask.lead_id == lead_id))).all()}
        now = utcnow()
        for rule in rules:
            if rule.id not in existing:
                s.add(FollowUpTask(lead_id=lead_id, rule_id=rule.id,
                                   due_at=now + timedelta(hours=rule.delay_hours)))
        await s.commit()


async def cancel_followups_on(lead_id: int, new_state: str) -> None:
    """Отменить pending-задачи, для которых новое состояние — в stop_states правила."""
    async with Session() as s:
        rows = await s.execute(
            select(FollowUpTask, FollowUpRule).join(FollowUpRule, FollowUpTask.rule_id == FollowUpRule.id)
            .where(FollowUpTask.lead_id == lead_id, FollowUpTask.status == "pending")
        )
        for task, rule in rows.all():
            if new_state in (rule.stop_states or []):
                task.status = "cancelled"
        await s.commit()


async def followup_tasks_due(now: datetime) -> list[tuple[FollowUpTask, FollowUpRule, Lead]]:
    async with Session() as s:
        rows = await s.execute(
            select(FollowUpTask, FollowUpRule, Lead)
            .join(FollowUpRule, FollowUpTask.rule_id == FollowUpRule.id)
            .join(Lead, FollowUpTask.lead_id == Lead.id)
            .where(FollowUpTask.status == "pending", FollowUpTask.due_at <= now,
                   Lead.id.not_in(_participant_lead_ids_subq()))  # участники аудита — не в воронке
            .limit(100)
        )
        return [tuple(r) for r in rows.all()]


async def mark_followup(task_id: int, status: str, due_at: datetime | None = None) -> None:
    async with Session() as s:
        task = await s.get(FollowUpTask, task_id)
        if task is None:
            return
        if due_at is not None:
            task.due_at = due_at
        else:
            task.status = status
        await s.commit()

# ---------------------------------------------------------------- Survey


async def active_questions() -> list[SurveyQuestion]:
    """Вопросы основного потока анкеты лида (менеджерский блок сюда не входит)."""
    async with Session() as s:
        rows = await s.execute(select(SurveyQuestion)
                               .where(SurveyQuestion.is_deleted.is_(False),
                                      SurveyQuestion.stage != "manager")
                               .order_by(SurveyQuestion.order))
        return list(rows.scalars())


async def questions_by_stage(*stages: str) -> list[SurveyQuestion]:
    """Активные вопросы указанных стадий (для веток углублённого аудита)."""
    async with Session() as s:
        rows = await s.execute(select(SurveyQuestion)
                               .where(SurveyQuestion.is_deleted.is_(False),
                                      SurveyQuestion.stage.in_(stages))
                               .order_by(SurveyQuestion.order))
        return list(rows.scalars())


async def all_active_questions() -> list[SurveyQuestion]:
    """Все активные вопросы, включая менеджерский блок — для редактора анкеты."""
    async with Session() as s:
        rows = await s.execute(select(SurveyQuestion)
                               .where(SurveyQuestion.is_deleted.is_(False))
                               .order_by(SurveyQuestion.order))
        return list(rows.scalars())


async def hidden_questions() -> list[SurveyQuestion]:
    """Скрытые (удалённые) вопросы — для восстановления."""
    async with Session() as s:
        rows = await s.execute(select(SurveyQuestion)
                               .where(SurveyQuestion.is_deleted.is_(True))
                               .order_by(SurveyQuestion.order))
        return list(rows.scalars())


async def add_question(text: str, field_type: str, options: list, required: bool,
                       stage: str = "main") -> SurveyQuestion:
    async with Session() as s:
        max_order = (await s.execute(select(func.coalesce(func.max(SurveyQuestion.order), 0)))).scalar()
        q = SurveyQuestion(order=max_order + 1, text=text, field_type=field_type,
                           options=options, required=required, stage=stage)
        s.add(q)
        await s.commit()
        return q


async def update_question(question_id: int, **fields) -> None:
    async with Session() as s:
        await s.execute(update(SurveyQuestion).where(SurveyQuestion.id == question_id).values(**fields))
        await s.commit()


async def get_question(question_id: int) -> SurveyQuestion | None:
    async with Session() as s:
        return await s.get(SurveyQuestion, question_id)


async def move_question(question_id: int, direction: int) -> bool:
    """direction: -1 вверх, +1 вниз — обмен order с соседом ТОЙ ЖЕ стадии.

    Перемещение — только внутри своей стадии (qualify/passport/audit_choice/main/manager),
    чтобы порядок блоков анкеты не нарушился (напр. выбор аудита не оказался выше фильтра,
    а вопрос владельца не «уехал» в анкету менеджеров). Возвращает False, если упёрлись
    в границу стадии.
    """
    moving = await get_question(question_id)
    if moving is None:
        return False
    qs = await (questions_by_stage("manager") if moving.stage == "manager"
                else active_questions())
    # соседи только той же стадии — так границы блоков непреодолимы
    qs = [q for q in qs if q.stage == moving.stage]
    idx = next((i for i, q in enumerate(qs) if q.id == question_id), None)
    if idx is None:
        return False
    other = idx + direction
    if not 0 <= other < len(qs):
        return False
    async with Session() as s:
        a = await s.get(SurveyQuestion, qs[idx].id)
        b = await s.get(SurveyQuestion, qs[other].id)
        a.order, b.order = b.order, a.order
        await s.commit()
    return True


async def get_open_response(lead_id: int) -> SurveyResponse | None:
    """Открытая ОСНОВНАЯ анкета лида (ответы участников аудита не учитываются)."""
    async with Session() as s:
        return (await s.execute(select(SurveyResponse).where(
            SurveyResponse.lead_id == lead_id, SurveyResponse.status == "in_progress",
            SurveyResponse.kind == "lead",
        ))).scalars().first()


async def create_response(lead_id: int, kind: str = "lead") -> SurveyResponse:
    async with Session() as s:
        r = SurveyResponse(lead_id=lead_id, kind=kind)
        s.add(r)
        await s.commit()
        return r


async def get_response(response_id: int) -> SurveyResponse | None:
    async with Session() as s:
        return await s.get(SurveyResponse, response_id)


async def save_answer(response_id: int, entry: dict, next_index: int) -> None:
    async with Session() as s:
        r = await s.get(SurveyResponse, response_id)
        r.answers = list(r.answers or []) + [entry]  # новая ссылка — JSON-поле точно перезапишется
        r.current_index = next_index
        await s.commit()


async def complete_response(response_id: int) -> SurveyResponse:
    async with Session() as s:
        r = await s.get(SurveyResponse, response_id)
        r.status = "completed"
        r.completed_at = utcnow()
        await s.commit()
        return r


async def reject_response(response_id: int) -> None:
    """Закрыть анкету отсеянного фильтром лида — чтобы её нельзя было возобновить."""
    async with Session() as s:
        r = await s.get(SurveyResponse, response_id)
        if r is not None:
            r.status = "rejected"
            r.completed_at = utcnow()
            await s.commit()


async def latest_completed_response(lead_id: int) -> SurveyResponse | None:
    async with Session() as s:
        return (await s.execute(
            select(SurveyResponse).where(SurveyResponse.lead_id == lead_id,
                                         SurveyResponse.status == "completed",
                                         SurveyResponse.kind == "lead")
            .order_by(SurveyResponse.id.desc()).limit(1)
        )).scalar_one_or_none()

# ---------------------------------------------------------------- Углублённый аудит


def _new_token() -> str:
    import secrets
    return secrets.token_hex(8)  # lowercase hex — безопасно для deep-link payload


async def create_participants(owner_lead_id: int, count: int) -> list[AuditParticipant]:
    """Создать персональные ссылки-слоты для команды владельца."""
    async with Session() as s:
        existing = (await s.execute(
            select(func.count(AuditParticipant.id))
            .where(AuditParticipant.owner_lead_id == owner_lead_id))).scalar() or 0
        created = []
        for i in range(count):
            created.append(AuditParticipant(owner_lead_id=owner_lead_id, token=_new_token(),
                                            label=f"Участник {existing + i + 1}"))
        s.add_all(created)
        await s.commit()
        return created


async def participants_for(owner_lead_id: int) -> list[AuditParticipant]:
    async with Session() as s:
        rows = await s.execute(select(AuditParticipant)
                               .where(AuditParticipant.owner_lead_id == owner_lead_id)
                               .order_by(AuditParticipant.id))
        return list(rows.scalars())


async def get_participant(pid: int) -> AuditParticipant | None:
    async with Session() as s:
        return await s.get(AuditParticipant, pid)


async def participant_by_token(token: str) -> AuditParticipant | None:
    async with Session() as s:
        return (await s.execute(select(AuditParticipant)
                                .where(AuditParticipant.token == token))).scalar_one_or_none()


async def open_participation(lead_id: int) -> AuditParticipant | None:
    """Незавершённое участие этого пользователя в чьём-то углублённом аудите."""
    async with Session() as s:
        return (await s.execute(
            select(AuditParticipant).where(
                AuditParticipant.participant_lead_id == lead_id,
                AuditParticipant.status.in_(("claimed", "in_progress")))
            .order_by(AuditParticipant.id))).scalars().first()


async def update_participant(pid: int, **fields) -> None:
    async with Session() as s:
        await s.execute(update(AuditParticipant)
                        .where(AuditParticipant.id == pid).values(**fields))
        await s.commit()

# ---------------------------------------------------------------- Assistant


async def add_assistant_msg(lead_id: int, role: str, content: str, tokens: int = 0) -> None:
    async with Session() as s:
        s.add(AssistantMessage(lead_id=lead_id, role=role, content=content, tokens=tokens))
        await s.commit()


async def assistant_history(lead_id: int, limit: int) -> list[AssistantMessage]:
    async with Session() as s:
        rows = await s.execute(select(AssistantMessage)
                               .where(AssistantMessage.lead_id == lead_id)
                               .order_by(AssistantMessage.id.desc()).limit(limit))
        return list(reversed(list(rows.scalars())))


async def ai_tokens_today() -> int:
    """Суммарный расход токенов за сегодня (глобальный дневной бюджет).

    Считает и ассистента, и продажника — бюджет общий на весь проект."""
    async with Session() as s:
        day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        a = (await s.execute(
            select(func.coalesce(func.sum(AssistantMessage.tokens), 0))
            .where(AssistantMessage.created_at >= day_start)
        )).scalar() or 0
        c = (await s.execute(
            select(func.coalesce(func.sum(CloserMessage.tokens), 0))
            .where(CloserMessage.created_at >= day_start)
        )).scalar() or 0
        return a + c


async def assistant_msgs_today(lead_id: int) -> int:
    async with Session() as s:
        day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        return (await s.execute(select(func.count(AssistantMessage.id)).where(
            AssistantMessage.lead_id == lead_id, AssistantMessage.role == "user",
            AssistantMessage.created_at >= day_start,
        ))).scalar() or 0


# ---------------------------------------------------------------- Closer (продажник)


async def add_closer_msg(lead_id: int, role: str, content: str, tokens: int = 0) -> None:
    async with Session() as s:
        s.add(CloserMessage(lead_id=lead_id, role=role, content=content, tokens=tokens))
        await s.commit()


async def closer_history(lead_id: int, limit: int) -> list[CloserMessage]:
    async with Session() as s:
        rows = await s.execute(select(CloserMessage)
                               .where(CloserMessage.lead_id == lead_id)
                               .order_by(CloserMessage.id.desc()).limit(limit))
        return list(reversed(list(rows.scalars())))


async def closer_msgs_today(lead_id: int) -> int:
    async with Session() as s:
        day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        return (await s.execute(select(func.count(CloserMessage.id)).where(
            CloserMessage.lead_id == lead_id, CloserMessage.role == "user",
            CloserMessage.created_at >= day_start,
        ))).scalar() or 0

# ---------------------------------------------------------------- Donations


async def leads_due_donation_offer(delay_hours: int) -> list[Lead]:
    cutoff = utcnow() - timedelta(hours=delay_hours)
    async with Session() as s:
        rows = await s.execute(select(Lead).where(
            Lead.funnel_state == Funnel.SURVEY_DONE,
            Lead.survey_completed_at.is_not(None), Lead.survey_completed_at <= cutoff,
            Lead.is_blocked.is_(False), Lead.do_not_disturb.is_(False),
            Lead.id.not_in(_participant_lead_ids_subq()),  # участники аудита — не в воронке
        ).limit(50))
        return list(rows.scalars())


async def leads_due_donation_reminder(days: int = 7) -> list[Lead]:
    cutoff = utcnow() - timedelta(days=days)
    async with Session() as s:
        rows = await s.execute(select(Lead).where(
            Lead.funnel_state == Funnel.DON_OFFERED,
            Lead.donation_offered_at.is_not(None), Lead.donation_offered_at <= cutoff,
            Lead.donation_reminded_at.is_(None),
            Lead.is_blocked.is_(False), Lead.do_not_disturb.is_(False),
            Lead.id.not_in(_participant_lead_ids_subq()),  # участники аудита — не в воронке
        ).limit(50))
        return list(rows.scalars())

# ---------------------------------------------------------------- MessageLog


async def log_message(**kw) -> None:
    async with Session() as s:
        s.add(MessageLog(**kw))
        try:
            await s.commit()
        except Exception:  # дубль по unique — уже отправляли, это ок
            await s.rollback()


async def ops_counts() -> dict:
    """Счётчики для админской диагностики: очереди фона."""
    async with Session() as s:
        pending_fu = (await s.execute(select(func.count(FollowUpTask.id))
                      .where(FollowUpTask.status == "pending"))).scalar() or 0
        active_sched = (await s.execute(select(func.count(Schedule.id))
                        .where(Schedule.is_active.is_(True),
                               Schedule.next_run_at.is_not(None)))).scalar() or 0
        questions = (await s.execute(select(func.count(SurveyQuestion.id))
                     .where(SurveyQuestion.is_deleted.is_(False)))).scalar() or 0
        return {"pending_followups": pending_fu, "active_schedules": active_sched,
                "questions": questions}


async def already_sent(broadcast_id: int, lead_id: int, run_no: int) -> bool:
    async with Session() as s:
        return (await s.execute(select(func.count(MessageLog.id)).where(
            MessageLog.broadcast_id == broadcast_id, MessageLog.lead_id == lead_id,
            MessageLog.run_no == run_no,
        ))).scalar() > 0
