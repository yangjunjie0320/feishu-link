import json
from pathlib import Path
from urllib.parse import quote

import pytest

from src.card_metadata import card_result
from src.parsers.base import CardStatus, MediaType, ParserError
from src.parsers.social_page import page_identity, parse_page_metadata

_TT = "https://www.tiktok.com/@writer/video/1234567890"
_DY = "https://www.douyin.com/video/9876543210"
_COVER = "https://p16.tiktokcdn.com/opaque-image?x-expires=123&signature=abc"


def _script(value: object, *, script_id: str = "__UNIVERSAL_DATA_FOR_REHYDRATION__") -> str:
    return f'<script id="{script_id}" type="application/json">{json.dumps(value)}</script>'


def test_tiktok_matches_target_among_recommendations_and_preserves_full_caption() -> None:
    target = {
        "id": "1234567890",
        "desc": "Real caption with examples " * 30,
        "author": {"nickname": "Writer"},
        "video": {"duration": 37, "originCover": _COVER},
        "stats": {"playCount": 0, "diggCount": 17, "commentCount": 5, "shareCount": 2},
    }
    html = _script(
        {
            "recommendations": [{**target, "id": "999", "desc": "Wrong post"}],
            "__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": target}}},
        }
    )

    meta = parse_page_metadata(_TT, html)

    assert meta.title == meta.description == target["desc"].strip()
    assert len(meta.description) > 300
    assert meta.channel == "Writer"
    assert meta.cover_url == _COVER
    assert meta.cover_candidates == [_COVER]
    assert meta.duration_seconds == 37
    assert meta.view_count == 0
    assert meta.like_count == 17
    assert meta.content_verified is True
    assert meta.has_visual is True
    assert meta.download_candidates == []


def test_tiktok_photo_uses_photo_images_not_music_duration_or_video() -> None:
    url = _TT.replace("/video/", "/photo/")
    payload = {
        "id": "1234567890",
        "desc": "A visual story",
        "imagePost": {
            "images": [
                {"displayImage": {"urlList": [_COVER]}},
                {"displayImage": {"urlList": ["https://cdn.example/second"]}},
            ]
        },
        "music": {"duration": 90},
    }

    meta = parse_page_metadata(url, "", payloads=[payload])

    assert meta.media_type == MediaType.ARTICLE
    assert meta.duration_seconds is None
    assert meta.cover_candidates == [_COVER]
    assert meta.canonical_url == url


def test_tiktok_old_sigi_keyed_item_module_matches_target() -> None:
    html = _script(
        {"ItemModule": {"1234567890": {"desc": "Old schema caption", "video": {"cover": _COVER}}}},
        script_id="SIGI_STATE",
    )
    assert parse_page_metadata(_TT, html).title == "Old schema caption"


@pytest.mark.parametrize(
    "url",
    [
        "https://v.douyin.com/AbCd/",
        "https://www.iesdouyin.com/share/video/9876543210/",
    ],
)
def test_douyin_short_and_share_urls_resolve_to_canonical_post(url: str) -> None:
    payload = {
        "aweme_detail": {
            "aweme_id": "9876543210",
            "desc": "原始文案",
            "author": {"nickname": "作者"},
            "video": {"duration": 31000, "cover": {"url_list": [_COVER]}},
            "statistics": {"digg_count": 0, "comment_count": 19, "share_count": 3},
        }
    }

    meta = parse_page_metadata(url, "", final_url=_DY + "?share_source=copy", payloads=[payload])

    assert meta.platform == "douyin"
    assert meta.canonical_url == _DY
    assert meta.title == "原始文案"
    assert meta.duration_seconds == 31
    assert meta.like_count == 0


def test_douyin_encoded_render_data_photo_keeps_first_real_image() -> None:
    data = {
        "aweme": {"aweme_id": "9876543210", "desc": "图文内容", "images": [{"url_list": [_COVER]}]}
    }
    html = f'<script id="RENDER_DATA">{quote(json.dumps(data))}</script>'
    meta = parse_page_metadata(_DY.replace("/video/", "/note/"), html)
    assert meta.media_type == MediaType.ARTICLE
    assert meta.cover_url == _COVER


def test_douyin_router_data_assignment_is_parsed_without_javascript_evaluation() -> None:
    data = {
        "aweme": {
            "aweme_id": "9876543210",
            "desc": "路由数据",
            "video": {"cover": {"url_list": [_COVER]}},
        }
    }
    html = f"<script>window._ROUTER_DATA = {json.dumps(data)}; forbiddenSideEffect();</script>"
    assert parse_page_metadata(_DY, html).title == "路由数据"


@pytest.mark.parametrize("split_record", [False, True])
def test_douyin_note_flight_stream_matches_real_target_among_other_records(
    split_record: bool,
) -> None:
    # Observed on 2026-09-06: note pages place their aweme in self.__pace_f,
    # while RENDER_DATA holds app settings and no card fields.
    target = {
        "aweme_id": "7658101922843080169",
        "desc": "真实图文正文\n第二段说明",
        "aweme_type": 68,
        "images": [{"url_list": [_COVER]}, {"url_list": ["https://cdn.example/photo2"]}],
        "author": {"nickname": "Fixture author"},
    }
    record = (
        "7:"
        + json.dumps(
            [
                "$",
                "$L9",
                None,
                {
                    "awemeId": target["aweme_id"],
                    "aweme": {"status_code": 0, "aweme_detail": target},
                },
            ]
        )
        + "\n"
    )
    chunks = [record[: len(record) // 2], record[len(record) // 2 :]] if split_record else [record]
    unrelated = {**target, "aweme_id": "999", "desc": "推荐图文不能覆盖原作品"}
    # Real note pages place an opaque Flight text record immediately before the aweme.
    chunks.insert(0, "5:T6,opaque")
    chunks.insert(0, "6:" + json.dumps(unrelated) + "\n")
    html = _script({"app": {"settings": {}}}, script_id="RENDER_DATA")
    html += "".join(
        f"<script>self.__pace_f.push({json.dumps([1, chunk])});</script>" for chunk in chunks
    )
    html += "<script>self.__pace_f.push(doNotExecute());</script>"

    meta = parse_page_metadata("https://www.douyin.com/note/7658101922843080169", html)

    assert meta.title == target["desc"]
    assert meta.channel == "Fixture author"
    assert meta.media_type == MediaType.ARTICLE
    assert meta.cover_candidates == [_COVER]


@pytest.mark.parametrize("platform", ["douyin", "tiktok"])
def test_social_photo_cover_candidates_only_include_first_image_variants(platform: str) -> None:
    first_variants = [_COVER, "https://cdn.example/first-other-cdn"]
    images = [{"url_list": first_variants}, {"url_list": ["https://cdn.example/second"]}]
    data = {"desc": "Photo caption", "video": {"cover": "https://cdn.example/music-cover"}}
    if platform == "douyin":
        url = _DY.replace("/video/", "/note/")
        data.update(aweme_id="9876543210", images=images)
    else:
        url = _TT.replace("/video/", "/photo/")
        data.update(id="1234567890", imagePost={"images": images})

    html = '<meta property="og:image" content="https://cdn.example/unrelated-og-poster">'
    poster = {
        "@type": "VideoObject",
        "url": url,
        "name": "Music video",
        "thumbnailUrl": "https://cdn.example/video-poster",
    }
    meta = parse_page_metadata(url, html, payloads=[data, poster])

    assert meta.cover_candidates == first_variants
    assert meta.media_type == MediaType.ARTICLE
    assert meta.content_verified is True


def test_photo_with_missing_first_image_does_not_use_second_or_og() -> None:
    data = {
        "aweme_id": "9876543210",
        "desc": "Photo caption",
        "images": [
            {},
            {"url_list": ["https://cdn.example/second"]},
        ],
    }
    html = '<meta property="og:image" content="https://cdn.example/og-poster">'

    meta = parse_page_metadata(_DY, html, payloads=[data])

    assert meta.cover_candidates == []
    assert meta.cover_url == ""
    assert meta.media_type == MediaType.ARTICLE


def test_douyin_note_flight_filter_detail_is_not_content() -> None:
    record = (
        "7:"
        + json.dumps(
            [
                "$",
                "$L9",
                None,
                {
                    "awemeId": "9876543210",
                    "aweme": {
                        "aweme_detail": None,
                        "filter_detail": {"aweme_id": "9876543210", "detail_msg": "作品暂不可用"},
                    },
                },
            ]
        )
        + "\n"
    )
    html = f"<script>self.__pace_f.push({json.dumps([1, record])});</script>"

    with pytest.raises(ParserError, match="no_content"):
        parse_page_metadata(_DY.replace("/video/", "/note/"), html)


def test_real_douyin_note_flight_fixture_keeps_camel_case_card_fields() -> None:
    # Captured anonymously on 2026-09-06; identifiers, caption, author, and image URLs
    # were replaced before retaining the fixture. It contains no session/page state.
    html = (Path(__file__).parent / "fixtures" / "douyin-note-flight.html").read_text()
    url = _DY.replace("/video/", "/note/")

    meta = parse_page_metadata(url, html)

    assert meta.title == "Fixture photo caption\nSecond paragraph"
    assert meta.channel == "Fixture author"
    assert meta.media_type == MediaType.ARTICLE
    assert meta.cover_candidates == [
        "https://images.example/slide-1.jpg?cdn=1",
        "https://images.example/slide-1.jpg?cdn=2",
        "https://images.example/slide-1.jpg?cdn=3",
    ]
    assert meta.canonical_url == url
    assert meta.content_verified is True


def test_instagram_carousel_uses_matching_shortcode_and_full_caption() -> None:
    url = "https://www.instagram.com/p/ABC_def/"
    payload = {
        "items": [
            {"code": "Other", "caption": {"text": "recommendation"}},
            {
                "code": "ABC_def",
                "caption": {"text": "A full caption"},
                "user": {"username": "writer"},
                "media_type": 8,
                "carousel_media": [{"image_versions2": {"candidates": [{"url": _COVER}]}}],
            },
        ]
    }
    meta = parse_page_metadata(url, "", payloads=[payload])
    assert meta.description == "A full caption"
    assert meta.channel == "writer"
    assert meta.cover_url == _COVER
    assert meta.media_type == MediaType.ARTICLE


def test_youtube_player_assignment_only_accepts_requested_video() -> None:
    url = "https://youtu.be/AbCdEfG1234"
    details = {
        "videoId": "AbCdEfG1234",
        "title": "Real video",
        "shortDescription": "Details",
        "author": "Channel",
        "lengthSeconds": "80",
        "viewCount": "0",
        "thumbnail": {"thumbnails": [{"url": _COVER}]},
    }
    html = (
        f"<script>var ytInitialPlayerResponse = {json.dumps({'videoDetails': details})};</script>"
    )
    meta = parse_page_metadata(url, html, final_url="https://www.youtube.com/watch?v=AbCdEfG1234")
    assert meta.title == "Real video"
    assert meta.duration_seconds == 80
    assert meta.view_count == 0
    assert meta.cover_url == _COVER


def test_x_target_tweet_prefers_long_note_and_confirms_text_only() -> None:
    url = "https://x.com/writer/status/12345"
    payload = {
        "data": {
            "tweetResult": {
                "result": {
                    "rest_id": "12345",
                    "legacy": {"full_text": "Short", "entities": {}, "favorite_count": 0},
                    "note_tweet": {
                        "note_tweet_results": {"result": {"text": "Complete long post"}}
                    },
                    "core": {"user_results": {"result": {"legacy": {"screen_name": "writer"}}}},
                }
            }
        }
    }
    meta = parse_page_metadata(url, "", payloads=[payload])
    assert meta.description == "Complete long post"
    assert meta.has_visual is False
    assert meta.media_type == MediaType.ARTICLE
    assert meta.like_count == 0


def test_x_dom_uses_permalink_time_to_exclude_replies() -> None:
    html = """
    <article data-testid="tweet"><a href="/other/status/999"><time>today</time></a>
      <div data-testid="tweetText">Other post</div></article>
    <article data-testid="tweet"><a href="/writer/status/12345"><time>today</time></a>
      <div data-testid="tweetText">Requested post</div>
      <div data-testid="tweetPhoto"><img src="https://cdn.example/picture"></div></article>
    """
    meta = parse_page_metadata("https://x.com/writer/status/12345", html)
    assert meta.description == "Requested post"
    assert meta.cover_url == "https://cdn.example/picture"


@pytest.mark.parametrize(
    "final_url,html,reason",
    [
        ("https://www.tiktok.com/login", "", "auth:"),
        (_TT, '<div id="captcha-verify-container"></div>', "challenge:"),
        (_TT.replace("1234567890", "999"), "", "target_mismatch"),
        ("https://example.com/video/1234567890", "", "target_mismatch"),
    ],
)
def test_login_challenge_and_redirected_content_are_explicit_failures(
    final_url: str, html: str, reason: str
) -> None:
    with pytest.raises(ParserError, match=reason):
        parse_page_metadata(_TT, html, final_url=final_url)


@pytest.mark.parametrize(
    "html",
    [
        '<title>TikTok - Make Your Day</title><meta property="og:title" content="tiktok.com">',
        _script({"id": "999", "desc": "A recommended post", "video": {"cover": _COVER}}),
        '<meta property="og:url" content="https://www.tiktok.com/@other/video/999">'
        '<meta property="og:title" content="A recommended post">',
        '<script type="application/ld+json">{"@type":["VideoObject"],"name":"Unmatched"}</script>',
    ],
)
def test_placeholder_or_recommendation_is_not_success(html: str) -> None:
    with pytest.raises(ParserError, match=r"no verified target content|target_mismatch"):
        parse_page_metadata(_TT, html)


def test_exact_target_jsonld_can_supply_caption_and_opaque_cover_url() -> None:
    html = _script(
        {
            "@type": "VideoObject",
            "url": _TT,
            "name": "Target video",
            "description": "Real description",
            "thumbnailUrl": [_COVER],
        }
    )
    meta = parse_page_metadata(_TT, html)
    assert meta.title == "Target video"
    assert meta.cover_url == _COVER


def test_profile_url_does_not_accept_an_arbitrary_recommended_video() -> None:
    with pytest.raises(ParserError, match="unsupported"):
        parse_page_metadata(
            "https://www.tiktok.com/@writer",
            _script({"id": "1234567890", "desc": "Recommendation", "video": {"cover": _COVER}}),
        )


def test_tiktok_short_link_requires_a_resolved_post_identity() -> None:
    short = "https://vm.tiktok.com/Short/"
    assert page_identity(short) == ("tiktok", "", "")
    html = _script({"id": "1234567890", "desc": "Resolved", "video": {"cover": _COVER}})
    assert parse_page_metadata(short, html, final_url=_TT).canonical_url == _TT


def test_recommended_dom_article_does_not_replace_target_structured_caption() -> None:
    html = _script({"id": "1234567890", "desc": "Target", "video": {"cover": _COVER}})
    html += '<article><a href="/other/video/999">other</a>'
    html += '<div data-e2e="video-desc">Much longer recommendation text</div></article>'
    assert parse_page_metadata(_TT, html).title == "Target"


def test_unrelated_dom_article_is_not_accepted_without_target_data() -> None:
    html = '<article><a href="/other/video/999">other</a>'
    html += '<div data-e2e="video-desc">Recommendation text</div></article>'
    with pytest.raises(ParserError, match="no_content"):
        parse_page_metadata(_TT, html)


def test_verified_native_photo_without_caption_can_return_its_real_image() -> None:
    payload = {
        "id": "1234567890",
        "desc": "",
        "imagePost": {"images": [{"displayImage": {"urlList": [_COVER]}}]},
    }
    meta = parse_page_metadata(_TT.replace("/video/", "/photo/"), "", payloads=[payload])
    assert meta.title == ""
    assert meta.cover_url == _COVER
    assert meta.content_verified is True
    assert meta.media_type == MediaType.ARTICLE


def test_generic_og_logo_alone_does_not_prove_a_native_photo() -> None:
    html = f'<meta property="og:image" content="{_COVER}">'
    with pytest.raises(ParserError, match="no_content"):
        parse_page_metadata(_TT, html)


@pytest.mark.parametrize("schema", ["api", "graphql"])
def test_instagram_selected_carousel_image_and_canonical_query_are_preserved(schema: str) -> None:
    url = "https://www.instagram.com/p/ABC_def/?img_index=2&utm_source=share"
    first = {"display_url": "https://cdn.example/first"}
    second = {
        "image_versions2": {
            "candidates": [
                {"url": "https://cdn.example/selected"},
                {"url": "https://cdn.example/selected-small"},
            ]
        }
    }
    payload = {"code": "ABC_def", "caption": {"text": "Carousel caption"}}
    if schema == "api":
        payload["carousel_media"] = [first, second]
    else:
        payload["edge_sidecar_to_children"] = {"edges": [{"node": first}, {"node": second}]}
    html = '<meta property="og:image" content="https://cdn.example/default">' + _script(
        {"@type": "ImageObject", "url": url, "image": "https://cdn.example/default"}
    )
    meta = parse_page_metadata(url, html, payloads=[payload])
    assert meta.canonical_url == "https://www.instagram.com/p/ABC_def?img_index=2"
    assert meta.cover_candidates == [
        "https://cdn.example/selected",
        "https://cdn.example/selected-small",
    ]


@pytest.mark.parametrize(
    "children",
    [
        {},
        {"carousel_media": [{"display_url": _COVER}]},
        {"edge_sidecar_to_children": {"edges": [{"node": {"display_url": _COVER}}]}},
    ],
)
def test_instagram_unverified_or_out_of_range_index_keeps_text_without_first_image(
    children: dict,
) -> None:
    url = "https://www.instagram.com/p/ABC_def/?img_index=2"
    payload = {
        "code": "ABC_def",
        "caption": {"text": "Carousel caption"},
        "user": {"username": "writer"},
        "display_url": _COVER,
        **children,
    }
    html = f'<meta property="og:image" content="{_COVER}">' + _script(
        {"@type": "ImageObject", "url": url, "image": _COVER}
    )
    meta = parse_page_metadata(url, html, payloads=[payload])

    assert meta.description == "Carousel caption"
    assert meta.channel == "writer"
    assert meta.cover_url == ""
    assert meta.cover_candidates == []
    assert card_result(meta).status == CardStatus.PARTIAL


def test_non_social_platform_reports_unsupported_so_existing_og_can_continue() -> None:
    with pytest.raises(ParserError, match="unsupported:"):
        parse_page_metadata(
            "https://www.bilibili.com/video/BV1example", '<meta property="og:title" content="Bili">'
        )
