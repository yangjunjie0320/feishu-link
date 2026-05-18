from feishu_link.platforms import detect_platform, normalized_domain


def test_normalized_domain() -> None:
    assert normalized_domain("https://www.youtube.com/watch?v=abc") == "youtube.com"


def test_detect_platforms() -> None:
    assert detect_platform("https://b23.tv/abc") == "bilibili"
    assert detect_platform("https://www.instagram.com/reel/abc") == "instagram"
    assert detect_platform("https://www.tiktok.com/@u/video/123") == "tiktok"
    assert detect_platform("https://youtu.be/abc") == "youtube"
    assert detect_platform("https://twitter.com/u/status/123") == "x"
    assert detect_platform("https://www.zhihu.com/question/123/answer/456") == "zhihu"
    assert detect_platform("https://zhuanlan.zhihu.com/p/123") == "zhihu"
    assert detect_platform("https://example.com") == "web"
