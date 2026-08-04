from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, sync_playwright

from app.config import Settings

COUNT_RE = re.compile(r"([\d,.]+)\s*([萬万千KkMm]?)")
POST_ID_RE = re.compile(r"/post/([^/?#]+)")


class CollectionError(RuntimeError):
    pass


class LoginRequired(CollectionError):
    pass


class RestrictedPage(CollectionError):
    pass


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

    def __enter__(self) -> ThreadsCollector:
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
        page = self._context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1500)
        current = page.url.lower()
        text = page.locator("body").inner_text(timeout=15_000).lower()
        if "/login" in current or "登入以查看更多" in text or "log in to see" in text:
            page.close()
            raise LoginRequired("Threads 登入工作階段已失效")
        restriction_markers = ["請稍後再試", "try again later", "challenge", "captcha"]
        if any(marker in text for marker in restriction_markers):
            page.close()
            raise RestrictedPage("Threads 顯示限制或驗證頁")
        return page

    def collect_profile(self, username: str) -> ProfileData:
        page = self._page(f"https://www.threads.com/@{username}")
        try:
            raw: dict[str, Any] = page.evaluate(
                """(username) => {
                    const body = document.body.innerText || '';
                    const headings = [...document.querySelectorAll('h1')]
                      .map(e => e.textContent?.trim()).filter(Boolean);
                    const avatar = [...document.querySelectorAll('img')].find(img =>
                      (img.alt || '').toLowerCase().includes(username.toLowerCase()) &&
                      ((img.alt || '').includes('大頭貼') || (img.alt || '').toLowerCase().includes('profile'))
                    );
                    const links = [...document.querySelectorAll('a')];
                    const follower = links.find(a => /粉絲|followers?/i.test(a.textContent || ''));
                    const following = links.find(a => /追蹤中|following/i.test(a.textContent || ''));
                    const external = links.find(a => {
                      try { return new URL(a.href).hostname !== 'www.threads.com' &&
                        !new URL(a.href).hostname.endsWith('threads.com'); } catch { return false; }
                    });
                    const profileRoot = avatar?.closest('div')?.parentElement?.parentElement;
                    return {
                      body,
                      headings,
                      avatarUrl: avatar?.currentSrc || avatar?.src || null,
                      followerText: follower?.textContent || null,
                      followingText: following?.textContent || null,
                      externalUrl: external?.href || null,
                      profileText: profileRoot?.innerText || ''
                    };
                }""",
                username,
            )
            body = raw.get("body", "")
            if "找不到此頁面" in body or "page isn't available" in body.lower():
                raise CollectionError("帳號不存在或無法公開存取")
            if "這是私人帳號" in body or "this profile is private" in body.lower():
                raise CollectionError("此帳號為私人帳號，不在監看範圍")
            headings = [
                item for item in raw.get("headings", []) if item.lower() != username.lower()
            ]
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
            return ProfileData(
                username=username,
                display_name=headings[0] if headings else None,
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
                page.mouse.wheel(0, 900)
                page.wait_for_timeout(800)
                if cursor and page.locator(f'a[href*="/post/{cursor}"]').count():
                    break
            raw_items: list[dict[str, Any]] = page.evaluate(
                r"""({username, limit}) => {
                  const results = [];
                  const seen = new Set();
                  const anchors = [...document.querySelectorAll('a[href*="/post/"]')];
                  for (const anchor of anchors) {
                    const href = anchor.href;
                    const id = (href.match(/\/post\/([^/?#]+)/) || [])[1];
                    if (!id || seen.has(id)) continue;
                    let root = anchor;
                    for (let i = 0; i < 7 && root.parentElement; i++) {
                      root = root.parentElement;
                      if (root.querySelectorAll('button').length >= 3 && root.innerText.length > 10) break;
                    }
                    const text = root.innerText || '';
                    const authorLink = root.querySelector('a[href^="/@"]');
                    const time = root.querySelector('time');
                    const media = [...root.querySelectorAll('img,video')].map(el => ({
                      url: el.tagName === 'VIDEO' ? (el.currentSrc || el.src) : (el.currentSrc || el.src),
                      type: el.tagName === 'VIDEO' ? 'video' : 'image',
                      alt: el.alt || ''
                    })).filter(m => m.url && !/profile|大頭貼/i.test(m.alt));
                    const buttons = [...root.querySelectorAll('button')].map(b => b.innerText || b.getAttribute('aria-label') || '');
                    const postIds = [...root.querySelectorAll('a[href*="/post/"]')]
                      .map(a => ((a.href.match(/\/post\/([^/?#]+)/) || [])[1])).filter(Boolean);
                    seen.add(id);
                    results.push({id, href, text, authorHref: authorLink?.getAttribute('href') || '',
                      datetime: time?.getAttribute('datetime') || null, media, buttons, postIds});
                    if (results.length >= limit * 4) break;
                  }
                  return results;
                }""",
                {"username": username, "limit": limit},
            )
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
                counts = self._button_counts(raw.get("buttons", []))
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
        finally:
            page.close()

    @staticmethod
    def _clean_content_text(value: str, author: str) -> str | None:
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        ignored = re.compile(
            r"^(讚|留言|轉發|分享|翻譯|like|reply|repost|share)(\s+[\d,.萬千KkMm]+)?$", re.I
        )
        kept = [
            line for line in lines if line.lower() != author.lower() and not ignored.match(line)
        ]
        return "\n".join(kept)[:20_000] or None

    @staticmethod
    def _button_counts(buttons: list[str]) -> dict[str, int | None]:
        result = {
            "like_count": None,
            "reply_count": None,
            "repost_count": None,
            "share_count": None,
        }
        mapping = {
            "like_count": re.compile(r"讚|like", re.I),
            "reply_count": re.compile(r"留言|回覆|repl", re.I),
            "repost_count": re.compile(r"轉發|repost", re.I),
            "share_count": re.compile(r"分享|share", re.I),
        }
        for button in buttons:
            for key, pattern in mapping.items():
                if pattern.search(button):
                    result[key] = parse_count(button)
        return result


def content_fingerprint(text: str | None, media_urls: list[str]) -> str:
    payload = json.dumps(
        {"text": text, "media": sorted(media_urls)}, ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(payload.encode()).hexdigest()
