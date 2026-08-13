from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import __version__
from app.config import get_settings
from app.db import create_schema, get_db
from app.models import (
    Account,
    CollectionRun,
    Content,
    ContentMedia,
    InteractionSnapshot,
    Job,
    MediaAsset,
    ProfileVersion,
    RelationshipChange,
    RelationshipMember,
    RelationshipScan,
    RelationshipScanMember,
    StatSnapshot,
)
from app.services.media import media_usage_bytes, media_usage_percent
from app.services.queue import enqueue_unique, now_utc
from app.services.usernames import InvalidUsername, normalize_username

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_schema()
    yield


app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
base_dir = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=base_dir / "static"), name="static")
templates = Jinja2Templates(directory=base_dir / "templates")
templates.env.globals["app_version"] = __version__


def format_number(value: int | None) -> str:
    return "未知" if value is None else f"{value:,}"


def format_time(value: datetime | None) -> str:
    if value is None:
        return "尚未"
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(settings.tz).strftime("%Y/%m/%d %H:%M")


templates.env.filters["number"] = format_number
templates.env.filters["localtime"] = format_time


def _account_cards(db: Session) -> list[dict]:
    accounts = db.scalars(
        select(Account)
        .where(Account.enabled.is_(True))
        .options(selectinload(Account.streams), selectinload(Account.avatar))
        .order_by(Account.sort_order, Account.id)
    ).all()
    cards = []
    for account in accounts:
        snapshots = db.scalars(
            select(StatSnapshot)
            .where(StatSnapshot.account_id == account.id)
            .order_by(StatSnapshot.observed_at.desc())
            .limit(2)
        ).all()
        follower_delta = None
        following_delta = None
        if len(snapshots) == 2:
            if snapshots[0].follower_count is not None and snapshots[1].follower_count is not None:
                follower_delta = snapshots[0].follower_count - snapshots[1].follower_count
            if (
                snapshots[0].following_count is not None
                and snapshots[1].following_count is not None
            ):
                following_delta = snapshots[0].following_count - snapshots[1].following_count
        total = len(account.streams) * settings.backfill_limit
        done = sum(
            min(stream.collected_count, settings.backfill_limit) for stream in account.streams
        )
        completed_streams = sum(stream.phase == "incremental" for stream in account.streams)
        progress = (
            100
            if account.streams and completed_streams == len(account.streams)
            else (round(done / total * 100) if total else 0)
        )
        cards.append(
            {
                "account": account,
                "follower_delta": follower_delta,
                "following_delta": following_delta,
                "progress": progress,
                "avatar_url": f"/media/{account.avatar.local_path}"
                if account.avatar and account.avatar.local_path
                else None,
            }
        )
    return cards


def _stream_views(account: Account) -> list[dict]:
    labels = {
        "post": "串文",
        "reply": "回覆",
        "repost": "轉發",
        "quote": "引用轉發",
    }
    streams = {stream.content_type: stream for stream in account.streams}
    views = []
    for content_type, label in labels.items():
        stream = streams.get(content_type)
        collected_count = stream.collected_count if stream else 0
        completed = bool(stream and stream.phase == "incremental")
        views.append(
            {
                "content_type": content_type,
                "label": label,
                "stream": stream,
                "collected_count": collected_count,
                "completed": completed,
                "progress": 100
                if completed
                else round(min(collected_count, settings.backfill_limit) / settings.backfill_limit * 100),
            }
        )
    return views


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), error: str | None = None):
    usage = media_usage_bytes(db)
    usage_percent = media_usage_percent(db, settings)
    login_required = bool(
        db.scalar(select(func.count(Account.id)).where(Account.status == "login_required"))
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "cards": _account_cards(db),
            "active_count": db.scalar(
                select(func.count(Account.id)).where(Account.enabled.is_(True))
            ),
            "max_accounts": settings.max_active_accounts,
            "usage_bytes": usage,
            "usage_percent": usage_percent,
            "usage_gb": usage / 1024**3,
            "login_required": login_required,
            "error": error,
            "version": __version__,
        },
    )


@app.post("/accounts")
def add_account(username: str = Form(...), db: Session = Depends(get_db)):
    try:
        normalized = normalize_username(username)
    except InvalidUsername as exc:
        return RedirectResponse(f"/?error={str(exc)}", status_code=303)
    account = db.scalar(select(Account).where(Account.username == normalized))
    if account:
        if account.enabled:
            return RedirectResponse("/?error=此帳號已在監看清單中", status_code=303)
        account.enabled = True
        account.status = "pending"
        account.status_message = "等待重新驗證"
        account.next_due_at = now_utc()
    else:
        active_count = int(
            db.scalar(select(func.count(Account.id)).where(Account.enabled.is_(True))) or 0
        )
        if active_count >= settings.max_active_accounts:
            return RedirectResponse("/?error=已達 16 個啟用帳號上限", status_code=303)
        max_order_value = db.scalar(select(func.max(Account.sort_order)))
        max_order = int(max_order_value) if max_order_value is not None else -1
        account = Account(
            username=normalized,
            status="pending",
            status_message="等待背景驗證",
            sort_order=max_order + 1,
            next_due_at=now_utc(),
        )
        db.add(account)
        db.flush()
    enqueue_unique(db, kind="verify", account_id=account.id, priority=1)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/accounts/reorder")
async def reorder_accounts(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    ids = payload.get("ids", [])
    if not isinstance(ids, list):
        raise HTTPException(400, "排序資料格式錯誤")
    accounts = db.scalars(
        select(Account).where(Account.id.in_(ids), Account.enabled.is_(True))
    ).all()
    by_id = {account.id: account for account in accounts}
    if len(by_id) != len(set(ids)):
        raise HTTPException(400, "排序包含未知帳號")
    for order, account_id in enumerate(ids):
        by_id[int(account_id)].sort_order = order
    db.commit()
    return JSONResponse({"ok": True})


@app.post("/accounts/{account_id}/retry")
def retry_account(account_id: int, db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if not account or not account.enabled:
        raise HTTPException(404)
    account.cooldown_until = None
    account.next_due_at = now_utc()
    account.status = "pending" if not account.last_success_at else "queued"
    account.status_message = None
    enqueue_unique(
        db,
        kind="verify" if not account.last_success_at else "profile",
        account_id=account.id,
        priority=1,
    )
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/accounts/{account_id}/stop")
def stop_account(account_id: int, db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404)
    account.enabled = False
    account.status = "stopped"
    for job in db.scalars(
        select(Job).where(Job.account_id == account.id, Job.status == "queued")
    ).all():
        job.status = "cancelled"
        job.finished_at = now_utc()
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/accounts/{account_id}/interval")
def update_interval(
    account_id: int, interval_hours: int = Form(...), db: Session = Depends(get_db)
):
    if interval_hours not in {6, 12, 24}:
        raise HTTPException(400, "拜訪週期只能是 6、12 或 24 小時")
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404)
    account.interval_hours = interval_hours
    db.commit()
    return RedirectResponse(f"/accounts/{account_id}", status_code=303)


def _latest_relationship_scan(
    db: Session, account_id: int, relationship_type: str, *, complete_only: bool = False
) -> RelationshipScan | None:
    query = select(RelationshipScan).where(
        RelationshipScan.account_id == account_id,
        RelationshipScan.relationship_type == relationship_type,
    )
    if complete_only:
        query = query.where(RelationshipScan.status == "complete")
    return db.scalar(
        query.order_by(RelationshipScan.scan_date.desc(), RelationshipScan.id.desc()).limit(1)
    )


def _comparison_members(
    db: Session, scans: dict[int, RelationshipScan]
) -> dict[str, dict]:
    if not scans:
        return {}
    members = db.scalars(
        select(RelationshipMember)
        .join(
            RelationshipScanMember,
            RelationshipScanMember.member_id == RelationshipMember.id,
        )
        .where(RelationshipScanMember.scan_id.in_([scan.id for scan in scans.values()]))
        .options(selectinload(RelationshipMember.avatar))
    ).all()
    result: dict[str, dict] = {}
    for member in members:
        key = member.username.lower()
        item = result.setdefault(
            key,
            {
                "username": member.username,
                "display_name": member.display_name,
                "avatar_url": None,
                "account_ids": set(),
            },
        )
        item["account_ids"].add(member.account_id)
        if not item["display_name"] and member.display_name:
            item["display_name"] = member.display_name
        if not item["avatar_url"] and member.avatar and member.avatar.local_path:
            item["avatar_url"] = f"/media/{member.avatar.local_path}"
    return result


@app.get("/relationships/compare", response_class=HTMLResponse)
def compare_relationships(
    request: Request,
    account_ids: list[int] = Query(default=[]),
    comparison_type: str = Query("followers"),
    min_present: int | None = Query(None, ge=1),
    include_partial: bool = Query(False),
    db: Session = Depends(get_db),
):
    if comparison_type not in {"followers", "following", "both"}:
        raise HTTPException(400, "未知的比較類型")
    selected_ids = list(dict.fromkeys(account_ids))[: settings.max_active_accounts]
    accounts = db.scalars(
        select(Account)
        .where(Account.enabled.is_(True))
        .options(selectinload(Account.avatar))
        .order_by(Account.sort_order, Account.id)
    ).all()
    accounts_by_id = {account.id: account for account in accounts}
    selected_accounts = [
        accounts_by_id[account_id]
        for account_id in selected_ids
        if account_id in accounts_by_id
    ]
    selected_ids = [account.id for account in selected_accounts]

    needed_types = (
        ("followers", "following") if comparison_type == "both" else (comparison_type,)
    )
    scan_statuses = []
    complete_scans: dict[str, dict[int, RelationshipScan]] = {
        relationship_type: {} for relationship_type in needed_types
    }
    comparison_scans: dict[str, dict[int, RelationshipScan]] = {
        relationship_type: {} for relationship_type in needed_types
    }
    provisional_account_ids: set[int] = set()
    for account in selected_accounts:
        type_statuses = {}
        for relationship_type in needed_types:
            latest = _latest_relationship_scan(db, account.id, relationship_type)
            complete = _latest_relationship_scan(
                db, account.id, relationship_type, complete_only=True
            )
            type_statuses[relationship_type] = {"latest": latest, "complete": complete}
            if complete:
                complete_scans[relationship_type][account.id] = complete
            selected_scan = complete
            if (
                include_partial
                and latest
                and latest.status in {"running", "failed"}
                and latest.collected_count > 0
            ):
                selected_scan = latest
                provisional_account_ids.add(account.id)
            if selected_scan:
                comparison_scans[relationship_type][account.id] = selected_scan
        scan_statuses.append({"account": account, "types": type_statuses})

    eligible_ids = set(selected_ids)
    for relationship_type in needed_types:
        eligible_ids &= set(comparison_scans[relationship_type])
    eligible_accounts = [account for account in selected_accounts if account.id in eligible_ids]
    threshold = min_present or len(eligible_accounts)
    threshold = max(threshold, 1)

    results = []
    if len(selected_accounts) >= 2 and eligible_accounts:
        member_maps = {
            relationship_type: _comparison_members(
                db,
                {
                    account_id: scan
                    for account_id, scan in comparison_scans[relationship_type].items()
                    if account_id in eligible_ids
                },
            )
            for relationship_type in needed_types
        }
        usernames = set.intersection(*(set(items) for items in member_maps.values()))
        for username in usernames:
            source_items = [member_maps[item_type][username] for item_type in needed_types]
            present_ids = set.intersection(
                *(set(item["account_ids"]) for item in source_items)
            )
            if len(present_ids) < threshold:
                continue
            display_source = next(
                (item for item in source_items if item["display_name"]), source_items[0]
            )
            avatar_source = next(
                (item for item in source_items if item["avatar_url"]), source_items[0]
            )
            results.append(
                {
                    "username": display_source["username"],
                    "display_name": display_source["display_name"],
                    "avatar_url": avatar_source["avatar_url"],
                    "accounts": [
                        account for account in eligible_accounts if account.id in present_ids
                    ],
                    "present_count": len(present_ids),
                }
            )
        results.sort(key=lambda item: (-item["present_count"], item["username"].lower()))

    return templates.TemplateResponse(
        request,
        "relationship_compare.html",
        {
            "accounts": accounts,
            "selected_ids": selected_ids,
            "selected_accounts": selected_accounts,
            "eligible_accounts": eligible_accounts,
            "scan_statuses": scan_statuses,
            "comparison_type": comparison_type,
            "min_present": threshold,
            "include_partial": include_partial,
            "is_provisional": bool(provisional_account_ids & eligible_ids),
            "results": results,
            "comparison_labels": {
                "followers": "共同粉絲",
                "following": "共同追蹤中",
                "both": "粉絲與追蹤中 Both",
            },
        },
    )


@app.get("/accounts/{account_id}", response_class=HTMLResponse)
def account_detail(
    account_id: int,
    request: Request,
    tab: str = Query("all"),
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    account = db.scalar(
        select(Account)
        .where(Account.id == account_id)
        .options(selectinload(Account.avatar), selectinload(Account.streams))
    )
    if not account:
        raise HTTPException(404)
    page_size = 20
    contents = []
    has_next = False
    if tab not in {"runs", "followers", "following", "changes"}:
        content_query = (
            select(Content)
            .where(Content.account_id == account.id)
            .options(
                selectinload(Content.versions),
                selectinload(Content.media_links).selectinload(ContentMedia.media),
            )
        )
        if tab in {"post", "reply", "repost", "quote"}:
            content_query = content_query.where(Content.content_type == tab)
        elif tab == "media":
            content_query = content_query.join(ContentMedia).distinct()
        contents = (
            db.scalars(
                content_query.order_by(Content.published_at.desc().nullslast(), Content.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size + 1)
            )
            .unique()
            .all()
        )
        has_next = len(contents) > page_size
        contents = contents[:page_size]
    content_views = []
    for content in contents:
        metrics = db.scalar(
            select(InteractionSnapshot)
            .where(InteractionSnapshot.content_id == content.id)
            .order_by(InteractionSnapshot.id.desc())
            .limit(1)
        )
        content_views.append({"content": content, "metrics": metrics})

    snapshots = db.scalars(
        select(StatSnapshot)
        .where(StatSnapshot.account_id == account.id)
        .order_by(StatSnapshot.observed_at.asc())
        .limit(365)
    ).all()
    chart_data = json.dumps(
        {
            "labels": [format_time(item.observed_at) for item in snapshots],
            "followers": [item.follower_count for item in snapshots],
            "following": [item.following_count for item in snapshots],
        },
        ensure_ascii=False,
    )
    runs = []
    if tab == "runs":
        runs = db.scalars(
            select(CollectionRun)
            .where(CollectionRun.account_id == account.id)
            .order_by(CollectionRun.started_at.desc())
            .limit(100)
        ).all()
    relationship_members = []
    relationship_scan = None
    if tab in {"followers", "following"}:
        relationship_members = db.scalars(
            select(RelationshipMember)
            .where(
                RelationshipMember.account_id == account.id,
                RelationshipMember.relationship_type == tab,
                RelationshipMember.active.is_(True),
            )
            .options(selectinload(RelationshipMember.avatar))
            .order_by(RelationshipMember.display_name, RelationshipMember.username)
            .offset((page - 1) * 50)
            .limit(51)
        ).all()
        has_next = len(relationship_members) > 50
        relationship_members = relationship_members[:50]
        relationship_scan = db.scalar(
            select(RelationshipScan)
            .where(
                RelationshipScan.account_id == account.id,
                RelationshipScan.relationship_type == tab,
            )
            .order_by(RelationshipScan.scan_date.desc(), RelationshipScan.id.desc())
            .limit(1)
        )
    relationship_changes = []
    if tab == "changes":
        relationship_changes = db.scalars(
            select(RelationshipChange)
            .where(RelationshipChange.account_id == account.id)
            .options(selectinload(RelationshipChange.member).selectinload(RelationshipMember.avatar))
            .order_by(
                RelationshipChange.observed_date.desc(), RelationshipChange.id.desc()
            )
            .offset((page - 1) * 100)
            .limit(101)
        ).all()
        has_next = len(relationship_changes) > 100
        relationship_changes = relationship_changes[:100]
    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "account": account,
            "tab": tab,
            "page": page,
            "has_next": has_next,
            "content_views": content_views,
            "runs": runs,
            "relationship_members": relationship_members,
            "relationship_scan": relationship_scan,
            "relationship_changes": relationship_changes,
            "relationship_batch_size": settings.relationship_batch_size,
            "chart_data": chart_data,
            "stream_views": _stream_views(account),
            "backfill_limit": settings.backfill_limit,
            "avatar_url": f"/media/{account.avatar.local_path}"
            if account.avatar and account.avatar.local_path
            else None,
        },
    )


@app.post("/accounts/{account_id}/delete")
def delete_account(account_id: int, confirmation: str = Form(...), db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404)
    if confirmation.strip().lower() != account.username.lower():
        return RedirectResponse(f"/accounts/{account_id}?error=確認文字不符", status_code=303)
    db.delete(account)
    db.flush()
    orphan_assets = db.scalars(
        select(MediaAsset).where(
            ~MediaAsset.id.in_(select(ContentMedia.media_id)),
            ~MediaAsset.id.in_(
                select(ProfileVersion.avatar_media_id).where(
                    ProfileVersion.avatar_media_id.is_not(None)
                )
            ),
            ~MediaAsset.id.in_(
                select(Account.avatar_media_id).where(Account.avatar_media_id.is_not(None))
            ),
            ~MediaAsset.id.in_(
                select(RelationshipMember.avatar_media_id).where(
                    RelationshipMember.avatar_media_id.is_not(None)
                )
            ),
        )
    ).all()
    for asset in orphan_assets:
        local_path = asset.local_path
        db.delete(asset)
        db.flush()
        still_shared = (
            db.scalar(select(func.count(MediaAsset.id)).where(MediaAsset.local_path == local_path))
            if local_path
            else 0
        )
        if local_path and not still_shared:
            path = (settings.media_root / local_path).resolve()
            if settings.media_root.resolve() in path.parents:
                path.unlink(missing_ok=True)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/media/{media_path:path}")
def serve_media(media_path: str):
    target = (settings.media_root / media_path).resolve()
    root = settings.media_root.resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    db.scalar(select(func.count(Account.id)))
    return {"status": "ok", "version": __version__, "time": now_utc().isoformat()}
