from src.platforms import detect_platform, normalize_url, normalized_domain


def test_normalized_domain() -> None:
    assert normalized_domain("https://www.youtube.com/watch?v=abc") == "youtube.com"


def test_detect_platforms() -> None:
    assert detect_platform("https://b23.tv/abc") == "bilibili"
    assert detect_platform("https://www.instagram.com/reel/abc") == "instagram"
    assert detect_platform("https://www.tiktok.com/@u/video/123") == "tiktok"
    assert detect_platform("https://www.douyin.com/note/123") == "douyin"
    assert detect_platform("https://v.douyin.com/abc/") == "douyin"
    assert detect_platform("https://www.iesdouyin.com/share/video/123/") == "douyin"
    assert detect_platform("https://douyin.com.example.com/video/123") == "web"
    assert detect_platform("https://youtu.be/abc") == "youtube"
    assert detect_platform("https://twitter.com/u/status/123") == "x"
    assert detect_platform("https://example.com") == "web"
    assert detect_platform("https://www.zhihu.com/question/123/answer/456") == "web"


def test_normalize_url_strips_bilibili_tracking_params() -> None:
    a = (
        "https://www.bilibili.com/video/BV12kga6cEe1/"
        "?share_source=copy_web&vd_source=59f3e07d39f3f1ec161bd88dbe8fe69e"
    )
    b = (
        "https://www.bilibili.com/video/BV12kga6cEe1/?-Arouter=story&buvid=XX983259EB9063A9"
        "&from_spmid=tm.recommend.0.0&is_story_h5=false&mid=abc123&plat_id=163"
        "&share_from=ugc&share_medium=android&share_plat=android"
        "&share_session_id=bf67956a&share_tag=s_i&spmid=main.ugc-video-detail-vertical.0.0"
        "&timestamp=1784809182&unique_k=xThOSEk&up_id=3546781006694901"
    )
    assert normalize_url(a) == normalize_url(b)


def test_normalize_url_different_videos_differ() -> None:
    a = "https://www.bilibili.com/video/BV12kga6cEe1/?share_source=copy_web"
    b = "https://www.bilibili.com/video/BV1eZgt6YEA9/?share_source=copy_web"
    assert normalize_url(a) != normalize_url(b)


def test_normalize_url_ignores_query_order_and_case() -> None:
    a = "https://X.COM/foo/status/123?s=46&utm_source=x"
    b = "http://x.com/foo/status/123?utm_source=x&s=46"
    assert normalize_url(a) == normalize_url(b)


def test_normalize_url_strips_trailing_slash_and_fragment() -> None:
    a = "https://www.youtube.com/watch?v=abc123"
    b = "https://www.youtube.com/watch/?v=abc123#t=10s"
    assert normalize_url(a) == normalize_url(b)


def test_normalize_url_strips_tiktok_web_share_params() -> None:
    url = "https://www.tiktok.com/@creator/video/7678926593120587038"
    shared = f"{url}?is_from_webapp=1&sender_device=pc"

    assert normalize_url(shared) == normalize_url(url)


def test_normalize_url_strips_instagram_web_share_params() -> None:
    url = "https://www.instagram.com/reel/DcTtB6sT3p_/"
    shared = f"{url}?utm_source=ig_web_copy_link&igsi=NTc4MTIwNjQ2YQ=="

    assert normalize_url(shared) == normalize_url(url)


def test_normalize_url_preserves_content_selectors() -> None:
    instagram = "https://www.instagram.com/p/Example/"
    assert normalize_url(f"{instagram}?img_index=1") != normalize_url(
        f"{instagram}?img_index=2"
    )
    assert normalize_url("https://www.youtube.com/watch?v=first") != normalize_url(
        "https://www.youtube.com/watch?v=second"
    )
