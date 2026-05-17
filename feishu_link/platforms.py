from __future__ import annotations

import re
from urllib.parse import urlparse


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
    if domain in {"youtube.com", "youtu.be"} or domain.endswith(".youtube.com"):
        return "youtube"
    if domain in {"x.com", "twitter.com"} or domain.endswith(".twitter.com"):
        return "x"
    return "web"


def is_short_video_platform(url: str) -> bool:
    return detect_platform(url) in {"bilibili", "instagram", "tiktok", "youtube", "x"}
