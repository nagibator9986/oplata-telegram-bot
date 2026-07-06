"""Вычисление next_run_at для расписаний (локальная TZ → наивный UTC)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import get_settings
from db.models import Schedule


def _to_utc_naive(local_dt: datetime) -> datetime:
    return local_dt.astimezone(timezone.utc).replace(tzinfo=None)


def _local_now() -> datetime:
    return datetime.now(get_settings().tz)


def parse_hhmm(raw: str) -> tuple[int, int] | None:
    try:
        h, m = raw.strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    return None


def parse_local_dt(raw: str) -> datetime | None:
    """'ДД.ММ.ГГГГ ЧЧ:ММ' (локальное время) → наивный UTC."""
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%y %H:%M"):
        try:
            local = datetime.strptime(raw.strip(), fmt).replace(tzinfo=get_settings().tz)
            return _to_utc_naive(local)
        except ValueError:
            continue
    return None


def next_run(kind: str, *, run_at: datetime | None = None, time_of_day: str = "",
             weekdays: list | None = None, day_of_month: int = 0,
             after: datetime | None = None) -> datetime | None:
    """Следующий запуск в наивном UTC. after — якорь «не раньше чем» (по умолчанию now)."""
    if kind == Schedule.KIND_ONCE:
        return run_at

    hm = parse_hhmm(time_of_day)
    if hm is None:
        return None
    h, m = hm
    anchor = after.replace(tzinfo=timezone.utc).astimezone(get_settings().tz) if after else _local_now()

    if kind == Schedule.KIND_DAILY:
        cand = anchor.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= anchor:
            cand += timedelta(days=1)
        return _to_utc_naive(cand)

    if kind == Schedule.KIND_WEEKLY:
        days = sorted(set(int(d) for d in (weekdays or []))) or [0]
        for offset in range(0, 8):
            cand = (anchor + timedelta(days=offset)).replace(hour=h, minute=m, second=0, microsecond=0)
            if cand.weekday() in days and cand > anchor:
                return _to_utc_naive(cand)
        return None

    if kind == Schedule.KIND_MONTHLY:
        day = min(max(day_of_month, 1), 28)  # 1..28 — существует в любом месяце
        cand = anchor.replace(day=day, hour=h, minute=m, second=0, microsecond=0)
        if cand <= anchor:
            year, month = (anchor.year + 1, 1) if anchor.month == 12 else (anchor.year, anchor.month + 1)
            cand = cand.replace(year=year, month=month)
        return _to_utc_naive(cand)

    return None


def next_run_for(sch: Schedule) -> datetime | None:
    """Пересчёт после срабатывания: once → None (деактивация).

    Якорь — прежний next_run_at, а не «сейчас»: при простое (redeploy/downtime) без этого
    все пропущенные слоты между старым next_run_at и now молча терялись. Догоняем до
    ближайшего строго будущего слота (пропущенные не отправляем повторно — только не теряем ритм).
    """
    if sch.kind == Schedule.KIND_ONCE:
        return None
    from db.models import utcnow
    now = utcnow()
    after = sch.next_run_at or now
    nxt = next_run(sch.kind, time_of_day=sch.time_of_day,
                   weekdays=sch.weekdays, day_of_month=sch.day_of_month, after=after)
    guard = 0
    while nxt is not None and nxt <= now and guard < 1000:  # догнать до будущего
        nxt = next_run(sch.kind, time_of_day=sch.time_of_day,
                       weekdays=sch.weekdays, day_of_month=sch.day_of_month, after=nxt)
        guard += 1
    return nxt


# Кэш quiet_hours, обновляется планировщиком раз в тик (чтобы не дёргать БД из sync-функций)
_QUIET_CACHE: dict = {"value": "9-21"}


def set_quiet_cache(value: str) -> None:
    _QUIET_CACHE["value"] = value or "9-21"


def quiet_hours_ok(now_utc: datetime | None = None) -> bool:
    """Можно ли сейчас слать инициативные сообщения (дожим/офферы)."""
    from db.models import utcnow  # локальный импорт — избегаем цикла
    raw = _QUIET_CACHE.get("value", "9-21")
    try:
        start, end = (int(x) for x in raw.split("-"))
    except ValueError:
        start, end = 9, 21
    local = (now_utc or utcnow()).replace(tzinfo=timezone.utc).astimezone(get_settings().tz)
    return start <= local.hour < end


def next_quiet_ok(now_utc: datetime) -> datetime:
    """Ближайший момент (UTC naive), когда тихие часы закончатся."""
    raw = _QUIET_CACHE.get("value", "9-21")
    try:
        start, _end = (int(x) for x in raw.split("-"))
    except ValueError:
        start = 9
    local = now_utc.replace(tzinfo=timezone.utc).astimezone(get_settings().tz)
    cand = local.replace(hour=start, minute=5, second=0, microsecond=0)
    if cand <= local:
        cand += timedelta(days=1)
    return _to_utc_naive(cand)
