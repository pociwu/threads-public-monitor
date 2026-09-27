import pytest

from app.services.content_text import clean_content_text


@pytest.mark.parametrize(
    "raw,body",
    [
        ("追蹤sin_9311原作者說讚更多一次又一次的失望吧讚1回覆1轉發分享", "一次又一次的失望吧"),
        (
            "追蹤sin_9311更多沒時間報備 有時間在這打一串讚1回覆轉發分享",
            "沒時間報備 有時間在這打一串",
        ),
        ("追蹤sin_9311原作者說讚更多太美了🥹故事和人讚1回覆轉發分享", "太美了🥹故事和人"),
    ],
)
def test_packed_threads_chrome(raw, body):
    assert clean_content_text(raw, "sin_9311") == body


@pytest.mark.parametrize(
    "body",
    [
        "我想要更多讚1回覆2轉發3分享4",
        "追蹤sin_9311的故事",
        "原作者說讚更多",
        "我說讚",
        "更多回覆",
        "第一行\n追蹤sin_9311更多\n最後一行",
    ],
)
def test_preserves_prose(body):
    assert clean_content_text(body, "sin_9311") == body
