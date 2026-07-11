"""Модели данных tenri-bot (SQLite)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


def utcnow() -> datetime:
    """Наивный UTC — единый формат времени в БД."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Funnel:
    """Состояния воронки. Переходы — только вперёд (кроме LOST)."""

    NEW = "new"
    WELCOMED = "welcomed"
    PHILOSOPHY = "philosophy_shown"
    INVITED = "invited_to_group"
    JOINED = "joined_group"
    SURVEYING = "surveying"
    SURVEY_DONE = "survey_done"
    DON_OFFERED = "donation_offered"
    DONATED = "donated"
    LOST = "lost"
    UNQUALIFIED = "unqualified"  # не прошёл фильтр масштаба — зовём в клуб, аудит не запускаем

    ORDER = [NEW, WELCOMED, PHILOSOPHY, INVITED, JOINED,
             SURVEYING, SURVEY_DONE, DON_OFFERED, DONATED]

    LABELS = {
        NEW: "новый", WELCOMED: "приветствие", PHILOSOPHY: "философия",
        INVITED: "приглашён в группу", JOINED: "в группе",
        SURVEYING: "проходит анкету", SURVEY_DONE: "анкета пройдена",
        DON_OFFERED: "предложен донат", DONATED: "донат ✅", LOST: "потерян",
        UNQUALIFIED: "не прошёл фильтр (в клуб)",
    }


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Lead(TimestampMixin, Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    first_name: Mapped[str] = mapped_column(String(128), default="")
    last_name: Mapped[str] = mapped_column(String(128), default="")
    phone: Mapped[str] = mapped_column(String(24), default="")
    funnel_state: Mapped[str] = mapped_column(String(32), default=Funnel.NEW, index=True)
    # Прошёл фильтр масштаба (qualify-вопросы). Только qualified лид получает ссылку в
    # группу и продолжение аудита. UNQUALIFIED — терминальный отказ, здесь остаётся False.
    qualified: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(64), default="")
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)      # заблокировал бота (403)
    do_not_disturb: Mapped[bool] = mapped_column(Boolean, default=False)  # нажал «Не беспокоить»
    in_group: Mapped[bool] = mapped_column(Boolean, default=False)
    invite_link: Mapped[str] = mapped_column(String(256), default="")     # персональная ссылка в группу
    group_joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    group_left_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    survey_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Углублённый аудит: лид выбрал его в анкете; после её завершения получает ссылки для команды
    deep_audit: Mapped[bool] = mapped_column(Boolean, default=False)
    company_name: Mapped[str] = mapped_column(String(200), default="")  # из паспорта анкеты
    donation_offered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    donation_reminded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    donated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    @property
    def display_name(self) -> str:
        name = f"{self.first_name} {self.last_name}".strip() or "Без имени"
        return f"{name} (@{self.username})" if self.username else name


class BotText(TimestampMixin, Base):
    """Редактируемый слот контента: сообщение воронки/дожима/оффера."""

    __tablename__ = "bot_texts"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    media_file_id: Mapped[str] = mapped_column(String(256), default="")
    buttons: Mapped[list] = mapped_column(JSON, default=list)  # [[{"text","url"|"cb"|"start"}]]
    updated_by: Mapped[str] = mapped_column(String(64), default="")


class TextRevision(Base):
    """История правок BotText — для отката."""

    __tablename__ = "text_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), index=True)
    old_text: Mapped[str] = mapped_column(Text, default="")
    old_media_file_id: Mapped[str] = mapped_column(String(256), default="")
    edited_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Broadcast(TimestampMixin, Base):
    __tablename__ = "broadcasts"

    TARGET_GROUP = "group"
    TARGET_DM_ALL = "dm_all"
    TARGET_DM_SEGMENT = "dm_segment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    target: Mapped[str] = mapped_column(String(16), default=TARGET_GROUP)
    segment: Mapped[dict] = mapped_column(JSON, default=dict)   # {"preset": "in_group"|...}
    text: Mapped[str] = mapped_column(Text, default="")
    media_file_id: Mapped[str] = mapped_column(String(256), default="")
    buttons: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|active|sending|done|failed
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)      # {"sent","failed","blocked","runs"}


class Schedule(Base):
    __tablename__ = "schedules"

    KIND_ONCE = "once"
    KIND_DAILY = "daily"
    KIND_WEEKLY = "weekly"
    KIND_MONTHLY = "monthly"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    broadcast_id: Mapped[int] = mapped_column(ForeignKey("broadcasts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)      # once (UTC)
    time_of_day: Mapped[str] = mapped_column(String(5), default="")               # "HH:MM" (локальное)
    weekdays: Mapped[list] = mapped_column(JSON, default=list)                    # [0..6], пн=0
    day_of_month: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FollowUpRule(Base):
    """Шаг дожим-серии: что и через сколько часов слать после входа в состояние."""

    __tablename__ = "followup_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger_state: Mapped[str] = mapped_column(String(32), index=True)
    step_order: Mapped[int] = mapped_column(Integer, default=1)
    delay_hours: Mapped[int] = mapped_column(Integer, default=24)
    text_key: Mapped[str] = mapped_column(String(64))
    stop_states: Mapped[list] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class FollowUpTask(Base):
    __tablename__ = "followup_tasks"
    __table_args__ = (UniqueConstraint("lead_id", "rule_id", name="uq_followup_lead_rule"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("followup_rules.id", ondelete="CASCADE"))
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|sent|skipped|cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SurveyQuestion(TimestampMixin, Base):
    __tablename__ = "survey_questions"

    TYPES = ("text", "number", "choice", "multichoice", "contact")

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    field_type: Mapped[str] = mapped_column(String(16), default="text")
    options: Mapped[list] = mapped_column(JSON, default=list)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)  # soft-delete
    # Стадия анкеты: qualify (фильтр) | passport (паспорт) | audit_choice | main (основная)
    # | manager (блок менеджеров углублённого аудита — в основной поток не попадает)
    stage: Mapped[str] = mapped_column(String(16), default="main")
    # Значения вариантов, при которых лид не проходит фильтр (мягкий отказ)
    disqualify_if: Mapped[list] = mapped_column(JSON, default=list)
    # Ключ BotText, который показать ПЕРЕД этим вопросом (вводный текст блока)
    intro_key: Mapped[str] = mapped_column(String(64), default="")

    STAGES = ("qualify", "passport", "audit_choice", "main", "manager")


class SurveyResponse(TimestampMixin, Base):
    __tablename__ = "survey_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    # kind: lead — основная анкета воронки; participant — анкета участника углублённого аудита.
    # Разделение нужно, чтобы прохождения не пересекались (один человек может быть и тем и другим).
    kind: Mapped[str] = mapped_column(String(16), default="lead", index=True)
    # снапшот вопрос+ответ — история не ломается при правке анкеты
    answers: Mapped[list] = mapped_column(JSON, default=list)  # [{"q": "...", "a": "..."}]
    current_index: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="in_progress")  # in_progress|completed
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditParticipant(TimestampMixin, Base):
    """Персональная ссылка участника углублённого аудита (Блок 4 ТЗ).

    Владелец получает набор ссылок и раздаёт их команде; одна ссылка — один участник.
    Ответы участника хранятся в SurveyResponse (kind=participant) его собственного лида,
    но участник НЕ входит в маркетинговую воронку (funnel не двигаем, дожим не шлём).
    """

    __tablename__ = "audit_participants"

    ROLE_LABELS = {"co_owner": "Совладелец", "top": "Топ-менеджер", "manager": "Менеджер"}
    STATUS_LABELS = {"created": "ссылка не открыта", "claimed": "открыл ссылку",
                     "in_progress": "заполняет", "completed": "завершил ✅"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    token: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(32), default="")  # «Участник 1» … для владельца
    participant_lead_id: Mapped[int | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(16), default="")   # co_owner | top | manager
    response_id: Mapped[int | None] = mapped_column(
        ForeignKey("survey_responses.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="created")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def role_label(self) -> str:
        return self.ROLE_LABELS.get(self.role, "роль не выбрана")


class AssistantMessage(Base):
    __tablename__ = "assistant_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(12))  # user|assistant
    content: Mapped[str] = mapped_column(Text)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class CloserMessage(Base):
    """История диалога AI-продажника («дожим» в личке).

    Отдельно от AssistantMessage: у продажника своя персона и контекст, поэтому
    история и per-lead суточный лимит считаются раздельно. Токены при этом идут в
    ОБЩИЙ дневной бюджет (repo.ai_tokens_today суммирует обе таблицы)."""

    __tablename__ = "closer_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(12))  # user|assistant
    content: Mapped[str] = mapped_column(Text)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class MessageLog(Base):
    """Журнал исходящих массовых отправок (диагностика + защита от дублей)."""

    __tablename__ = "message_log"
    __table_args__ = (UniqueConstraint("broadcast_id", "lead_id", "run_no", name="uq_bc_lead_run"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    broadcast_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    lead_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_no: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(24), default="")  # broadcast|followup|donation|notify
    status: Mapped[str] = mapped_column(String(16), default="sent")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
