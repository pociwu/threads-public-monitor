from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Account, CollectionStream, Job, RelationshipMember, RelationshipScan


def make_client() -> tuple[TestClient, Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    return TestClient(app), session


def test_dashboard_and_add_account() -> None:
    client, db = make_client()
    try:
        response = client.get("/")
        assert response.status_code == 200
        assert "新增 Threads 帳號" in response.text
        assert 'id="account-grid-region"' in response.text
        assert 'data-refresh-interval="5000"' in response.text

        response = client.post(
            "/accounts",
            data={"username": "https://www.threads.com/@Sin_9311"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        account = db.scalar(select(Account).where(Account.username == "sin_9311"))
        assert account is not None
        assert account.status == "pending"
        assert "最後拜訪" in client.get("/").text
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_reorder_accounts_persists_order() -> None:
    client, db = make_client()
    try:
        first = Account(username="first", sort_order=0)
        second = Account(username="second", sort_order=1)
        db.add_all([first, second])
        db.commit()
        response = client.post("/accounts/reorder", json={"ids": [second.id, first.id]})
        assert response.status_code == 200
        db.refresh(first)
        db.refresh(second)
        assert second.sort_order == 0
        assert first.sort_order == 1
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_account_trend_is_collapsible() -> None:
    client, db = make_client()
    try:
        account = Account(username="example", status="active")
        db.add(account)
        db.commit()

        response = client.get(f"/accounts/{account.id}")

        assert response.status_code == 200
        assert '<details class="chart-panel collapsible-panel">' in response.text
        assert "危險操作（永久刪除資料）" in response.text
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_account_detail_lists_completed_and_pending_backfill_streams() -> None:
    client, db = make_client()
    try:
        account = Account(username="example", status="active")
        db.add(account)
        db.flush()
        db.add_all(
            [
                CollectionStream(
                    account_id=account.id,
                    content_type="post",
                    phase="incremental",
                    collected_count=7,
                ),
                CollectionStream(
                    account_id=account.id,
                    content_type="reply",
                    phase="backfill",
                    collected_count=18,
                ),
            ]
        )
        db.commit()

        response = client.get(f"/accounts/{account.id}")

        assert response.status_code == 200
        assert "已回補清單" in response.text
        assert "尚未回補清單" in response.text
        assert "7 筆" in response.text
        assert "18 / 100" in response.text
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_retry_moves_login_required_account_back_to_pending() -> None:
    client, db = make_client()
    try:
        account = Account(
            username="example",
            status="login_required",
            status_message="Threads 登入工作階段已失效",
        )
        db.add(account)
        db.commit()

        response = client.post(f"/accounts/{account.id}/retry", follow_redirects=False)

        assert response.status_code == 303
        db.refresh(account)
        assert account.status == "pending"
        assert account.status_message is None
        job = db.scalar(select(Job).where(Job.account_id == account.id))
        assert job is not None
        assert job.kind == "verify"
        assert job.status == "queued"
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_account_detail_shows_relationship_tabs_members_and_scan_status() -> None:
    client, db = make_client()
    try:
        account = Account(username="example", status="active", follower_count=51)
        db.add(account)
        db.flush()
        db.add(
            RelationshipMember(
                account_id=account.id,
                relationship_type="followers",
                username="alice",
                display_name="Alice",
                active=True,
            )
        )
        db.add(
            RelationshipScan(
                account_id=account.id,
                relationship_type="followers",
                scan_date=date(2026, 8, 4),
                status="running",
                collected_count=5,
            )
        )
        db.commit()

        response = client.get(f"/accounts/{account.id}?tab=followers")

        assert response.status_code == 200
        assert "粉絲名單" in response.text
        assert "每日差異" in response.text
        assert "已擷取 5 人" in response.text
        assert "@alice" in response.text
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_account_detail_marks_following_list_as_not_public() -> None:
    client, db = make_client()
    try:
        account = Account(username="example", status="active")
        db.add(account)
        db.flush()
        db.add(
            RelationshipScan(
                account_id=account.id,
                relationship_type="following",
                scan_date=date(2026, 8, 8),
                status="unavailable",
            )
        )
        db.commit()

        response = client.get(f"/accounts/{account.id}?tab=following")

        assert response.status_code == 200
        assert "Threads 未公開" in response.text
    finally:
        app.dependency_overrides.clear()
        db.close()
