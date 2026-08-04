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
