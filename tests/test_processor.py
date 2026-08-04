from datetime import date, datetime
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models import (
    Account,
    CollectionStream,
    Content,
    ContentVersion,
    InteractionSnapshot,
    Job,
    ProfileVersion,
    RelationshipChange,
    RelationshipMember,
    RelationshipScan,
    StatSnapshot,
)
from app.services.collector import (
    CollectionError,
    ContentData,
    ProfileData,
    RelationshipBatch,
    RelationshipMemberData,
)
from app.services.processor import JobProcessor


def make_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return Session(engine)


class FakeCollector:
    profile = ProfileData(
        username="example",
        display_name="範例帳號",
        bio="公開簡介",
        external_url=None,
        avatar_url=None,
        follower_count=123,
        following_count=45,
    )
    contents = [
        ContentData(
            threads_id="post-1",
            author_username="example",
            content_type="post",
            source_url="https://www.threads.com/@example/post/post-1",
            text="第一則內容",
            published_at=datetime(2026, 1, 1),
            like_count=3,
        )
    ]

    def __init__(self, _settings):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def collect_profile(self, _username):
        return self.profile

    def collect_content(self, *_args, **_kwargs):
        return self.contents


def test_profile_job_versions_profile_and_schedules_stream(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
        batch_min_delay_seconds=0,
        batch_max_delay_seconds=0,
    )
    with make_session() as db:
        account = Account(username="example", status="pending")
        db.add(account)
        db.flush()
        job = Job(account_id=account.id, kind="verify", status="running")
        db.add(job)
        db.commit()

        with patch("app.services.processor.ThreadsCollector", FakeCollector):
            JobProcessor(settings).process(db, job)
        db.commit()

        assert account.status == "active"
        assert account.follower_count == 123
        assert db.scalar(select(func.count(ProfileVersion.id))) == 1
        assert db.scalar(select(func.count(StatSnapshot.id))) == 1
        assert db.scalar(select(func.count(CollectionStream.id))) == 4
        assert db.scalar(select(func.count(Job.id)).where(Job.kind == "content")) == 1
        assert db.scalar(select(func.count(RelationshipScan.id))) == 2
        assert db.scalar(select(func.count(Job.id)).where(Job.kind == "relationship")) == 2


def test_successful_retry_clears_login_required_status(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
        batch_min_delay_seconds=0,
        batch_max_delay_seconds=0,
    )
    with make_session() as db:
        account = Account(
            username="example",
            status="login_required",
            status_message="Threads 登入工作階段已失效",
        )
        db.add(account)
        db.flush()
        job = Job(account_id=account.id, kind="verify", status="running")
        db.add(job)
        db.commit()

        with patch("app.services.processor.ThreadsCollector", FakeCollector):
            JobProcessor(settings).process(db, job)
        db.commit()

        assert account.status == "active"
        assert account.status_message is None


def test_content_job_saves_version_and_changed_metrics(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
        batch_min_delay_seconds=0,
        batch_max_delay_seconds=0,
    )
    with make_session() as db:
        account = Account(username="example", status="active")
        db.add(account)
        db.flush()
        db.add(CollectionStream(account_id=account.id, content_type="post"))
        job = Job(account_id=account.id, kind="content", content_type="post", status="running")
        db.add(job)
        db.commit()

        with patch("app.services.processor.ThreadsCollector", FakeCollector):
            JobProcessor(settings).process(db, job)
        db.commit()

        content = db.scalar(select(Content).where(Content.threads_id == "post-1"))
        assert content is not None
        assert db.scalar(select(func.count(ContentVersion.id))) == 1
        metrics = db.scalar(select(InteractionSnapshot))
        assert metrics is not None
        assert metrics.like_count == 3


def test_content_success_does_not_replace_last_scheduled_profile_visit(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
        batch_min_delay_seconds=0,
        batch_max_delay_seconds=0,
    )
    profile_success = datetime(2026, 8, 4, 18, 45)
    next_visit = datetime(2026, 8, 5, 6, 57)
    with make_session() as db:
        account = Account(
            username="example",
            status="active",
            last_success_at=profile_success,
            next_due_at=next_visit,
        )
        db.add(account)
        db.flush()
        db.add(CollectionStream(account_id=account.id, content_type="post"))
        job = Job(account_id=account.id, kind="content", content_type="post", status="running")
        db.add(job)
        db.commit()

        with patch("app.services.processor.ThreadsCollector", FakeCollector):
            JobProcessor(settings).process(db, job)
        db.commit()

        assert account.last_success_at == profile_success
        assert account.next_due_at == next_visit


def relationship_batch(*usernames: str, complete: bool = True) -> RelationshipBatch:
    return RelationshipBatch(
        members=[
            RelationshipMemberData(username=name, display_name=name.title(), avatar_url=None)
            for name in usernames
        ],
        cursor=usernames[-1] if usernames else None,
        complete=complete,
    )


def test_relationship_scans_create_baseline_then_daily_added_removed_diff(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
    )
    processor = JobProcessor(settings)
    with make_session() as db:
        account = Account(username="example", status="active")
        db.add(account)
        db.flush()
        first = RelationshipScan(
            account_id=account.id,
            relationship_type="followers",
            scan_date=date(2026, 8, 3),
            status="running",
        )
        db.add(first)
        db.flush()
        processor._save_relationship_batch(
            db, account, first, relationship_batch("alice", "bob")
        )
        db.flush()

        assert first.status == "complete"
        assert db.scalar(select(func.count(RelationshipChange.id))) == 0

        second = RelationshipScan(
            account_id=account.id,
            relationship_type="followers",
            scan_date=date(2026, 8, 4),
            status="running",
        )
        db.add(second)
        db.flush()
        processor._save_relationship_batch(
            db, account, second, relationship_batch("bob", "carol")
        )
        db.flush()

        changes = db.scalars(select(RelationshipChange).order_by(RelationshipChange.id)).all()
        members = {
            member.username: member
            for member in db.scalars(select(RelationshipMember)).all()
        }
        assert [(change.change_type, change.member.username) for change in changes] == [
            ("added", "carol"),
            ("removed", "alice"),
        ]
        assert members["alice"].active is False
        assert members["bob"].active is True
        assert members["carol"].active is True


def test_relationship_failure_does_not_mark_account_error(tmp_path) -> None:
    class FailingRelationshipCollector(FakeCollector):
        def collect_relationships(self, *_args, **_kwargs):
            raise CollectionError("清單目前不可存取")

    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
    )
    with make_session() as db:
        account = Account(username="example", status="active")
        db.add(account)
        db.flush()
        scan = RelationshipScan(
            account_id=account.id,
            relationship_type="followers",
            scan_date=date(2026, 8, 4),
            status="running",
        )
        job = Job(
            account_id=account.id,
            kind="relationship",
            content_type="followers",
            status="running",
        )
        db.add_all([scan, job])
        db.commit()

        with patch("app.services.processor.ThreadsCollector", FailingRelationshipCollector):
            JobProcessor(settings).process(db, job)
        db.commit()

        assert job.status == "failed"
        assert scan.status == "failed"
        assert account.status == "active"
        assert account.status_message is None
