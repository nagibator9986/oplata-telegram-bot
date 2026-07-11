"""FSM-состояния (aiogram MemoryStorage; прогресс анкеты дублируется в SQLite)."""
from aiogram.fsm.state import State, StatesGroup


# --- Клиент ---

class SurveyStates(StatesGroup):
    answering = State()


class DeepAuditStates(StatesGroup):
    """Анкета участника углублённого аудита (по персональной ссылке)."""
    choosing_role = State()  # data: pid
    answering = State()      # data: pid, resp_id, qstage


class AssistantStates(StatesGroup):
    chatting = State()


class CloserStates(StatesGroup):
    """AI-продажник: доводит квалифицированного лида до заказа разбора."""
    chatting = State()


class ContactStates(StatesGroup):
    waiting_message = State()


# --- Админ ---

class AdminTextStates(StatesGroup):
    waiting_text = State()   # data: key
    confirm = State()        # data: key, new_text, new_media


class BroadcastWizard(StatesGroup):
    text = State()
    buttons = State()
    target = State()
    when = State()
    once_dt = State()
    time_of_day = State()
    weekdays = State()       # data: weekdays=[...]
    day_of_month = State()
    confirm = State()


class SurveyEditor(StatesGroup):
    q_text = State()
    q_options = State()      # data: q_text, q_type
    edit_text = State()      # data: qid
    edit_options = State()   # data: qid


class LeadAdmin(StatesGroup):
    searching = State()
    writing = State()        # data: lead_id
    noting = State()         # data: lead_id


class AdminReply(StatesGroup):
    replying = State()       # data: lead_id


class AdminSettings(StatesGroup):
    quiet_hours = State()
    donation_delay = State()
    static_link = State()
