from datetime import datetime
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
    StatSnapshot,
)
from app.services.collector import ContentData, ProfileData
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
