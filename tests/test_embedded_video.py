import json

from app.services.collector import embedded_post_videos


def test_only_matching_post_video_and_one_variant_per_attachment():
    scripts = [
        json.dumps(
            {
                "data": [
                    {"code": "other", "video_versions": [{"url": "https://cdn.example/other.mp4"}]},
                    {
                        "code": "target",
                        "video_versions": [
                            {"url": "blob:temporary"},
                            {"url": "https://cdn.example/real.mp4"},
                            {"url": "https://cdn.example/variant.mp4"},
                        ],
                    },
                ]
            }
        ),
        "not json",
    ]
    assert embedded_post_videos(scripts, "target") == [("https://cdn.example/real.mp4", "video")]
    assert embedded_post_videos(scripts, "absent") == []


def test_carousel_does_not_include_quoted_post_video():
    scripts = [
        json.dumps(
            {
                "code": "target",
                "carousel_media": [
                    {"video_versions": [{"url": "https://cdn.example/one.mp4"}]},
                    {"video_versions": [{"url": "https://cdn.example/two.mp4"}]},
                ],
                "quoted_post": {"video_versions": [{"url": "https://cdn.example/quote.mp4"}]},
            }
        )
    ]
    assert embedded_post_videos(scripts, "target") == [
        ("https://cdn.example/one.mp4", "video"),
        ("https://cdn.example/two.mp4", "video"),
    ]
