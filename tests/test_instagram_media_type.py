import json

import httpx
import pytest
import respx

from src.card_metadata import merge_metadata
from src.config import Settings
from src.dispatch import Dispatcher
from src.parsers.base import LinkMetadata, MediaType
from src.parsers.og_meta import OGMetaParser
from src.parsers.social_page import parse_page_metadata

_REEL = "https://www.instagram.com/reel/DcTtB6sT3p_/"
_COVER = "https://cdn.example/reel-poster.jpg"
_POST = {
    "shortcode": "DcTtB6sT3p_",
    "caption": {"text": "A real Reel caption"},
    "display_url": _COVER,
}


@pytest.mark.parametrize("payload", [
    _POST,
    {
        "@type": "SocialMediaPosting",
        "url": _REEL,
        "articleBody": "A real Reel caption",
        "image": _COVER,
    },
])
def test_public_reel_weak_page_type_preserves_video_for_media_preparation(payload: dict) -> None:
    html = f'<script type="application/ld+json">{json.dumps(payload)}</script>'
    parsed = parse_page_metadata(_REEL, html)
    metadata = LinkMetadata(source_url=_REEL, platform="instagram", media_type=MediaType.VIDEO)

    merge_metadata(metadata, parsed)

    assert parsed.media_type == MediaType.UNKNOWN
    assert parsed.content_verified is True
    assert metadata.media_type == MediaType.VIDEO
    assert metadata.description == "A real Reel caption"
    assert metadata.cover_url == _COVER


@pytest.mark.parametrize("fields,expected", [
    ({"is_video": True}, MediaType.VIDEO),
    ({"media_type": 2}, MediaType.VIDEO),
    ({"__typename": "GraphVideo"}, MediaType.VIDEO),
    ({"is_video": False}, MediaType.ARTICLE),
    ({"media_type": 1}, MediaType.ARTICLE),
    ({"media_type": 8}, MediaType.ARTICLE),
    ({"__typename": "GraphImage"}, MediaType.ARTICLE),
    ({"__typename": "GraphSidecar"}, MediaType.ARTICLE),
])
def test_explicit_instagram_media_type_overrides_reel_url_guess(
    fields: dict, expected: MediaType,
) -> None:
    parsed = parse_page_metadata(_REEL, "", payloads=[{**_POST, **fields}])
    metadata = LinkMetadata(source_url=_REEL, platform="instagram", media_type=MediaType.VIDEO)

    merge_metadata(metadata, parsed)

    assert parsed.media_type == expected
    assert metadata.media_type == expected


@pytest.mark.parametrize("schema,expected", [
    ("VideoObject", MediaType.VIDEO),
    ("ImageObject", MediaType.ARTICLE),
])
@pytest.mark.parametrize("native_first", [True, False])
def test_weak_instagram_source_does_not_erase_explicit_jsonld_type(
    schema: str, expected: MediaType, native_first: bool,
) -> None:
    known = {"@type": schema, "url": _REEL, "description": "Post", "image": _COVER}
    weak = {"@type": "SocialMediaPosting", "url": _REEL, "description": "Post"}
    payloads = [_POST, known, weak] if native_first else [known, weak, _POST]

    assert parse_page_metadata(_REEL, "", payloads=payloads).media_type == expected


@pytest.mark.parametrize("schema", ["api", "graphql"])
@pytest.mark.parametrize("index,expected", [(1, MediaType.ARTICLE), (2, MediaType.VIDEO)])
def test_instagram_selected_carousel_child_controls_media_type(
    schema: str, index: int, expected: MediaType,
) -> None:
    children = [
        {"is_video": False, "display_url": "https://cdn.example/photo.jpg"},
        {"is_video": True, "display_url": "https://cdn.example/video-poster.jpg"},
    ]
    parent = {**_POST, "media_type": 8, "is_video": False}
    if schema == "api":
        parent["carousel_media"] = children
    else:
        parent["edge_sidecar_to_children"] = {"edges": [{"node": child} for child in children]}
    # The whole-post image schema is less specific than the selected child.
    poster = {"@type": "ImageObject", "url": _REEL, "image": _COVER}

    parsed = parse_page_metadata(_REEL + f"?img_index={index}", "", payloads=[parent, poster])

    assert parsed.media_type == expected
    assert parsed.cover_url == children[index - 1]["display_url"]


def test_instagram_post_url_without_media_evidence_stays_unknown() -> None:
    parsed = parse_page_metadata(_REEL.replace("/reel/", "/p/"), "", payloads=[_POST])

    assert parsed.media_type == MediaType.UNKNOWN


def test_native_instagram_photo_survives_post_level_video_schema() -> None:
    poster = {"@type": "VideoObject", "url": _REEL, "thumbnailUrl": _COVER}

    parsed = parse_page_metadata(_REEL, "", payloads=[{**_POST, "media_type": 1}, poster])

    assert parsed.media_type == MediaType.ARTICLE


@pytest.mark.parametrize("og_type", ["", "article", "video.other"])
@pytest.mark.parametrize("structured_type", ["shortcode", "SocialMediaPosting"])
@pytest.mark.asyncio
@respx.mock
async def test_public_og_reel_survives_full_dispatcher_merge(
    og_type: str, structured_type: str,
) -> None:
    payload = _POST if structured_type == "shortcode" else {
        "@type": "SocialMediaPosting", "url": _REEL,
        "articleBody": "A real Reel caption", "image": _COVER,
    }
    html = f'<script type="application/ld+json">{json.dumps(payload)}</script>'
    if og_type:
        html += f'<meta property="og:type" content="{og_type}">'
    respx.get(_REEL).mock(return_value=httpx.Response(200, text=html))
    async with httpx.AsyncClient() as client:
        dispatcher = Dispatcher(Settings(), client)
        parser = OGMetaParser(client)
        dispatcher._card_sources = lambda url, platform: [  # type: ignore[method-assign]
            ("instagram_public_page", parser.parse_public, False)
        ]

        parsed = await dispatcher.parse_card(_REEL)

    assert parsed.has_content
    assert parsed.metadata.media_type == MediaType.VIDEO
    assert parsed.metadata.description == "A real Reel caption"
    assert parsed.metadata.cover_url == _COVER


@pytest.mark.parametrize("fields", [
    {"media_type": 1},
    {"is_video": False},
    {"carousel_media": [{"is_video": False, "display_url": _COVER}]},
])
@pytest.mark.asyncio
@respx.mock
async def test_public_og_video_poster_cannot_override_native_photo(fields: dict) -> None:
    html = f'<script type="application/json">{json.dumps({**_POST, **fields})}</script>'
    html += '<meta property="og:type" content="video.other">'
    respx.get(_REEL).mock(return_value=httpx.Response(200, text=html))
    async with httpx.AsyncClient() as client:
        parser = OGMetaParser(client)
        parsed = await parser.parse_public(_REEL)

    metadata = LinkMetadata(source_url=_REEL, platform="instagram", media_type=MediaType.VIDEO)
    merge_metadata(metadata, parsed)
    assert parsed.media_type == MediaType.ARTICLE
    assert metadata.media_type == MediaType.ARTICLE
