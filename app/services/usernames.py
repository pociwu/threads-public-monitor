from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

USERNAME_RE = re.compile(r"^[a-z0-9._]{1,30}$")


class InvalidUsername(ValueError):
    pass


def normalize_username(value: str) -> str:
    raw = unquote(value.strip())
    if not raw:
        raise InvalidUsername("請輸入 Threads 使用者名稱")

    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.hostname not in {"threads.com", "www.threads.com"}:
            raise InvalidUsername("只接受 threads.com 網址")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise InvalidUsername("網址中找不到使用者名稱")
        raw = parts[0]

    normalized = raw.removeprefix("@").strip().lower()
    if not USERNAME_RE.fullmatch(normalized):
        raise InvalidUsername("使用者名稱只能包含英文字母、數字、句點與底線")
    return normalized
