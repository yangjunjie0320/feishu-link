from __future__ import annotations

import copy
import json

import pytest

from src.parsers.base import MediaType
from src.parsers.social_page import parse_page_metadata
from src.parsers.youtube_page_metrics import public_count

_YT_ID = "zqi5t1xvfL8"
_YT_URL = f"https://www.youtube.com/watch?v={_YT_ID}"
_TT_ID = "1234567890"
_TT_URL = f"https://www.tiktok.com/@writer/video/{_TT_ID}"


def _watch_payload() -> dict:
    # Public anonymous watch page observed 2026-09-08 (Beijing). Only counters
    # and their structural keys are retained; session/tracking fields are omitted.
    return {
        "currentVideoEndpoint": {"watchEndpoint": {"videoId": _YT_ID}},
        "contents": {"twoColumnWatchNextResults": {"results": {"results": {"contents": [{
            "videoPrimaryInfoRenderer": {
                "viewCount": {"videoViewCountRenderer": {
                    "viewCount": {"simpleText": "687,116 views"},
                    "originalViewCount": "687116",
                }},
                "videoActions": {"menuRenderer": {"topLevelButtons": [{
                    "segmentedLikeDislikeButtonViewModel": {"likeButtonViewModel": {
                        "likeButtonViewModel": {"toggleButtonViewModel": {
                            "toggleButtonViewModel": {"defaultButtonViewModel": {
                                "buttonViewModel": {
                                    "title": "8K",
                                    "accessibilityText": (
                                        "like this video along with 8,041 other people"
                                    ),
                                },
                            }},
                        }},
                    }},
                }]}},
            },
        }]}}}},
        "engagementPanels": [{"engagementPanelSectionListRenderer": {
            "panelIdentifier": "engagement-panel-comments-section",
            "header": {"engagementPanelTitleHeaderRenderer": {
                "title": {"runs": [{"text": "Comments"}]},
                "contextualInfo": {"runs": [{"text": "921"}]},
            }},
        }}],
    }


def _primary(payload: dict) -> dict:
    return payload["contents"]["twoColumnWatchNextResults"]["results"]["results"]["contents"][0][
        "videoPrimaryInfoRenderer"
    ]


def _html(payload: dict) -> str:
    player = {"videoDetails": {
        "videoId": _YT_ID, "title": "Target video", "viewCount": "687116",
        "thumbnail": {"thumbnails": [{"url": "https://images.example/cover"}]},
    }}
    return (
        f"<script>var ytInitialPlayerResponse = {json.dumps(player)};</script>"
        f"<script>var ytInitialData = {json.dumps(payload)};</script>"
    )


def test_youtube_public_watch_payload_supplies_all_counts_without_api_key() -> None:
    meta = parse_page_metadata(_YT_URL, _html(_watch_payload()))
    assert (meta.view_count, meta.like_count, meta.comment_count) == (687116, 8041, 921)
    assert meta.download_candidates == []


@pytest.mark.parametrize("wrong_id", ["OtherVideo1", None])
def test_youtube_metrics_require_this_payloads_own_matching_identity(wrong_id: str | None) -> None:
    payload = _watch_payload()
    payload["currentVideoEndpoint"]["watchEndpoint"]["videoId"] = wrong_id
    meta = parse_page_metadata(_YT_URL, _html(payload))
    assert meta.view_count == 687116
    assert meta.like_count is None
    assert meta.comment_count is None


def test_youtube_recommendations_and_unrelated_panels_are_never_counts() -> None:
    payload = _watch_payload()
    columns = payload["contents"]["twoColumnWatchNextResults"]
    columns["secondaryResults"] = copy.deepcopy(columns["results"])
    columns["results"] = {}
    payload["engagementPanels"][0]["engagementPanelSectionListRenderer"]["panelIdentifier"] = "ads"
    meta = parse_page_metadata(_YT_URL, _html(payload))
    assert meta.like_count is None
    assert meta.comment_count is None


def test_youtube_metrics_preserve_previously_obtained_real_zero() -> None:
    html = _html(_watch_payload()).replace('"viewCount": "687116"', '"viewCount": "0"', 1)
    later = _watch_payload()
    meta = parse_page_metadata(_YT_URL, html, payloads=[later])
    assert meta.view_count == 0
    assert meta.like_count == 8041


def test_youtube_old_like_button_and_comments_entry_point_zero() -> None:
    payload = _watch_payload()
    _primary(payload)["videoActions"]["menuRenderer"]["topLevelButtons"] = [
        {"toggleButtonRenderer": {
            "defaultIcon": {"iconType": "DISLIKE"},
            "defaultText": {"simpleText": "999"},
        }},
        {"segmentedLikeDislikeButtonRenderer": {"likeButton": {"toggleButtonRenderer": {
            "defaultText": {
                "simpleText": "0",
                "accessibility": {"accessibilityData": {"label": "0 likes"}},
            },
        }}}},
    ]
    payload["contents"]["twoColumnWatchNextResults"]["results"]["results"]["contents"].append({
        "itemSectionRenderer": {"contents": [{"commentsEntryPointHeaderRenderer": {
            "commentCount": {"simpleText": "0 Comments"},
        }}]},
    })
    meta = parse_page_metadata(_YT_URL, _html(payload))
    assert meta.like_count == meta.comment_count == 0


def test_youtube_live_concurrent_viewers_are_not_play_count() -> None:
    payload = _watch_payload()
    _primary(payload)["viewCount"]["videoViewCountRenderer"]["isLive"] = True
    html = _html(payload).replace(', "viewCount": "687116"', "", 1)
    meta = parse_page_metadata(_YT_URL, html)
    assert meta.view_count is None


def test_youtube_malformed_metric_shapes_preserve_card_content() -> None:
    payload = _watch_payload()
    _primary(payload)["videoActions"] = [1, None]
    payload["engagementPanels"] = [
        None, {"engagementPanelSectionListRenderer": "invalid"},
        {"engagementPanelSectionListRenderer": {"panelIdentifier": []}},
    ]
    meta = parse_page_metadata(_YT_URL, _html(payload))
    assert meta.title == "Target video"
    assert meta.like_count is None
    assert meta.comment_count is None


@pytest.mark.parametrize(
    "stats,stats_v2,expected",
    [
        ({}, {"playCount": "1200", "diggCount": "14", "commentCount": "0", "shareCount": "3"},
         (1200, 14, 0, 3)),
        ({"playCount": 0, "diggCount": 0}, {"playCount": "5", "diggCount": "6"},
         (0, 0, None, None)),
        ({"playCount": "invalid", "diggCount": None}, {"playCount": "5", "diggCount": "6"},
         (5, 6, None, None)),
        (None, "invalid", (None, None, None, None)),
    ],
)
def test_tiktok_stats_v2_supplements_each_missing_field(
    stats: object, stats_v2: object, expected: tuple[int | None, ...],
) -> None:
    payload = {"id": _TT_ID, "desc": "Target caption", "stats": stats, "statsV2": stats_v2}
    recommendation = {**payload, "id": "999", "stats": {
        "playCount": 999, "diggCount": 999, "commentCount": 999, "shareCount": 999,
    }}
    meta = parse_page_metadata(_TT_URL, "", payloads=[recommendation, payload])
    assert (meta.view_count, meta.like_count, meta.comment_count, meta.repost_count) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, 0), ("0", 0), ("1,234 views", 1234), ("1\u202f234 mentions J’aime", 1234),
        ("1.234 Aufrufe", 1234), ("1.2K", 1200), ("1,2 M", 1_200_000), ("1.2万", 12000),
        ("8,041 other people", 8041), (True, None), (-1, None), ("-1", None), ("-1,000", None),
        ("likes hidden", None), ("1 like and 2 comments", None), ("1.2", None),
        (float("nan"), None), ({}, None),
    ],
)
def test_public_counts_keep_real_zero_and_reject_unknown(
    value: object, expected: int | None,
) -> None:
    assert public_count(value) == expected


def test_tiktok_real_photo_image_url_retains_first_image_and_all_counts() -> None:
    url = 'https://www.tiktok.com/@creator/photo/7470966924462132486'
    first = 'https://cdn.example/first-image?signature=first&expires=123'
    alternate = 'https://cdn.example/first-image-alt?signature=alternate&expires=123'
    target = {
        'id': '7470966924462132486', 'desc': 'A photo story',
        'imagePost': {'images': [
            {'imageURL': {'urlList': [first, alternate]}},
            {'imageURL': {'urlList': ['https://cdn.example/second-image']}},
        ]},
        'stats': {'playCount': 264900, 'diggCount': 42900,
                  'commentCount': 232, 'shareCount': 2639},
    }
    meta = parse_page_metadata(url, '', payloads=[{'itemInfo': {'itemStruct': target}}])
    assert meta.cover_candidates == [first, alternate]
    assert meta.cover_url == first
    assert meta.media_type == MediaType.ARTICLE
    assert (meta.view_count, meta.like_count, meta.comment_count, meta.repost_count) == (
        264900, 42900, 232, 2639,
    )
