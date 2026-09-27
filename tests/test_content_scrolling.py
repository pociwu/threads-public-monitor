from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.collector import ThreadsCollector


def make_collector(tmp_path, screens):
    collector = ThreadsCollector(Settings(media_root=tmp_path / "media"))

    class Page:
        index = 0
        closed = False

        def __init__(self):
            self.mouse = SimpleNamespace(wheel=self.scroll)

        def scroll(self, x, y):
            self.index += 1

        def wait_for_timeout(self, ms):
            assert ms == 800

        def evaluate(self, script, args):
            return screens[min(self.index, len(screens) - 1)]

        def locator(self, selector):
            return SimpleNamespace(count=lambda: 0)

        def close(self):
            self.closed = True

    page = Page()
    collector._page = lambda url: page
    return collector, page


def row(pid, author="sin_9311", quoted=False):
    return {
        "id": pid,
        "href": f"/@{author}/post/{pid}",
        "authorHref": f"/@{author}",
        "text": "example content",
        "postIds": [pid, "quoted"] if quoted else [pid],
    }


@pytest.mark.parametrize(
    "kind,author,quoted",
    [
        ("post", "sin_9311", False),
        ("reply", "sin_9311", False),
        ("repost", "chuanmenzi_019", False),
        ("quote", "sin_9311", True),
    ],
)
def test_preserves_first_screen_before_virtualization(tmp_path, kind, author, quoted):
    collector, page = make_collector(
        tmp_path, [[row("new", author, quoted)], [row("old", author, quoted)]]
    )
    items = collector.collect_content("sin_9311", kind, limit=2)
    assert [i.threads_id for i in items] == ["new", "old"]
    assert page.index == 1
    assert page.closed


def test_deduplicates_overlapping_screens(tmp_path):
    collector, page = make_collector(tmp_path, [[row("a"), row("b")], [row("b"), row("c")]])
    assert [i.threads_id for i in collector.collect_content("sin_9311", "reply", 3)] == [
        "a",
        "b",
        "c",
    ]
    assert page.index == 1


def test_cursor_survives_leaving_dom(tmp_path):
    collector, page = make_collector(
        tmp_path, [[row("new"), row("cursor")], [row("older")], [row("oldest")]]
    )
    items = collector.collect_content("sin_9311", "reply", 2, cursor="cursor")
    assert [i.threads_id for i in items] == ["older", "oldest"]
    assert page.index == 2


def test_missing_cursor_does_not_return_wrong_batch(tmp_path):
    collector, page = make_collector(tmp_path, [[row("new")]])
    assert collector.collect_content("sin_9311", "reply", 2, cursor="missing") == []
    assert page.index == 8


def test_full_first_screen_needs_no_scroll(tmp_path):
    collector, page = make_collector(tmp_path, [[row("new")]])
    assert len(collector.collect_content("sin_9311", "reply", 1)) == 1
    assert page.index == 0
