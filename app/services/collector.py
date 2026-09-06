from __future__ import annotations

import hashlib
import json
import math
import random
import re
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    Response,
    sync_playwright,
)
from playwright.sync_api import (
    Error as PlaywrightError,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from app.config import Settings
from app.services.content_text import clean_content_text

COUNT_RE = re.compile(r"([\d,.]+)\s*([萬万千KkMm]?)")
POST_ID_RE = re.compile(r"/post/([^/?#]+)")
ACTIVE_RELATIONSHIP_DIALOG_SELECTOR = (
    '[role="dialog"]:visible, [aria-modal="true"]:visible'
)
RATE_LIMIT_RESOURCE_TYPES = frozenset({"document", "xhr", "fetch"})
META_UI_HOST_SUFFIXES = ("threads.com", "threads.net", "instagram.com", "facebook.com")
RELATIONSHIP_SCROLL_MIN_DELAY_MS = 750
RELATIONSHIP_SCROLL_MAX_DELAY_MS = 1_250
RELATIONSHIP_END_STABLE_MS = 8_000
RELATIONSHIP_NON_SCROLLABLE_END_STABLE_MS = 20_000
RELATIONSHIP_MAX_SCROLL_TURNS = 160


class CollectionError(RuntimeError):
    pass


class LoginRequired(CollectionError):
    pass


class RestrictedPage(CollectionError):
    pass


class RateLimited(RestrictedPage):
    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: int | None = None,
        source_url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.source_url = source_url


class TransientRelationshipError(CollectionError):
    """A temporary relationship-list failure that may be retried after backoff."""


@dataclass(slots=True)
class ProfileData:
    username: str
    display_name: str | None
    bio: str | None
    external_url: str | None
    avatar_url: str | None
    follower_count: int | None
    following_count: int | None


@dataclass(slots=True)
class ContentData:
    threads_id: str
    author_username: str
    content_type: str
    source_url: str
    text: str | None
    published_at: datetime | None
    media: list[tuple[str, str]] = field(default_factory=list)
    like_count: int | None = None
    reply_count: int | None = None
    repost_count: int | None = None
    share_count: int | None = None
    reply_to_threads_id: str | None = None
    quoted_threads_id: str | None = None


@dataclass(slots=True)
class RelationshipMemberData:
    username: str
    display_name: str | None
    avatar_url: str | None


@dataclass(slots=True)
class RelationshipBatch:
    members: list[RelationshipMemberData]
    cursor: str | None
    complete: bool
    follower_count: int | None = None
    following_count: int | None = None
    observed_usernames: set[str] | None = None


def parse_count(value: str | None) -> int | None:
    if not value:
        return None
    match = COUNT_RE.search(value.replace(" ", ""))
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    suffix = match.group(2).lower()
    multiplier = {"k": 1_000, "m": 1_000_000, "千": 1_000, "萬": 10_000, "万": 10_000}.get(
        suffix, 1
    )
    return int(number * multiplier)


def parse_labeled_count(
    primary: str | None, fallback_text: str | None, label_pattern: str
) -> int | None:
    primary_count = parse_count(primary)
    if primary_count is not None:
        return primary_count
    for line in (fallback_text or "").splitlines():
        if re.search(label_pattern, line, re.I):
            count = parse_count(line)
            if count is not None:
                return count
    return None


def parse_retry_after(
    value: str | None, *, now: datetime | None = None
) -> int | None:
    """Parse an HTTP Retry-After delta or date into a non-negative delay."""
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.isdecimal():
        return int(raw)
    try:
        retry_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at is None:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return max(0, math.ceil((retry_at - reference).total_seconds()))


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


class ThreadsCollector:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright = None
        self._context: BrowserContext | None = None
        self._rate_limit_url: str | None = None
        self._rate_limit_retry_after_seconds: int | None = None

    def __enter__(self) -> ThreadsCollector:
        self._rate_limit_url = None
        self._rate_limit_retry_after_seconds = None
        self._playwright = sync_playwright().start()
        executable = Path(self.settings.chromium_executable)
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.settings.browser_profile_dir),
                executable_path=str(executable) if executable.exists() else None,
                headless=True,
                locale="zh-TW",
                timezone_id=self.settings.timezone,
                viewport={"width": 1280, "height": 1200},
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            self._context.on("response", self._record_rate_limit_response)
        except BaseException:
            self._playwright.stop()
            self._playwright = None
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        if self._context:
            self._context.close()
        if self._playwright:
            self._playwright.stop()

    def _page(self, url: str) -> Page:
        if not self._context:
            raise RuntimeError("Collector context is not open")
        self._raise_if_rate_limited()
        page = self._context.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            if response is not None:
                self._record_rate_limit_response(response)
            self._raise_if_rate_limited()
            page.wait_for_timeout(1500)
            self._raise_if_rate_limited()
            current = page.url.lower()
            text = page.locator("body").inner_text(timeout=15_000).lower()
            self._raise_if_rate_limited()
            if "/login" in current or "登入以查看更多" in text or "log in to see" in text:
                raise LoginRequired("Threads 登入工作階段已失效")
            rate_limit_markers = [
                "請稍後再試",
                "try again later",
                "too many requests",
                "rate limit",
                "please wait a few minutes",
            ]
            if len(text) <= 2_000 and any(marker in text for marker in rate_limit_markers):
                raise RateLimited(
                    "Threads 顯示要求稍後再試，已停止本次擷取",
                    source_url=page.url,
                )
            restriction_markers = ["challenge", "captcha"]
            if any(marker in text for marker in restriction_markers):
                raise RestrictedPage("Threads 顯示限制或驗證頁")
            return page
        except BaseException:
            try:
                self._raise_if_rate_limited()
            finally:
                with suppress(Exception):
                    page.close()
            raise

    @staticmethod
    def _is_meta_ui_host(url: str) -> bool:
        try:
            hostname = (urlparse(url).hostname or "").casefold()
        except ValueError:
            return False
        return any(
            hostname == suffix or hostname.endswith(f".{suffix}")
            for suffix in META_UI_HOST_SUFFIXES
        )

    def _record_rate_limit_response(self, response: Response) -> None:
        """Remember relevant 429 details without raising inside an event callback."""
        try:
            if response.status != 429:
                return
            if response.request.resource_type.casefold() not in RATE_LIMIT_RESOURCE_TYPES:
                return
            if not self._is_meta_ui_host(response.url):
                return
        except (AttributeError, TypeError, ValueError):
            return
        if self._rate_limit_url is None:
            self._rate_limit_url = response.url
        retry_after = None
        with suppress(AttributeError, TypeError, ValueError):
            retry_after_header = next(
                (
                    value
                    for name, value in response.headers.items()
                    if name.casefold() == "retry-after"
                ),
                None,
            )
            retry_after = parse_retry_after(retry_after_header)
        if retry_after is not None:
            self._rate_limit_retry_after_seconds = max(
                retry_after,
                self._rate_limit_retry_after_seconds or 0,
            )

    def _raise_if_rate_limited(self) -> None:
        if self._rate_limit_url is None:
            return
        raise RateLimited(
            "Threads 回應 HTTP 429，已停止本次擷取",
            retry_after_seconds=self._rate_limit_retry_after_seconds,
            source_url=self._rate_limit_url,
        )

    def collect_profile(self, username: str) -> ProfileData:
        page = self._page(f"https://www.threads.com/@{username}")
        try:
            raw: dict[str, Any] = page.evaluate(
                r"""(username) => {
                    const body = document.body.innerText || '';
                    const headings = [...document.querySelectorAll('h1')]
                      .map(e => e.textContent?.trim()).filter(Boolean);
                    const matchingAvatar = [...document.querySelectorAll('img')].find(img =>
                      (img.alt || '').toLowerCase().includes(username.toLowerCase()) &&
                      ((img.alt || '').includes('大頭貼') || (img.alt || '').toLowerCase().includes('profile'))
                    );
                    const links = [...document.querySelectorAll('a')];
                    const ogImage = document.querySelector('meta[property="og:image"]')?.content;
                    const ogTitle = document.querySelector('meta[property="og:title"]')?.content;
                    const ogDescription = document.querySelector(
                      'meta[property="og:description"]'
                    )?.content;
                    const avatar = matchingAvatar || [...document.querySelectorAll('img')].find(img =>
                      (img.alt || '').toLowerCase().includes(username.toLowerCase())
                    );
                    const identityUrls = [
                      document.querySelector('link[rel="canonical"]')?.href,
                      document.querySelector('meta[property="og:url"]')?.content,
                      ...links.map(link => link.href)
                    ].filter(Boolean);
                    const profileAnchorUrls = links.map(link => link.href).filter(Boolean);
                    const controls = [...document.querySelectorAll('a,button,[role="button"]')]
                      .map(el => [el.getAttribute('aria-label') || '', el.textContent || '']
                        .join(' ').replace(/\s+/g, ' ').trim())
                      .filter(text => text.length < 240);
                    const follower = controls.find(text => /粉絲|followers?/i.test(text));
                    const following = controls.find(text => /追蹤中|following/i.test(text));
                    const external = links.find(a => {
                      try { return new URL(a.href).hostname !== 'www.threads.com' &&
                        !new URL(a.href).hostname.endsWith('threads.com'); } catch { return false; }
                    });
                    const profileRoot = avatar?.closest('div')?.parentElement?.parentElement;
                    return {
                      body,
                      headings,
                      profilePaths: identityUrls.map(value => {
                        try { return new URL(value, location.origin).pathname; }
                        catch { return ''; }
                      }).filter(Boolean),
                      profileAnchorPaths: profileAnchorUrls.map(value => {
                        try { return new URL(value, location.origin).pathname; }
                        catch { return ''; }
                      }).filter(Boolean),
                      avatarUrl: avatar?.currentSrc || avatar?.src || ogImage || null,
                      ogTitle: ogTitle || null,
                      ogDescription: ogDescription || null,
                      followerText: follower || null,
                      followingText: following || null,
                      externalUrl: external?.href || null,
                      profileText: profileRoot?.innerText || ''
                    };
                }""",
                username,
            )
            self._raise_if_rate_limited()
            body = raw.get("body", "")
            profile_source = f"{body}\n{raw.get('ogDescription') or ''}"
            if "找不到此頁面" in body or "page isn't available" in body.lower():
                raise CollectionError("帳號不存在或無法公開存取")
            if "這是私人帳號" in body or "this profile is private" in body.lower():
                raise CollectionError("此帳號為私人帳號，不在監看範圍")
            if not self._profile_matches_username(raw, username):
                raise CollectionError("Threads 回應未包含指定帳號的個人檔案身分")
            headings = [
                item for item in raw.get("headings", []) if item.lower() != username.lower()
            ]
            display_name = self._profile_display_name(raw, username, headings)
            profile_lines = [
                line.strip() for line in raw.get("profileText", "").splitlines() if line.strip()
            ]
            excluded = {username.lower(), *(h.lower() for h in headings)}
            bio_lines = [
                line
                for line in profile_lines
                if line.lower() not in excluded
                and not re.search(r"粉絲|followers?|追蹤|following", line, re.I)
            ]
            body = profile_source
            return ProfileData(
                username=username,
                display_name=display_name,
                bio="\n".join(bio_lines[:4]) or None,
                external_url=raw.get("externalUrl"),
                avatar_url=raw.get("avatarUrl"),
                follower_count=parse_labeled_count(
                    raw.get("followerText"), body, r"粉絲|followers?"
                ),
                following_count=parse_labeled_count(
                    raw.get("followingText"), body, r"追蹤中|following"
                ),
            )
        finally:
            page.close()

    @staticmethod
    def _profile_matches_username(raw: dict[str, Any], username: str) -> bool:
        expected = f"/@{username}".casefold().rstrip("/")
        has_identity_path = any(
            str(path).casefold().rstrip("/") == expected
            for path in raw.get("profilePaths", [])
        )
        has_profile_control = bool(raw.get("followerText")) or any(
            str(path).casefold().rstrip("/") == expected
            for path in raw.get("profileAnchorPaths", [])
        )
        return (
            has_identity_path
            and has_profile_control
            and username.casefold() in str(raw.get("body", "")).casefold()
        )

    @staticmethod
    def _profile_display_name(
        raw: dict[str, Any], username: str, headings: list[str]
    ) -> str | None:
        if headings:
            return headings[0]
        title = str(raw.get("ogTitle") or "").strip()
        match = re.match(rf"^(.*?)\s*\(@{re.escape(username)}\)", title, re.I)
        if match and match.group(1).strip().casefold() != username.casefold():
            return match.group(1).strip()
        return None

    def collect_content(
        self,
        username: str,
        content_type: str,
        limit: int = 10,
        cursor: str | None = None,
    ) -> list[ContentData]:
        suffix = {"post": "", "reply": "/replies", "repost": "/reposts", "quote": "/reposts"}[
            content_type
        ]
        page = self._page(f"https://www.threads.com/@{username}{suffix}")
        try:
            for _ in range(8):
                self._raise_if_rate_limited()
                page.mouse.wheel(0, 900)
                page.wait_for_timeout(800)
                self._raise_if_rate_limited()
                if cursor and page.locator(f'a[href*="/post/{cursor}"]').count():
                    break
            return self._content_items_from_page(
                page,
                username,
                content_type,
                limit,
                cursor=cursor,
            )
        finally:
            page.close()

    def collect_content_url(
        self,
        source_url: str,
        username: str,
        content_type: str,
    ) -> ContentData:
        post_match = POST_ID_RE.search(source_url)
        if not post_match:
            raise CollectionError("舊貼文網址格式無效")
        target_id = post_match.group(1)
        page = self._page(source_url)
        try:
            page.wait_for_timeout(1200)
            self._raise_if_rate_limited()
            items = self._content_items_from_page(
                page,
                username,
                content_type,
                1,
                target_id=target_id,
            )
            if not items:
                raise CollectionError("Threads 舊貼文目前無法存取或尚未載入")
            return items[0]
        finally:
            page.close()

    def _content_items_from_page(
        self,
        page: Page,
        username: str,
        content_type: str,
        limit: int,
        *,
        cursor: str | None = None,
        target_id: str | None = None,
    ) -> list[ContentData]:
        raw_items: list[dict[str, Any]] = page.evaluate(
            r"""({username, limit, targetId}) => {
                  const results = [];
                  const seen = new Set();
                  const anchors = [...document.querySelectorAll('a[href*="/post/"]')];
                  for (const anchor of anchors) {
                    const href = anchor.href;
                    const id = (href.match(/\/post\/([^/?#]+)/) || [])[1];
                    if (!id || seen.has(id) || (targetId && id !== targetId)) continue;
                    let root = anchor;
                    for (let i = 0; i < 7 && root.parentElement; i++) {
                      root = root.parentElement;
                      if (
                        root.querySelectorAll('button,[role="button"]').length >= 3 &&
                        root.innerText.length > 10
                      ) break;
                    }
                    const textRoot = root.cloneNode(true);
                    textRoot.querySelectorAll('button,time,a[aria-label]')
                      .forEach(el => el.remove());
                    const text = textRoot.innerText || textRoot.textContent || '';
                    const authorLink = root.querySelector('a[href^="/@"]');
                    const time = root.querySelector('time');
                    const media = [...root.querySelectorAll('img,video')].map(el => ({
                      url: el.tagName === 'VIDEO' ? (el.currentSrc || el.src) : (el.currentSrc || el.src),
                      type: el.tagName === 'VIDEO' ? 'video' : 'image',
                      alt: el.alt || ''
                    })).filter(m => m.url && !/profile|大頭貼/i.test(m.alt));
                    const buttons = [...root.querySelectorAll(
                      'button,a[aria-label],[role="button"]'
                    )].map(control => {
                      const nestedLabel = control.querySelector('[aria-label]')
                        ?.getAttribute('aria-label') || '';
                      const siblingText = [
                        control.previousElementSibling?.innerText || '',
                        control.nextElementSibling?.innerText || ''
                      ].join(' ').trim();
                      const parentText = control.parentElement?.innerText || '';
                      return {
                        text: control.innerText || '',
                        label: [
                          control.getAttribute('aria-label') || '',
                          control.getAttribute('title') || '',
                          nestedLabel
                        ].join(' ').trim(),
                        nearby: siblingText || (parentText.length < 80 ? parentText : '')
                      };
                    });
                    const postIds = [...root.querySelectorAll('a[href*="/post/"]')]
                      .map(a => ((a.href.match(/\/post\/([^/?#]+)/) || [])[1])).filter(Boolean);
                    seen.add(id);
                    results.push({id, href, text, authorHref: authorLink?.getAttribute('href') || '',
                      datetime: time?.getAttribute('datetime') || null, media, buttons, postIds});
                    if (results.length >= limit * 4) break;
                  }
                  return results;
                }""",
            {"username": username, "limit": limit, "targetId": target_id},
        )
        self._raise_if_rate_limited()
        if cursor:
            cursor_index = next(
                (index for index, raw in enumerate(raw_items) if raw.get("id") == cursor), -1
            )
            raw_items = raw_items[cursor_index + 1 :] if cursor_index >= 0 else []
        items: list[ContentData] = []
        for raw in raw_items:
            post_match = POST_ID_RE.search(raw.get("href", ""))
            if not post_match:
                continue
            author = raw.get("authorHref", "").split("/@")[-1].split("/")[0] or username
            post_ids = set(raw.get("postIds", []))
            if content_type in {"repost", "quote"}:
                actual_type = (
                    "quote"
                    if author.lower() == username.lower() and len(post_ids) > 1
                    else "repost"
                )
                if actual_type != content_type:
                    continue
            else:
                actual_type = content_type
            counts = self._button_counts(raw.get("buttons", []), raw.get("text", ""))
            cleaned_text = self._clean_content_text(raw.get("text", ""), author)
            media = [(m["url"], m["type"]) for m in raw.get("media", []) if m.get("url")]
            items.append(
                ContentData(
                    threads_id=post_match.group(1),
                    author_username=author,
                    content_type=actual_type,
                    source_url=raw["href"],
                    text=cleaned_text,
                    published_at=_parse_datetime(raw.get("datetime")),
                    media=media,
                    **counts,
                )
            )
            if len(items) >= limit:
                break
        return items

    def collect_relationships(
        self,
        username: str,
        relationship_type: str,
        limit: int = 25,
        cursor: str | None = None,
        expected_count: int | None = None,
        seen_usernames: set[str] | None = None,
    ) -> RelationshipBatch:
        if relationship_type not in {"followers", "following"}:
            raise CollectionError(f"未知關係類型：{relationship_type}")
        page = self._page(f"https://www.threads.com/@{username}")
        try:
            opened = self._click_when_available(
                page,
                r"""() => {
                  const controls = [...document.querySelectorAll(
                    'a,button,[role="button"],[role="tab"]'
                  )];
                  const target = controls.find(el => {
                    const text = [el.getAttribute('aria-label') || '', el.textContent || '']
                      .join(' ').replace(/\s+/g, ' ').trim();
                    return text.length < 240 && /粉絲|followers?/i.test(text);
                  });
                  if (!target) return false;
                  target.click();
                  return true;
                }""",
            )
            if opened and relationship_type == "following":
                page.wait_for_timeout(800)
                self._raise_if_rate_limited()
                opened = self._click_when_available(
                    page,
                    r"""() => {
                      const dialog = [...document.querySelectorAll(
                        '[role="dialog"],[aria-modal="true"]'
                      )].find(element => {
                        const bounds = element.getBoundingClientRect();
                        const style = getComputedStyle(element);
                        return bounds.width > 0 && bounds.height > 0 &&
                          style.display !== 'none' && style.visibility !== 'hidden' &&
                          /粉絲|followers?|追蹤中|following/i.test(element.innerText || '');
                      });
                      if (!dialog) return false;
                      const controls = [...dialog.querySelectorAll(
                        'a,button,[role="button"],[role="tab"]'
                      )];
                      const target = controls.find(el => /追蹤中|following/i.test(
                        [el.getAttribute('aria-label') || '', el.textContent || ''].join(' ')
                      ));
                      if (!target) return false;
                      target.click();
                      return true;
                    }""",
                )
            if not opened:
                self._save_relationship_diagnostic(page, username, relationship_type)
                raise TransientRelationshipError(
                    "Threads 目前未提供可存取的粉絲／追蹤中清單控制項"
                )
            page.wait_for_timeout(1000)
            self._raise_if_rate_limited()
            dialog = self._active_relationship_dialog(page)
            try:
                dialog.wait_for(state="visible", timeout=10_000)
                self._raise_if_rate_limited()
            except PlaywrightTimeoutError as exc:
                self._raise_if_rate_limited()
                self._save_relationship_diagnostic(page, username, relationship_type)
                raise TransientRelationshipError(
                    self._relationship_timeout_message(relationship_type)
                ) from exc
            relationship_counts: dict[str, str | None] = dialog.evaluate(
                r"""dialog => {
                  const labels = [...dialog.querySelectorAll('a,button,[role="button"]')]
                    .map(el =>
                      [el.getAttribute('aria-label') || '', el.textContent || '']
                        .join(' ').replace(/\s+/g, ' ').trim()
                    );
                  return {
                    followers: labels.find(text => /粉絲|followers?/i.test(text)) || null,
                    following: labels.find(text => /追蹤中|following/i.test(text)) || null
                  };
                }"""
            )
            self._raise_if_rate_limited()
            effective_expected_count = self._effective_relationship_count(
                relationship_type, relationship_counts, expected_count
            )
            try:
                self._wait_for_relationship_rows(page, effective_expected_count)
            except PlaywrightTimeoutError as exc:
                self._save_relationship_diagnostic(page, username, relationship_type)
                raise TransientRelationshipError(
                    self._relationship_timeout_message(relationship_type)
                ) from exc
            try:
                raw = self._scan_relationship_dialog(
                    page,
                    dialog,
                    owner=username,
                    limit=limit,
                    cursor=cursor,
                    expected_count=effective_expected_count,
                    seen_usernames=seen_usernames or set(),
                )
            except PlaywrightError as exc:
                self._raise_if_rate_limited()
                self._save_relationship_diagnostic(page, username, relationship_type)
                raise TransientRelationshipError(
                    "Threads 關係名單掃描中斷，已保留目前進度"
                ) from exc
            self._raise_if_rate_limited()
            if not raw.get("available", True):
                raise CollectionError("Threads 名單視窗未成功開啟")
            if not raw.get("members"):
                self._save_relationship_diagnostic(page, username, relationship_type)
            members = [
                RelationshipMemberData(
                    username=item["username"],
                    display_name=item.get("displayName"),
                    avatar_url=item.get("avatarUrl"),
                )
                for item in raw.get("members", [])
                if item.get("username")
            ]
            complete = self._relationship_batch_complete(raw)
            return RelationshipBatch(
                members=members,
                cursor=members[-1].username if members else cursor,
                complete=complete,
                follower_count=parse_count(relationship_counts.get("followers")),
                following_count=parse_count(relationship_counts.get("following")),
                observed_usernames=(
                    {
                        str(value).strip().lstrip("@").casefold()
                        for value in raw.get("observedUsernames", [])
                        if str(value).strip().lstrip("@")
                    }
                    if complete and "observedUsernames" in raw
                    else None
                ),
            )
        finally:
            page.close()

    def _scan_relationship_dialog(
        self,
        page: Page,
        dialog: Locator,
        *,
        owner: str,
        limit: int,
        cursor: str | None,
        expected_count: int | None,
        seen_usernames: set[str],
    ) -> dict[str, Any]:
        """Read one relationship batch while yielding to Python between scrolls.

        Threads virtualizes the dialog, so each snapshot can contain different rows.
        Keeping the loop in Python lets response callbacks surface a 429 before the
        next scroll and lets us use a conservative, jittered interaction cadence.
        """
        snapshot_script = r"""(dialog, {owner}) => {
          const avatarUrlFrom = root => {
            const image = root.querySelector('img[src],img[srcset]');
            if (image?.currentSrc || image?.src) return image.currentSrc || image.src;
            const svgImage = root.querySelector('image[href]');
            const svgHref = svgImage?.href?.baseVal || svgImage?.getAttribute('href');
            if (svgHref) return svgHref;
            for (const element of [root, ...root.querySelectorAll('*')]) {
              const background = getComputedStyle(element).backgroundImage || '';
              const match = background.match(/^url\(["']?(.*?)["']?\)$/);
              if (match?.[1] && !match[1].startsWith('data:')) return match[1];
            }
            return null;
          };
          const members = [];
          const visibleUsernames = new Set();
          for (const anchor of dialog.querySelectorAll('a[href*="/@"]')) {
            let pathname = '';
            try {
              pathname = new URL(
                anchor.getAttribute('href') || anchor.href || '', location.origin
              ).pathname;
            } catch (_error) {
              continue;
            }
            const match = pathname.match(/^\/@([^/?#]+)/);
            if (!match) continue;
            const memberUsername = decodeURIComponent(match[1]).toLowerCase();
            if (memberUsername === owner.toLowerCase() || visibleUsernames.has(memberUsername)) {
              continue;
            }
            let item = anchor;
            for (let level = 0; level < 10 && item.parentElement; level++) {
              item = item.parentElement;
              if (avatarUrlFrom(item) && item.innerText.trim().length > 0) break;
            }
            const lines = (item.innerText || '')
              .split('\n').map(value => value.trim()).filter(Boolean);
            const displayName = lines.find(line =>
              line.toLowerCase() !== memberUsername &&
              line.toLowerCase() !== `@${memberUsername}` &&
              !/追蹤|follow/i.test(line)
            ) || null;
            visibleUsernames.add(memberUsername);
            members.push({
              username: memberUsername,
              displayName,
              avatarUrl: avatarUrlFrom(item)
            });
          }

          const rateLimitPattern = /請稍後再試|請稍候再試|稍後再試|try again later|too many requests|please wait a few minutes|rate[ -]?limit/i;
          const noticeText = [...document.querySelectorAll(
            '[role="alert"],[role="status"],[role="dialog"],[aria-modal="true"]'
          )]
            .filter(element => {
              const bounds = element.getBoundingClientRect();
              const style = getComputedStyle(element);
              return bounds.width > 0 && bounds.height > 0 &&
                style.display !== 'none' && style.visibility !== 'hidden';
            })
            .map(element => element.innerText || element.textContent || '')
            .join(' ');
          const pageText = document.body?.innerText || '';
          const rateLimited = rateLimitPattern.test(noticeText) ||
            (members.length === 0 && rateLimitPattern.test(pageText));

          const candidates = [dialog, ...dialog.querySelectorAll('*')]
            .filter(element => {
              const bounds = element.getBoundingClientRect();
              const style = getComputedStyle(element);
              return bounds.width > 0 && bounds.height > 0 &&
                style.display !== 'none' && style.visibility !== 'hidden' &&
                element.scrollHeight > element.clientHeight + 40;
            })
            .sort((a, b) =>
              (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight)
            );
          const scrollable = candidates.length > 0;
          const scroller = candidates[0] || dialog;
          const beforeTop = Number(scroller.scrollTop || 0);
          const clientHeight = Number(scroller.clientHeight || 0);
          const scrollHeight = Number(scroller.scrollHeight || 0);
          const beforeEnd = beforeTop + clientHeight >= scrollHeight - 8;
          if (!beforeEnd && scrollable) {
            scroller.scrollTop = Math.min(
              beforeTop + Math.max(clientHeight * 0.8, 320),
              scrollHeight
            );
            scroller.dispatchEvent(new Event('scroll', {bubbles: true}));
          }
          const scrollTop = Number(scroller.scrollTop || 0);
          return {
            members,
            rateLimited,
            atEnd: scrollTop + clientHeight >= scrollHeight - 8,
            scrollable,
            scrollTop,
            scrollHeight,
            clientHeight,
            moved: scrollTop > beforeTop + 1
          };
        }"""

        owner_key = owner.casefold()
        cursor_key = cursor.casefold() if cursor else None
        previously_saved = {username.casefold() for username in seen_usernames}
        ordered: dict[str, dict[str, Any]] = {}
        cursor_found = cursor_key is None or cursor_key in previously_saved
        last_signature: tuple[int, int, int, int] | None = None
        last_snapshot: dict[str, Any] = {}
        stagnant = 0
        stable_end_ms = 0

        for turn in range(RELATIONSHIP_MAX_SCROLL_TURNS):
            self._raise_if_rate_limited()
            snapshot: dict[str, Any] = dialog.evaluate(
                snapshot_script,
                {"owner": owner},
            )
            self._raise_if_rate_limited()
            if snapshot.get("rateLimited"):
                raise RateLimited("Threads 顯示要求稍後再試，已停止本次擷取")
            last_snapshot = snapshot
            added = 0
            for item in snapshot.get("members", []):
                username = str(item.get("username") or "").strip().lstrip("@")
                username_key = username.casefold()
                if not username or username_key == owner_key:
                    continue
                if cursor_key is not None and username_key == cursor_key:
                    cursor_found = True
                existing = ordered.get(username_key)
                if existing is None:
                    ordered[username_key] = {
                        "username": username,
                        "displayName": item.get("displayName"),
                        "avatarUrl": item.get("avatarUrl"),
                    }
                    added += 1
                else:
                    existing["displayName"] = (
                        item.get("displayName") or existing.get("displayName")
                    )
                    existing["avatarUrl"] = (
                        item.get("avatarUrl") or existing.get("avatarUrl")
                    )

            members = [
                member
                for username_key, member in ordered.items()
                if username_key not in previously_saved
            ]
            common = {
                "cursorFound": cursor_found,
                "available": True,
                "atEnd": bool(snapshot.get("atEnd")),
                "orderedCount": len(ordered),
            }
            # When the count is exactly the persistence limit, keep observing long
            # enough to prove the accessible end.  Only an extra member proves that
            # another checkpoint job is required.
            if len(members) > limit:
                return {
                    **common,
                    "members": members[:limit],
                    "complete": False,
                    "stagnant": stagnant,
                    "terminationReason": "batch_limit",
                }

            signature = (
                len(ordered),
                round(float(snapshot.get("scrollTop") or 0)),
                round(float(snapshot.get("scrollHeight") or 0)),
                round(float(snapshot.get("clientHeight") or 0)),
            )
            at_end = bool(snapshot.get("atEnd"))
            stable_candidate = at_end and added == 0 and signature == last_signature
            if stable_candidate:
                stagnant += 1
            else:
                stagnant = 0
                stable_end_ms = 0
            last_signature = signature

            delay_ms = random.randint(
                RELATIONSHIP_SCROLL_MIN_DELAY_MS,
                RELATIONSHIP_SCROLL_MAX_DELAY_MS,
            )
            if turn < RELATIONSHIP_MAX_SCROLL_TURNS - 1:
                page.wait_for_timeout(delay_ms)
                self._raise_if_rate_limited()
                if stable_candidate:
                    stable_end_ms += delay_ms

            has_rows = bool(ordered)
            known_empty = expected_count == 0
            count_reached = (
                expected_count is not None and len(ordered) >= expected_count
            )
            stable_requirement_ms = (
                RELATIONSHIP_END_STABLE_MS
                if bool(snapshot.get("scrollable", True)) or count_reached
                else RELATIONSHIP_NON_SCROLLABLE_END_STABLE_MS
            )
            if (
                stable_end_ms >= stable_requirement_ms
                and (has_rows or known_empty)
            ):
                return {
                    **common,
                    "members": members[:limit],
                    "complete": True,
                    "observedUsernames": [
                        member["username"] for member in ordered.values()
                    ],
                    "stagnant": stagnant,
                    "stableEndMs": stable_end_ms,
                    "terminationReason": "accessible_end",
                }

        return {
            "members": members[:limit],
            "complete": False,
            "cursorFound": cursor_found,
            "available": True,
            "atEnd": bool(last_snapshot.get("atEnd")),
            "stagnant": stagnant,
            "stableEndMs": stable_end_ms,
            "orderedCount": len(ordered),
            "terminationReason": "turn_limit",
        }

    @staticmethod
    def _relationship_batch_complete(raw: dict[str, Any]) -> bool:
        return bool(raw.get("complete"))

    def _click_when_available(self, page: Page, script: str, attempts: int = 60) -> bool:
        for attempt in range(attempts):
            self._raise_if_rate_limited()
            if page.evaluate(script):
                self._raise_if_rate_limited()
                return True
            self._raise_if_rate_limited()
            if attempt < attempts - 1:
                page.wait_for_timeout(500)
                self._raise_if_rate_limited()
        return False

    @staticmethod
    def _active_relationship_dialog(page: Page) -> Locator:
        return page.locator(ACTIVE_RELATIONSHIP_DIALOG_SELECTOR).filter(
            has_text=re.compile(r"粉絲|followers?|追蹤中|following", re.I)
        ).first

    @staticmethod
    def _relationship_timeout_message(relationship_type: str) -> str:
        label = "粉絲" if relationship_type == "followers" else "追蹤中"
        return f"Threads {label}清單載入逾時，未取得任何成員"

    @staticmethod
    def _effective_relationship_count(
        relationship_type: str,
        relationship_counts: dict[str, str | None],
        fallback: int | None,
    ) -> int | None:
        current = parse_count(relationship_counts.get(relationship_type))
        return current if current is not None else fallback

    def _wait_for_relationship_rows(
        self, page: Page, expected_count: int | None
    ) -> None:
        if expected_count is not None and expected_count <= 0:
            return
        rows = page.locator(
            '[role="dialog"]:visible a[href*="/@"], '
            '[aria-modal="true"]:visible a[href*="/@"]'
        ).first
        for attempt, timeout in enumerate((30_000, 20_000)):
            self._raise_if_rate_limited()
            try:
                rows.wait_for(state="attached", timeout=timeout)
                self._raise_if_rate_limited()
                return
            except PlaywrightTimeoutError:
                self._raise_if_rate_limited()
                if attempt == 1:
                    raise
                # Threads sometimes paints an empty dialog shell before its rows.
                # A passive pause and second observation adds no click or page reload.
                page.wait_for_timeout(2_500)
                self._raise_if_rate_limited()

    def _save_relationship_diagnostic(
        self, page: Page, username: str, relationship_type: str
    ) -> None:
        """Keep one bounded diagnostic artifact when Threads renders no member rows."""
        safe_username = re.sub(r"[^a-zA-Z0-9._-]", "_", username)
        debug_dir = self.settings.media_root.parent / "debug"
        json_path = debug_dir / f"relationship-{safe_username}-{relationship_type}-latest.json"
        png_path = debug_dir / f"relationship-{safe_username}-{relationship_type}-latest.png"
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
            snapshot = page.evaluate(
                r"""() => {
                  const dialog = [...document.querySelectorAll(
                    '[role="dialog"],[aria-modal="true"]'
                  )].find(element => {
                    const bounds = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return bounds.width > 0 && bounds.height > 0 &&
                      style.display !== 'none' && style.visibility !== 'hidden' &&
                      /粉絲|followers?|追蹤中|following/i.test(element.innerText || '');
                  });
                  const describe = el => ({
                    tag: el.tagName,
                    role: el.getAttribute('role'),
                    href: el.getAttribute('href'),
                    ariaLabel: el.getAttribute('aria-label'),
                    text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()
                      .slice(0, 500)
                  });
                  return {
                    url: location.href,
                    title: document.title,
                    dialogHtml: dialog?.outerHTML?.slice(0, 2_000_000) || null,
                    dialogLinks: dialog
                      ? [...dialog.querySelectorAll('a[href]')].slice(0, 200).map(describe)
                      : [],
                    dialogControls: dialog
                      ? [...dialog.querySelectorAll('button,[role="button"]')]
                        .slice(0, 200).map(describe)
                      : [],
                    dialogImages: dialog
                      ? [...dialog.querySelectorAll('img,image')].slice(0, 200).map(el => ({
                        tag: el.tagName,
                        src: el.currentSrc || el.src || el.getAttribute('href'),
                        alt: el.getAttribute('alt'),
                        ariaLabel: el.getAttribute('aria-label')
                      }))
                      : []
                  };
                }"""
            )
            snapshot["capturedAt"] = datetime.now(UTC).isoformat(timespec="seconds")
            snapshot["username"] = username
            snapshot["relationshipType"] = relationship_type
            json_path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            page.screenshot(path=str(png_path), full_page=True)
        except Exception:
            # Diagnostics must never replace the original collection result.
            return

    @staticmethod
    def _clean_content_text(value: str, author: str) -> str | None:
        return clean_content_text(value, author)

    @staticmethod
    def _button_counts(
        buttons: list[dict[str, str] | str], fallback_text: str | None = None
    ) -> dict[str, int | None]:
        result = {
            "like_count": None,
            "reply_count": None,
            "repost_count": None,
            "share_count": None,
        }
        mapping = {
            "like_count": re.compile(r"讚|like", re.I),
            "reply_count": re.compile(r"留言|回覆|repl(?:y|ies)?", re.I),
            "repost_count": re.compile(r"轉發|reposts?", re.I),
            "share_count": re.compile(r"分享|shares?", re.I),
        }
        for button in buttons:
            if isinstance(button, dict):
                text = button.get("text", "")
                label = button.get("label", "")
                nearby = button.get("nearby", "")
                descriptor = f"{label} {text}".strip()
            else:
                text = button
                label = ""
                nearby = ""
                descriptor = button
            for key, pattern in mapping.items():
                if pattern.search(descriptor):
                    for candidate in (text, label, nearby, descriptor):
                        count = parse_count(candidate)
                        if count is not None:
                            result[key] = count
                            break

        fallback_metric_labels = sum(
            bool(pattern.search(fallback_text or "")) for pattern in mapping.values()
        )
        if fallback_metric_labels < 2:
            return result

        count_token = r"[\d,.]+\s*[萬万千KkMm]?"
        for key, pattern in mapping.items():
            if result[key] is not None:
                continue
            label_pattern = pattern.pattern
            labeled_count = re.search(
                rf"(?:{label_pattern})\s*[:：]?\s*({count_token})",
                fallback_text or "",
                re.I,
            )
            count_labeled = re.search(
                rf"({count_token})\s*(?:{label_pattern})",
                fallback_text or "",
                re.I,
            )
            match = labeled_count or count_labeled
            if match:
                result[key] = parse_count(match.group(1))
        return result


def content_fingerprint(text: str | None, media_urls: list[str]) -> str:
    payload = json.dumps(
        {"text": text, "media": sorted(media_urls)}, ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(payload.encode()).hexdigest()
