from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Account,
    CollectionRun,
    CollectionStream,
    Content,
    ContentMedia,
    ContentVersion,
    InteractionSnapshot,
    Job,
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
    ThreadsCollector,
    content_fingerprint,
)
from app.services.media import MediaStore
from app.services.queue import enqueue_unique, next_account_due, next_batch_time, now_utc

STREAM_TYPES = ("post", "reply", "repost", "quote")
RELATIONSHIP_TYPES = ("followers", "following")


class JobProcessor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.media = MediaStore(settings)

    def process(self, db: Session, job: Job) -> None:
        run = CollectionRun(
            account_id=job.account_id,
            job_kind=job.kind,
            content_type=job.content_type,
            status="running",
        )
        db.add(run)
        db.flush()
        try:
            account = db.get(Account, job.account_id) if job.account_id else None
            if not account or not account.enabled:
                raise CollectionError("監看帳號已停用或不存在")
            with ThreadsCollector(self.settings) as collector:
                if job.kind in {"verify", "profile"}:
                    profile = collector.collect_profile(account.username)
                    self._save_profile(db, account, profile)
                    self._schedule_relationship_scans(db, account)
                    self._schedule_stream(db, account)
                    run.item_count = 1
                elif job.kind == "content" and job.content_type in STREAM_TYPES:
                    stream = db.scalar(
                        select(CollectionStream).where(
                            CollectionStream.account_id == account.id,
                            CollectionStream.content_type == job.content_type,
                        )
                    )
                    items = collector.collect_content(
                        account.username,
                        job.content_type,
                        self.settings.batch_size,
                        cursor=stream.cursor if stream and stream.phase == "backfill" else None,
                    )
                    run.item_count = self._save_content_batch(db, account, job.content_type, items)
                elif job.kind == "relationship" and job.content_type in RELATIONSHIP_TYPES:
                    scan = self._current_relationship_scan(db, account, job.content_type)
                    if not scan:
                        raise CollectionError("找不到可執行的關係名單掃描")
                    batch = collector.collect_relationships(
                        account.username,
                        job.content_type,
                        self.settings.relationship_batch_size,
                        cursor=scan.cursor,
                    )
                    run.item_count = self._save_relationship_batch(db, account, scan, batch)
                else:
                    raise CollectionError(f"未知工作類型：{job.kind}")
            self._success(db, account, job, run)
            if job.kind == "relationship" and job.content_type in RELATIONSHIP_TYPES:
                db.flush()
                scan = self._current_relationship_scan(db, account, job.content_type)
                if scan and scan.status == "running":
                    enqueue_unique(
                        db,
                        kind="relationship",
                        account_id=account.id,
                        content_type=job.content_type,
                        priority=40,
                        not_before=next_batch_time(self.settings),
                    )
        except LoginRequired as exc:
            self._failure(db, job, run, exc, login_required=True)
        except Exception as exc:
            self._failure(db, job, run, exc)

    def _save_profile(self, db: Session, account: Account, data: ProfileData) -> None:
        now = now_utc()
        avatar_id = account.avatar_media_id
        if data.avatar_url:
            avatar = self.media.register(db, data.avatar_url, "image")
            self.media.download(db, avatar)
            avatar_id = avatar.id

        latest = db.scalar(
            select(ProfileVersion)
            .where(ProfileVersion.account_id == account.id)
            .order_by(ProfileVersion.id.desc())
            .limit(1)
        )
        signature = (data.display_name, data.bio, data.external_url, avatar_id)
        latest_signature = (
            (
                latest.display_name,
                latest.bio,
                latest.external_url,
                latest.avatar_media_id,
            )
            if latest
            else None
        )
        if latest and latest_signature == signature:
            latest.last_seen_at = now
        else:
            db.add(
                ProfileVersion(
                    account_id=account.id,
                    display_name=data.display_name,
                    bio=data.bio,
                    external_url=data.external_url,
                    avatar_media_id=avatar_id,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        db.add(
            StatSnapshot(
                account_id=account.id,
                follower_count=data.follower_count,
                following_count=data.following_count,
                observed_at=now,
            )
        )
        account.display_name = data.display_name
        account.bio = data.bio
        account.external_url = data.external_url
        account.avatar_media_id = avatar_id
        account.follower_count = data.follower_count
        account.following_count = data.following_count
        account.status = "active"
        account.status_message = None
        account.last_attempt_at = now
        account.last_success_at = now
        account.consecutive_failures = 0
        account.cooldown_until = None
        account.next_due_at = next_account_due(account, self.settings)
        for stream_type in STREAM_TYPES:
            stream = db.scalar(
                select(CollectionStream).where(
                    CollectionStream.account_id == account.id,
                    CollectionStream.content_type == stream_type,
                )
            )
            if not stream:
                db.add(CollectionStream(account_id=account.id, content_type=stream_type))
        db.flush()

    def _schedule_relationship_scans(self, db: Session, account: Account) -> None:
        scan_date = datetime.now(self.settings.tz).date()
        for relationship_type in RELATIONSHIP_TYPES:
            scan = db.scalar(
                select(RelationshipScan).where(
                    RelationshipScan.account_id == account.id,
                    RelationshipScan.relationship_type == relationship_type,
                    RelationshipScan.scan_date == scan_date,
                )
            )
            if scan is None:
                scan = RelationshipScan(
                    account_id=account.id,
                    relationship_type=relationship_type,
                    scan_date=scan_date,
                    status="running",
                )
                db.add(scan)
                db.flush()
            if scan.status == "running":
                enqueue_unique(
                    db,
                    kind="relationship",
                    account_id=account.id,
                    content_type=relationship_type,
                    priority=40,
                    not_before=next_batch_time(self.settings),
                )

    @staticmethod
    def _current_relationship_scan(
        db: Session, account: Account, relationship_type: str
    ) -> RelationshipScan | None:
        return db.scalar(
            select(RelationshipScan)
            .where(
                RelationshipScan.account_id == account.id,
                RelationshipScan.relationship_type == relationship_type,
                RelationshipScan.status == "running",
            )
            .order_by(RelationshipScan.scan_date, RelationshipScan.id)
            .limit(1)
        )

    def _save_relationship_batch(
        self,
        db: Session,
        account: Account,
        scan: RelationshipScan,
        batch: RelationshipBatch,
    ) -> int:
        now = now_utc()
        saved = 0
        for item in batch.members:
            avatar_id = None
            member = db.scalar(
                select(RelationshipMember).where(
                    RelationshipMember.account_id == account.id,
                    RelationshipMember.relationship_type == scan.relationship_type,
                    RelationshipMember.username == item.username,
                )
            )
            if item.avatar_url:
                avatar = self.media.register(db, item.avatar_url, "image")
                self.media.download(db, avatar)
                avatar_id = avatar.id
            if member is None:
                member = RelationshipMember(
                    account_id=account.id,
                    relationship_type=scan.relationship_type,
                    username=item.username,
                    display_name=item.display_name,
                    avatar_media_id=avatar_id,
                    active=True,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                db.add(member)
                db.flush()
            else:
                member.display_name = item.display_name or member.display_name
                member.avatar_media_id = avatar_id or member.avatar_media_id
                member.last_seen_at = now
            observed = db.scalar(
                select(RelationshipScanMember).where(
                    RelationshipScanMember.scan_id == scan.id,
                    RelationshipScanMember.member_id == member.id,
                )
            )
            if not observed:
                db.add(RelationshipScanMember(scan_id=scan.id, member_id=member.id))
                saved += 1

        db.flush()
        scan.cursor = batch.cursor
        scan.collected_count = int(
            db.scalar(
                select(func.count(RelationshipScanMember.id)).where(
                    RelationshipScanMember.scan_id == scan.id
                )
            )
            or 0
        )
        if batch.complete:
            self._complete_relationship_scan(db, account, scan)
        return saved

    @staticmethod
    def _complete_relationship_scan(
        db: Session, account: Account, scan: RelationshipScan
    ) -> None:
        now = now_utc()
        previous = db.scalar(
            select(RelationshipScan)
            .where(
                RelationshipScan.account_id == account.id,
                RelationshipScan.relationship_type == scan.relationship_type,
                RelationshipScan.status == "complete",
                RelationshipScan.id != scan.id,
            )
            .order_by(RelationshipScan.scan_date.desc(), RelationshipScan.id.desc())
            .limit(1)
        )
        current_ids = set(
            db.scalars(
                select(RelationshipScanMember.member_id).where(
                    RelationshipScanMember.scan_id == scan.id
                )
            ).all()
        )
        previous_ids: set[int] = set()
        if previous:
            previous_ids = set(
                db.scalars(
                    select(RelationshipScanMember.member_id).where(
                        RelationshipScanMember.scan_id == previous.id
                    )
                ).all()
            )
            for member_id in current_ids - previous_ids:
                db.add(
                    RelationshipChange(
                        account_id=account.id,
                        scan_id=scan.id,
                        member_id=member_id,
                        relationship_type=scan.relationship_type,
                        change_type="added",
                        observed_date=scan.scan_date,
                    )
                )
            for member_id in previous_ids - current_ids:
                db.add(
                    RelationshipChange(
                        account_id=account.id,
                        scan_id=scan.id,
                        member_id=member_id,
                        relationship_type=scan.relationship_type,
                        change_type="removed",
                        observed_date=scan.scan_date,
                    )
                )

        members = db.scalars(
            select(RelationshipMember).where(
                RelationshipMember.account_id == account.id,
                RelationshipMember.relationship_type == scan.relationship_type,
            )
        ).all()
        for member in members:
            member.active = member.id in current_ids
            if member.active:
                member.last_seen_at = now
                member.removed_at = None
            elif previous and member.id in previous_ids:
                member.removed_at = now
        scan.status = "complete"
        scan.completed_at = now

    def _schedule_stream(self, db: Session, account: Account) -> None:
        db.flush()
        stream = db.scalar(
            select(CollectionStream)
            .where(CollectionStream.account_id == account.id)
            .order_by(
                (CollectionStream.phase == "backfill").desc(),
                CollectionStream.last_collected_at.asc().nullsfirst(),
                CollectionStream.id,
            )
            .limit(1)
        )
        if stream:
            enqueue_unique(
                db,
                kind="content",
                account_id=account.id,
                content_type=stream.content_type,
                priority=50,
                not_before=next_batch_time(self.settings),
            )

    def _save_content_batch(
        self, db: Session, account: Account, content_type: str, items: list[ContentData]
    ) -> int:
        stream = db.scalar(
            select(CollectionStream).where(
                CollectionStream.account_id == account.id,
                CollectionStream.content_type == content_type,
            )
        )
        if not stream:
            stream = CollectionStream(account_id=account.id, content_type=content_type)
            db.add(stream)
            db.flush()
        new_count = 0
        for item in items:
            content = db.scalar(select(Content).where(Content.threads_id == item.threads_id))
            is_new = content is None
            if content is None:
                content = Content(
                    threads_id=item.threads_id,
                    account_id=account.id,
                    author_username=item.author_username,
                    content_type=item.content_type,
                    source_url=item.source_url,
                    published_at=item.published_at,
                    reply_to_threads_id=item.reply_to_threads_id,
                    quoted_threads_id=item.quoted_threads_id,
                )
                db.add(content)
                db.flush()
                new_count += 1
            content.last_seen_at = now_utc()
            content.unavailable_checks = 0
            content.suspected_removed = False

            media_assets = []
            for position, (url, media_type) in enumerate(item.media):
                asset = self.media.register(db, url, media_type)
                self.media.download(db, asset)
                media_assets.append(asset)
                exists = db.scalar(
                    select(ContentMedia).where(
                        ContentMedia.content_id == content.id, ContentMedia.media_id == asset.id
                    )
                )
                if not exists:
                    db.add(
                        ContentMedia(content_id=content.id, media_id=asset.id, position=position)
                    )

            fingerprint = content_fingerprint(
                item.text, [asset.sha256 or asset.source_key for asset in media_assets]
            )
            latest_version = db.scalar(
                select(ContentVersion)
                .where(ContentVersion.content_id == content.id)
                .order_by(ContentVersion.id.desc())
                .limit(1)
            )
            if not latest_version or latest_version.fingerprint != fingerprint:
                db.add(
                    ContentVersion(content_id=content.id, text=item.text, fingerprint=fingerprint)
                )

            metrics = (item.like_count, item.reply_count, item.repost_count, item.share_count)
            latest_metrics = db.scalar(
                select(InteractionSnapshot)
                .where(InteractionSnapshot.content_id == content.id)
                .order_by(InteractionSnapshot.id.desc())
                .limit(1)
            )
            previous = (
                (
                    latest_metrics.like_count,
                    latest_metrics.reply_count,
                    latest_metrics.repost_count,
                    latest_metrics.share_count,
                )
                if latest_metrics
                else None
            )
            if previous != metrics and (is_new or any(value is not None for value in metrics)):
                db.add(
                    InteractionSnapshot(
                        content_id=content.id,
                        like_count=item.like_count,
                        reply_count=item.reply_count,
                        repost_count=item.repost_count,
                        share_count=item.share_count,
                    )
                )

        stream.last_collected_at = now_utc()
        stream.collected_count = int(
            db.scalar(
                select(func.count(Content.id)).where(
                    Content.account_id == account.id, Content.content_type == content_type
                )
            )
            or 0
        )
        stream.cursor = items[-1].threads_id if items else stream.cursor
        stream.empty_batches = stream.empty_batches + 1 if new_count == 0 else 0
        if stream.phase == "backfill" and (
            stream.collected_count >= self.settings.backfill_limit or stream.empty_batches >= 2
        ):
            stream.phase = "incremental"

        self._schedule_stream(db, account)
        return new_count

    def _success(self, db: Session, account: Account, job: Job, run: CollectionRun) -> None:
        now = now_utc()
        job.status = "succeeded"
        job.finished_at = now
        run.status = "succeeded"
        run.finished_at = now
        account.last_attempt_at = now
        account.last_success_at = now
        account.consecutive_failures = 0
        if account.status not in {"pending", "login_required"}:
            account.status = "active"
            account.status_message = None

    def _failure(
        self,
        db: Session,
        job: Job,
        run: CollectionRun,
        exc: Exception,
        *,
        login_required: bool = False,
    ) -> None:
        now = now_utc()
        message = str(exc)[:1000]
        job.status = "failed"
        job.error = message
        job.finished_at = now
        run.status = "failed"
        run.message = message
        run.finished_at = now
        account = db.get(Account, job.account_id) if job.account_id else None
        if not account:
            return
        account.last_attempt_at = now
        if job.kind == "relationship":
            scan = db.scalar(
                select(RelationshipScan)
                .where(
                    RelationshipScan.account_id == account.id,
                    RelationshipScan.relationship_type == job.content_type,
                    RelationshipScan.status == "running",
                )
                .order_by(RelationshipScan.id)
                .limit(1)
            )
            if scan:
                scan.status = "failed"
                scan.completed_at = now
            if not login_required:
                return
        account.status_message = message
        if login_required:
            account.status = "login_required"
            return
        account.consecutive_failures += 1
        if account.consecutive_failures >= 3:
            account.status = "cooldown"
            account.cooldown_until = now + timedelta(hours=24)
            account.next_due_at = account.cooldown_until
        else:
            delay_minutes = min(2**account.consecutive_failures * 5, 120)
            account.status = "error"
            account.next_due_at = now + timedelta(minutes=delay_minutes)
