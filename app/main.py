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
