from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models import (
    Account,
    CollectionRun,
    CollectionStream,
    Content,
    ContentMedia,
    ContentVersion,
    InteractionSnapshot,
    Job,
    MediaAsset,
    NotificationOutbox,
    ProfileVersion,
    RelationshipChange,
    RelationshipMember,
    RelationshipScan,
    RelationshipScanMember,
    StatSnapshot,
)
from app.services.collector import (
    CollectionError,
    ContentData,
    LoginRequired,
    ProfileData,
    RelationshipBatch,
    RelationshipMemberData,
    TransientRelationshipError,
)
from app.services.processor import JobProcessor


def test_incremental_content_batch_queues_telegram_notification(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
        telegram_bot_token="secret-token",
        telegram_chat_id="-100123",
        batch_min_delay_seconds=0,
        batch_max_delay_seconds=0,
    )
    processor = JobProcessor(settings)
    with make_session() as db:
        account = Account(username="example", status="active")
        db.add(account)
        db.flush()
        db.add(
            CollectionStream(
                account_id=account.id,
                content_type="post",
                phase="incremental",
            )
        )
        db.flush()

        processor._save_content_batch(db, account, "post", [FakeCollector.contents[0]])
        db.flush()

        notification = db.scalar(select(NotificationOutbox))
        assert notification is not None
        assert notification.event_type == "content_post"
        assert "第一則內容" in notification.body


def test_complete_relationship_scan_queues_only_real_diff(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
        telegram_bot_token="secret-token",
        telegram_chat_id="-100123",
    )
    processor = JobProcessor(settings)
    with make_session() as db:
        account = Account(username="example", status="active")
        db.add(account)
        db.flush()
        alice = RelationshipMember(
            account_id=account.id,
            relationship_type="followers",
            username="alice",
        )
        bob = RelationshipMember(
            account_id=account.id,
            relationship_type="followers",
            username="bob",
        )
        previous = RelationshipScan(
            account_id=account.id,
            relationship_type="followers",
            scan_date=date(2026, 8, 25),
            status="complete",
        )
        current = RelationshipScan(
            account_id=account.id,
            relationship_type="followers",
            scan_date=date(2026, 8, 26),
            status="running",
        )
        db.add_all([alice, bob, previous, current])
        db.flush()
        db.add_all(
            [
                RelationshipScanMember(scan_id=previous.id, member_id=alice.id),
                RelationshipScanMember(scan_id=current.id, member_id=bob.id),
            ]
        )
        db.flush()

        processor._complete_relationship_scan(db, account, current)
        db.flush()

        notification = db.scalar(select(NotificationOutbox))
        assert notification is not None
        assert "新增 1：@bob" in notification.body
        assert "退出 1：@alice" in notification.body


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

        with pytest.raises(TransientRelationshipError, match="粉絲清單尚未載入"):
            processor._save_relationship_batch(
                db, account, scan, relationship_batch(complete=True)
            )

        assert scan.status == "running"
        assert scan.collected_count == 0


def test_unknown_relationship_total_cannot_complete_as_empty(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
    )
    processor = JobProcessor(settings)
    with make_session() as db:
        account = Account(username="example", status="active", follower_count=None)
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

        with pytest.raises(TransientRelationshipError, match="粉絲清單尚未載入"):
            processor._save_relationship_batch(
                db, account, scan, relationship_batch(complete=True)
            )

        assert scan.status == "running"


def test_known_zero_relationship_total_can_complete_as_empty(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
    )
    processor = JobProcessor(settings)
    with make_session() as db:
        account = Account(username="example", status="active", follower_count=0)
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

        processor._save_relationship_batch(
            db, account, scan, relationship_batch(complete=True)
        )

        assert scan.status == "complete"
        assert scan.collected_count == 0


def test_follower_batch_discovers_following_count_and_queues_following_scan(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
        batch_min_delay_seconds=0,
        batch_max_delay_seconds=0,
    )
    processor = JobProcessor(settings)
    with make_session() as db:
        account = Account(username="example", status="active", follower_count=51)
        db.add(account)
        db.flush()
        follower_scan = RelationshipScan(
            account_id=account.id,
            relationship_type="followers",
            scan_date=datetime.now(settings.tz).date(),
            status="running",
        )
        following_scan = RelationshipScan(
            account_id=account.id,
            relationship_type="following",
            scan_date=datetime.now(settings.tz).date(),
            status="unavailable",
        )
        db.add_all([follower_scan, following_scan])
        db.flush()

        batch = RelationshipBatch(
            members=[RelationshipMemberData("alice", "Alice", None)],
            cursor="alice",
            complete=False,
            following_count=149,
        )
        processor._save_relationship_batch(db, account, follower_scan, batch)
        db.flush()

        assert account.following_count == 149
        assert following_scan.status == "running"
        queued = db.scalar(
            select(Job).where(
                Job.account_id == account.id,
                Job.kind == "relationship",
                Job.content_type == "following",
                Job.status == "queued",
            )
        )
        assert queued is not None


def test_relationship_scan_cannot_complete_before_known_total(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
    )
    processor = JobProcessor(settings)
    with make_session() as db:
        account = Account(username="example", status="active", follower_count=51)
        db.add(account)
        db.flush()
        scan = RelationshipScan(
            account_id=account.id,
            relationship_type="followers",
            scan_date=date(2026, 8, 9),
            status="running",
        )
        db.add(scan)
        db.flush()

        processor._save_relationship_batch(
            db,
            account,
            scan,
            relationship_batch("alice", "bob", complete=True),
        )
        db.flush()

        assert scan.collected_count == 2
        assert scan.status == "running"


def test_empty_incomplete_relationship_batch_is_transient_no_progress(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
    )
    processor = JobProcessor(settings)
    with make_session() as db:
        account = Account(username="example", status="active", follower_count=51)
        db.add(account)
        db.flush()
        scan = RelationshipScan(
            account_id=account.id,
            relationship_type="followers",
            scan_date=date(2026, 8, 9),
            status="running",
            cursor="saved_cursor",
            collected_count=5,
        )
        db.add(scan)
        db.flush()

        with pytest.raises(TransientRelationshipError, match="未取得新成員"):
            processor._save_relationship_batch(
                db,
                account,
                scan,
                RelationshipBatch(
                    members=[],
                    cursor="saved_cursor",
                    complete=False,
                ),
            )

        assert scan.status == "running"
        assert scan.cursor == "saved_cursor"


def test_schedule_keeps_cross_day_running_relationship_scan(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
        batch_min_delay_seconds=0,
        batch_max_delay_seconds=0,
    )
    processor = JobProcessor(settings)
    today = datetime.now(settings.tz).date()
    with make_session() as db:
        account = Account(username="example", status="active", follower_count=51)
        db.add(account)
        db.flush()
        old_scan = RelationshipScan(
            account_id=account.id,
            relationship_type="followers",
            scan_date=today - timedelta(days=1),
            status="running",
            cursor="saved_cursor",
            collected_count=5,
        )
        retry_job = Job(
            account_id=account.id,
            kind="relationship",
            content_type="followers",
            status="queued",
            attempts=1,
        )
        db.add_all([old_scan, retry_job])
        db.flush()

        processor._schedule_relationship_scans(db, account)
        db.flush()

        scans = db.scalars(
            select(RelationshipScan).where(
                RelationshipScan.account_id == account.id,
                RelationshipScan.relationship_type == "followers",
            )
        ).all()
        jobs = db.scalars(
            select(Job).where(
                Job.account_id == account.id,
                Job.kind == "relationship",
                Job.content_type == "followers",
                Job.status.in_(["queued", "running"]),
            )
        ).all()
        assert scans == [old_scan]
        assert jobs == [retry_job]


def test_schedule_does_not_reopen_final_failed_scan_on_same_day(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
        batch_min_delay_seconds=0,
        batch_max_delay_seconds=0,
    )
    processor = JobProcessor(settings)
    today = datetime.now(settings.tz).date()
    with make_session() as db:
        account = Account(username="example", status="active", follower_count=51)
        db.add(account)
        db.flush()
        failed_scan = RelationshipScan(
            account_id=account.id,
            relationship_type="followers",
            scan_date=today,
            status="failed",
            collected_count=5,
            completed_at=datetime(2026, 8, 26, 4, 0),
        )
        db.add(failed_scan)
        db.flush()

        processor._schedule_relationship_scans(db, account)
        db.flush()

        assert failed_scan.status == "failed"
        assert failed_scan.completed_at is not None
        queued = db.scalar(
            select(func.count(Job.id)).where(
                Job.account_id == account.id,
                Job.kind == "relationship",
                Job.content_type == "followers",
                Job.status.in_(["queued", "running"]),
            )
        )
        assert queued == 0


def test_schedule_starts_new_scan_after_prior_day_final_failure(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
        batch_min_delay_seconds=0,
        batch_max_delay_seconds=0,
    )
    processor = JobProcessor(settings)
    today = datetime.now(settings.tz).date()
    with make_session() as db:
        account = Account(username="example", status="active", follower_count=51)
        db.add(account)
        db.flush()
        failed_scan = RelationshipScan(
            account_id=account.id,
            relationship_type="followers",
            scan_date=today - timedelta(days=1),
            status="failed",
            collected_count=5,
        )
        db.add(failed_scan)
        db.flush()

        processor._schedule_relationship_scans(db, account)
        db.flush()

        scans = db.scalars(
            select(RelationshipScan)
            .where(
                RelationshipScan.account_id == account.id,
                RelationshipScan.relationship_type == "followers",
            )
            .order_by(RelationshipScan.scan_date)
        ).all()
        assert [scan.status for scan in scans] == ["failed", "running"]
        assert [scan.scan_date for scan in scans] == [today - timedelta(days=1), today]


def test_following_zero_does_not_reopen_same_day_final_failure(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
        batch_min_delay_seconds=0,
        batch_max_delay_seconds=0,
    )
    processor = JobProcessor(settings)
    today = datetime.now(settings.tz).date()
    with make_session() as db:
        account = Account(username="example", status="active", following_count=149)
        db.add(account)
        db.flush()
        failed_scan = RelationshipScan(
            account_id=account.id,
            relationship_type="following",
            scan_date=today,
            status="failed",
            collected_count=5,
            completed_at=datetime(2026, 8, 26, 4, 0),
        )
        db.add(failed_scan)
        db.flush()

        processor._activate_following_scan(db, account, 0)
        account.following_count = 149
        processor._schedule_relationship_scans(db, account)
        db.flush()

        assert failed_scan.status == "failed"
        assert failed_scan.completed_at is not None
        queued = db.scalar(
            select(func.count(Job.id)).where(
                Job.account_id == account.id,
                Job.kind == "relationship",
                Job.content_type == "following",
                Job.status.in_(["queued", "running"]),
            )
        )
        assert queued == 0


def test_following_zero_completes_new_scan_and_records_removed_members(tmp_path) -> None:
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
        batch_min_delay_seconds=0,
        batch_max_delay_seconds=0,
    )
    processor = JobProcessor(settings)
    today = datetime.now(settings.tz).date()
    with make_session() as db:
        account = Account(username="example", status="active", following_count=0)
        db.add(account)
        db.flush()
        member = RelationshipMember(
            account_id=account.id,
            relationship_type="following",
            username="previous_member",
            active=True,
        )
        previous_scan = RelationshipScan(
            account_id=account.id,
            relationship_type="following",
            scan_date=today - timedelta(days=1),
            status="complete",
            collected_count=1,
            completed_at=datetime(2026, 8, 25, 4, 0),
        )
        db.add_all([member, previous_scan])
        db.flush()
        db.add(RelationshipScanMember(scan_id=previous_scan.id, member_id=member.id))
        db.flush()

        processor._activate_following_scan(db, account, 0)
        db.flush()

        current_scan = db.scalar(
            select(RelationshipScan).where(
                RelationshipScan.account_id == account.id,
                RelationshipScan.relationship_type == "following",
                RelationshipScan.scan_date == today,
            )
        )
        change = db.scalar(
            select(RelationshipChange).where(
                RelationshipChange.scan_id == current_scan.id,
                RelationshipChange.member_id == member.id,
                RelationshipChange.change_type == "removed",
            )
        )
        assert current_scan.status == "complete"
        assert current_scan.collected_count == 0
        assert member.active is False
        assert member.removed_at is not None
        assert change is not None


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


def test_transient_relationship_failure_requeues_same_job_after_long_backoff(
    tmp_path,
) -> None:
    class SlowRelationshipCollector(FakeCollector):
        def collect_relationships(self, *_args, **_kwargs):
            raise TransientRelationshipError("Threads 粉絲清單載入逾時，未取得任何成員")

    retry_at = datetime(2026, 8, 26, 3, 30)
    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
        telegram_bot_token="secret-token",
        telegram_chat_id="-100123",
    )
    with make_session() as db:
        account = Account(username="example", status="active", follower_count=51)
        db.add(account)
        db.flush()
        scan = RelationshipScan(
            account_id=account.id,
            relationship_type="followers",
            scan_date=date(2026, 8, 26),
            status="running",
            cursor="saved_cursor",
        )
        job = Job(
            account_id=account.id,
            kind="relationship",
            content_type="followers",
            status="running",
            attempts=1,
            started_at=datetime(2026, 8, 26, 2, 0),
        )
        db.add_all([scan, job])
        db.commit()

        with (
            patch("app.services.processor.ThreadsCollector", SlowRelationshipCollector),
            patch(
                "app.services.processor.next_relationship_retry",
                return_value=retry_at,
                create=True,
            ),
        ):
            JobProcessor(settings).process(db, job)
        db.commit()

        assert job.status == "queued"
        assert job.attempts == 1
        assert job.not_before == retry_at
        assert job.started_at is None
        assert job.finished_at is None
        assert "載入逾時" in (job.error or "")
        assert scan.status == "running"
        assert scan.cursor == "saved_cursor"
        assert scan.completed_at is None
        assert account.status == "active"
        assert db.scalar(select(func.count(Job.id))) == 1
        assert db.scalar(select(NotificationOutbox)) is None
        run = db.scalar(select(CollectionRun))
        assert run is not None
        assert run.status == "failed"
        assert "載入逾時" in (run.message or "")


def test_transient_relationship_failure_stops_after_attempt_limit(tmp_path) -> None:
    class SlowRelationshipCollector(FakeCollector):
        def collect_relationships(self, *_args, **_kwargs):
            raise TransientRelationshipError("Threads 粉絲清單載入逾時，未取得任何成員")

    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
        telegram_bot_token="secret-token",
        telegram_chat_id="-100123",
    )
    with make_session() as db:
        account = Account(username="example", status="active", follower_count=51)
        db.add(account)
        db.flush()
        scan = RelationshipScan(
            account_id=account.id,
            relationship_type="followers",
            scan_date=date(2026, 8, 26),
            status="running",
        )
        job = Job(
            account_id=account.id,
            kind="relationship",
            content_type="followers",
            status="running",
            attempts=3,
        )
        db.add_all([scan, job])
        db.commit()

        with patch("app.services.processor.ThreadsCollector", SlowRelationshipCollector):
            JobProcessor(settings).process(db, job)
        db.commit()

        assert job.status == "failed"
        assert job.finished_at is not None
        assert scan.status == "failed"
        assert scan.completed_at is not None
        assert account.status == "active"
        assert db.scalar(select(func.count(Job.id)).where(Job.status == "queued")) == 0
        notification = db.scalar(select(NotificationOutbox))
        assert notification is not None
        assert notification.event_type == "relationship_followers_failed"
        assert "載入逾時" in notification.body


def test_relationship_login_failure_is_never_automatically_requeued(tmp_path) -> None:
    class LoggedOutCollector(FakeCollector):
        def collect_relationships(self, *_args, **_kwargs):
            raise LoginRequired("Threads 登入工作階段已失效")

    settings = Settings(
        database_url="sqlite:///:memory:",
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
    )
    with make_session() as db:
        account = Account(username="example", status="active", follower_count=51)
        db.add(account)
        db.flush()
        scan = RelationshipScan(
            account_id=account.id,
            relationship_type="followers",
            scan_date=date(2026, 8, 26),
            status="running",
        )
        job = Job(
            account_id=account.id,
            kind="relationship",
            content_type="followers",
            status="running",
            attempts=1,
        )
        db.add_all([scan, job])
        db.commit()

        with patch("app.services.processor.ThreadsCollector", LoggedOutCollector):
            JobProcessor(settings).process(db, job)
        db.commit()

        assert job.status == "failed"
        assert scan.status == "failed"
        assert account.status == "login_required"
        assert "登入工作階段已失效" in (account.status_message or "")
