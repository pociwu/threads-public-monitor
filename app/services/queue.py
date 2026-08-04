from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Account, Job, RuntimeState


def now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def enqueue_unique(
    db: Session,
    *,
    kind: str,
    account_id: int | None,
    content_type: str | None = None,
    priority: int = 100,
    not_before: datetime | None = None,
) -> Job | None:
    existing = db.scalar(
        select(Job).where(
            Job.account_id == account_id,
            Job.kind == kind,
            Job.content_type == content_type,
            Job.status.in_(["queued", "running"]),
        )
    )
    if existing:
        return None
    job = Job(
        account_id=account_id,
        kind=kind,
        content_type=content_type,
        priority=priority,
        not_before=not_before or now_utc(),
    )
    db.add(job)
    db.flush()
    return job


def schedule_due_accounts(db: Session, settings: Settings) -> int:
    now = now_utc()
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


def claim_next_job(db: Session, settings: Settings) -> Job | None:
    now = now_utc()
    if get_daily_batch_count(db, settings) >= settings.daily_batch_limit:
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
    db.flush()
    return job


def next_batch_time(settings: Settings) -> datetime:
    return now_utc() + timedelta(
        seconds=random.randint(settings.batch_min_delay_seconds, settings.batch_max_delay_seconds)
    )


def next_account_due(account: Account, settings: Settings) -> datetime:
    jitter = random.randint(0, settings.schedule_jitter_minutes)
    return now_utc() + timedelta(hours=account.interval_hours, minutes=jitter)
