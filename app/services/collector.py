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
                    const textRoot = root.cloneNode(true);
                    textRoot.querySelectorAll('button,time').forEach(el => el.remove());
                    const text = textRoot.innerText || textRoot.textContent || '';
                    const authorLink = root.querySelector('a[href^="/@"]');
                    const time = root.querySelector('time');
                    const media = [...root.querySelectorAll('img,video')].map(el => ({
                      url: el.tagName === 'VIDEO' ? (el.currentSrc || el.src) : (el.currentSrc || el.src),
                      type: el.tagName === 'VIDEO' ? 'video' : 'image',
                      alt: el.alt || ''
                    })).filter(m => m.url && !/profile|大頭貼/i.test(m.alt));
                    const buttons = [...root.querySelectorAll('button')].map(b => ({
                      text: b.innerText || '',
                      label: b.getAttribute('aria-label') || ''
                    }));
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

    def collect_relationships(
        self,
        username: str,
        relationship_type: str,
        limit: int = 5,
        cursor: str | None = None,
    ) -> RelationshipBatch:
        if relationship_type not in {"followers", "following"}:
            raise CollectionError(f"未知關係類型：{relationship_type}")
        page = self._page(f"https://www.threads.com/@{username}")
        try:
            opened = page.evaluate(
                r"""() => {
                  const controls = [...document.querySelectorAll('a,button,[role="button"]')];
                  const target = controls.find(el => {
                    const text = [el.getAttribute('aria-label') || '', el.textContent || '']
                      .join(' ').replace(/\s+/g, ' ').trim();
                    return text.length < 240 && /粉絲|followers?/i.test(text);
                  });
                  if (!target) return false;
                  target.click();
                  return true;
                }"""
            )
            if opened and relationship_type == "following":
                page.wait_for_timeout(800)
                opened = page.evaluate(
                    r"""() => {
                      const dialog = document.querySelector(
                        '[role="dialog"],[aria-modal="true"]'
                      );
                      if (!dialog) return false;
                      const controls = [...dialog.querySelectorAll('a,button,[role="button"]')];
                      const target = controls.find(el => /追蹤中|following/i.test(
                        [el.getAttribute('aria-label') || '', el.textContent || ''].join(' ')
                      ));
                      if (!target) return false;
                      target.click();
                      return true;
                    }"""
                )
            if not opened:
                raise CollectionError(
                    "Threads 目前未提供可存取的粉絲／追蹤中清單控制項"
                )
            page.wait_for_timeout(1000)
            raw: dict[str, Any] = page.evaluate(
                r"""async ({owner, limit, cursor}) => {
                  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
                  const dialog = document.querySelector('[role="dialog"],[aria-modal="true"]');
                  if (!dialog) {
                    return {members: [], complete: false, cursorFound: !cursor, available: false};
                  }
                  const candidates = [dialog, ...dialog.querySelectorAll('*')]
                    .filter(el => el.scrollHeight > el.clientHeight + 40);
                  const scroller = candidates.sort(
                    (a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight)
                  )[0] || dialog;
                  const ordered = [];
                  const seen = new Set();
                  let cursorFound = !cursor;
                  let stagnant = 0;
                  let previousSize = 0;
                  let complete = false;
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

                  for (let turn = 0; turn < 80; turn++) {
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
                      if (memberUsername === owner.toLowerCase() || seen.has(memberUsername)) continue;
                      let item = anchor;
                      for (let level = 0; level < 10 && item.parentElement; level++) {
                        item = item.parentElement;
                        if (avatarUrlFrom(item) && item.innerText.trim().length > 0) break;
                      }
                      const avatarUrl = avatarUrlFrom(item);
                      const lines = (item.innerText || '').split('\n').map(v => v.trim()).filter(Boolean);
                      const displayName = lines.find(line =>
                        line.toLowerCase() !== memberUsername &&
                        line.toLowerCase() !== `@${memberUsername}` &&
                        !/追蹤|follow/i.test(line)
                      ) || null;
                      seen.add(memberUsername);
                      ordered.push({
                        username: memberUsername,
                        displayName,
                        avatarUrl
                      });
                      if (memberUsername === (cursor || '').toLowerCase()) cursorFound = true;
                    }

                    const afterCursor = cursorFound
                      ? ordered.slice(cursor ? ordered.findIndex(m => m.username === cursor.toLowerCase()) + 1 : 0)
                      : [];
                    if (afterCursor.length > limit) {
                      return {
                        members: afterCursor.slice(0, limit), complete: false,
                        cursorFound, available: true
                      };
                    }
                    const atEnd = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 8;
                    if (atEnd && ordered.length === previousSize) stagnant += 1;
                    else stagnant = 0;
                    // Threads often renders an empty dialog before its member rows arrive.
                    // Do not treat that transient state as a complete empty list.
                    if (atEnd && stagnant >= 2 && (ordered.length > 0 || turn >= 12)) {
                      complete = true;
                      return {
                        members: afterCursor.slice(0, limit), complete,
                        cursorFound, available: true
                      };
                    }
                    previousSize = ordered.length;
                    scroller.scrollTop = Math.min(
                      scroller.scrollTop + Math.max(scroller.clientHeight * 0.8, 320),
                      scroller.scrollHeight
                    );
                    scroller.dispatchEvent(new Event('scroll', {bubbles: true}));
                    await sleep(450);
                  }
                  const start = cursor
                    ? ordered.findIndex(m => m.username === cursor.toLowerCase()) + 1 : 0;
                  const members = cursorFound ? ordered.slice(start, start + limit) : [];
                  return {members, complete: false, cursorFound, available: true};
                }""",
                {"owner": username, "limit": limit, "cursor": cursor},
            )
            if not raw.get("available", True):
                raise CollectionError("Threads 名單視窗未成功開啟")
            if cursor and not raw.get("cursorFound"):
                raise CollectionError("Threads 關係名單已變動，無法延續本次分批游標")
            members = [
                RelationshipMemberData(
                    username=item["username"],
                    display_name=item.get("displayName"),
                    avatar_url=item.get("avatarUrl"),
                )
                for item in raw.get("members", [])
                if item.get("username")
            ]
            return RelationshipBatch(
                members=members,
                cursor=members[-1].username if members else cursor,
                complete=bool(raw.get("complete")),
            )
        finally:
            page.close()

    @staticmethod
    def _clean_content_text(value: str, author: str) -> str | None:
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        ignored = re.compile(
            r"^(讚|留言|轉發|分享|翻譯|like|reply|repost|share)(\s+[\d,.萬千KkMm]+)?$", re.I
        )
        date_or_separator = re.compile(r"^(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|/)$")
        numeric = re.compile(r"^[\d,.]+\s*[萬万千KkMm]?(?:\s*次)?$")
        candidates = [
            line
            for line in lines
            if line.lower() != author.lower()
            and not ignored.match(line)
            and not date_or_separator.match(line)
        ]
        has_text = any(not numeric.match(line) for line in candidates)
        kept = [line for line in candidates if not has_text or not numeric.match(line)]
        return "\n".join(kept)[:20_000] or None

    @staticmethod
    def _button_counts(buttons: list[dict[str, str] | str]) -> dict[str, int | None]:
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
            if isinstance(button, dict):
                text = button.get("text", "")
                label = button.get("label", "")
                descriptor = f"{label} {text}".strip()
            else:
                text = button
                descriptor = button
            for key, pattern in mapping.items():
                if pattern.search(descriptor):
                    result[key] = parse_count(text) or parse_count(descriptor)
        return result


def content_fingerprint(text: str | None, media_urls: list[str]) -> str:
    payload = json.dumps(
        {"text": text, "media": sorted(media_urls)}, ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(payload.encode()).hexdigest()
