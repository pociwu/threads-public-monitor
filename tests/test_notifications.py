from datetime import date, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models import Account, NotificationOutbox
from app.services.collector import ContentData
from app.services.notifications import NotificationService


def make_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def telegram_settings(tmp_path, **overrides) -> Settings:
    values = {
        "database_url": "sqlite:///:memory:",
        "media_root": tmp_path / "media",
        "browser_profile_dir": tmp_path / "profile",
        "telegram_bot_token": "secret-token",
        "telegram_chat_id": "-100123",
    }
    values.update(overrides)
    return Settings(**values)


def content_item(threads_id: str, content_type: str = "post") -> ContentData:
    return ContentData(
        threads_id=threads_id,
        author_username="example",
        content_type=content_type,
        source_url=f"https://www.threads.com/@example/post/{threads_id}",
        text=f"{content_type} notification body",
        published_at=datetime(2026, 8, 26, 8, 30),
    )


def test_content_notifications_only_queue_incremental_posts_and_replies(tmp_path) -> None:
    service = NotificationService(telegram_settings(tmp_path))
    with make_session() as db:
        account = Account(username="example", status="active")
        db.add(account)
        db.flush()

        service.queue_content_changes(db, account, "backfill", "post", [content_item("old-post")])
        service.queue_content_changes(
            db, account, "incremental", "repost", [content_item("repost", "repost")]
        )
        service.queue_content_changes(
            db,
            account,
            "incremental",
            "post",
            [content_item("new-post"), content_item("new-post-2")],
        )
        service.queue_content_changes(
            db, account, "incremental", "reply", [content_item("new-reply", "reply")]
        )
        db.flush()

        rows = db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)).all()
        assert len(rows) == 2
        assert "新串文" in rows[0].body
        assert "共 2 則" in rows[0].body
        assert "new-post" in rows[0].body
        assert "新回覆" in rows[1].body


def test_relationship_notification_is_batched_and_deduplicated(tmp_path) -> None:
    service = NotificationService(telegram_settings(tmp_path))
    with make_session() as db:
        account = Account(username="example", status="active")
        db.add(account)
        db.flush()

        service.queue_relationship_changes(
            db,
            account,
            scan_id=9,
            relationship_type="followers",
            scan_date=date(2026, 8, 26),
            added=["alice", "bob"],
            removed=["carol"],
        )
        service.queue_relationship_changes(
            db,
            account,
            scan_id=9,
            relationship_type="followers",
            scan_date=date(2026, 8, 26),
            added=["alice", "bob"],
            removed=["carol"],
        )
        db.flush()

        rows = db.scalars(select(NotificationOutbox)).all()
        assert len(rows) == 1
        assert "粉絲名單異動" in rows[0].body
        assert "新增 2" in rows[0].body
        assert "@alice" in rows[0].body
        assert "退出 1" in rows[0].body
        assert "@carol" in rows[0].body


def test_final_relationship_failure_notification_includes_progress_and_reason(
    tmp_path,
) -> None:
    service = NotificationService(telegram_settings(tmp_path))
    with make_session() as db:
        account = Account(username="example", status="active")
        db.add(account)
        db.flush()

        service.queue_relationship_failure(
            db,
            account,
            scan_id=10,
            relationship_type="following",
            scan_date=date(2026, 8, 26),
            collected_count=15,
            reason="Threads 追蹤中清單載入逾時",
        )
        db.flush()

        row = db.scalar(select(NotificationOutbox))
        assert row is not None
        assert row.event_type == "relationship_following_failed"
        assert "追蹤中名單掃描失敗" in row.body
        assert "本輪已擷取：15 人" in row.body
        assert "載入逾時" in row.body


def test_disabled_telegram_does_not_create_outbox_rows(tmp_path) -> None:
    settings = telegram_settings(tmp_path, telegram_bot_token="")
    service = NotificationService(settings)
    with make_session() as db:
        account = Account(username="example", status="active")
        db.add(account)
        db.flush()
        service.queue_content_changes(
            db, account, "incremental", "post", [content_item("new-post")]
        )
        assert db.scalar(select(NotificationOutbox)) is None


def test_delivery_marks_message_sent(tmp_path) -> None:
    sent: list[tuple[str, str, str]] = []

    def sender(token: str, chat_id: str, body: str, _timeout: int) -> None:
        sent.append((token, chat_id, body))

    settings = telegram_settings(tmp_path)
    service = NotificationService(settings, sender=sender)
    with make_session() as db:
        account = Account(username="example", status="active")
        db.add(account)
        db.flush()
        service.queue_content_changes(
            db, account, "incremental", "post", [content_item("new-post")]
        )
        db.flush()

        assert service.deliver_next(db) is True
        row = db.scalar(select(NotificationOutbox))
        assert row is not None
        assert row.status == "sent"
        assert row.sent_at is not None
        assert sent == [("secret-token", "-100123", row.body)]


def test_delivery_failure_retries_without_leaking_bot_token(tmp_path) -> None:
    def sender(token: str, _chat_id: str, _body: str, _timeout: int) -> None:
        raise RuntimeError(f"https://api.telegram.org/bot{token}/sendMessage failed")

    settings = telegram_settings(tmp_path, telegram_notification_max_attempts=2)
    service = NotificationService(settings, sender=sender)
    with make_session() as db:
        account = Account(username="example", status="active")
        db.add(account)
        db.flush()
        service.queue_content_changes(
            db, account, "incremental", "post", [content_item("new-post")]
        )
        db.flush()

        assert service.deliver_next(db) is False
        row = db.scalar(select(NotificationOutbox))
        assert row is not None
        assert row.status == "pending"
        assert row.attempts == 1
        assert "secret-token" not in (row.last_error or "")
        row.not_before = datetime(2000, 1, 1)

        assert service.deliver_next(db) is False
        assert row.status == "failed"
        assert row.attempts == 2
