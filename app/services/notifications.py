from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Account, NotificationOutbox
from app.services.collector import ContentData
from app.services.queue import now_utc

TelegramSender = Callable[[str, str, str, int], None]

logger = logging.getLogger("threads-monitor.notifications")


def _send_telegram(token: str, chat_id: str, body: str, timeout: int) -> None:
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": body[:4096],
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(str(result.get("description") or "Telegram API 回傳失敗"))


def _preview(text: str | None, limit: int = 180) -> str:
    cleaned = " ".join((text or "（無文字內容）").split())
    return cleaned if len(cleaned) <= limit else f"{cleaned[: limit - 1]}…"


def _mentions(usernames: Sequence[str], limit: int = 30) -> str:
    shown = [f"@{username}" for username in usernames[:limit]]
    if len(usernames) > limit:
        shown.append(f"另 {len(usernames) - limit} 人")
    return "、".join(shown)


class NotificationService:
    """Persist event notifications and deliver them independently from collection jobs."""

    def __init__(self, settings: Settings, *, sender: TelegramSender | None = None):
        self.settings = settings
        self.sender = sender or _send_telegram

    def queue_content_changes(
        self,
        db: Session,
        account: Account,
        stream_phase: str,
        content_type: str,
        items: Sequence[ContentData],
    ) -> None:
        if (
            not self.settings.telegram_notifications_enabled
            or stream_phase != "incremental"
            or content_type not in {"post", "reply", "quote", "repost"}
            or not items
        ):
            return
        digest = hashlib.sha256(
            "\n".join(sorted(item.threads_id for item in items)).encode("utf-8")
        ).hexdigest()[:24]
        icon, label = {
            "post": ("🧵", "新串文"),
            "reply": ("💬", "新回覆"),
            "quote": ("❝", "新引用"),
            "repost": ("🔁", "新轉發"),
        }[content_type]
        lines = [f"{icon} {label} · @{account.username}"]
        if len(items) > 1:
            lines.append(f"共 {len(items)} 則")
        for index, item in enumerate(items[:10], start=1):
            prefix = f"{index}. " if len(items) > 1 else ""
            lines.extend((f"{prefix}{_preview(item.text)}", item.source_url))
        if len(items) > 10:
            lines.append(f"另 {len(items) - 10} 則未列出")
        self._queue(
            db,
            event_key=f"content:{account.id}:{content_type}:{digest}",
            event_type=f"content_{content_type}",
            account=account,
            body="\n".join(lines)[:4096],
        )

    def queue_relationship_changes(
        self,
        db: Session,
        account: Account,
        *,
        scan_id: int,
        relationship_type: str,
        scan_date: date,
        added: Sequence[str],
        removed: Sequence[str],
    ) -> None:
        if (
            not self.settings.telegram_notifications_enabled
            or relationship_type not in {"followers", "following"}
            or not (added or removed)
        ):
            return
        label = "粉絲" if relationship_type == "followers" else "追蹤中"
        lines = [f"👥 {label}名單異動 · @{account.username}"]
        if added:
            lines.append(f"新增 {len(added)}：{_mentions(added)}")
        if removed:
            removed_label = "退出" if relationship_type == "followers" else "取消追蹤"
            lines.append(f"{removed_label} {len(removed)}：{_mentions(removed)}")
        lines.append(f"完整掃描日期：{scan_date.isoformat()}")
        self._queue(
            db,
            event_key=f"relationship-scan:{scan_id}",
            event_type=f"relationship_{relationship_type}",
            account=account,
            body="\n".join(lines)[:4096],
        )

    def queue_relationship_failure(
        self,
        db: Session,
        account: Account,
        *,
        scan_id: int,
        relationship_type: str,
        scan_date: date,
        collected_count: int,
        reason: str,
    ) -> None:
        if (
            not self.settings.telegram_notifications_enabled
            or relationship_type not in {"followers", "following"}
        ):
            return
        label = "粉絲" if relationship_type == "followers" else "追蹤中"
        body = "\n".join(
            (
                f"⚠️ {label}名單掃描失敗 · @{account.username}",
                f"掃描日期：{scan_date.isoformat()}",
                f"本輪已擷取：{collected_count} 人",
                f"原因：{_preview(reason, 500)}",
                "自動重試已結束，將等待下一輪排程。",
            )
        )
        self._queue(
            db,
            event_key=f"relationship-scan-failed:{scan_id}",
            event_type=f"relationship_{relationship_type}_failed",
            account=account,
            body=body[:4096],
        )

    def deliver_next(self, db: Session) -> bool:
        if not self.settings.telegram_notifications_enabled:
            return False
        row = db.scalar(
            select(NotificationOutbox)
            .where(
                NotificationOutbox.status == "pending",
                NotificationOutbox.not_before <= now_utc(),
            )
            .order_by(NotificationOutbox.not_before, NotificationOutbox.id)
            .limit(1)
        )
        if row is None:
            return False
        row.attempts += 1
        try:
            self.sender(
                self.settings.telegram_bot_token.strip(),
                self.settings.telegram_chat_id.strip(),
                row.body,
                self.settings.telegram_notification_timeout_seconds,
            )
        except Exception as exc:
            message = str(exc).replace(self.settings.telegram_bot_token.strip(), "[REDACTED]")
            row.last_error = message[:1000]
            if row.attempts >= self.settings.telegram_notification_max_attempts:
                row.status = "failed"
                logger.error("Telegram 通知 id=%s 已達重試上限：%s", row.id, row.last_error)
            else:
                delay = min(60 * (2 ** (row.attempts - 1)), 3600)
                row.not_before = now_utc() + timedelta(seconds=delay)
                logger.warning("Telegram 通知 id=%s 傳送失敗，稍後重試：%s", row.id, row.last_error)
            return False
        row.status = "sent"
        row.sent_at = now_utc()
        row.last_error = None
        return True

    @staticmethod
    def _queue(
        db: Session,
        *,
        event_key: str,
        event_type: str,
        account: Account,
        body: str,
    ) -> None:
        exists = db.scalar(
            select(NotificationOutbox.id).where(NotificationOutbox.event_key == event_key)
        )
        if exists is None:
            db.add(
                NotificationOutbox(
                    event_key=event_key,
                    event_type=event_type,
                    account_id=account.id,
                    body=body,
                )
            )
