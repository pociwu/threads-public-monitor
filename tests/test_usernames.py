import pytest

from app.services.usernames import InvalidUsername, normalize_username


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://www.threads.com/@sin_9311", "sin_9311"),
        ("@Sin_9311", "sin_9311"),
        ("sin_9311", "sin_9311"),
        (" https://threads.com/@some.one/ ", "some.one"),
    ],
)
def test_normalize_username(value: str, expected: str) -> None:
    assert normalize_username(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "https://example.com/@name", "bad/name", "has space", "@", "a" * 31],
)
def test_reject_invalid_username(value: str) -> None:
    with pytest.raises(InvalidUsername):
        normalize_username(value)
