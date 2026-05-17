from feishu_link.parsers.base import MediaType
from feishu_link.parsers.ytdlp import _metadata_from_info


def test_metadata_from_ytdlp_info() -> None:
    meta = _metadata_from_info(
        "https://www.tiktok.com/@u/video/123",
        "tiktok",
        {
            "title": "Short clip",
            "description": "hello",
            "thumbnail": "https://example.com/cover.jpg",
            "duration": 12.3,
            "view_count": 123456,
            "like_count": 7890,
            "comment_count": 123,
            "repost_count": 45,
            "uploader": "creator",
            "webpage_url": "https://www.tiktok.com/@u/video/123",
            "formats": [{
                "url": "https://cdn.example.com/video.mp4?token=secret",
                "format_id": "18",
                "ext": "mp4",
                "filesize": 1234,
            }],
        },
    )

    assert meta.platform == "tiktok"
    assert meta.site_name == "TikTok"
    assert meta.media_type == MediaType.VIDEO
    assert meta.duration_seconds == 12
    assert meta.channel == "creator"
    assert meta.view_count == 123456
    assert meta.like_count == 7890
    assert meta.comment_count == 123
    assert meta.repost_count == 45
    assert meta.download_candidates[0].format_id == "18"
