from src.image_uploader import _normalize_cover_url


def test_normalize_cover_url_accepts_protocol_relative_urls() -> None:
    url = "//i0.hdslb.com/bfs/archive/example.jpg@100w_100h_1c.png"

    assert _normalize_cover_url(url) == (
        "https://i0.hdslb.com/bfs/archive/example.jpg@100w_100h_1c.png"
    )


def test_normalize_cover_url_strips_whitespace() -> None:
    assert _normalize_cover_url("  https://example.com/cover.jpg  ") == (
        "https://example.com/cover.jpg"
    )


def test_normalize_cover_url_returns_empty_for_blank_url() -> None:
    assert _normalize_cover_url("   ") == ""
