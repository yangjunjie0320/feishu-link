"""Read card fields from a specific post, never from a recommended item."""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from ..card_metadata import is_placeholder
from .base import LinkMetadata, MediaType, ParserError

_LABELS = {
    "tiktok": "TikTok",
    "douyin": "抖音",
    "instagram": "Instagram",
    "youtube": "YouTube",
    "x": "X",
}
_PLACEHOLDERS = {
    "tiktok",
    "tiktok.com",
    "tiktok - make your day",
    "make your day",
    "instagram",
    "instagram.com",
    "youtube",
    "youtube.com",
    "x",
    "x.com",
    "twitter",
    "twitter.com",
    "douyin.com",
    "抖音",
    "抖音-记录美好生活",
    "抖音 - 记录美好生活",
    "记录美好生活",
    "login",
    "log in",
    "sign in",
    "登录",
    "安全验证",
    "security check",
}


def page_identity(url: str) -> tuple[str, str, str]:
    """Return platform, exact content ID and post kind; profiles have no ID."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path
    platform, content_id, kind = "", "", ""
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        platform = "tiktok"
        match = re.search(r"/(video|photo)/(\d+)(?:/|$)", path)
        if match:
            kind, content_id = match.groups()
    elif any(host == h or host.endswith(f".{h}") for h in ("douyin.com", "iesdouyin.com")):
        platform = "douyin"
        match = re.search(r"/(video|note|slides)/(\d+)(?:/|$)", path)
        if match:
            kind, content_id = match.groups()
        elif re.fullmatch(r"\d+", parse_qs(parsed.query).get("modal_id", [""])[0]):
            content_id = parse_qs(parsed.query)["modal_id"][0]
            kind = "video"
    elif host == "instagram.com" or host.endswith(".instagram.com"):
        platform = "instagram"
        match = re.search(r"/(p|reel|reels)/([\w-]+)(?:/|$)", path)
        if match:
            kind, content_id = match.groups()
    elif host in {"youtu.be", "youtube.com"} or host.endswith(".youtube.com"):
        platform, kind = "youtube", "video"
        if host == "youtu.be":
            content_id = path.strip("/").split("/")[0]
        else:
            match = re.search(r"/(?:shorts|embed|live)/([\w-]+)(?:/|$)", path)
            content_id = match.group(1) if match else parse_qs(parsed.query).get("v", [""])[0]
    elif host in {"x.com", "twitter.com"} or host.endswith(".twitter.com"):
        platform, kind = "x", "post"
        match = re.search(r"/status/(\d+)(?:/|$)", path)
        if match:
            content_id = match.group(1)
    return platform, content_id, kind


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _usable(value: Any) -> str:
    text = _text(value)
    return "" if " ".join(text.lower().split()) in _PLACEHOLDERS else text


def _at(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _number(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def _image_urls(value: Any) -> list[str]:
    """Only call on known image fields; CDN images need not have an extension."""
    found: list[str] = []
    stack = [value]
    while stack and len(found) < 24:
        item = stack.pop()
        if isinstance(item, str):
            candidate = "https:" + item if item.startswith("//") else item
            if urlsplit(candidate).scheme in {"http", "https"} and candidate not in found:
                found.append(candidate)
        elif isinstance(item, list):
            stack.extend(reversed(item[:24]))
        elif isinstance(item, dict):
            for key in reversed(
                (
                    "url",
                    "url_list",
                    "urlList",
                    "displayImage",
                    "thumbnail",
                    "thumbnails",
                    "candidates",
                    "image_versions2",
                )
            ):
                if key in item:
                    stack.append(item[key])
    return found


def _walk(value: Any) -> Iterator[tuple[str, dict[str, Any]]]:
    stack: list[tuple[str, Any]] = [("", value)]
    visited = 0
    while stack and visited < 30000:
        key, node = stack.pop()
        visited += 1
        if isinstance(node, dict):
            yield key, node
            stack.extend((str(k), v) for k, v in reversed(list(node.items())))
        elif isinstance(node, list):
            stack.extend(("", v) for v in reversed(node))


def _script_payloads(soup: BeautifulSoup) -> Iterator[Any]:
    decoder = json.JSONDecoder()
    flight_chunks: list[str] = []
    flight_size = 0
    for script in soup.find_all("script"):
        raw = script.string or script.get_text()
        if not raw or len(raw) > 6_000_000:
            continue
        if script.get("id") == "RENDER_DATA":
            raw = unquote(raw)
        if raw.lstrip().startswith(("{", "[")):
            with contextlib.suppress(ValueError):
                yield json.loads(raw)
        else:
            # Known assignments are JSON; never evaluate website JavaScript.
            for match in re.finditer(
                r"(?:ytInitialPlayerResponse|ytInitialData|_ROUTER_DATA)\s*=\s*", raw
            ):
                with contextlib.suppress(ValueError):
                    yield decoder.raw_decode(raw[match.end() :].lstrip())[0]
            # Current Douyin notes stream their aweme object in React Flight records.
            # Parse only JSON arguments to the observed transport, never execute JavaScript.
            for match in re.finditer(r"self\.__pace_f\.push\(\s*", raw):
                with contextlib.suppress(ValueError):
                    chunk = decoder.raw_decode(raw[match.end() :].lstrip())[0]
                    if (
                        isinstance(chunk, list)
                        and len(chunk) == 2
                        and chunk[0] == 1
                        and isinstance(chunk[1], str)
                        and flight_size + len(chunk[1]) <= 6_000_000
                    ):
                        flight_chunks.append(chunk[1])
                        flight_size += len(chunk[1])
    # JSON records can span pushes. Flight also emits length-prefixed text records
    # without a trailing newline, so retain push boundaries instead of only splitting lines.
    stream = "".join(flight_chunks)
    boundaries = {match.start() for match in re.finditer(r"(?m)^[0-9a-fA-F]+:", stream)}
    offset = 0
    for chunk in flight_chunks:
        boundaries.add(offset)
        offset += len(chunk)
    parsed_until = 0
    record_start = re.compile(r"[0-9a-fA-F]+:(?=[\[{])")
    for offset in sorted(boundaries):
        if offset < parsed_until:
            continue
        match = record_start.match(stream, offset)
        if match:
            with contextlib.suppress(ValueError):
                value, parsed_until = decoder.raw_decode(stream, match.end())
                yield value


def _base(url: str, final_url: str, platform: str, kind: str) -> LinkMetadata:
    canonical = final_url
    _, content_id, _ = page_identity(final_url)
    if platform == "douyin":
        kind = "note" if kind in {"note", "slides"} else "video"
        canonical = f"https://www.douyin.com/{kind}/{content_id}"
    elif platform == "youtube":
        canonical = f"https://www.youtube.com/watch?v={content_id}"
    else:
        p = urlsplit(final_url)
        path = re.sub(r"(/status/\d+)/(?:video|photo)/\d+.*$", r"\1", p.path)
        canonical = urlunsplit(("https", p.netloc, path.rstrip("/"), "", ""))
        if platform == "instagram" and (index := _image_index(url)) is not None:
            canonical += "?" + urlencode({"img_index": index})
    return LinkMetadata(
        source_url=url,
        canonical_url=canonical,
        platform=platform,
        site_name=_LABELS[platform],
        content_verified=True,
        media_type=MediaType.ARTICLE
        if kind in {"photo", "note", "slides", "p", "post"}
        else MediaType.VIDEO,
        has_visual=True if platform != "x" else None,
    )


def _image_index(url: str) -> int | None:
    raw = parse_qs(urlsplit(url).query).get("img_index", [""])[0]
    value = _number(raw)
    return value if value is not None and value > 0 else None


def _instagram_images(node: dict[str, Any], image_index: int | None) -> list[str]:
    children = node.get("carousel_media") or [
        _at(child, "node") for child in _at(node, "edge_sidecar_to_children", "edges") or []
    ]
    if isinstance(children, list) and children:
        index = image_index - 1 if image_index else 0
        if index >= len(children) or not isinstance(children[index], dict):
            return []
        node = children[index]
    elif image_index and image_index > 1:
        # A top-level thumbnail does not prove which carousel slide it shows.
        return []
    return _image_urls(
        [node.get("display_url"), node.get("thumbnail_src"), node.get("image_versions2")]
    )


def _structured_fields(
    node: dict[str, Any],
    key: str,
    platform: str,
    content_id: str,
    image_index: int | None = None,
) -> dict[str, Any] | None:
    if platform in {"tiktok", "douyin"}:
        candidate = str(node.get("aweme_id") or node.get("awemeId") or node.get("id") or key)
        if candidate != content_id or not any(k in node for k in ("desc", "video", "imagePost")):
            return None
        is_douyin = platform == "douyin"
        video = node.get("video") or {}
        author = node.get("author") or node.get("authorInfo") or {}
        stats = node.get("statistics") if is_douyin else node.get("stats")
        stats = stats if isinstance(stats, dict) else {}
        images = node.get("images") or _at(node, "imagePost", "images")
        if isinstance(images, list) and images:
            # A photo card represents its first image; CDN alternatives must refer to
            # that same image, never another slide or its background-music video.
            covers = _image_urls(images[0])
        else:
            covers = []
            for field in ("origin_cover", "cover", "originCover", "thumbnail", "dynamicCover"):
                covers.extend(_image_urls(_at(video, field)))
        duration = _number(_at(video, "duration"))
        return {
            "title": _usable(node.get("desc")),
            "description": _usable(node.get("desc")),
            "channel": _text(_at(author, "nickname")) or _text(_at(author, "uniqueId")) or None,
            "cover_candidates": covers,
            "duration_seconds": duration // 1000
            if is_douyin and duration is not None
            else duration,
            "media_type": MediaType.ARTICLE if images else MediaType.VIDEO,
            "view_count": _number(stats.get("play_count" if is_douyin else "playCount")),
            "like_count": _number(stats.get("digg_count" if is_douyin else "diggCount")),
            "comment_count": _number(stats.get("comment_count" if is_douyin else "commentCount")),
            "repost_count": _number(stats.get("share_count" if is_douyin else "shareCount")),
        }
    if platform == "instagram":
        if str(node.get("shortcode") or node.get("code") or "") != content_id:
            return None
        caption = _at(node, "caption", "text")
        edges = _at(node, "edge_media_to_caption", "edges") or []
        if not caption and edges:
            caption = _at(edges[0], "node", "text")
        covers = _instagram_images(node, image_index)
        return {
            "title": _usable(caption),
            "description": _usable(caption),
            "channel": _text(_at(node, "owner", "username"))
            or _text(_at(node, "user", "username"))
            or None,
            "cover_candidates": covers,
            "media_type": MediaType.VIDEO
            if node.get("is_video") or node.get("media_type") == 2
            else MediaType.ARTICLE,
            "like_count": _number(
                node.get("like_count")
                if "like_count" in node
                else _at(node, "edge_media_preview_like", "count")
            ),
            "comment_count": _number(
                node.get("comment_count")
                if "comment_count" in node
                else _at(node, "edge_media_to_comment", "count")
            ),
            "duration_seconds": _number(node.get("video_duration")),
        }
    if platform == "youtube":
        if str(node.get("videoId") or "") != content_id or not isinstance(node.get("title"), str):
            return None
        return {
            "title": _usable(node.get("title")),
            "description": _text(node.get("shortDescription")),
            "channel": _text(node.get("author")) or None,
            "duration_seconds": _number(node.get("lengthSeconds")),
            "view_count": _number(node.get("viewCount")),
            "cover_candidates": _image_urls(_at(node, "thumbnail", "thumbnails")),
            "media_type": MediaType.VIDEO,
        }
    if platform == "x":
        legacy = node.get("legacy") if isinstance(node.get("legacy"), dict) else node
        if str(node.get("rest_id") or legacy.get("id_str") or "") != content_id:
            return None
        caption = _at(node, "note_tweet", "note_tweet_results", "result", "text")
        caption = caption or legacy.get("full_text")
        if not caption:
            return None
        media = _at(legacy, "extended_entities", "media") or _at(legacy, "entities", "media") or []
        author = _at(node, "core", "user_results", "result", "legacy") or node.get("user") or {}
        return {
            "title": _usable(caption),
            "description": _usable(caption),
            "channel": _text(_at(author, "screen_name")) or _text(_at(author, "name")) or None,
            "cover_candidates": _image_urls([item.get("media_url_https") for item in media]),
            "has_visual": bool(media) if media or "entities" in legacy else None,
            "media_type": MediaType.VIDEO
            if any(item.get("type") == "video" for item in media)
            else MediaType.ARTICLE,
            "like_count": _number(legacy.get("favorite_count")),
            "comment_count": _number(legacy.get("reply_count")),
            "repost_count": _number(legacy.get("retweet_count")),
            "view_count": _number(_at(node, "views", "count")),
        }
    return None


def _jsonld_fields(node: dict[str, Any], platform: str, content_id: str) -> dict[str, Any] | None:
    if not isinstance(node.get("@type"), str) or node["@type"] not in {
        "VideoObject",
        "SocialMediaPosting",
        "ImageObject",
    }:
        return None
    identifiers = (
        node.get("url"),
        node.get("@id"),
        node.get("embedUrl"),
        node.get("mainEntityOfPage"),
    )
    if not any(
        isinstance(x, str) and page_identity(x)[:2] == (platform, content_id) for x in identifiers
    ):
        return None
    author = node.get("author") or {}
    return {
        "title": _usable(node.get("name") or node.get("headline")),
        "description": _usable(node.get("description") or node.get("articleBody")),
        "channel": _text(author if isinstance(author, str) else author.get("name")) or None,
        "cover_candidates": _image_urls([node.get("thumbnailUrl"), node.get("image")]),
        "media_type": MediaType.VIDEO if node.get("@type") == "VideoObject" else MediaType.ARTICLE,
    }


def _apply(meta: LinkMetadata, fields: dict[str, Any]) -> None:
    for key, value in fields.items():
        if key == "cover_candidates":
            meta.cover_candidates = list(dict.fromkeys([*meta.cover_candidates, *value]))[:12]
            meta.cover_url = next(iter(meta.cover_candidates), "")
        elif value is not None and value != "":
            if key in {"title", "description"} and len(str(value)) < len(getattr(meta, key)):
                continue
            setattr(meta, key, value)


def _page_wall(soup: BeautifulSoup, final_url: str) -> str:
    path = urlsplit(final_url).path.lower()
    if any(x in path for x in ("/login", "/accounts/login", "/i/flow/login", "/signin")):
        return "auth: login required"
    if any(x in path for x in ("/captcha", "/challenge", "/checkpoint")):
        return "challenge: verification required"
    if soup.select_one("#captcha-verify-container, .captcha_verify_container, #challenge-form"):
        return "challenge: verification required"
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    if title in {"security check", "安全验证", "captcha", "verify you are human"}:
        return "challenge: verification required"
    return ""


def _dom_fields(soup: BeautifulSoup, platform: str, content_id: str) -> dict[str, Any]:
    selectors = {
        "tiktok": (
            '[data-e2e="browse-video-desc"], [data-e2e="video-desc"], [data-e2e="photo-desc"]'
        ),
        "douyin": '[data-e2e="video-desc"], [data-e2e="note-desc"]',
        "instagram": "article h1",
        "youtube": "h1.ytd-watch-metadata yt-formatted-string, #title h1 yt-formatted-string",
        "x": '[data-testid="tweetText"]',
    }
    root: BeautifulSoup | Tag = soup
    if platform == "x":
        articles = []
        for article in soup.select('article[data-testid="tweet"]'):
            time = article.find("time")
            link = time.find_parent("a") if time else None
            if (
                link
                and page_identity(urljoin("https://x.com", str(link.get("href", ""))))[1]
                == content_id
            ):
                articles.append(article)
        if len(articles) != 1:
            return {}
        root = articles[0]
    nodes = root.select(selectors[platform])
    if platform != "x":
        filtered = []
        for node in nodes:
            article = node.find_parent("article")
            if article is not None:
                linked = {
                    page_identity(urljoin(f"https://{platform}.com", str(a.get("href"))))[:2]
                    for a in article.select("a[href]")
                }
                post_ids = {item for p, item in linked if p == platform and item}
                if post_ids and content_id not in post_ids:
                    continue
            filtered.append(node)
        nodes = filtered
    # Multiple descriptions usually indicate a feed; do not guess which post is active.
    if not nodes or (len(nodes) > 1 and platform != "x"):
        return {}
    caption = _usable(nodes[0].get_text(" ", strip=True))
    if not caption:
        return {}
    covers = []
    if platform == "x":
        covers = _image_urls(
            [im.get("src") for im in root.select('[data-testid="tweetPhoto"] img')]
        )
    return {"title": caption, "description": caption, "cover_candidates": covers}


def parse_page_metadata(
    url: str, html: str, *, final_url: str | None = None, payloads: Iterable[Any] = ()
) -> LinkMetadata:
    """Parse already obtained HTML/JSON; this function performs no I/O."""
    final = final_url or url
    platform, expected, kind = page_identity(url)
    final_platform, landed_id, landed_kind = page_identity(final)
    if platform not in _LABELS:
        raise ParserError(url, "unsupported: no social page parser for this platform")
    soup = BeautifulSoup(html[:8_000_000], "lxml")
    if reason := _page_wall(soup, final):
        raise ParserError(url, reason)
    if final_platform != platform:
        raise ParserError(url, "target_mismatch: browser redirected outside the source platform")
    if expected and landed_id != expected:
        raise ParserError(url, "target_mismatch: content ID differs from final URL")
    content_id = expected or landed_id
    if not content_id:
        raise ParserError(url, "unsupported: browser could not resolve a target content ID")
    kind = landed_kind or kind
    meta = _base(url, final, platform, kind)
    matched_structure = False
    photo_cover_candidates: list[str] | None = None
    for tag in soup.select('meta[property="og:url"], link[rel="canonical"]'):
        value = str(tag.get("content") or tag.get("href") or "")
        if value and page_identity(urljoin(final, value))[:2] != (platform, content_id):
            raise ParserError(url, "target_mismatch: canonical URL identifies a different post")
    for payload in [*_script_payloads(soup), *payloads]:
        for key, node in _walk(payload):
            fields = _structured_fields(node, key, platform, content_id, _image_index(url))
            if fields is not None and platform in {"tiktok", "douyin"}:
                images = node.get("images") or _at(node, "imagePost", "images")
                if isinstance(images, list) and images:
                    photo_cover_candidates = list(
                        dict.fromkeys(
                            [*(photo_cover_candidates or []), *fields.get("cover_candidates", [])]
                        )
                    )[:12]
            if fields is None:
                fields = _jsonld_fields(node, platform, content_id)
                if fields and platform == "instagram" and (_image_index(url) or 1) > 1:
                    fields.pop("cover_candidates", None)
            if fields:
                matched_structure = True
                _apply(meta, fields)
    dom = _dom_fields(soup, platform, content_id)
    if meta.title or meta.description:
        dom.pop("title", None)
        dom.pop("description", None)
    _apply(meta, dom)

    # OG may supplement this exact page only, never a canonical/og:url pointing at another post.
    declared = []
    for tag in soup.select('meta[property="og:url"], link[rel="canonical"]'):
        value = str(tag.get("content") or tag.get("href") or "")
        declared.append(page_identity(urljoin(final, value)))
    if not any(p != platform or item != content_id for p, item, _ in declared):
        og = {
            str(t.get("property") or t.get("name")): str(t.get("content") or "")
            for t in soup.select("meta[content]")
        }
        title = _usable(og.get("og:title"))
        description = _usable(og.get("og:description"))
        if not meta.title:
            meta.title = title
        if not meta.description:
            meta.description = description
        if (title or description or meta.title or meta.description) and not (
            platform == "instagram" and (_image_index(url) or 1) > 1
        ):
            _apply(meta, {"cover_candidates": _image_urls(og.get("og:image"))})
    if photo_cover_candidates is not None:
        # Native photo identity also takes precedence over JSON-LD/OG video posters.
        meta.cover_candidates = photo_cover_candidates
        meta.cover_url = next(iter(photo_cover_candidates), "")
        meta.media_type = MediaType.ARTICLE
    if is_placeholder(meta.title, platform, url):
        meta.title = ""
    if is_placeholder(meta.description, platform, url):
        meta.description = ""
    if (
        not _usable(meta.title)
        and not _usable(meta.description)
        and not (matched_structure and meta.cover_url)
    ):
        raise ParserError(url, "no_content: browser returned no verified target content")
    meta.title = _usable(meta.title) or _usable(meta.description)
    if kind in {"photo", "note", "slides"}:
        meta.media_type = MediaType.ARTICLE
    if meta.cover_url:
        meta.has_visual = True
    return meta
