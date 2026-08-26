import json
from pathlib import Path

import pytest

from app.config import Settings
from app.services.collector import (
    ThreadsCollector,
    content_fingerprint,
    parse_count,
    parse_labeled_count,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("未知", None),
        ("51 位粉絲", 51),
        ("讚 1,234", 1234),
        ("2.5K followers", 2500),
        ("1.2萬位粉絲", 12000),
    ],
)
def test_parse_count(value: str | None, expected: int | None) -> None:
    assert parse_count(value) == expected


def test_parse_labeled_count_falls_back_to_visible_profile_text() -> None:
    body = "顯示名稱\n1.2萬位粉絲\n個人簡介"
    assert parse_labeled_count(None, body, r"粉絲|followers?") == 12_000
    assert parse_labeled_count(None, body, r"追蹤中|following") is None


def test_content_fingerprint_is_order_independent_for_media() -> None:
    first = content_fingerprint("hello", ["b", "a"])
    second = content_fingerprint("hello", ["a", "b"])
    assert first == second
    assert first != content_fingerprint("changed", ["a", "b"])


def test_empty_relationship_diagnostic_keeps_bounded_latest_artifacts(tmp_path) -> None:
    class FakePage:
        def evaluate(self, _script):
            return {
                "url": "https://www.threads.com/@example",
                "title": "Threads",
                "dialogHtml": "<div role=\"dialog\">changed DOM</div>",
                "dialogLinks": [],
                "dialogControls": [],
                "dialogImages": [],
            }

        def screenshot(self, *, path, full_page):
            assert full_page is True
            Path(path).write_bytes(b"png")

    settings = Settings(
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
    )
    collector = ThreadsCollector(settings)

    collector._save_relationship_diagnostic(FakePage(), "example", "followers")

    debug_dir = tmp_path / "debug"
    json_path = debug_dir / "relationship-example-followers-latest.json"
    png_path = debug_dir / "relationship-example-followers-latest.png"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["dialogHtml"] == '<div role="dialog">changed DOM</div>'
    assert payload["username"] == "example"
    assert png_path.read_bytes() == b"png"
    assert sorted(path.name for path in debug_dir.iterdir()) == [
        "relationship-example-followers-latest.json",
        "relationship-example-followers-latest.png",
    ]


def test_positive_follower_count_waits_for_a_visible_relationship_row(tmp_path) -> None:
    class FakeLocator:
        @property
        def first(self):
            return self

        def wait_for(self, *, state, timeout):
            assert state == "visible"
            assert timeout == 30_000

    class FakePage:
        def __init__(self):
            self.selector = None

        def locator(self, selector):
            self.selector = selector
            return FakeLocator()

    settings = Settings(
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
    )
    collector = ThreadsCollector(settings)
    page = FakePage()

    collector._wait_for_relationship_rows(page, expected_count=51)

    assert page.selector == (
        '[role="dialog"] a[href*="/@"], [aria-modal="true"] a[href*="/@"]'
    )


def test_relationship_control_retries_until_threads_renders_it() -> None:
    class FakePage:
        def __init__(self):
            self.results = iter([False, False, True])
            self.waits = []

        def evaluate(self, _script):
            return next(self.results)

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    page = FakePage()

    assert ThreadsCollector._click_when_available(page, "script", attempts=60) is True
    assert page.waits == [500, 500]


def test_content_text_excludes_trailing_threads_ui_numbers() -> None:
    raw = "example\n2026-6-10\n杜拜巧克力＝杜力\n1\n/\n2\n12 萬\n731\n4,014\n2.3 萬"
    assert ThreadsCollector._clean_content_text(raw, "example") == "杜拜巧克力＝杜力"


@pytest.mark.parametrize(
    "ui_suffix",
    [
        "\n1/2\n讚21\n回覆20\n轉發3\n分享1",
        "\n1 / 2\n讚 21\n回覆 20\n轉發 3\n分享 1",
        " 1/2讚21回覆20轉發3分享1",
    ],
)
def test_content_text_excludes_carousel_and_labeled_interaction_suffix(
    ui_suffix: str,
) -> None:
    body = (
        "從6月初訂購到今日8月快底才來 等了兩個月爆炸久的穿戴甲\n"
        "連催三次才送到\n"
        "一次忘記寄出最後一次說補償兩組結果沒有🫥\n"
        "款式真的很漂亮 但真的太久了...\n"
        "還是我錯了 穿戴甲都要等這麼久"
    )
    raw = f"sin_9311\n{body}{ui_suffix}"

    assert ThreadsCollector._clean_content_text(raw, "sin_9311") == body


@pytest.mark.parametrize(
    "body",
    ["比例是 1/2", "比例是1/2分享1回覆2", "今天想分享1個故事"],
)
def test_content_text_keeps_numbers_that_are_part_of_the_post(body: str) -> None:
    assert ThreadsCollector._clean_content_text(f"sin_9311\n{body}", "sin_9311") == body


@pytest.mark.parametrize(
    "body",
    [
        "今天先說\n分享1\n明天再說",
        "活動日期\n2026/08/26\n歡迎來玩",
        "總共有\n100\n份",
    ],
)
def test_content_text_keeps_chrome_like_lines_inside_the_post(body: str) -> None:
    assert ThreadsCollector._clean_content_text(f"sin_9311\n{body}", "sin_9311") == body


def test_content_text_excludes_inline_interactions_without_carousel() -> None:
    assert (
        ThreadsCollector._clean_content_text(
            "sin_9311\n這款真的很好看 讚21回覆20轉發3分享1", "sin_9311"
        )
        == "這款真的很好看"
    )


def test_interaction_counts_pair_button_text_with_accessible_labels() -> None:
    controls = [
        {"text": "6,601", "label": "讚 6,601 次"},
        {"text": "322", "label": "322 則回覆"},
        {"text": "352", "label": "轉發 352 次"},
        {"text": "803", "label": "分享 803 次"},
    ]
    assert ThreadsCollector._button_counts(controls) == {
        "like_count": 6601,
        "reply_count": 322,
        "repost_count": 352,
        "share_count": 803,
    }


def test_collector_stops_playwright_when_browser_launch_fails(monkeypatch, tmp_path) -> None:
    class FailingChromium:
        def launch_persistent_context(self, *_args, **_kwargs):
            raise RuntimeError("browser launch failed")

    class FakePlaywright:
        chromium = FailingChromium()

        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    runtime = FakePlaywright()

    class FakeManager:
        def start(self):
            return runtime

    monkeypatch.setattr("app.services.collector.sync_playwright", FakeManager)
    collector = ThreadsCollector(
        Settings(browser_profile_dir=tmp_path / "profile", chromium_executable="missing")
    )

    with pytest.raises(RuntimeError, match="browser launch failed"):
        collector.__enter__()

    assert runtime.stopped is True
    assert collector._playwright is None


def test_profile_identity_requires_requested_threads_account() -> None:
    assert ThreadsCollector._profile_matches_username(
        {
            "profilePaths": ["/@xin.121"],
            "profileAnchorPaths": ["/@xin.121"],
            "body": "xin.121\n51 followers",
        },
        "xin.121",
    )
    assert not ThreadsCollector._profile_matches_username(
        {
            "profilePaths": ["/@xin.121"],
            "profileAnchorPaths": [],
            "body": "xin.121. Something went wrong. Visit the Instagram help centre.",
        },
        "xin.121",
    )


def test_profile_display_name_falls_back_to_open_graph_title() -> None:
    assert (
        ThreadsCollector._profile_display_name(
            {"ogTitle": "小欣 (@xin.121) · Threads"}, "xin.121", []
        )
        == "小欣"
    )
