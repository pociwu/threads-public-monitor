from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models import Account, Job
from app.services.queue import (
    claim_next_job,
    enqueue_unique,
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
