from __future__ import annotations

import hashlib
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
    MediaAsset,
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
    RateLimited,
    RelationshipBatch,
    ThreadsCollector,
    TransientRelationshipError,
    content_fingerprint,
)
from app.services.media import (
    MediaStore,
    canonical_media_key,
    deduplicate_media_candidates,
    media_equivalent,
    media_identity,
)
from app.services.notifications import NotificationService
from app.services.queue import (
    defer_for_rate_limit,
    enqueue_unique,
    next_account_due,
    next_batch_time,
    next_relationship_retry,
    now_utc,
)

STREAM_TYPES = ("post", "reply", "repost", "quote")
RELATIONSHIP_TYPES = ("followers", "following")


class JobProcessor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.media = MediaStore(settings)
        self.notifications = NotificationService(settings)

    def process(self, db: Session, job: Job) -> None:
        run = CollectionRun(
            account_id=job.account_id,
            job_kind=job.kind,
            content_type=job.content_type,
            status="running",
        )
        db.add(run)
        db.flush()
        # Persist the run marker before browser/network I/O.  Keeping this INSERT
        # uncommitted holds SQLite's single writer lock for the entire collection
        # and prevents otherwise unrelated web/worker writes from completing.
        db.commit()
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
                elif job.kind == "content_refresh" and job.content_id is not None:
                    content = db.scalar(
                        select(Content).where(
                            Content.id == job.content_id,
                            Content.account_id == account.id,
                        )
                    )
                    if content is None:
                        raise CollectionError("找不到要更新的舊貼文")
                    run.content_type = content.content_type
                    item = collector.collect_content_url(
                        content.source_url,
                        account.username,
                        content.content_type,
                    )
                    if item.threads_id != content.threads_id:
                        raise CollectionError("Threads 回傳的貼文與排定更新目標不一致")
                    self._save_content_batch(
                        db,
                        account,
                        content.content_type,
                        [item],
                        schedule_stream=False,
                    )
                    run.item_count = 1
                elif job.kind == "relationship" and job.content_type in RELATIONSHIP_TYPES:
                    scan = self._current_relationship_scan(db, account, job.content_type)
                    if not scan:
                        raise CollectionError("找不到可執行的關係名單掃描")
                    expected_count = (
                        account.follower_count
                        if job.content_type == "followers"
                        else account.following_count
                    )
                    seen_usernames = set(
                        db.scalars(
                            select(RelationshipMember.username)
                            .join(
                                RelationshipScanMember,
                                RelationshipScanMember.member_id == RelationshipMember.id,
                            )
                            .where(RelationshipScanMember.scan_id == scan.id)
                        ).all()
                    )
                    batch = collector.collect_relationships(
                        account.username,
                        job.content_type,
                        self.settings.relationship_batch_size_for(expected_count),
                        cursor=scan.cursor,
                        expected_count=expected_count,
                        seen_usernames=seen_usernames,
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
        except RateLimited as exc:
            self._rate_limited(db, job, run, exc)
        except LoginRequired as exc:
            self._failure(db, job, run, exc, login_required=True)
        except Exception as exc:
            self._failure(db, job, run, exc)

    def _save_profile(self, db: Session, account: Account, data: ProfileData) -> None:
        now = now_utc()
        avatar_id = account.avatar_media_id
        if data.avatar_url:
            avatar = self.media.register(db, data.avatar_url, "image")
            # A profile has already been collected and no profile-version changes
            # have been staged yet.  Commit only the registered asset so SQLite
            # does not retain a writer lock throughout the CDN request.
            db.commit()
            avatar = self.media.download(db, avatar)
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
                    RelationshipScan.status == "running",
                )
                .order_by(RelationshipScan.scan_date, RelationshipScan.id)
                .limit(1)
            )
            if scan is None:
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
                    status="unavailable" if relationship_type == "following" else "running",
                )
                db.add(scan)
                db.flush()
            if relationship_type == "following" and account.following_count is None:
                scan.status = "unavailable"
                scan.completed_at = now_utc()
                continue
            if relationship_type == "following" and scan.status == "unavailable":
                scan.status = "running"
                scan.completed_at = None
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
        if batch.follower_count is not None:
            account.follower_count = batch.follower_count
        if batch.following_count is not None:
            account.following_count = batch.following_count
            self._activate_following_scan(db, account, batch.following_count)
        expected_count = (
            account.follower_count
            if scan.relationship_type == "followers"
            else account.following_count
        )
        if (
            batch.complete
            and batch.observed_usernames is not None
            and not batch.observed_usernames
            and expected_count != 0
        ):
            label = "粉絲" if scan.relationship_type == "followers" else "追蹤中"
            raise TransientRelationshipError(
                f"{label}清單尚未載入，拒絕將未知或非空帳號記為空名單"
            )
        awaiting_removal_confirmation = False
        if batch.complete and batch.observed_usernames is not None:
            removal_fingerprint = self._relationship_removal_fingerprint(
                db,
                account,
                scan,
                batch.observed_usernames,
            )
            if (
                removal_fingerprint is not None
                and scan.removal_confirmation_fingerprint != removal_fingerprint
            ):
                scan.removal_confirmation_fingerprint = removal_fingerprint
                awaiting_removal_confirmation = True
            else:
                scan.removal_confirmation_fingerprint = None
        for item in batch.members:
            member = db.scalar(
                select(RelationshipMember).where(
                    RelationshipMember.account_id == account.id,
                    RelationshipMember.relationship_type == scan.relationship_type,
                    RelationshipMember.username == item.username,
                )
            )
            avatar_id = member.avatar_media_id if member is not None else None
            if item.avatar_url:
                current_avatar = db.get(MediaAsset, avatar_id) if avatar_id else None
                current_key = (
                    canonical_media_key(current_avatar.source_url)
                    if current_avatar is not None
                    else None
                )
                incoming_key = canonical_media_key(item.avatar_url)
                unchanged_downloaded_avatar = bool(
                    current_avatar is not None
                    and current_avatar.download_status == "downloaded"
                    and current_avatar.local_path
                    and (
                        current_avatar.source_url == item.avatar_url
                        or (incoming_key is not None and incoming_key == current_key)
                    )
                )
                if not unchanged_downloaded_avatar:
                    avatar = self.media.register(db, item.avatar_url, "image")
                    # Relationship scans are checkpointed by design.  Persist the
                    # asset registration before network I/O so web writes are not
                    # blocked, without making MediaStore commit its caller's work.
                    db.commit()
                    avatar = self.media.download(db, avatar)
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
        if (
            batch.complete
            and batch.observed_usernames is not None
            and not awaiting_removal_confirmation
        ):
            observed_usernames = {
                username.casefold() for username in batch.observed_usernames
            }
            scan_members = db.execute(
                select(RelationshipScanMember, RelationshipMember.username)
                .join(
                    RelationshipMember,
                    RelationshipMember.id == RelationshipScanMember.member_id,
                )
                .where(RelationshipScanMember.scan_id == scan.id)
            ).all()
            for scan_member, member_username in scan_members:
                if member_username.casefold() not in observed_usernames:
                    db.delete(scan_member)
            db.flush()
        previous_cursor = scan.cursor
        scan.cursor = batch.cursor if saved > 0 else previous_cursor
        scan.collected_count = int(
            db.scalar(
                select(func.count(RelationshipScanMember.id)).where(
                    RelationshipScanMember.scan_id == scan.id
                )
            )
            or 0
        )
        if (
            batch.complete
            and scan.collected_count == 0
            and expected_count != 0
        ):
            label = "粉絲" if scan.relationship_type == "followers" else "追蹤中"
            raise TransientRelationshipError(
                f"{label}清單尚未載入，拒絕將未知或非空帳號記為空名單"
            )
        if batch.complete and not awaiting_removal_confirmation:
            self._complete_relationship_scan(db, account, scan)
        if (
            scan.status == "running"
            and saved == 0
            and not awaiting_removal_confirmation
        ):
            label = "粉絲" if scan.relationship_type == "followers" else "追蹤中"
            raise TransientRelationshipError(
                f"Threads {label}清單本批未取得新成員，已保留目前進度"
            )
        return saved

    @staticmethod
    def _relationship_removal_fingerprint(
        db: Session,
        account: Account,
        scan: RelationshipScan,
        observed_usernames: set[str],
    ) -> str | None:
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
        if previous is None:
            return None
        previous_usernames = {
            username.casefold()
            for username in db.scalars(
                select(RelationshipMember.username)
                .join(
                    RelationshipScanMember,
                    RelationshipScanMember.member_id == RelationshipMember.id,
                )
                .where(RelationshipScanMember.scan_id == previous.id)
            ).all()
        }
        observed = {username.casefold() for username in observed_usernames}
        removed = sorted(previous_usernames - observed)
        if not removed:
            return None
        return hashlib.sha256("\n".join(removed).encode()).hexdigest()

    def _activate_following_scan(
        self, db: Session, account: Account, following_count: int
    ) -> None:
        scan_date = datetime.now(self.settings.tz).date()
        scan = db.scalar(
            select(RelationshipScan).where(
                RelationshipScan.account_id == account.id,
                RelationshipScan.relationship_type == "following",
                RelationshipScan.status == "running",
            )
            .order_by(RelationshipScan.scan_date, RelationshipScan.id)
            .limit(1)
        )
        if scan is None:
            scan = db.scalar(
                select(RelationshipScan).where(
                    RelationshipScan.account_id == account.id,
                    RelationshipScan.relationship_type == "following",
                    RelationshipScan.scan_date == scan_date,
                )
            )
        if scan is None:
            scan = RelationshipScan(
                account_id=account.id,
                relationship_type="following",
                scan_date=scan_date,
                status="running",
            )
            db.add(scan)
            db.flush()
        if scan.status == "failed":
            return
        elif following_count > 0 and scan.status == "unavailable":
            scan.status = "running"
            scan.completed_at = None
        elif following_count == 0 and scan.status in {"running", "unavailable"}:
            self._complete_relationship_scan(db, account, scan)
            return
        if scan.status == "running":
            enqueue_unique(
                db,
                kind="relationship",
                account_id=account.id,
                content_type="following",
                priority=40,
                not_before=next_batch_time(self.settings),
            )

    def _complete_relationship_scan(
        self, db: Session, account: Account, scan: RelationshipScan
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
        added_usernames: list[str] = []
        removed_usernames: list[str] = []
        if previous:
            previous_ids = set(
                db.scalars(
                    select(RelationshipScanMember.member_id).where(
                        RelationshipScanMember.scan_id == previous.id
                    )
                ).all()
            )
            changed_members = {
                member.id: member.username
                for member in db.scalars(
                    select(RelationshipMember).where(
                        RelationshipMember.id.in_(current_ids ^ previous_ids)
                    )
                ).all()
            }
            added_ids = current_ids - previous_ids
            removed_ids = previous_ids - current_ids
            added_usernames = sorted(changed_members[member_id] for member_id in added_ids)
            removed_usernames = sorted(changed_members[member_id] for member_id in removed_ids)
            for member_id in added_ids:
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
            for member_id in removed_ids:
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
        if previous:
            self.notifications.queue_relationship_changes(
                db,
                account,
                scan_id=scan.id,
                relationship_type=scan.relationship_type,
                scan_date=scan.scan_date,
                added=added_usernames,
                removed=removed_usernames,
            )

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
        self,
        db: Session,
        account: Account,
        content_type: str,
        items: list[ContentData],
        *,
        schedule_stream: bool = True,
    ) -> int:
        stream = None
        if schedule_stream:
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
        starting_phase = stream.phase if stream else "incremental"
        new_count = 0
        new_items: list[ContentData] = []
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
                new_items.append(item)
            else:
                content.author_username = item.author_username or content.author_username
                content.content_type = item.content_type or content.content_type
                content.source_url = item.source_url or content.source_url
                content.published_at = item.published_at or content.published_at
                content.reply_to_threads_id = (
                    item.reply_to_threads_id or content.reply_to_threads_id
                )
                content.quoted_threads_id = (
                    item.quoted_threads_id or content.quoted_threads_id
                )
            content.last_seen_at = now_utc()
            content.unavailable_checks = 0
            content.suspected_removed = False

            media_assets = []
            item_media_keys: set[tuple[str, str]] = set()
            linked_media_links = db.scalars(
                select(ContentMedia)
                .where(ContentMedia.content_id == content.id)
                .order_by(ContentMedia.position, ContentMedia.id)
            ).all()
            linked_media_assets = [
                asset
                for link in linked_media_links
                if (asset := db.get(MediaAsset, link.media_id)) is not None
            ]
            media_candidates = deduplicate_media_candidates(item.media)
            for position, (url, media_type) in enumerate(media_candidates):
                asset = self.media.register(db, url, media_type)
                asset = self.media.download(db, asset)
                key = media_identity(asset)
                if key in item_media_keys or any(
                    media_equivalent(asset, existing, self.settings.media_root)
                    for existing in media_assets
                ):
                    continue
                item_media_keys.add(key)
                media_assets.append(asset)
                matching_index = next(
                    (
                        index
                        for index, existing in enumerate(linked_media_assets)
                        if media_identity(existing) == key
                    ),
                    None,
                )
                if matching_index is not None:
                    existing = linked_media_assets[matching_index]
                    if (asset.byte_size or 0) > (existing.byte_size or 0):
                        linked_media_links[matching_index].media_id = asset.id
                        linked_media_assets[matching_index] = asset
                    continue
                if any(
                    media_equivalent(asset, existing, self.settings.media_root)
                    for existing in linked_media_assets
                ):
                    continue
                link = ContentMedia(
                    content_id=content.id, media_id=asset.id, position=position
                )
                db.add(link)
                linked_media_links.append(link)
                linked_media_assets.append(asset)

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

        if stream:
            stream.last_collected_at = now_utc()
            stream.collected_count = int(
                db.scalar(
                    select(func.count(Content.id)).where(
                        Content.account_id == account.id,
                        Content.content_type == content_type,
                    )
                )
                or 0
            )
            stream.cursor = items[-1].threads_id if items else stream.cursor
            stream.empty_batches = stream.empty_batches + 1 if new_count == 0 else 0
            if stream.phase == "backfill" and (
                stream.collected_count >= self.settings.backfill_limit
                or stream.empty_batches >= 2
            ):
                stream.phase = "incremental"

            self.notifications.queue_content_changes(
                db, account, starting_phase, content_type, new_items
            )
            self._schedule_stream(db, account)
        return new_count

    def _success(self, db: Session, account: Account, job: Job, run: CollectionRun) -> None:
        now = now_utc()
        job.status = "succeeded"
        job.error = None
        job.finished_at = now
        run.status = "succeeded"
        run.finished_at = now
        account.last_attempt_at = now
        if job.kind in {"verify", "profile"}:
            account.last_success_at = now
        account.consecutive_failures = 0
        if account.status not in {"pending", "login_required"}:
            account.status = "active"
            account.status_message = None

    def _rate_limited(
        self,
        db: Session,
        job: Job,
        run: CollectionRun,
        exc: RateLimited,
    ) -> None:
        """Preserve the current job while a shared Threads cooldown is active."""
        now = now_utc()
        message = str(exc)[:1000]
        retry_at, _hits = defer_for_rate_limit(
            db,
            self.settings,
            retry_after_seconds=exc.retry_after_seconds,
            reason=message,
        )

        job.status = "queued"
        job.attempts = max(job.attempts - 1, 0)
        job.not_before = retry_at
        job.started_at = None
        job.finished_at = None
        job.error = message

        run.status = "failed"
        run.message = message
        run.finished_at = now

        account = db.get(Account, job.account_id) if job.account_id else None
        if account:
            account.last_attempt_at = now

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
            if (
                scan
                and isinstance(exc, TransientRelationshipError)
                and not login_required
                and job.attempts < self.settings.relationship_max_attempts
            ):
                job.status = "queued"
                job.not_before = next_relationship_retry(self.settings, job.attempts)
                job.started_at = None
                job.finished_at = None
                scan.status = "running"
                scan.completed_at = None
                return
            if scan:
                scan.status = "failed"
                scan.completed_at = now
                self.notifications.queue_relationship_failure(
                    db,
                    account,
                    scan_id=scan.id,
                    relationship_type=scan.relationship_type,
                    scan_date=scan.scan_date,
                    collected_count=scan.collected_count,
                    reason=message,
                )
            if not login_required:
                return
        if job.kind == "content_refresh" and not login_required:
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
