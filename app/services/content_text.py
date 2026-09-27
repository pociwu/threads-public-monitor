import re

_COUNT = r"(?:\d[\d,.]*\s*[萬万千KkMm]?(?:\s*次)?)"
_LABEL = (
    r"(?:讚|留言|回覆|轉發|分享|翻譯|"
    r"likes?|comments?|repl(?:y|ies)|reposts?|shares?|translate)"
)
_INTERACTION_TOKEN = rf"{_LABEL}(?:\s*{_COUNT})?"
_INTERACTION_TOKEN_RE = re.compile(_INTERACTION_TOKEN, re.I)
_INTERACTION_LINE = re.compile(rf"^(?:{_INTERACTION_TOKEN}\s*)+$", re.I)
_CAROUSEL = r"\d+\s*/\s*\d+"
_CAROUSEL_LINE = re.compile(rf"^{_CAROUSEL}$")
_CAROUSEL_WITH_INTERACTIONS = re.compile(rf"^{_CAROUSEL}\s*(?:{_INTERACTION_TOKEN}\s*)+$", re.I)
_INLINE_CAROUSEL_SUFFIX = re.compile(rf"\s+{_CAROUSEL}\s*(?:{_INTERACTION_TOKEN}\s*)+$", re.I)
_INLINE_INTERACTION_SUFFIX = re.compile(rf"\s+(?:{_INTERACTION_TOKEN}\s*){{2,}}$", re.I)
_INLINE_CAROUSEL_ONLY = re.compile(rf"\s+{_CAROUSEL}$")
_DATE_OR_SEPARATOR = re.compile(r"^(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|/)$")
_NUMERIC = re.compile(r"^[\d,.]+\s*[萬万千KkMm]?(?:\s*次)?$")


def _interaction_count(value: str) -> int:
    if not _INTERACTION_LINE.fullmatch(value):
        return 0
    return len(_INTERACTION_TOKEN_RE.findall(value))


def _remove_inline_ui_suffix(lines: list[str]) -> bool:
    if not lines:
        return False
    original = lines[-1]
    cleaned = _INLINE_CAROUSEL_SUFFIX.sub("", original).rstrip()
    if cleaned == original:
        cleaned = _INLINE_INTERACTION_SUFFIX.sub("", original).rstrip()
    if cleaned == original:
        return False
    if cleaned:
        lines[-1] = cleaned
    else:
        lines.pop()
    return True


def _remove_trailing_interactions(lines: list[str]) -> bool:
    if not lines:
        return False
    if _CAROUSEL_WITH_INTERACTIONS.fullmatch(lines[-1]):
        lines.pop()
        return True

    start = len(lines)
    token_count = 0
    while start and _INTERACTION_LINE.fullmatch(lines[start - 1]):
        start -= 1
        token_count += _interaction_count(lines[start])

    has_carousel = bool(
        start
        and (
            _CAROUSEL_LINE.fullmatch(lines[start - 1])
            or _INLINE_CAROUSEL_ONLY.search(lines[start - 1])
        )
    )
    if token_count < 2 and not (token_count and has_carousel):
        return False

    del lines[start:]
    if lines and _CAROUSEL_LINE.fullmatch(lines[-1]):
        lines.pop()
    elif lines:
        lines[-1] = _INLINE_CAROUSEL_ONLY.sub("", lines[-1]).rstrip()
        if not lines[-1]:
            lines.pop()
    return True


def _remove_legacy_numeric_suffix(lines: list[str], header_removed: bool) -> None:
    start = len(lines)
    while start and (_NUMERIC.fullmatch(lines[start - 1]) or lines[start - 1] == "/"):
        start -= 1
    suffix = lines[start:]
    if not suffix or not lines[:start]:
        return

    slash_index = next((index for index, value in enumerate(suffix) if value == "/"), -1)
    has_carousel = (
        0 < slash_index < len(suffix) - 1
        and _NUMERIC.fullmatch(suffix[slash_index - 1])
        and _NUMERIC.fullmatch(suffix[slash_index + 1])
    )
    numeric_count = sum(bool(_NUMERIC.fullmatch(value)) for value in suffix)
    if has_carousel or (header_removed and numeric_count >= 4):
        del lines[start:]


def clean_content_text(value: str, author: str) -> str | None:
    """Remove only trailing Threads chrome while preserving post prose."""
    # Detached DOM textContent can concatenate the header and toolbar without
    # separators. Require the exact author header before stripping a packed
    # toolbar; ordinary prose mentioning likes or follows must remain intact.
    packed_header = re.match(
        rf"^\s*追蹤\s*@?{re.escape(author)}\s*(?:原作者說讚\s*)?更多",
        value,
    )
    if packed_header:
        value = value[packed_header.end() :]
        value = re.sub(
            rf"讚\s*(?:{_COUNT})?\s*回覆\s*(?:{_COUNT})?\s*"
            rf"轉發\s*(?:{_COUNT})?\s*分享\s*(?:{_COUNT})?\s*$",
            "",
            value,
        )
    lines = [line.strip() for line in value.splitlines() if line.strip()]

    author_names = {author.casefold(), f"@{author}".casefold()}
    header_removed = bool(lines and lines[0].casefold() in author_names)
    if header_removed:
        lines.pop(0)
        while lines and _DATE_OR_SEPARATOR.fullmatch(lines[0]):
            lines.pop(0)

    removed_ui = _remove_inline_ui_suffix(lines)
    if not removed_ui:
        removed_ui = _remove_trailing_interactions(lines)
    if not removed_ui:
        _remove_legacy_numeric_suffix(lines, header_removed)

    return "\n".join(lines)[:20_000] or None
