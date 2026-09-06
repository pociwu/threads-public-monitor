from __future__ import annotations

import json
import random
from datetime import UTC, date, datetime, timedelta
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Account, CollectionRun, Content, Job, RuntimeState

GLOBAL_NEXT_BATCH_KEY = "global-next-batch-at"
GLOBAL_RATE_LIMIT_KEY = "global-rate-limit"
GLOBAL_RATE_LIMIT_STATE_VERSION = 1
INTERRUPTED_JOB_MESSAGE = "Worker 重新啟動，已回收中斷工作並重新排隊"
INTERRUPTED_RUN_MESSAGE = "Worker 重新啟動，工作執行中斷"


class GlobalRateLimitState(TypedDict):
    version: int
    consecutive_hits: int
    last_hit_at: datetime
    cooldown_until: datetime
    last_reason: str | None


def now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _naive_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _load_global_rate_limit_state(db: Session) -> GlobalRateLimitState | None:
    row = db.get(RuntimeState, GLOBAL_RATE_LIMIT_KEY)
    if row is None:
        return None
    try:
        payload = json.loads(row.value)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != GLOBAL_RATE_LIMIT_STATE_VERSION:
        return None

    try:
        consecutive_hits = int(payload["consecutive_hits"])
    except (KeyError, TypeError, ValueError):
        return None
    last_hit_at = _parse_datetime(payload.get("last_hit_at"))
    cooldown_until = _parse_datetime(payload.get("cooldown_until"))
    if consecutive_hits < 1 or last_hit_at is None or cooldown_until is None:
        return None
    last_reason = payload.get("last_reason")
    return GlobalRateLimitState(
        version=GLOBAL_RATE_LIMIT_STATE_VERSION,
        consecutive_hits=consecutive_hits,
        last_hit_at=last_hit_at,
        cooldown_until=cooldown_until,
        last_reason=last_reason if isinstance(last_reason, str) else None,
    )


def active_global_rate_limit(
    db: Session, now: datetime | None = None
) -> GlobalRateLimitState | None:
    """Return the persisted global rate-limit state while its cooldown is active."""
    state = _load_global_rate_limit_state(db)
    current = _naive_utc(now) if now is not None else now_utc()
    if state is None or state["cooldown_until"] <= current:
        return None
    return state


def _store_runtime_state(db: Session, key: str, value: str) -> None:
    row = db.get(RuntimeState, key)
    if row is None:
        db.add(RuntimeState(key=key, value=value))
    else:
        row.value = value


def _defer_global_until(db: Session, deadline: datetime) -> datetime:
    deadline = _naive_utc(deadline)
    existing = global_next_batch_at(db)
    effective = max(deadline, existing) if existing is not None else deadline
    _store_runtime_state(db, GLOBAL_NEXT_BATCH_KEY, effective.isoformat())
    return effective


def _scaled_rate_limit_delay(settings: Settings, consecutive_hits: int) -> tuple[int, int]:
    minimum = min(
        settings.rate_limit_initial_min_delay_seconds,
        settings.rate_limit_max_delay_seconds,
    )
    maximum = min(
        settings.rate_limit_initial_max_delay_seconds,
        settings.rate_limit_max_delay_seconds,
    )
    if settings.rate_limit_backoff_multiplier <= 1:
        return minimum, maximum
    remaining = max(consecutive_hits - 1, 0)
    while remaining and (
        minimum < settings.rate_limit_max_delay_seconds
        or maximum < settings.rate_limit_max_delay_seconds
    ):
        minimum = min(
            minimum * settings.rate_limit_backoff_multiplier,
            settings.rate_limit_max_delay_seconds,
        )
        maximum = min(
            maximum * settings.rate_limit_backoff_multiplier,
            settings.rate_limit_max_delay_seconds,
        )
        remaining -= 1
    return minimum, maximum


def defer_for_rate_limit(
    db: Session,
    settings: Settings,
    *,
    retry_after_seconds: int | None = None,
    reason: str | None = None,
    now: datetime | None = None,
) -> tuple[datetime, int]:
    """Persist and return a global cooldown for a Threads rate-limit response."""
    current = _naive_utc(now) if now is not None else now_utc()
    previous = _load_global_rate_limit_state(db)
    reset_after = timedelta(seconds=settings.rate_limit_streak_reset_seconds)
    if previous is not None and current < previous["last_hit_at"] + reset_after:
        consecutive_hits = previous["consecutive_hits"] + 1
    else:
        consecutive_hits = 1

    minimum, maximum = _scaled_rate_limit_delay(settings, consecutive_hits)
    policy_delay = random.randint(minimum, maximum)
    retry_after = max(retry_after_seconds or 0, 0)
    deadline = current + timedelta(seconds=max(policy_delay, retry_after))
    if previous is not None:
        deadline = max(deadline, previous["cooldown_until"])

    payload = {
        "version": GLOBAL_RATE_LIMIT_STATE_VERSION,
        "consecutive_hits": consecutive_hits,
        "last_hit_at": current.isoformat(),
        "cooldown_until": deadline.isoformat(),
        "last_reason": str(reason)[:1000] if reason is not None else None,
    }
    _store_runtime_state(
        db,
        GLOBAL_RATE_LIMIT_KEY,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
    )
    _defer_global_until(db, deadline)
    db.flush()
    return deadline, consecutive_hits


def enqueue_unique(
    db: Session,
    *,
    kind: str,
    account_id: int | None,
    content_id: int | None = None,
    content_type: str | None = None,
    priority: int = 100,
    not_before: datetime | None = None,
) -> Job | None:
    existing = db.scalar(
        select(Job).where(
            Job.account_id == account_id,
            Job.content_id == content_id,
            Job.kind == kind,
            Job.content_type == content_type,
            Job.status.in_(["queued", "running"]),
        )
    )
    if existing:
        return None
    job = Job(
        account_id=account_id,
        content_id=content_id,
        kind=kind,
        content_type=content_type,
        priority=priority,
        not_before=not_before or now_utc(),
    )
    db.add(job)
    db.flush()
    return job


def recover_interrupted_jobs(db: Session, now: datetime | None = None) -> int:
    """Requeue jobs left running when the single worker process was interrupted."""
    recovered_at = _naive_utc(now) if now is not None else now_utc()
    jobs = db.scalars(select(Job).where(Job.status == "running")).all()
    for job in jobs:
        job.status = "queued"
        # claim_next_job() consumes one attempt before execution starts.  A
        # process interruption is not a functional collection failure, so give
        # that in-flight attempt back when recovering the job.
        job.attempts = max(job.attempts - 1, 0)
        job.not_before = recovered_at
        job.started_at = None
        job.finished_at = None
        job.error = INTERRUPTED_JOB_MESSAGE

    runs = db.scalars(select(CollectionRun).where(CollectionRun.status == "running")).all()
    for run in runs:
        run.status = "failed"
        run.finished_at = recovered_at
        run.message = INTERRUPTED_RUN_MESSAGE

    db.flush()
    return len(jobs)


def schedule_content_refreshes(db: Session, settings: Settings, account_id: int) -> int:
    """Queue stored content refreshes with cumulative global-safe spacing."""
    active_content_ids = set(
        db.scalars(
            select(Job.content_id).where(
                Job.account_id == account_id,
                Job.kind == "content_refresh",
                Job.status.in_(["queued", "running"]),
                Job.content_id.is_not(None),
            )
        ).all()
    )
    if active_content_ids:
        return 0
    content_ids = db.scalars(
        select(Content.id)
        .where(Content.account_id == account_id)
        .order_by(Content.published_at.asc().nullsfirst(), Content.id)
    ).all()
    scheduled_at = now_utc()
    queued = 0
    for content_id in content_ids:
        scheduled_at += timedelta(
            seconds=random.randint(
                settings.batch_min_delay_seconds,
                settings.batch_max_delay_seconds,
            )
        )
        job = enqueue_unique(
            db,
            kind="content_refresh",
            account_id=account_id,
            content_id=content_id,
            priority=50,
            not_before=scheduled_at,
        )
        if job:
            queued += 1
    return queued


def schedule_due_accounts(db: Session, settings: Settings) -> int:
    now = now_utc()
    if active_global_rate_limit(db, now) is not None:
        return 0
    accounts = db.scalars(
        select(Account).where(
            Account.enabled.is_(True),
            Account.status.notin_(["login_required"]),
            (Account.cooldown_until.is_(None) | (Account.cooldown_until <= now)),
            (Account.next_due_at.is_(None) | (Account.next_due_at <= now)),
        )
    ).all()
    count = 0
    for account in accounts:
        job = enqueue_unique(
            db,
            kind="verify" if account.status == "pending" else "profile",
            account_id=account.id,
            priority=10,
        )
        if job:
            count += 1
    return count


def _daily_key(day: date) -> str:
    return f"batch-count:{day.isoformat()}"


def get_daily_batch_count(db: Session, settings: Settings, day: date | None = None) -> int:
    local_day = day or datetime.now(settings.tz).date()
    state = db.get(RuntimeState, _daily_key(local_day))
    return int(state.value) if state else 0


def increment_daily_batch_count(db: Session, settings: Settings) -> int:
    key = _daily_key(datetime.now(settings.tz).date())
    state = db.get(RuntimeState, key)
    if state is None:
        state = RuntimeState(key=key, value="1")
        db.add(state)
        return 1
    value = int(state.value) + 1
    state.value = str(value)
    return value


def global_next_batch_at(db: Session) -> datetime | None:
    state = db.get(RuntimeState, GLOBAL_NEXT_BATCH_KEY)
    if not state:
        return None
    return _parse_datetime(state.value)


def defer_global_next_batch(db: Session, settings: Settings, now: datetime) -> datetime:
    current = _naive_utc(now)
    next_at = current + timedelta(
        seconds=random.randint(settings.batch_min_delay_seconds, settings.batch_max_delay_seconds)
    )
    rate_limit = active_global_rate_limit(db, current)
    if rate_limit is not None:
        next_at = max(next_at, rate_limit["cooldown_until"])
    return _defer_global_until(db, next_at)


def claim_next_job(db: Session, settings: Settings) -> Job | None:
    now = now_utc()
    if get_daily_batch_count(db, settings) >= settings.daily_batch_limit:
        return None
    if active_global_rate_limit(db, now) is not None:
        return None
    global_not_before = global_next_batch_at(db)
    if global_not_before and global_not_before > now:
        return None
    job = db.scalar(
        select(Job)
        .where(Job.status == "queued", Job.not_before <= now)
        .order_by(Job.priority, Job.not_before, Job.id)
        .limit(1)
    )
    if not job:
        return None
    job.status = "running"
    job.started_at = now
    job.attempts += 1
    increment_daily_batch_count(db, settings)
    defer_global_next_batch(db, settings, now)
    db.flush()
    return job


def next_batch_time(settings: Settings) -> datetime:
    return now_utc() + timedelta(
        seconds=random.randint(settings.batch_min_delay_seconds, settings.batch_max_delay_seconds)
    )


def next_relationship_retry(settings: Settings, attempts: int) -> datetime:
    """Schedule transient list retries with exponential, long random backoff."""
    multiplier = 2 ** max(attempts - 1, 0)
    minimum = settings.relationship_retry_min_delay_seconds * multiplier
    maximum = settings.relationship_retry_max_delay_seconds * multiplier
    return now_utc() + timedelta(seconds=random.randint(minimum, maximum))


def next_account_due(account: Account, settings: Settings) -> datetime:
    jitter = random.randint(0, settings.schedule_jitter_minutes)
    return now_utc() + timedelta(hours=account.interval_hours, minutes=jitter)
