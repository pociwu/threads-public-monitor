import pytest

from app.services.collector import content_fingerprint, parse_count


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


def test_content_fingerprint_is_order_independent_for_media() -> None:
    first = content_fingerprint("hello", ["b", "a"])
    second = content_fingerprint("hello", ["a", "b"])
    assert first == second
    assert first != content_fingerprint("changed", ["a", "b"])
