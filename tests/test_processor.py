from datetime import date, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models import (
    Account,
    CollectionStream,
    Content,
    ContentMedia,
    ContentVersion,
    InteractionSnapshot,
    Job,
    MediaAsset,
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
        assert db.scalar(select(func.count(Job.id)).where(Job.kind == "relationship")) == 1


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


def test_profile_without_public_following_count_does_not_queue_following_list(tmp_path) -> None:
    class FollowersOnlyCollector(FakeCollector):
        profile = ProfileData(
            username="example",
            display_name="Example",
            bio=None,
            external_url=None,
            avatar_url=None,
            follower_count=51,
            following_count=None,
        )

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

        with patch("app.services.processor.ThreadsCollector", FollowersOnlyCollector):
            JobProcessor(settings).process(db, job)
        db.commit()

        relationship_jobs = db.scalars(
            select(Job).where(Job.kind == "relationship").order_by(Job.content_type)
        ).all()
        scans = db.scalars(
            select(RelationshipScan).order_by(RelationshipScan.relationship_type)
        ).all()
        assert [item.content_type for item in relationship_jobs] == ["followers"]
        assert [(scan.relationship_type, scan.status) for scan in scans] == [
            ("followers", "running"),
            ("following", "unavailable"),
        ]


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


def test_content_batch_links_canonical_media_only_once_for_duplicate_bytes(tmp_path) -> None:
    class CanonicalMediaStore:
        def __init__(self):
            self.canonical = None

        def register(self, db, url, media_type):
            asset = MediaAsset(
                source_url=url,
                source_key=url,
                media_type=media_type,
            )
            db.add(asset)
            db.flush()
            return asset

        def download(self, _db, asset):
            if self.canonical is not None:
                return self.canonical
            asset.sha256 = "a" * 64
            asset.local_path = "aa/canonical.mp4"
            asset.download_status = "downloaded"
            self.canonical = asset
            return asset

    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
    )
    with make_session() as db:
        account = Account(username="example", status="active")
        db.add(account)
        db.flush()
        db.add(CollectionStream(account_id=account.id, content_type="post"))
        processor = JobProcessor(settings)
        processor.media = CanonicalMediaStore()
        duplicate_media_item = ContentData(
            threads_id="duplicate-media",
            author_username="example",
            content_type="post",
            source_url="https://www.threads.com/@example/post/duplicate-media",
            text="same video",
            published_at=datetime(2026, 8, 8),
            media=[
                ("https://cdn.example/first.mp4", "video"),
                ("https://cdn.example/second.mp4", "video"),
            ],
        )

        processor._save_content_batch(db, account, "post", [duplicate_media_item])
        db.flush()

        content = db.scalar(select(Content).where(Content.threads_id == "duplicate-media"))
        assert content is not None
        links = db.scalars(
            select(ContentMedia).where(ContentMedia.content_id == content.id)
        ).all()
        assert len(links) == 1


def test_content_batch_prefers_full_size_instagram_variant(tmp_path) -> None:
    class RecordingMediaStore:
        def register(self, db, url, media_type):
            asset = MediaAsset(
                source_url=url,
                source_key=url,
                media_type=media_type,
                byte_size=300_000,
                download_status="downloaded",
            )
            db.add(asset)
            db.flush()
            return asset

        def download(self, _db, asset):
            return asset

    thumbnail = (
        "https://scontent.cdninstagram.com/v/t51/same_n.jpg"
        "?stp=dst-jpg_p240x240&ig_cache_key=SAME"
    )
    full_size = (
        "https://scontent.cdninstagram.com/v/t51/same_n.jpg"
        "?stp=dst-jpg&ig_cache_key=SAME"
    )
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
    )
    with make_session() as db:
        account = Account(username="example", status="active")
        db.add(account)
        db.flush()
        db.add(CollectionStream(account_id=account.id, content_type="post"))
        processor = JobProcessor(settings)
        processor.media = RecordingMediaStore()
        item = ContentData(
            threads_id="resolution-variant",
            author_username="example",
            content_type="post",
            source_url="https://www.threads.com/@example/post/resolution-variant",
            text="same image",
            published_at=datetime(2026, 8, 8),
            media=[(thumbnail, "image"), (full_size, "image")],
        )

        processor._save_content_batch(db, account, "post", [item])
        db.flush()

        content = db.scalar(select(Content).where(Content.threads_id == "resolution-variant"))
        link = db.scalar(select(ContentMedia).where(ContentMedia.content_id == content.id))
        assert link.media.source_url == full_size


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


def test_nonempty_follower_profile_cannot_complete_with_empty_scan(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
    )
    processor = JobProcessor(settings)
    with make_session() as db:
        account = Account(
            username="example", status="active", follower_count=51
        )
        db.add(account)
        db.flush()
        scan = RelationshipScan(
            account_id=account.id,
            relationship_type="followers",
            scan_date=date(2026, 8, 8),
            status="running",
        )
        db.add(scan)
        db.flush()

        with pytest.raises(CollectionError, match="粉絲清單尚未載入"):
            processor._save_relationship_batch(
                db, account, scan, relationship_batch(complete=True)
            )

        assert scan.status == "running"
        assert scan.collected_count == 0


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
