from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Account, Job


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

        response = client.post(
            "/accounts",
            data={"username": "https://www.threads.com/@Sin_9311"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        account = db.scalar(select(Account).where(Account.username == "sin_9311"))
        assert account is not None
        assert account.status == "pending"
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
