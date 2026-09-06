from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# 跟踪/分享参数,归档去重比对时忽略(尽力而为的黑名单,来自实际见过的分享链接)
_TRACKING_PARAMS = {
    "share_source",
    "vd_source",
    "spm_id_from",
    "spmid",
    "share_medium",
    "share_plat",
    "share_session_id",
    "share_tag",
    "share_times",
    "share_from",
    "unique_k",
    "buvid",
    "up_id",
    "from",
    "from_spmid",
    "seid",
    "from_source",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "scene",
    "click_id",
    "s",
    "si",
    "is_story_h5",
    "is_from_webapp",
    "sender_device",
    "igsi",
    "mid",
    "plat_id",
    "timestamp",
    "-arouter",
}


def normalize_url(url: str) -> str:
    """归档去重键:统一 https、域名小写、去跟踪参数与 fragment、去尾斜杠、参数排序。

    只作为内部比较键,不改变实际存档/展示的 URL。
    """
    p = urlparse(url.strip())
    host = p.netloc.lower()
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    query = sorted((k, v) for k, v in parse_qsl(p.query) if k.lower() not in _TRACKING_PARAMS)
    path = p.path.rstrip("/")
    return urlunparse(("https", host, path, "", urlencode(query), ""))


def normalized_domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    host = host.split(":", 1)[0]
    return re.sub(r"^www\.", "", host)


def detect_platform(url: str) -> str:
    domain = normalized_domain(url)
    if domain in {"bilibili.com", "b23.tv"} or domain.endswith(".bilibili.com"):
        return "bilibili"
    if domain == "instagram.com" or domain.endswith(".instagram.com"):
        return "instagram"
    if domain == "tiktok.com" or domain.endswith(".tiktok.com"):
        return "tiktok"
    if domain in {"douyin.com", "iesdouyin.com"} or domain.endswith(
        (".douyin.com", ".iesdouyin.com")
    ):
        return "douyin"
    if domain in {"youtube.com", "youtu.be"} or domain.endswith(".youtube.com"):
        return "youtube"
    if domain in {"x.com", "twitter.com"} or domain.endswith(".twitter.com"):
        return "x"
    return "web"


def is_short_video_platform(url: str) -> bool:
    # Historical name: this is the card-processing allowlist, not permission
    # to download a video. Douyin is deliberately card-only.
    return detect_platform(url) in {"bilibili", "instagram", "tiktok", "youtube", "x", "douyin"}
