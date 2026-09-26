from datetime import date, datetime
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import __version__
from app.db import Base, get_db
from app.main import app
from app.models import (
    Account,
    CollectionStream,
    Content,
    ContentMedia,
    ContentVersion,
    InteractionSnapshot,
    Job,
    MediaAsset,
    RelationshipMember,
    RelationshipScan,
    RelationshipScanMember,
)


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


def test_priority_switch_persists_without_enqueuing_or_clearing_cooldown() -> None:
    client, db = make_client()
    try:
        account = Account(username="sin_9311", status="active",
                          cooldown_until=datetime(2099, 1, 1))
        db.add(account)
        db.commit()
        for enabled in (True, True, False):
            response = client.post(f"/accounts/{account.id}/priority",
                                   data={"priority_enabled": str(enabled).lower()},
                                   follow_redirects=False)
            assert response.status_code == 303
            db.refresh(account)
            assert account.priority_enabled is enabled
            assert account.cooldown_until == datetime(2099, 1, 1)
            assert db.scalar(select(Job)) is None
            assert f'aria-checked="{str(enabled).lower()}"' in client.get("/").text
        assert client.post("/accounts/99999/priority",
                           data={"priority_enabled": "true"}).status_code == 404
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_static_stylesheets_are_cache_busted_by_app_version() -> None:
    client, db = make_client()
    try:
        response = client.get("/relationships/compare")

        assert response.status_code == 200
        assert f'/static/app.css?v={__version__}' in response.text
        assert f'/static/relationships.css?v={__version__}' in response.text
        assert f'/static/content.css?v={__version__}' in response.text
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_account_detail_renders_threads_like_content_card_structure() -> None:
    client, db = make_client()
    try:
        avatar = MediaAsset(
            source_url="https://cdn.example/avatar.jpg",
            source_key="avatar",
            media_type="image",
            local_path="avatars/sin_9311.jpg",
            download_status="downloaded",
        )
        first_media = MediaAsset(
            source_url="https://cdn.example/one.jpg",
            source_key="one",
            media_type="image",
            local_path="posts/one.jpg",
            download_status="downloaded",
        )
        second_media = MediaAsset(
            source_url="https://cdn.example/two.jpg",
            source_key="two",
            media_type="image",
            local_path="posts/two.jpg",
            download_status="downloaded",
        )
        db.add_all([avatar, first_media, second_media])
        db.flush()
        account = Account(
            username="sin_9311",
            display_name="鴨仔",
            status="active",
            avatar_media_id=avatar.id,
        )
        db.add(account)
        db.flush()
        content = Content(
            threads_id="threads-like-post",
            account_id=account.id,
            author_username="sin_9311",
            content_type="post",
            source_url="https://www.threads.com/@sin_9311/post/threads-like-post",
            published_at=datetime(2026, 8, 20, 8, 0),
        )
        db.add(content)
        db.flush()
        older_content = Content(
            threads_id="threads-like-post-without-metrics",
            account_id=account.id,
            author_username="sin_9311",
            content_type="post",
            source_url=(
                "https://www.threads.com/@sin_9311/post/"
                "threads-like-post-without-metrics"
            ),
            published_at=datetime(2026, 8, 19, 8, 0),
        )
        db.add(older_content)
        db.flush()
        db.add_all(
            [
                ContentVersion(
                    content_id=content.id,
                    text="從6月初訂購到今日才收到 1/2讚21回覆20轉發3分享1",
                    fingerprint="f" * 64,
                ),
                ContentMedia(content_id=content.id, media_id=first_media.id, position=0),
                ContentMedia(content_id=content.id, media_id=second_media.id, position=1),
                InteractionSnapshot(
                    content_id=content.id,
                    like_count=21,
                    reply_count=20,
                    repost_count=3,
                    share_count=1,
                ),
                ContentVersion(
                    content_id=older_content.id,
                    text="尚無互動快照的獨立貼文",
                    fingerprint="e" * 64,
                ),
            ]
        )
        db.commit()

        response = client.get(f"/accounts/{account.id}?tab=post")

        assert response.status_code == 200
        assert 'class="content-author-row"' in response.text
        assert 'class="content-author-avatar"' in response.text
        assert 'src="/media/avatars/sin_9311.jpg"' in response.text
        assert 'class="content-source"' in response.text
        assert 'class="content-time-relative"' in response.text
        assert 'class="content-time-exact">2026/08/20 16:00<' in response.text
        assert 'class="media-carousel"' in response.text
        assert 'class="media-counter">1/2<' in response.text
        assert 'class="content-actions"' in response.text
        assert 'aria-label="讚 21"' in response.text
        assert 'aria-label="回覆 20"' in response.text
        assert 'aria-label="轉發 3"' in response.text
        assert 'aria-label="分享 1"' in response.text
        assert 'aria-label="讚數未知"' in response.text
        assert response.text.count('class="content-actions"') == 2
        assert 'class="content-thread-line"' not in response.text
        assert "1/2讚21回覆20轉發3分享1" not in response.text
        assert response.text.index("content-author-row") < response.text.index(
            "從6月初訂購到今日才收到"
        )
        assert response.text.index("從6月初訂購到今日才收到") < response.text.index(
            "media-carousel"
        )
        assert response.text.index("media-carousel") < response.text.index("content-actions")
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


def test_account_can_schedule_staggered_existing_content_refreshes() -> None:
    client, db = make_client()
    try:
        account = Account(username="example", status="active")
        db.add(account)
        db.flush()
        stored_contents = [
            Content(
                threads_id="old-1",
                account_id=account.id,
                author_username="example",
                content_type="post",
                source_url="https://www.threads.com/@example/post/old-1",
                published_at=datetime(2026, 1, 1),
            ),
            Content(
                threads_id="old-2",
                account_id=account.id,
                author_username="example",
                content_type="reply",
                source_url="https://www.threads.com/@example/post/old-2",
                published_at=datetime(2026, 1, 2),
            ),
        ]
        db.add_all(stored_contents)
        db.commit()

        response = client.post(
            f"/accounts/{account.id}/refresh-content", follow_redirects=False
        )

        assert response.status_code == 303
        jobs = db.scalars(
            select(Job)
            .where(Job.account_id == account.id, Job.kind == "content_refresh")
            .order_by(Job.not_before, Job.id)
        ).all()
        assert [job.content_id for job in jobs] == [item.id for item in stored_contents]
        assert jobs[0].not_before < jobs[1].not_before
        assert "refresh_queued=2" in response.headers["location"]

        second = client.post(
            f"/accounts/{account.id}/refresh-content", follow_redirects=False
        )
        assert "refresh_queued=0" in second.headers["location"]
        assert len(db.scalars(select(Job).where(Job.kind == "content_refresh")).all()) == 2

        page = client.get(f"/accounts/{account.id}")
        assert "排定更新舊貼文" in page.text
        assert "2 筆等待更新" in page.text
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


def test_dashboard_shows_global_rate_limit_and_retry_cannot_bypass_it() -> None:
    client, db = make_client()
    try:
        original_due = datetime(2026, 9, 1, 1, 0)
        account = Account(
            username="example",
            status="active",
            last_success_at=datetime(2026, 8, 30, 1, 0),
            next_due_at=original_due,
        )
        db.add(account)
        db.commit()
        rate_limit = {
            "version": 1,
            "consecutive_hits": 2,
            "last_hit_at": datetime(2026, 8, 30, 2, 0),
            "cooldown_until": datetime(2026, 8, 31, 2, 0),
            "last_reason": "Threads 要求次數過多",
        }

        with patch("app.main.active_global_rate_limit", return_value=rate_limit):
            page = client.get("/")
            response = client.post(
                f"/accounts/{account.id}/retry",
                follow_redirects=False,
            )

        assert page.status_code == 200
        assert "Threads 全域冷卻中" in page.text
        assert "Threads 要求次數過多" in page.text
        assert 'type="submit" disabled>全域冷卻中</button>' in page.text
        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        db.refresh(account)
        assert account.status == "active"
        assert account.next_due_at == original_due
        assert db.scalar(select(Job).where(Job.account_id == account.id)) is None
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_retry_can_clear_account_cooldown_when_no_global_rate_limit() -> None:
    client, db = make_client()
    try:
        cooldown_until = datetime(2099, 1, 1, 0, 0)
        account = Account(
            username="example",
            status="cooldown",
            status_message="連續失敗，等待冷卻",
            last_success_at=datetime(2026, 8, 30, 1, 0),
            next_due_at=cooldown_until,
            cooldown_until=cooldown_until,
        )
        db.add(account)
        db.commit()

        page = client.get("/")
        response = client.post(
            f"/accounts/{account.id}/retry",
            follow_redirects=False,
        )

        assert '>立即重試</button>' in page.text
        assert response.status_code == 303
        db.refresh(account)
        assert account.status == "queued"
        assert account.cooldown_until is None
        assert account.next_due_at <= datetime.now()
        job = db.scalar(select(Job).where(Job.account_id == account.id))
        assert job is not None
        assert job.kind == "profile"
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


def test_account_detail_shows_failure_reason_from_current_relationship_scan() -> None:
    client, db = make_client()
    try:
        account = Account(username="example", status="active", following_count=20)
        db.add(account)
        db.flush()
        scan = RelationshipScan(
            account_id=account.id,
            relationship_type="following",
            scan_date=date(2026, 8, 26),
            status="failed",
            collected_count=10,
            started_at=datetime(2026, 8, 26, 1, 0),
            completed_at=datetime(2026, 8, 26, 2, 0),
        )
        db.add(scan)
        db.add_all(
            [
                Job(
                    account_id=account.id,
                    kind="relationship",
                    content_type="following",
                    status="failed",
                    error="上一輪錯誤，不應顯示",
                    created_at=datetime(2026, 8, 25, 23, 0),
                ),
                Job(
                    account_id=account.id,
                    kind="relationship",
                    content_type="following",
                    status="failed",
                    error="Threads 名單載入逾時，已保留目前 10 人",
                    created_at=datetime(2026, 8, 26, 1, 15),
                    finished_at=datetime(2026, 8, 26, 2, 0),
                ),
                Job(
                    account_id=account.id,
                    kind="relationship",
                    content_type="following",
                    status="failed",
                    error="本輪結束後的錯誤，不應顯示",
                    created_at=datetime(2026, 8, 26, 2, 30),
                    finished_at=datetime(2026, 8, 26, 2, 40),
                ),
            ]
        )
        db.commit()

        response = client.get(f"/accounts/{account.id}?tab=following")

        assert response.status_code == 200
        assert "查看本輪失敗原因" in response.text
        assert "Threads 名單載入逾時，已保留目前 10 人" in response.text
        assert "上一輪錯誤，不應顯示" not in response.text
        assert "本輪結束後的錯誤，不應顯示" not in response.text
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_account_detail_shows_transient_relationship_retry_without_stale_error() -> None:
    client, db = make_client()
    try:
        account = Account(username="example", status="active", follower_count=51)
        db.add(account)
        db.flush()
        db.add(
            RelationshipScan(
                account_id=account.id,
                relationship_type="followers",
                scan_date=date(2026, 8, 26),
                status="running",
                collected_count=5,
                started_at=datetime(2026, 8, 26, 1, 0),
            )
        )
        db.add_all(
            [
                Job(
                    account_id=account.id,
                    kind="relationship",
                    content_type="followers",
                    status="failed",
                    error="舊掃描錯誤，不應顯示",
                    created_at=datetime(2026, 8, 25, 23, 0),
                    finished_at=datetime(2026, 8, 25, 23, 30),
                ),
                Job(
                    account_id=account.id,
                    kind="relationship",
                    content_type="followers",
                    status="queued",
                    error="Threads 粉絲清單載入逾時，未取得任何成員",
                    created_at=datetime(2026, 8, 26, 1, 15),
                    not_before=datetime(2026, 8, 26, 2, 0),
                ),
            ]
        )
        db.commit()

        response = client.get(f"/accounts/{account.id}?tab=followers")

        assert response.status_code == 200
        assert "等待重試" in response.text
        assert "查看本輪暫時失敗原因" in response.text
        assert "Threads 粉絲清單載入逾時，未取得任何成員" in response.text
        assert "已保留目前進度；下次最早重試" in response.text
        assert "舊掃描錯誤，不應顯示" not in response.text
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_account_detail_keeps_clean_queued_relationship_job_as_collecting() -> None:
    client, db = make_client()
    try:
        account = Account(username="example", status="active", follower_count=51)
        db.add(account)
        db.flush()
        db.add(
            RelationshipScan(
                account_id=account.id,
                relationship_type="followers",
                scan_date=date(2026, 8, 26),
                status="running",
                collected_count=5,
                started_at=datetime(2026, 8, 26, 1, 0),
            )
        )
        db.add(
            Job(
                account_id=account.id,
                kind="relationship",
                content_type="followers",
                status="queued",
                error=None,
                created_at=datetime(2026, 8, 26, 1, 15),
                not_before=datetime(2026, 8, 26, 2, 0),
            )
        )
        db.commit()

        response = client.get(f"/accounts/{account.id}?tab=followers")

        assert response.status_code == 200
        assert "分批擷取中" in response.text
        assert "等待重試" not in response.text
        assert "查看本輪暫時失敗原因" not in response.text
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
        db.add(
            RelationshipMember(
                account_id=account.id,
                relationship_type="following",
                username="known_user",
                active=True,
            )
        )
        db.commit()

        response = client.get(f"/accounts/{account.id}?tab=following")

        assert response.status_code == 200
        assert "Threads 目前未公開" in response.text
        assert "顯示最後已知名單 1 人" in response.text
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_relationship_compare_supports_multiple_accounts_and_threshold() -> None:
    client, db = make_client()
    try:
        accounts = [Account(username=name, status="active") for name in ("one", "two", "three")]
        db.add_all(accounts)
        db.flush()
        for account in accounts:
            scan = RelationshipScan(
                account_id=account.id,
                relationship_type="followers",
                scan_date=date(2026, 8, 14),
                status="complete",
            )
            db.add(scan)
            db.flush()
            shared = RelationshipMember(
                account_id=account.id,
                relationship_type="followers",
                username="shared_user",
                display_name="Shared User",
                active=True,
            )
            db.add(shared)
            db.flush()
            db.add(RelationshipScanMember(scan_id=scan.id, member_id=shared.id))
        db.commit()

        response = client.get(
            "/relationships/compare",
            params=[
                ("account_ids", accounts[0].id),
                ("account_ids", accounts[1].id),
                ("account_ids", accounts[2].id),
                ("comparison_type", "followers"),
                ("min_present", 2),
            ],
        )

        assert response.status_code == 200
        assert "多帳號名單比較" in response.text
        assert "@shared_user" in response.text
        assert "出現在 3 / 3 個帳號" in response.text
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_relationship_compare_both_requires_member_in_both_lists_per_account() -> None:
    client, db = make_client()
    try:
        accounts = [Account(username=name, status="active") for name in ("one", "two")]
        db.add_all(accounts)
        db.flush()
        for account in accounts:
            for relationship_type in ("followers", "following"):
                scan = RelationshipScan(
                    account_id=account.id,
                    relationship_type=relationship_type,
                    scan_date=date(2026, 8, 14),
                    status="complete",
                )
                db.add(scan)
                db.flush()
                member = RelationshipMember(
                    account_id=account.id,
                    relationship_type=relationship_type,
                    username="mutual_user",
                    active=True,
                )
                db.add(member)
                db.flush()
                db.add(RelationshipScanMember(scan_id=scan.id, member_id=member.id))
        db.commit()

        response = client.get(
            "/relationships/compare",
            params=[
                ("account_ids", accounts[0].id),
                ("account_ids", accounts[1].id),
                ("comparison_type", "both"),
            ],
        )

        assert response.status_code == 200
        assert "@mutual_user" in response.text
        assert "粉絲與追蹤中" in response.text
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_relationship_compare_can_include_partial_scans_as_provisional_results() -> None:
    client, db = make_client()
    try:
        complete_account = Account(username="complete", status="active")
        partial_account = Account(username="partial", status="active")
        db.add_all([complete_account, partial_account])
        db.flush()
        for account, status in (
            (complete_account, "complete"),
            (partial_account, "running"),
        ):
            scan = RelationshipScan(
                account_id=account.id,
                relationship_type="followers",
                scan_date=date(2026, 8, 14),
                status=status,
                collected_count=1,
            )
            db.add(scan)
            db.flush()
            member = RelationshipMember(
                account_id=account.id,
                relationship_type="followers",
                username="visible_so_far",
                active=True,
            )
            db.add(member)
            db.flush()
            db.add(RelationshipScanMember(scan_id=scan.id, member_id=member.id))
        db.commit()

        response = client.get(
            "/relationships/compare",
            params=[
                ("account_ids", complete_account.id),
                ("account_ids", partial_account.id),
                ("comparison_type", "followers"),
                ("min_present", 2),
                ("include_partial", "true"),
            ],
        )

        assert response.status_code == 200
        assert "@visible_so_far" in response.text
        assert "出現在 2 / 2 個帳號" in response.text
        assert "@partial" in response.text
        assert "暫定結果" in response.text
        assert "已擷取 1 人" in response.text
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_relationship_compare_uses_previous_nonempty_partial_while_latest_is_empty() -> None:
    client, db = make_client()
    try:
        complete_account = Account(username="complete", status="active")
        partial_account = Account(username="partial", status="active")
        db.add_all([complete_account, partial_account])
        db.flush()

        complete_scan = RelationshipScan(
            account_id=complete_account.id,
            relationship_type="followers",
            scan_date=date(2026, 8, 25),
            status="complete",
            collected_count=1,
        )
        previous_partial_scan = RelationshipScan(
            account_id=partial_account.id,
            relationship_type="followers",
            scan_date=date(2026, 8, 25),
            status="failed",
            collected_count=1,
        )
        latest_empty_scan = RelationshipScan(
            account_id=partial_account.id,
            relationship_type="followers",
            scan_date=date(2026, 8, 26),
            status="running",
            collected_count=0,
        )
        db.add_all([complete_scan, previous_partial_scan, latest_empty_scan])
        db.flush()

        for account, scan in (
            (complete_account, complete_scan),
            (partial_account, previous_partial_scan),
        ):
            member = RelationshipMember(
                account_id=account.id,
                relationship_type="followers",
                username="known_shared_user",
                active=True,
            )
            db.add(member)
            db.flush()
            db.add(RelationshipScanMember(scan_id=scan.id, member_id=member.id))
        db.commit()

        response = client.get(
            "/relationships/compare",
            params=[
                ("account_ids", complete_account.id),
                ("account_ids", partial_account.id),
                ("comparison_type", "followers"),
                ("min_present", 2),
                ("include_partial", "true"),
            ],
        )

        assert response.status_code == 200
        assert "@known_shared_user" in response.text
        assert "出現在 2 / 2 個帳號" in response.text
        assert "暫定結果" in response.text
        assert "暫定比較 2026-08-25" in response.text
        assert "最新回補中 2026-08-26" in response.text
        assert "1 個帳號缺少所需的完整名單" not in response.text
    finally:
        app.dependency_overrides.clear()
        db.close()
