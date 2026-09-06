import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models import Account, CollectionRun, Job, RuntimeState
from app.services.queue import (
    active_global_rate_limit,
    claim_next_job,
    defer_for_rate_limit,
    defer_global_next_batch,
    enqueue_unique,
    next_relationship_retry,
    now_utc,
    recover_interrupted_jobs,
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


def test_persisted_global_rate_limit_blocks_scheduling_and_claiming() -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        batch_min_delay_seconds=0,
        batch_max_delay_seconds=0,
        rate_limit_initial_min_delay_seconds=2700,
        rate_limit_initial_max_delay_seconds=2700,
    )
    fixed_now = datetime(2026, 8, 30, 1, 0)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        account = Account(
            username="due",
            status="active",
            next_due_at=fixed_now - timedelta(minutes=1),
        )
        db.add(account)
        db.flush()
        account_id = account.id
        deadline, hits = defer_for_rate_limit(
            db,
            settings,
            reason="Threads HTTP 429",
            now=fixed_now,
        )
        db.commit()

    assert deadline == fixed_now + timedelta(minutes=45)
    assert hits == 1

    with Session(engine) as db:
        active = active_global_rate_limit(db, fixed_now)
        assert active is not None
        assert active["consecutive_hits"] == 1
        assert active["last_hit_at"] == fixed_now
        assert active["cooldown_until"] == deadline
        assert active["last_reason"] == "Threads HTTP 429"
        persisted = db.get(RuntimeState, "global-rate-limit")
        assert persisted is not None
        payload = json.loads(persisted.value)
        assert payload["cooldown_until"] == deadline.isoformat()
        assert payload["consecutive_hits"] == 1

        with patch("app.services.queue.now_utc", return_value=fixed_now):
            assert schedule_due_accounts(db, settings) == 0
        assert db.scalar(select(Job)) is None

        enqueue_unique(db, kind="profile", account_id=account_id, priority=1)
        db.commit()
        with patch("app.services.queue.now_utc", return_value=fixed_now):
            assert claim_next_job(db, settings) is None


def test_global_rate_limit_backoff_multiplies_by_four_and_caps_at_one_day() -> None:
    settings = Settings(database_url="sqlite:///:memory:")
    first_at = datetime(2026, 8, 30, 1, 0)
    second_at = first_at + timedelta(hours=2)
    third_at = first_at + timedelta(hours=9)
    with (
        make_session() as db,
        patch(
            "app.services.queue.random.randint",
            side_effect=lambda _minimum, maximum: maximum,
        ) as randint,
    ):
        first_deadline, first_hits = defer_for_rate_limit(db, settings, now=first_at)
        second_deadline, second_hits = defer_for_rate_limit(db, settings, now=second_at)
        third_deadline, third_hits = defer_for_rate_limit(db, settings, now=third_at)

    assert (first_hits, second_hits, third_hits) == (1, 2, 3)
    assert first_deadline == first_at + timedelta(minutes=90)
    assert second_deadline == second_at + timedelta(hours=6)
    assert third_deadline == third_at + timedelta(hours=24)
    assert [call.args for call in randint.call_args_list] == [
        (2700, 5400),
        (10800, 21600),
        (43200, 86400),
    ]


def test_retry_after_is_honored_and_later_events_do_not_shorten_cooldown() -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        rate_limit_initial_min_delay_seconds=2700,
        rate_limit_initial_max_delay_seconds=2700,
    )
    fixed_now = datetime(2026, 8, 30, 1, 0)
    with make_session() as db:
        first_deadline, first_hits = defer_for_rate_limit(
            db,
            settings,
            retry_after_seconds=36 * 60 * 60,
            now=fixed_now,
        )
        second_deadline, second_hits = defer_for_rate_limit(
            db,
            settings,
            now=fixed_now + timedelta(minutes=1),
        )

        normal_deadline = defer_global_next_batch(db, settings, fixed_now + timedelta(minutes=2))
        gate = db.get(RuntimeState, "global-next-batch-at")

    assert (first_hits, second_hits) == (1, 2)
    assert first_deadline == fixed_now + timedelta(hours=36)
    assert second_deadline == first_deadline
    assert normal_deadline == first_deadline
    assert gate is not None
    assert datetime.fromisoformat(gate.value) == first_deadline


def test_global_rate_limit_streak_resets_after_twenty_four_quiet_hours() -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        rate_limit_initial_min_delay_seconds=2700,
        rate_limit_initial_max_delay_seconds=2700,
        rate_limit_streak_reset_seconds=86400,
    )
    fixed_now = datetime(2026, 8, 30, 1, 0)
    with make_session() as db:
        first_deadline, first_hits = defer_for_rate_limit(db, settings, now=fixed_now)
        reset_at = fixed_now + timedelta(hours=24)
        assert active_global_rate_limit(db, reset_at) is None
        reset_deadline, reset_hits = defer_for_rate_limit(db, settings, now=reset_at)

    assert first_hits == 1
    assert first_deadline == fixed_now + timedelta(minutes=45)
    assert reset_hits == 1
    assert reset_deadline == reset_at + timedelta(minutes=45)


def test_corrupt_global_rate_limit_state_does_not_crash_or_block_queue() -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        batch_min_delay_seconds=0,
        batch_max_delay_seconds=0,
        rate_limit_initial_min_delay_seconds=2700,
        rate_limit_initial_max_delay_seconds=2700,
    )
    fixed_now = datetime(2026, 8, 30, 1, 0)
    with make_session() as db:
        account = Account(username="example", status="active")
        db.add_all(
            [
                account,
                RuntimeState(key="global-rate-limit", value="{not valid json"),
            ]
        )
        db.flush()
        enqueue_unique(
            db,
            kind="profile",
            account_id=account.id,
            not_before=fixed_now,
        )

        assert active_global_rate_limit(db, fixed_now) is None
        with patch("app.services.queue.now_utc", return_value=fixed_now):
            claimed = claim_next_job(db, settings)
        assert claimed is not None

        deadline, hits = defer_for_rate_limit(db, settings, now=fixed_now)
        repaired = db.get(RuntimeState, "global-rate-limit")

    assert deadline == fixed_now + timedelta(minutes=45)
    assert hits == 1
    assert repaired is not None
    assert json.loads(repaired.value)["consecutive_hits"] == 1


def test_rate_limit_multiplier_one_handles_a_large_persisted_streak() -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        rate_limit_initial_min_delay_seconds=2700,
        rate_limit_initial_max_delay_seconds=5400,
        rate_limit_backoff_multiplier=1,
    )
    fixed_now = datetime(2026, 8, 30, 1, 0)
    payload = {
        "version": 1,
        "consecutive_hits": 1_000_000_000,
        "last_hit_at": (fixed_now - timedelta(minutes=1)).isoformat(),
        "cooldown_until": fixed_now.isoformat(),
        "last_reason": "previous",
    }
    with make_session() as db:
        db.add(
            RuntimeState(
                key="global-rate-limit",
                value=json.dumps(payload),
            )
        )
        db.commit()
        with patch("app.services.queue.random.randint", return_value=2700) as randint:
            deadline, hits = defer_for_rate_limit(db, settings, now=fixed_now)

    assert hits == 1_000_000_001
    assert deadline == fixed_now + timedelta(minutes=45)
    randint.assert_called_once_with(2700, 5400)


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


def test_recover_interrupted_jobs_requeues_jobs_and_closes_running_runs() -> None:
    interrupted_at = datetime(2026, 9, 6, 2, 30)
    original_started_at = interrupted_at - timedelta(minutes=12)
    with make_session() as db:
        account = Account(username="interrupted", status="active")
        db.add(account)
        db.flush()
        job = Job(
            account_id=account.id,
            kind="relationship",
            content_type="followers",
            status="running",
            attempts=2,
            not_before=original_started_at,
            started_at=original_started_at,
        )
        run = CollectionRun(
            account_id=account.id,
            job_kind="relationship",
            content_type="followers",
            status="running",
            started_at=original_started_at,
        )
        finished_run = CollectionRun(
            account_id=account.id,
            job_kind="profile",
            status="succeeded",
            started_at=original_started_at,
            finished_at=original_started_at + timedelta(minutes=1),
        )
        db.add_all([job, run, finished_run])
        db.commit()

        recovered = recover_interrupted_jobs(db, now=interrupted_at)
        db.commit()

        assert recovered == 1
        assert job.status == "queued"
        assert job.attempts == 1
        assert job.not_before == interrupted_at
        assert job.started_at is None
        assert job.finished_at is None
        assert job.error == "Worker 重新啟動，已回收中斷工作並重新排隊"
        assert run.status == "failed"
        assert run.finished_at == interrupted_at
        assert run.message == "Worker 重新啟動，工作執行中斷"
        assert finished_run.status == "succeeded"
        assert finished_run.finished_at == original_started_at + timedelta(minutes=1)


def test_recover_interrupted_jobs_is_idempotent() -> None:
    interrupted_at = datetime(2026, 9, 6, 2, 30)
    with make_session() as db:
        db.add(Job(kind="profile", status="running", attempts=1, started_at=interrupted_at))
        db.commit()

        assert recover_interrupted_jobs(db, now=interrupted_at) == 1
        db.commit()
        recovered_job = db.scalar(select(Job))
        assert recovered_job.attempts == 0
        assert recover_interrupted_jobs(db, now=interrupted_at + timedelta(minutes=1)) == 0


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


@pytest.mark.parametrize(
    "settings",
    [
        {"rate_limit_initial_min_delay_seconds": 0},
        {"rate_limit_backoff_multiplier": 0},
        {
            "rate_limit_initial_min_delay_seconds": 5400,
            "rate_limit_initial_max_delay_seconds": 2700,
        },
        {
            "rate_limit_initial_max_delay_seconds": 90_000,
            "rate_limit_max_delay_seconds": 86_400,
        },
    ],
)
def test_rate_limit_settings_reject_unsafe_ranges(settings) -> None:
    with pytest.raises(ValidationError):
        Settings(**settings)
