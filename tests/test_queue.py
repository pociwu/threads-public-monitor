from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models import Account, Job, RuntimeState
from app.services.queue import (
    claim_next_job,
    enqueue_unique,
    next_relationship_retry,
    now_utc,
    schedule_due_accounts,
)


def make_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def test_enqueue_unique_blocks_duplicate_active_job() -> None:
    with make_session() as db:
        account = Account(username="example", next_due_at=now_utc())
        db.add(account)
        db.flush()
        first = enqueue_unique(db, kind="verify", account_id=account.id)
        second = enqueue_unique(db, kind="verify", account_id=account.id)
        assert first is not None
        assert second is None


def test_schedule_and_claim_due_account() -> None:
    settings = Settings(database_url="sqlite:///:memory:", daily_batch_limit=200)
    with make_session() as db:
        account = Account(
            username="due", status="pending", next_due_at=now_utc() - timedelta(minutes=1)
        )
        db.add(account)
        db.commit()
        assert schedule_due_accounts(db, settings) == 1
        db.commit()
        job = claim_next_job(db, settings)
        assert job is not None
        assert job.kind == "verify"
        assert job.status == "running"


def test_disabled_account_is_not_scheduled() -> None:
    settings = Settings(database_url="sqlite:///:memory:")
    with make_session() as db:
        db.add(Account(username="stopped", enabled=False, next_due_at=now_utc()))
        db.commit()
        assert schedule_due_accounts(db, settings) == 0
        assert db.scalar(select(Job)) is None


def test_global_batch_gate_spaces_ready_jobs_across_accounts() -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        batch_min_delay_seconds=180,
        batch_max_delay_seconds=180,
    )
    with make_session() as db:
        first_account = Account(username="first")
        second_account = Account(username="second")
        db.add_all([first_account, second_account])
        db.flush()
        enqueue_unique(db, kind="profile", account_id=first_account.id)
        enqueue_unique(db, kind="profile", account_id=second_account.id)
        db.commit()

        first = claim_next_job(db, settings)
        db.commit()
        second = claim_next_job(db, settings)

        assert first is not None
        assert second is None
        gate = db.get(RuntimeState, "global-next-batch-at")
        assert gate is not None
        assert datetime.fromisoformat(gate.value) >= first.started_at + timedelta(seconds=180)


def test_relationship_retry_delay_doubles_after_each_failed_attempt() -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        relationship_retry_min_delay_seconds=2700,
        relationship_retry_max_delay_seconds=2700,
    )
    fixed_now = datetime(2026, 8, 26, 1, 0)
    with patch("app.services.queue.now_utc", return_value=fixed_now):
        first_retry = next_relationship_retry(settings, attempts=1)
        second_retry = next_relationship_retry(settings, attempts=2)

    assert first_retry == fixed_now + timedelta(minutes=45)
    assert second_retry == fixed_now + timedelta(minutes=90)


def test_requeued_relationship_job_cannot_run_before_backoff() -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        batch_min_delay_seconds=0,
        batch_max_delay_seconds=0,
    )
    fixed_now = datetime(2026, 8, 26, 1, 0)
    retry_at = fixed_now + timedelta(minutes=45)
    with make_session() as db:
        account = Account(username="example", status="active")
        db.add(account)
        db.flush()
        job = Job(
            account_id=account.id,
            kind="relationship",
            content_type="followers",
            status="queued",
            attempts=1,
            not_before=retry_at,
        )
        db.add(job)
        db.commit()

        with patch("app.services.queue.now_utc", return_value=fixed_now):
            assert claim_next_job(db, settings) is None

        with patch("app.services.queue.now_utc", return_value=retry_at):
            claimed = claim_next_job(db, settings)

        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.attempts == 2


@pytest.mark.parametrize(
    "settings",
    [
        {"relationship_max_attempts": 0},
        {"relationship_retry_min_delay_seconds": -1},
        {
            "relationship_retry_min_delay_seconds": 5400,
            "relationship_retry_max_delay_seconds": 2700,
        },
    ],
)
def test_relationship_retry_settings_reject_unsafe_ranges(settings) -> None:
    with pytest.raises(ValidationError):
        Settings(**settings)
