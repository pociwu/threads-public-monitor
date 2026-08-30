import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.config import Settings
from app.services.collector import (
    RateLimited,
    RestrictedPage,
    ThreadsCollector,
    TransientRelationshipError,
    content_fingerprint,
    parse_count,
    parse_labeled_count,
    parse_retry_after,
)


class FakeRequest:
    def __init__(self, resource_type: str) -> None:
        self.resource_type = resource_type


class FakeResponse:
    def __init__(
        self,
        *,
        status: int,
        url: str = "https://www.threads.com/api/graphql",
        resource_type: str = "xhr",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.url = url
        self.request = FakeRequest(resource_type)
        self.headers = headers or {}


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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("invalid", None),
        ("-1", None),
        ("120", 120),
        ("Sun, 30 Aug 2026 10:02:00 GMT", 120),
        ("Sun, 30 Aug 2026 09:59:00 GMT", 0),
    ],
)
def test_parse_retry_after_accepts_delta_seconds_and_http_dates(
    value: str | None, expected: int | None
) -> None:
    assert (
        parse_retry_after(value, now=datetime(2026, 8, 30, 10, 0, tzinfo=UTC))
        == expected
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.threads.com/api/graphql",
        "https://www.threads.net/api/graphql",
        "https://www.instagram.com/api/v1/example",
        "https://www.facebook.com/api/graphql",
    ],
)
def test_collector_records_429_from_meta_ui_requests(tmp_path, url: str) -> None:
    collector = ThreadsCollector(
        Settings(
            media_root=tmp_path / "media",
            browser_profile_dir=tmp_path / "profile",
        )
    )
    collector._record_rate_limit_response(
        FakeResponse(
            status=429,
            url=url,
            resource_type="fetch",
            headers={"retry-after": "900"},
        )
    )

    with pytest.raises(RateLimited) as raised:
        collector._raise_if_rate_limited()

    assert raised.value.retry_after_seconds == 900
    assert raised.value.source_url == url


def test_collector_keeps_longest_retry_after_from_multiple_429_responses(tmp_path) -> None:
    collector = ThreadsCollector(
        Settings(
            media_root=tmp_path / "media",
            browser_profile_dir=tmp_path / "profile",
        )
    )
    collector._record_rate_limit_response(
        FakeResponse(status=429, headers={"Retry-After": "60"})
    )
    collector._record_rate_limit_response(
        FakeResponse(status=429, headers={"retry-after": "1800"})
    )

    with pytest.raises(RateLimited) as raised:
        collector._raise_if_rate_limited()

    assert raised.value.retry_after_seconds == 1800


@pytest.mark.parametrize("resource_type", ["image", "media", "font", "stylesheet"])
def test_collector_ignores_429_from_media_resources(
    tmp_path, resource_type: str
) -> None:
    collector = ThreadsCollector(
        Settings(
            media_root=tmp_path / "media",
            browser_profile_dir=tmp_path / "profile",
        )
    )

    collector._record_rate_limit_response(
        FakeResponse(status=429, resource_type=resource_type)
    )

    collector._raise_if_rate_limited()


def test_collector_ignores_429_from_unrelated_hosts(tmp_path) -> None:
    collector = ThreadsCollector(
        Settings(
            media_root=tmp_path / "media",
            browser_profile_dir=tmp_path / "profile",
        )
    )

    collector._record_rate_limit_response(
        FakeResponse(status=429, url="https://example.com/api", resource_type="xhr")
    )

    collector._raise_if_rate_limited()


def test_page_closes_and_raises_rate_limited_for_navigation_429(tmp_path) -> None:
    class FakePage:
        def __init__(self) -> None:
            self.closed = False

        def goto(self, _url, *, wait_until, timeout):
            assert wait_until == "domcontentloaded"
            assert timeout == 60_000
            return FakeResponse(
                status=429,
                url="https://www.threads.com/@example",
                resource_type="document",
                headers={"retry-after": "1800"},
            )

        def close(self) -> None:
            self.closed = True

    class FakeContext:
        def __init__(self) -> None:
            self.page = FakePage()

        def new_page(self):
            return self.page

    collector = ThreadsCollector(
        Settings(
            media_root=tmp_path / "media",
            browser_profile_dir=tmp_path / "profile",
        )
    )
    context = FakeContext()
    collector._context = context

    with pytest.raises(RateLimited) as raised:
        collector._page("https://www.threads.com/@example")

    assert raised.value.retry_after_seconds == 1800
    assert context.page.closed is True


@pytest.mark.parametrize(
    ("body", "error_type"),
    [
        ("Too many requests. Try again later.", RateLimited),
        ("Complete this captcha challenge", RestrictedPage),
    ],
)
def test_page_classifies_visible_limit_pages_and_closes(
    tmp_path, body: str, error_type: type[Exception]
) -> None:
    class FakePage:
        url = "https://www.threads.com/@example"

        def __init__(self) -> None:
            self.closed = False

        def goto(self, _url, **_kwargs):
            return FakeResponse(
                status=200,
                url=self.url,
                resource_type="document",
            )

        def wait_for_timeout(self, milliseconds):
            assert milliseconds == 1500

        def locator(self, selector):
            assert selector == "body"
            return self

        def inner_text(self, *, timeout):
            assert timeout == 15_000
            return body

        def close(self) -> None:
            self.closed = True

    class FakeContext:
        def __init__(self) -> None:
            self.page = FakePage()

        def new_page(self):
            return self.page

    collector = ThreadsCollector(
        Settings(
            media_root=tmp_path / "media",
            browser_profile_dir=tmp_path / "profile",
        )
    )
    context = FakeContext()
    collector._context = context

    with pytest.raises(error_type):
        collector._page(context.page.url)

    assert context.page.closed is True


def test_page_does_not_treat_rate_limit_words_in_long_post_body_as_limit_page(
    tmp_path,
) -> None:
    class FakePage:
        url = "https://www.threads.com/@example"

        def __init__(self) -> None:
            self.closed = False

        def goto(self, _url, **_kwargs):
            return FakeResponse(
                status=200,
                url=self.url,
                resource_type="document",
            )

        def wait_for_timeout(self, _milliseconds):
            pass

        def locator(self, _selector):
            return self

        def inner_text(self, **_kwargs):
            return f"{'一般貼文內容' * 500}\ntry again later"

        def close(self) -> None:
            self.closed = True

    class FakeContext:
        def __init__(self) -> None:
            self.page = FakePage()

        def new_page(self):
            return self.page

    collector = ThreadsCollector(
        Settings(
            media_root=tmp_path / "media",
            browser_profile_dir=tmp_path / "profile",
        )
    )
    context = FakeContext()
    collector._context = context

    page = collector._page(context.page.url)

    assert page is context.page
    assert context.page.closed is False


def test_content_collection_stops_scrolling_after_xhr_429(tmp_path) -> None:
    collector = ThreadsCollector(
        Settings(
            media_root=tmp_path / "media",
            browser_profile_dir=tmp_path / "profile",
        )
    )

    class FakeMouse:
        def __init__(self) -> None:
            self.wheels = 0

        def wheel(self, _x, _y):
            self.wheels += 1

    class FakePage:
        def __init__(self) -> None:
            self.mouse = FakeMouse()
            self.closed = False
            self.waits = 0

        def wait_for_timeout(self, milliseconds):
            assert milliseconds == 800
            self.waits += 1
            collector._record_rate_limit_response(
                FakeResponse(
                    status=429,
                    resource_type="xhr",
                    headers={"retry-after": "600"},
                )
            )

        def close(self) -> None:
            self.closed = True

    page = FakePage()
    collector._page = lambda _url: page

    with pytest.raises(RateLimited) as raised:
        collector.collect_content("example", "post")

    assert raised.value.retry_after_seconds == 600
    assert page.mouse.wheels == 1
    assert page.waits == 1
    assert page.closed is True


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


def test_positive_follower_count_waits_for_an_active_relationship_row(tmp_path) -> None:
    class FakeLocator:
        @property
        def first(self):
            return self

        def wait_for(self, *, state, timeout):
            assert state == "attached"
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
        '[role="dialog"]:visible a[href*="/@"], '
        '[aria-modal="true"]:visible a[href*="/@"]'
    )


def test_unknown_relationship_count_also_waits_for_an_active_row(tmp_path) -> None:
    class FakeLocator:
        called = False

        @property
        def first(self):
            return self

        def wait_for(self, *, state, timeout):
            assert state == "attached"
            assert timeout == 30_000
            self.called = True

    class FakePage:
        def __init__(self):
            self.rows = FakeLocator()

        def locator(self, _selector):
            return self.rows

    collector = ThreadsCollector(
        Settings(
            media_root=tmp_path / "media",
            browser_profile_dir=tmp_path / "profile",
        )
    )
    page = FakePage()

    collector._wait_for_relationship_rows(page, expected_count=None)

    assert page.rows.called is True


def test_relationship_helpers_target_active_dialog_and_correct_list_name() -> None:
    class FakeLocator:
        def __init__(self):
            self.has_text = None

        def filter(self, *, has_text):
            self.has_text = has_text
            return self

        @property
        def first(self):
            return "active-dialog"

    class FakePage:
        def __init__(self):
            self.selector = None

        def locator(self, selector):
            self.selector = selector
            return FakeLocator()

    page = FakePage()

    assert ThreadsCollector._active_relationship_dialog(page) == "active-dialog"
    assert page.selector == '[role="dialog"]:visible, [aria-modal="true"]:visible'
    assert ThreadsCollector._relationship_timeout_message("followers") == (
        "Threads 粉絲清單載入逾時，未取得任何成員"
    )
    assert ThreadsCollector._relationship_timeout_message("following") == (
        "Threads 追蹤中清單載入逾時，未取得任何成員"
    )
    assert ThreadsCollector._effective_relationship_count(
        "followers", {"followers": "粉絲 0", "following": "追蹤中 149"}, 51
    ) == 0
    assert ThreadsCollector._effective_relationship_count(
        "following", {"followers": "粉絲 51", "following": None}, 149
    ) == 149


def test_relationship_row_wait_survives_one_transient_timeout(tmp_path) -> None:
    class FakeLocator:
        def __init__(self):
            self.attempts = 0
            self.timeouts = []

        @property
        def first(self):
            return self

        def wait_for(self, *, state, timeout):
            assert state == "attached"
            self.attempts += 1
            self.timeouts.append(timeout)
            if self.attempts == 1:
                raise PlaywrightTimeoutError("Threads rendered the list slowly")

    class FakePage:
        def __init__(self):
            self.rows = FakeLocator()
            self.waits = []

        def locator(self, _selector):
            return self.rows

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    settings = Settings(
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
    )
    collector = ThreadsCollector(settings)
    page = FakePage()

    collector._wait_for_relationship_rows(page, expected_count=51)

    assert page.rows.timeouts == [30_000, 20_000]
    assert page.waits == [2_500]


def test_relationship_row_wait_stops_after_two_passive_timeouts(tmp_path) -> None:
    class FakeLocator:
        def __init__(self):
            self.timeouts = []

        @property
        def first(self):
            return self

        def wait_for(self, *, state, timeout):
            assert state == "attached"
            self.timeouts.append(timeout)
            raise PlaywrightTimeoutError("Threads did not render the list")

    class FakePage:
        def __init__(self):
            self.rows = FakeLocator()
            self.waits = []

        def locator(self, _selector):
            return self.rows

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    settings = Settings(
        media_root=tmp_path / "media",
        browser_profile_dir=tmp_path / "profile",
    )
    collector = ThreadsCollector(settings)
    page = FakePage()

    with pytest.raises(PlaywrightTimeoutError):
        collector._wait_for_relationship_rows(page, expected_count=51)

    assert page.rows.timeouts == [30_000, 20_000]
    assert page.waits == [2_500]


def test_relationship_row_timeout_preserves_detected_rate_limit(tmp_path) -> None:
    collector = ThreadsCollector(
        Settings(
            media_root=tmp_path / "media",
            browser_profile_dir=tmp_path / "profile",
        )
    )

    class FakeLocator:
        @property
        def first(self):
            return self

        def wait_for(self, *, state, timeout):
            assert state == "attached"
            assert timeout == 30_000
            collector._record_rate_limit_response(
                FakeResponse(
                    status=429,
                    resource_type="xhr",
                    headers={"retry-after": "3600"},
                )
            )
            raise PlaywrightTimeoutError("Threads did not render the list")

    class FakePage:
        def locator(self, _selector):
            return FakeLocator()

    with pytest.raises(RateLimited) as raised:
        collector._wait_for_relationship_rows(FakePage(), expected_count=51)

    assert raised.value.retry_after_seconds == 3600


def test_relationship_control_retries_until_threads_renders_it(tmp_path) -> None:
    class FakePage:
        def __init__(self):
            self.results = iter([False, False, True])
            self.waits = []

        def evaluate(self, _script):
            return next(self.results)

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    collector = ThreadsCollector(
        Settings(
            media_root=tmp_path / "media",
            browser_profile_dir=tmp_path / "profile",
        )
    )
    page = FakePage()

    assert collector._click_when_available(page, "script", attempts=60) is True
    assert page.waits == [500, 500]


def test_missing_relationship_control_is_retryable_and_saves_diagnostic(tmp_path) -> None:
    class FakePage:
        def __init__(self):
            self.closed = False
            self.scripts = []

        def evaluate(self, script):
            self.scripts.append(script)
            return len(self.scripts) == 1

        def wait_for_timeout(self, _milliseconds):
            pass

        def close(self):
            self.closed = True

    collector = ThreadsCollector(
        Settings(
            media_root=tmp_path / "media",
            browser_profile_dir=tmp_path / "profile",
        )
    )
    page = FakePage()
    diagnostics = []
    collector._page = lambda _url: page
    collector._save_relationship_diagnostic = (
        lambda _page, username, relationship_type: diagnostics.append(
            (username, relationship_type)
        )
    )

    with pytest.raises(TransientRelationshipError, match="清單控制項"):
        collector.collect_relationships("example", "following")

    assert diagnostics == [("example", "following")]
    assert '[role="tab"]' in page.scripts[0]
    assert '[role="tab"]' in page.scripts[1]
    assert page.closed is True


def test_accessible_relationship_end_can_complete_below_profile_count() -> None:
    assert ThreadsCollector._relationship_batch_complete(
        {
            "complete": False,
            "atEnd": True,
            "stagnant": 8,
            "orderedCount": 122,
        }
    ) is True
    assert ThreadsCollector._relationship_batch_complete(
        {
            "complete": False,
            "atEnd": True,
            "stagnant": 2,
            "orderedCount": 122,
        }
    ) is False


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


def test_interaction_counts_use_numbers_next_to_icon_controls() -> None:
    controls = [
        {"text": "", "label": "讚", "nearby": "3K"},
        {"text": "", "label": "回覆", "nearby": "36"},
        {"text": "", "label": "轉發", "nearby": "45"},
        {"text": "", "label": "分享", "nearby": "2K"},
    ]

    assert ThreadsCollector._button_counts(controls) == {
        "like_count": 3000,
        "reply_count": 36,
        "repost_count": 45,
        "share_count": 2000,
    }


def test_interaction_counts_fall_back_to_inline_metric_summary() -> None:
    assert ThreadsCollector._button_counts(
        [], "貼文正文\n讚3.2 萬回覆256轉發1,725分享3,229"
    ) == {
        "like_count": 32000,
        "reply_count": 256,
        "repost_count": 1725,
        "share_count": 3229,
    }


def test_interaction_counts_do_not_treat_post_prose_as_a_metric_summary() -> None:
    assert ThreadsCollector._button_counts([], "今天想分享1個故事") == {
        "like_count": None,
        "reply_count": None,
        "repost_count": None,
        "share_count": None,
    }


def test_collector_registers_browser_context_429_observer(monkeypatch, tmp_path) -> None:
    class FakeContext:
        def __init__(self) -> None:
            self.response_handler = None
            self.closed = False

        def on(self, event, handler) -> None:
            assert event == "response"
            self.response_handler = handler

        def close(self) -> None:
            self.closed = True

    context = FakeContext()

    class FakeChromium:
        def launch_persistent_context(self, *_args, **_kwargs):
            return context

    class FakePlaywright:
        chromium = FakeChromium()

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
        Settings(
            media_root=tmp_path / "media",
            browser_profile_dir=tmp_path / "profile",
            chromium_executable="missing",
        )
    )

    with collector:
        assert context.response_handler is not None
        context.response_handler(
            FakeResponse(
                status=429,
                url="https://www.facebook.com/api/graphql",
                resource_type="fetch",
                headers={"Retry-After": "300"},
            )
        )
        with pytest.raises(RateLimited) as raised:
            collector._raise_if_rate_limited()
        assert raised.value.retry_after_seconds == 300

    assert context.closed is True
    assert runtime.stopped is True


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
