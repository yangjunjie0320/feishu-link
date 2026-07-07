from __future__ import annotations

import asyncio
import hashlib
import html
import itertools
import json
import logging
import re
import time
import urllib.parse
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings
from .cookie_utils import cookie_value, get_cookie_header, temporary_cookie_file
from .parsers.instagram_media_info import _shortcode_from_url, _shortcode_to_media_id
from .parsers.og_meta import _USER_AGENT, _request_headers
from .parsers.x_graphql import tweet_id_from_url, x_api_headers, x_graphql_endpoint
from .platforms import detect_platform
from .ytdlp_options import apply_ytdlp_runtime

logger = logging.getLogger(__name__)

_MAX_COMMENT_FETCH_LIMIT = 500
_BILIBILI_BVID_RE = re.compile(r"(?:bilibili\.com/video/|b23\.tv/)?(BV[A-Za-z0-9]+)")
_BILIBILI_WBI_KEY_TTL_SECONDS = 30
_X_TWEET_DETAIL_QUERY_ID = "jd3V43oDY9cY7obs1YMfbQ"
_RAW_COMMENT_SCAN_LIMIT = 5000
_BILIBILI_MIXIN_KEY_ENC_TAB = [
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
]
_X_TWEET_DETAIL_FEATURES = {
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}
_X_TWEET_DETAIL_FIELD_TOGGLES = {
    "withArticleRichContentState": True,
    "withArticlePlainText": False,
    "withGrokAnalyze": False,
    "withDisallowedReplyControls": False,
}


class CommentAnalysisError(Exception):
    pass


_CJK_RE = re.compile(r"[㐀-鿿]")

_TOP_COMMENT_COUNT = 8


def _is_chinese(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _bilibili_anon_headers(referer: str = "https://www.bilibili.com/") -> dict[str, str]:
    """Anonymous headers for the public bilibili comment endpoints.

    The comment APIs are public; injecting a stale bilibili login cookie makes the
    heat-mode pagination cursor stop advancing after ~2 pages (see DESIGN.md), so
    fetch without cookies.
    """
    return {"User-Agent": _USER_AGENT, "Referer": referer}


@dataclass(frozen=True)
class VideoComment:
    text: str
    author: str = ""
    author_url: str = ""
    comment_url: str = ""
    like_count: int = 0
    reply_count: int = 0
    comment_id: str = ""
    parent_id: str = ""


@dataclass(frozen=True)
class CommentAnalysisResult:
    markdown: str
    fetched_count: int
    prompt_count: int
    total_comment_count: int | None
    top_comments: list[VideoComment]


@dataclass(frozen=True)
class FetchedComments:
    comments: list[VideoComment]
    total_count: int | None = None


@dataclass(frozen=True)
class CommentInsight:
    summary: str
    sentiment: str
    consensus: list[str]
    controversy: list[str]
    notable_points: list[str]
    top_comment_translations: list[str]


class CommentAnalyzer:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._bilibili_wbi_key: tuple[str, float] | None = None
        # Dedicated pool for the blocking yt-dlp comment extraction. wait_for can
        # only cancel the awaiting coroutine, not the running thread, so a stuck
        # extraction is isolated here instead of starving the shared default
        # executor used by video download and Chrome cookie extraction.
        self._comment_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="comment-fetch"
        )

    async def analyze(self, url: str) -> CommentAnalysisResult:
        try:
            fetched = await asyncio.wait_for(
                self.fetch_comment_page(url),
                timeout=self._settings.comment_fetch_timeout,
            )
        except TimeoutError as e:
            raise CommentAnalysisError("评论抓取超时，请稍后重试。") from e
        comments = sort_comments_by_heat(fetched.comments)
        if not comments:
            raise CommentAnalysisError("没有获取到评论内容。")

        top_comments = select_top_comments(comments, count=_TOP_COMMENT_COUNT)
        prompt_comments = comments[: max(1, self._settings.comment_analysis_prompt_comments)]
        insight = await self._summarize_with_llm(
            url,
            comments,
            top_comments,
            prompt_comments,
            total_comment_count=fetched.total_count,
        )
        markdown = render_comment_analysis_markdown(
            insight,
            comments,
            top_comments,
            prompt_count=len(prompt_comments),
            total_comment_count=fetched.total_count,
        )

        return CommentAnalysisResult(
            markdown=markdown,
            fetched_count=len(comments),
            prompt_count=len(prompt_comments),
            total_comment_count=fetched.total_count,
            top_comments=top_comments,
        )

    async def fetch_comments(self, url: str) -> list[VideoComment]:
        return (await self.fetch_comment_page(url)).comments

    async def fetch_comment_page(self, url: str) -> FetchedComments:
        platform = detect_platform(url)
        max_comments = _comment_fetch_limit(self._settings)
        instagram_error: CommentAnalysisError | None = None

        if platform == "x":
            return await self._fetch_x_comments(url, max_comments)

        if platform == "instagram":
            # Profile / fundraiser 等提不出 shortcode 的链接没有评论区可言，
            # 且 yt-dlp 的 Instagram 提取器上游已标记 broken，兜底只会抛原始堆栈。
            if not _shortcode_from_url(url):
                raise CommentAnalysisError("该链接不是 Instagram 帖子/Reel，无法分析评论。")
            try:
                fetched = await self._fetch_instagram_comments(url, max_comments)
                if fetched.comments:
                    return fetched
            except CommentAnalysisError as e:
                instagram_error = e
                logger.info("instagram comment endpoint failed, falling back to yt-dlp: %s", e)

        if platform == "bilibili":
            try:
                fetched = await self._fetch_bilibili_comments(url, max_comments)
                if fetched.comments:
                    return fetched
            except CommentAnalysisError as e:
                logger.info("bilibili comment endpoint failed, falling back to yt-dlp: %s", e)

        try:
            return await self._fetch_ytdlp_comments(url, platform, max_comments)
        except CommentAnalysisError as e:
            if instagram_error is not None:
                raise CommentAnalysisError(
                    f"Instagram 评论抓取失败: {instagram_error}; yt-dlp 兜底失败: {e}"
                ) from e
            raise

    async def _fetch_instagram_comments(
        self,
        url: str,
        max_comments: int,
    ) -> FetchedComments:
        shortcode = _shortcode_from_url(url)
        if not shortcode:
            raise CommentAnalysisError("Instagram shortcode not found.")

        endpoint = (
            f"https://www.instagram.com/api/v1/media/{_shortcode_to_media_id(shortcode)}/comments/"
        )
        headers = _instagram_comment_headers(url, self._settings)
        await self._augment_instagram_comment_headers(url, headers)

        raw_comments: list[object] = []
        total_count: int | None = None
        params = {
            "can_support_threading": "true",
            "permalink_enabled": "false",
        }
        for _ in range(25):
            try:
                response = await self._client.get(
                    endpoint,
                    headers=headers,
                    params=params,
                    follow_redirects=True,
                )
            except httpx.RequestError as e:
                raise CommentAnalysisError(f"Instagram comments request error: {e}") from e

            if response.status_code >= 400:
                raise CommentAnalysisError(f"Instagram comments HTTP {response.status_code}")

            try:
                data: dict[str, Any] = response.json()
            except ValueError as e:
                raise CommentAnalysisError(_instagram_non_json_reason(response)) from e

            page_comments = data.get("comments") or []
            if isinstance(page_comments, list):
                raw_comments.extend(page_comments)
            total_count = _prefer_known_total(
                total_count,
                _extract_total_comment_count(data),
            )
            if len(raw_comments) >= max_comments:
                break

            next_max_id = data.get("next_max_id") or data.get("next_min_id")
            if not data.get("has_more_comments") or not next_max_id:
                break
            params["max_id"] = str(next_max_id)

        return FetchedComments(
            comments=comments_from_raw(raw_comments, max_comments=max_comments),
            total_count=total_count,
        )

    async def _augment_instagram_comment_headers(
        self,
        url: str,
        headers: dict[str, str],
    ) -> None:
        if "Cookie" not in headers:
            return
        try:
            response = await self._client.get(url, headers=headers, follow_redirects=True)
        except httpx.RequestError as e:
            logger.info("instagram web bootstrap failed before comments request: %s", e)
            return
        if response.status_code >= 400:
            return

        rollout = re.search(r'"rollout_hash":"([^"]+)"', response.text)
        claim = re.search(r'"claim":"([^"]+)"', response.text)
        if rollout:
            headers["X-Instagram-AJAX"] = rollout.group(1)
        headers["X-IG-WWW-Claim"] = claim.group(1) if claim else "0"
        headers.update(
            {
                "Origin": "https://www.instagram.com",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }
        )

    async def _fetch_x_comments(self, url: str, max_comments: int) -> FetchedComments:
        tweet_id = tweet_id_from_url(url)
        if not tweet_id:
            raise CommentAnalysisError("X tweet id not found.")

        cookie_file = self._settings.cookie_file_for_platform("x")
        cookie_header = get_cookie_header(cookie_file, "x.com")
        csrf_token = cookie_value(cookie_header, "ct0")
        if not cookie_header or not csrf_token:
            raise CommentAnalysisError("X 评论抓取需要有效 x cookie。")

        variables = {
            "focalTweetId": tweet_id,
            "with_rux_injections": False,
            "rankingMode": "Relevance",
            "includePromotedContent": False,
            "withCommunity": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withBirdwatchNotes": True,
            "withVoice": True,
        }
        endpoint = x_graphql_endpoint(
            _X_TWEET_DETAIL_QUERY_ID,
            "TweetDetail",
            variables,
            _X_TWEET_DETAIL_FEATURES,
            field_toggles=_X_TWEET_DETAIL_FIELD_TOGGLES,
        )
        try:
            response = await self._client.get(
                endpoint,
                headers=x_api_headers(url, cookie_header, csrf_token, user_agent=_USER_AGENT),
                follow_redirects=True,
            )
        except httpx.RequestError as e:
            raise CommentAnalysisError(f"X comments request error: {e}") from e

        if response.status_code == 429:
            raise CommentAnalysisError("X 评论接口触发限流。")
        if response.status_code in {401, 403}:
            raise CommentAnalysisError(
                f"X 评论接口需要有效登录 cookie (HTTP {response.status_code})。"
            )
        if response.status_code >= 400:
            raise CommentAnalysisError(f"X comments HTTP {response.status_code}")

        try:
            data: dict[str, Any] = response.json()
        except ValueError as e:
            raise CommentAnalysisError("X comments returned non-json response") from e

        if errors := data.get("errors"):
            message = _x_error_message(errors)
            raise CommentAnalysisError(f"X comments API error: {message}")

        comments, total_count = _x_comments_from_tweet_detail(
            data, tweet_id, max_comments=max_comments
        )
        return FetchedComments(comments=comments, total_count=total_count)

    async def _resolve_bilibili_url(self, url: str) -> str:
        if "b23.tv" not in url:
            return url
        try:
            response = await self._client.head(url, follow_redirects=True)
            return str(response.url)
        except httpx.HTTPError:
            return url

    async def _fetch_bilibili_comments(
        self,
        url: str,
        max_comments: int,
    ) -> FetchedComments:
        resolved_url = await self._resolve_bilibili_url(url)
        bvid = _bilibili_bvid_from_url(resolved_url)
        if not bvid:
            raise CommentAnalysisError("Bilibili bvid not found.")

        headers = _bilibili_anon_headers(referer=url)
        try:
            view_response = await self._client.get(
                "https://api.bilibili.com/x/web-interface/view",
                headers=headers,
                params={"bvid": bvid},
                follow_redirects=True,
            )
            view_response.raise_for_status()
            view_data = view_response.json()
        except httpx.HTTPError as e:
            raise CommentAnalysisError(f"Bilibili view request error: {e}") from e
        except ValueError as e:
            raise CommentAnalysisError("Bilibili view API returned non-json response") from e
        if view_data.get("code") != 0:
            raise CommentAnalysisError(
                f"Bilibili view API error {view_data.get('code')}: {view_data.get('message')}"
            )

        video_data = view_data.get("data") if isinstance(view_data.get("data"), dict) else {}
        aid = _as_optional_int(video_data.get("aid"))
        if aid is None:
            raise CommentAnalysisError("Bilibili aid not found.")
        total_count = _as_optional_int((video_data.get("stat") or {}).get("reply"))

        raw_comments: list[object] = []
        offset = ""
        # Fetch comment pages through an isolated client: the pipeline's shared
        # cookie jar can hold a bilibili buvid3 (set when og_meta fetched the
        # video page), and that cookie freezes the heat-mode pagination cursor
        # after ~2 pages. See DESIGN.md.
        async with httpx.AsyncClient(timeout=self._settings.request_timeout) as reply_client:
            for _ in range(20):
                params = await self._sign_bilibili_wbi(
                    {
                        "oid": aid,
                        "type": 1,
                        "mode": 3,
                        "pagination_str": json.dumps(
                            {"offset": offset},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        "plat": 1,
                        "web_location": 1315875,
                    },
                    bvid,
                )
                try:
                    response = await reply_client.get(
                        "https://api.bilibili.com/x/v2/reply/wbi/main",
                        headers=headers,
                        params=params,
                        follow_redirects=True,
                    )
                    response.raise_for_status()
                    data = response.json()
                except httpx.HTTPError as e:
                    raise CommentAnalysisError(f"Bilibili reply request error: {e}") from e
                except ValueError as e:
                    raise CommentAnalysisError(
                        "Bilibili reply API returned non-json response"
                    ) from e
                if data.get("code") != 0:
                    raise CommentAnalysisError(
                        f"Bilibili reply API error {data.get('code')}: {data.get('message')}"
                    )

                payload = data.get("data") if isinstance(data.get("data"), dict) else {}
                replies = payload.get("replies") if isinstance(payload.get("replies"), list) else []
                if not replies:
                    break
                raw_comments.extend(_normalize_bilibili_reply(reply, bvid) for reply in replies)
                if len(raw_comments) >= max_comments:
                    break

                cursor = payload.get("cursor") if isinstance(payload.get("cursor"), dict) else {}
                known_total = _as_optional_int(cursor.get("all_count"))
                total_count = _prefer_known_total(total_count, known_total)
                pagination_reply = (
                    cursor.get("pagination_reply")
                    if isinstance(cursor.get("pagination_reply"), dict)
                    else {}
                )
                next_offset = str(pagination_reply.get("next_offset") or "")
                if cursor.get("is_end") or not next_offset or next_offset == offset:
                    break
                offset = next_offset

        return FetchedComments(
            comments=comments_from_raw(raw_comments, max_comments=max_comments),
            total_count=total_count,
        )

    async def _sign_bilibili_wbi(
        self,
        params: dict[str, object],
        video_id: str,
    ) -> dict[str, str]:
        params = {**params, "wts": round(time.time())}
        signed_params = {
            key: "".join(char for char in str(value) if char not in "!'()*")
            for key, value in sorted(params.items())
        }
        query = urllib.parse.urlencode(signed_params)
        wbi_key = await self._fetch_bilibili_wbi_key(video_id)
        signed_params["w_rid"] = hashlib.md5(f"{query}{wbi_key}".encode()).hexdigest()
        return signed_params

    async def _fetch_bilibili_wbi_key(self, video_id: str) -> str:
        if (
            self._bilibili_wbi_key is not None
            and time.time() < self._bilibili_wbi_key[1] + _BILIBILI_WBI_KEY_TTL_SECONDS
        ):
            return self._bilibili_wbi_key[0]

        try:
            response = await self._client.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers=_bilibili_anon_headers(),
                follow_redirects=True,
            )
            response.raise_for_status()
            nav_data = response.json()
        except httpx.HTTPError as e:
            raise CommentAnalysisError(f"Bilibili nav request error: {e}") from e
        except ValueError as e:
            raise CommentAnalysisError("Bilibili nav API returned non-json response") from e

        data = nav_data.get("data") if isinstance(nav_data.get("data"), dict) else {}
        wbi_img = data.get("wbi_img") if isinstance(data.get("wbi_img"), dict) else {}
        lookup = "".join(
            _bilibili_wbi_image_key(wbi_img.get(key)) for key in ("img_url", "sub_url")
        )
        if len(lookup) < 64:
            raise CommentAnalysisError("Bilibili wbi key not found.")

        wbi_key = "".join(lookup[index] for index in _BILIBILI_MIXIN_KEY_ENC_TAB)[:32]
        self._bilibili_wbi_key = (wbi_key, time.time())
        return wbi_key

    async def _fetch_ytdlp_comments(
        self,
        url: str,
        platform: str,
        max_comments: int,
    ) -> FetchedComments:
        def _extract() -> FetchedComments:
            try:
                import yt_dlp
            except ModuleNotFoundError as e:
                raise CommentAnalysisError("yt-dlp is not installed") from e

            options: dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
                "noprogress": True,
                "getcomments": True,
                "socket_timeout": 30,
                "extractor_retries": 2,
                "source_address": "0.0.0.0",
                "logger": _YtDlpCommentLogger(),
            }
            # Cap total comments and skip reply threads so pagination terminates in
            # bounded time; the wall-clock timeout in analyze() is the real guard,
            # this just shortens how long a stuck extraction thread lingers.
            options["extractor_args"] = {
                "youtube": {
                    "comment_sort": ["top"],
                    "max_comments": [str(max_comments), "all", "0", "0"],
                }
            }
            apply_ytdlp_runtime(options, platform)

            cookie_file = self._settings.cookie_file_for_platform(platform)
            try:
                with temporary_cookie_file(cookie_file) as temp_cookie_file:
                    if temp_cookie_file:
                        options["cookiefile"] = temp_cookie_file
                    with yt_dlp.YoutubeDL(options) as ydl:
                        info = ydl.extract_info(url, download=False)
            except CommentAnalysisError:
                raise
            except Exception as e:
                raise CommentAnalysisError(f"评论抓取失败: {e}") from e

            if not isinstance(info, dict):
                raise CommentAnalysisError("评论抓取返回了无效数据。")

            video_id = str(info.get("id") or "")
            extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
            is_youtube = "youtube" in extractor

            raw_comments: list[object] = []
            for raw in info.get("comments") or []:
                if is_youtube and video_id and isinstance(raw, dict):
                    cid = str(raw.get("id") or "")
                    comment_url = (
                        f"https://www.youtube.com/watch?v={video_id}&lc={cid}" if cid else ""
                    )
                    raw["comment_url"] = comment_url
                raw_comments.append(raw)

            comments = comments_from_raw(raw_comments, max_comments=_RAW_COMMENT_SCAN_LIMIT)
            comments = _select_comment_sample(comments, max_comments=max_comments)

            return FetchedComments(
                comments=comments,
                total_count=_as_optional_int(info.get("comment_count")),
            )

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._comment_executor, _extract)

    def _build_comment_llm_payload(
        self,
        url: str,
        comments: list[VideoComment],
        top_comments: list[VideoComment],
        prompt_comments: list[VideoComment],
        total_comment_count: int | None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self._settings.deepseek_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是社交媒体评论区分析助手。只输出严格 JSON, 不要输出 Markdown。"
                        "所有字段必须使用简体中文。保持简洁, 不要使用 emoji, 不要编造评论。"
                    ),
                },
                {
                    "role": "user",
                    "content": build_comment_analysis_prompt(
                        url,
                        comments,
                        top_comments,
                        prompt_comments,
                        total_comment_count=total_comment_count,
                    ),
                },
            ],
            "temperature": 0.2,
            "max_tokens": 1500,
        }
        if not self._settings.deepseek_thinking_enabled:
            payload["thinking"] = {"type": "disabled"}
        elif self._settings.deepseek_reasoning_effort is not None:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = self._settings.deepseek_reasoning_effort
        return payload

    async def _summarize_with_llm(
        self,
        url: str,
        comments: list[VideoComment],
        top_comments: list[VideoComment],
        prompt_comments: list[VideoComment],
        *,
        total_comment_count: int | None,
    ) -> CommentInsight:
        if not self._settings.deepseek_api_key:
            raise CommentAnalysisError("DeepSeek API key is not configured.")

        endpoint = self._settings.deepseek_base_url.rstrip("/") + "/chat/completions"
        response = await self._client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {self._settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            json=self._build_comment_llm_payload(
                url, comments, top_comments, prompt_comments, total_comment_count
            ),
            timeout=self._settings.comment_analysis_timeout,
        )
        if response.status_code >= 400:
            raise CommentAnalysisError(f"LLM HTTP {response.status_code}: {response.text[:200]}")

        data: dict[str, Any] = response.json()
        choices = data.get("choices", [])
        if not choices:
            raise CommentAnalysisError("LLM returned no choices.")
        message = choices[0].get("message", {})
        content = str(message.get("content") or "").strip()
        if not content:
            raise CommentAnalysisError("LLM returned empty content.")
        return parse_comment_insight(content, top_comment_count=len(top_comments))


def comments_from_raw(
    raw_comments: Iterable[object],
    *,
    max_comments: int,
) -> list[VideoComment]:
    comments: list[VideoComment] = []
    seen: set[tuple[str, str]] = set()

    for raw in _iter_raw_comments(raw_comments):
        if len(comments) >= max_comments:
            break
        if not isinstance(raw, dict):
            continue
        comment = _comment_from_raw(raw)
        if not comment:
            continue
        dedupe_key = (comment.author.lower(), comment.text)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        comments.append(comment)

    reply_counts = Counter(c.parent_id for c in comments if c.parent_id)
    if not reply_counts:
        return comments

    return [
        VideoComment(
            text=comment.text,
            author=comment.author,
            author_url=comment.author_url,
            comment_url=comment.comment_url,
            like_count=comment.like_count,
            reply_count=max(comment.reply_count, reply_counts.get(comment.comment_id, 0)),
            comment_id=comment.comment_id,
            parent_id=comment.parent_id,
        )
        for comment in comments
    ]


def _instagram_comment_headers(url: str, settings: Settings) -> dict[str, str]:
    headers = _request_headers("https://www.instagram.com/", settings)
    cookie_header = headers.get("Cookie", "")
    csrf_token = cookie_value(cookie_header, "csrftoken")
    headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Referer": url,
            "X-ASBD-ID": "129477",
            "X-IG-App-ID": "936619743392459",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    if csrf_token:
        headers["X-CSRFToken"] = csrf_token
    return headers


def _instagram_non_json_reason(response: httpx.Response) -> str:
    prefix = response.text[:300].lstrip().lower()
    content_type = response.headers.get("content-type", "")
    if prefix.startswith("<!doctype") or prefix.startswith("<html") or "text/html" in content_type:
        return "Instagram comments returned HTML/login response"
    return "Instagram comments returned non-json response"


def _x_error_message(errors: object) -> str:
    if not isinstance(errors, list) or not errors:
        return "unknown error"
    first = errors[0]
    if not isinstance(first, dict):
        return str(first)
    return str(first.get("message") or first.get("code") or "unknown error")


def _x_comments_from_tweet_detail(
    data: dict[str, Any],
    tweet_id: str,
    *,
    max_comments: int,
) -> tuple[list[VideoComment], int | None]:
    comments: list[VideoComment] = []
    seen: set[str] = set()
    total_count: int | None = None
    for tweet in _iter_x_tweet_results(data):
        legacy = tweet.get("legacy") if isinstance(tweet.get("legacy"), dict) else {}
        comment_id = str(legacy.get("id_str") or tweet.get("rest_id") or "")
        if not comment_id or comment_id in seen:
            continue
        if comment_id == tweet_id:
            if total_count is None:
                total_count = _as_optional_int(legacy.get("reply_count"))
            continue
        seen.add(comment_id)
        comment = _x_comment_from_tweet(tweet, legacy, comment_id, root_tweet_id=tweet_id)
        if comment is not None:
            comments.append(comment)
    return _select_comment_sample(comments, max_comments=max_comments), total_count


def _iter_x_tweet_results(value: object) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        legacy = value.get("legacy") if isinstance(value.get("legacy"), dict) else {}
        if "full_text" in legacy and (legacy.get("id_str") or value.get("rest_id")):
            yield value
        for child in value.values():
            yield from _iter_x_tweet_results(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_x_tweet_results(child)


def _x_comment_from_tweet(
    tweet: dict[str, Any],
    legacy: dict[str, Any],
    comment_id: str,
    *,
    root_tweet_id: str,
) -> VideoComment | None:
    if str(legacy.get("conversation_id_str") or "") != root_tweet_id:
        return None

    text = _clean_comment_text(str(legacy.get("full_text") or ""))
    if not text:
        return None

    user_result = tweet.get("core", {}).get("user_results", {}).get("result", {})
    user_core = user_result.get("core") if isinstance(user_result, dict) else {}
    user_legacy = user_result.get("legacy") if isinstance(user_result, dict) else {}
    screen_name = ""
    display_name = ""
    if isinstance(user_core, dict):
        screen_name = str(user_core.get("screen_name") or "")
        display_name = str(user_core.get("name") or "")
    if not screen_name and isinstance(user_legacy, dict):
        screen_name = str(user_legacy.get("screen_name") or "")
    author = f"@{screen_name}" if screen_name else display_name
    author_url = f"https://x.com/{screen_name}" if screen_name else ""
    comment_url = f"https://x.com/{screen_name}/status/{comment_id}" if screen_name else ""

    return VideoComment(
        text=text,
        author=author,
        author_url=author_url,
        comment_url=comment_url,
        like_count=_as_int(legacy.get("favorite_count")),
        reply_count=_as_int(legacy.get("reply_count")),
        comment_id=comment_id,
        parent_id=str(legacy.get("in_reply_to_status_id_str") or ""),
    )


def _select_comment_sample(
    comments: list[VideoComment],
    *,
    max_comments: int,
) -> list[VideoComment]:
    if len(comments) <= max_comments:
        return comments
    return sort_comments_by_heat(comments)[:max_comments]


def _bilibili_bvid_from_url(url: str) -> str | None:
    match = _BILIBILI_BVID_RE.search(url)
    return match.group(1) if match else None


def _bilibili_wbi_image_key(value: object) -> str:
    if not value:
        return ""
    return str(value).rsplit("/", 1)[-1].split(".", 1)[0]


def _normalize_bilibili_reply(reply: object, bvid: str = "") -> object:
    if not isinstance(reply, dict):
        return reply

    content = reply.get("content") if isinstance(reply.get("content"), dict) else {}
    member = reply.get("member") if isinstance(reply.get("member"), dict) else {}
    child_replies = reply.get("replies") if isinstance(reply.get("replies"), list) else []
    mid = member.get("mid")
    author_url = f"https://space.bilibili.com/{mid}" if mid else ""
    rpid = reply.get("rpid")
    parent = reply.get("parent") or 0
    comment_url = ""
    if bvid and rpid and not parent:
        comment_url = (
            f"https://www.bilibili.com/video/{bvid}?comment_on=1&comment_root_id={rpid}#reply{rpid}"
        )
    return {
        "id": rpid,
        "text": content.get("message"),
        "author": member.get("uname") or member.get("name") or member.get("mid"),
        "author_url": author_url,
        "comment_url": comment_url,
        "like_count": reply.get("like"),
        "reply_count": reply.get("rcount") or reply.get("count") or len(child_replies),
        "parent": parent or "",
        "replies": [_normalize_bilibili_reply(child, bvid) for child in child_replies],
    }


def _iter_raw_comments(raw_comments: Iterable[object]) -> Iterable[object]:
    for raw in raw_comments:
        yield raw
        if not isinstance(raw, dict):
            continue

        parent_id = str(raw.get("id") or raw.get("pk") or raw.get("comment_id") or "")
        for child in _raw_child_comments(raw):
            if not isinstance(child, dict):
                continue
            if parent_id and not (child.get("parent") or child.get("parent_id")):
                child = {**child, "parent_id": parent_id}
            yield child


def _raw_child_comments(raw: dict[str, Any]) -> list[object]:
    for key in ("replies", "preview_child_comments", "child_comments"):
        value = raw.get(key)
        if isinstance(value, list):
            return value
    return []


def select_top_comments(
    comments: list[VideoComment],
    *,
    count: int,
) -> list[VideoComment]:
    selected: list[VideoComment] = []
    seen: set[tuple[str, str, str]] = set()
    for comment in sort_comments_by_heat(comments):
        key = (comment.comment_id, comment.author.lower(), comment.text)
        if key in seen:
            continue
        seen.add(key)
        selected.append(comment)
        if len(selected) >= count:
            break
    return selected


def sort_comments_by_heat(comments: list[VideoComment]) -> list[VideoComment]:
    return sorted(
        comments,
        key=lambda c: (c.like_count, c.reply_count, len(c.text)),
        reverse=True,
    )


def build_comment_analysis_prompt(
    url: str,
    comments: list[VideoComment],
    top_comments: list[VideoComment],
    prompt_comments: list[VideoComment],
    *,
    total_comment_count: int | None = None,
) -> str:
    n = len(top_comments)
    top_lines = "\n".join(
        f"{index}. {_format_comment_for_prompt(comment, max_chars=300)}"
        for index, comment in enumerate(top_comments, start=1)
    )
    comment_lines = "\n".join(
        f"- {_format_comment_for_prompt(comment, max_chars=180)}" for comment in prompt_comments
    )
    translation_placeholders = ", ".join(
        f'"第 {i} 条评论中文翻译（若已是中文则原样返回）"' for i in range(1, n + 1)
    )
    return (
        f"视频链接: {url}\n"
        f"评论总数: {_format_total_count(total_comment_count)}; "
        f"本次按热度抓取/排序 {len(comments)} 条; "
        f"用于总结的评论: {len(prompt_comments)} 条。\n\n"
        "任务: 翻译为中文, 保持简洁, 避免多层 bullet。请基于评论内容返回严格 JSON, "
        "不要包裹 ```json 代码块, 不要添加额外解释。\n"
        "JSON 模版:\n"
        "{\n"
        '  "summary": "一句话总结评论区主要观点",\n'
        '  "sentiment": "整体情绪基调，例如：以正面称赞为主 / 两极分化 / 普遍质疑",\n'
        '  "consensus": ["共识 1", "共识 2"],\n'
        '  "controversy": ["争议 1"],\n'
        '  "notable_points": ["有价值的信息 1"],\n'
        f'  "top_comment_translations": [{translation_placeholders}]\n'
        "}\n\n"
        f"排名最靠前的 {n} 条评论:\n"
        f"{top_lines}\n\n"
        "可用于整体总结的评论集合:\n"
        f"{comment_lines}"
    )


def parse_comment_insight(raw_content: str, *, top_comment_count: int) -> CommentInsight:
    content = raw_content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content).strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # Reasoning models may wrap the JSON in surrounding text; extract the
        # outermost {...} object as a fallback.
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(content[start : end + 1])
            except json.JSONDecodeError as e:
                raise CommentAnalysisError("LLM returned invalid JSON.") from e
        else:
            logger.warning("comment insight parse failed, raw content: %s", content[:300])
            raise CommentAnalysisError("LLM returned invalid JSON.") from None
    if not isinstance(data, dict):
        raise CommentAnalysisError("LLM returned invalid JSON shape.")

    return CommentInsight(
        summary=_clean_insight_text(data.get("summary")) or "评论区观点较分散。",
        sentiment=_clean_insight_text(data.get("sentiment")) or "",
        consensus=_clean_insight_list(data.get("consensus"), limit=3),
        controversy=_clean_insight_list(data.get("controversy"), limit=2),
        notable_points=_clean_insight_list(data.get("notable_points"), limit=2),
        top_comment_translations=_clean_insight_list(
            data.get("top_comment_translations"),
            limit=max(1, top_comment_count),
            strip_bullets=False,
        ),
    )


def render_comment_analysis_markdown(
    insight: CommentInsight,
    comments: list[VideoComment],
    top_comments: list[VideoComment],
    *,
    prompt_count: int,
    total_comment_count: int | None = None,
) -> str:
    overview = [
        "- **分析概览**",
        f"    评论总数: {_format_total_count(total_comment_count)} · 样本 {len(comments)} 条",
        f"    - {_clean_card_text(insight.summary)}",
    ]
    if insight.sentiment:
        overview.append(f"    - 情绪基调: {_clean_card_text(insight.sentiment)}")
    overview += [
        f"    - 共识: {_join_points(insight.consensus)}",
        f"    - 争议: {_join_points(insight.controversy)}",
        f"    - 信息增量: {_join_points(insight.notable_points)}",
    ]
    hot = ["- **热门评论**"]
    for index, (comment, translation) in enumerate(
        itertools.zip_longest(top_comments, insight.top_comment_translations, fillvalue=""),
        start=1,
    ):
        display_text = _pick_display_text(comment.text, translation)

        author = _clean_card_text(comment.author or "unknown")
        author_md = f"[{author}]({comment.author_url})" if comment.author_url else f"**{author}**"
        meta = (
            f"    {index}. {author_md} · 点赞 {comment.like_count} · 子评论 {comment.reply_count}"
        )
        if comment.comment_url:
            meta += f" · [查看原评论]({comment.comment_url})"
        hot.append(meta)
        hot.append(f"    > {_clean_card_text(display_text)}")
        hot.append("")

    return "\n".join([*overview, "", *hot]).strip()


def _pick_display_text(original: str, translation: str) -> str:
    if _is_chinese(original):
        return original
    return translation if translation else original


def _clean_insight_text(value: object) -> str:
    if isinstance(value, list):
        value = " / ".join(str(item) for item in value if str(item).strip())
    return _clean_card_text(str(value or ""))


def _clean_insight_list(
    value: object,
    *,
    limit: int,
    strip_bullets: bool = True,
) -> list[str]:
    if isinstance(value, list):
        items = value
    elif value:
        items = re.split(r"[;\n]+", str(value))
    else:
        items = []

    cleaned: list[str] = []
    for item in items:
        text = _clean_card_text(str(item)) if strip_bullets else str(item).strip()
        if text:
            cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


def _join_points(points: list[str]) -> str:
    if not points:
        return "暂无明显信号"
    return " / ".join(_clean_card_text(point) for point in points)


def _format_total_count(total_count: int | None) -> str:
    if total_count is None:
        return "未知"
    return f"{total_count} 条"


def _comment_fetch_limit(settings: Settings) -> int:
    return min(_MAX_COMMENT_FETCH_LIMIT, max(1, settings.comment_analysis_max_comments))


def _prefer_known_total(current: int | None, candidate: int | None) -> int | None:
    if current is None:
        return candidate
    if candidate is None:
        return current
    return max(current, candidate)


def _extract_total_comment_count(data: dict[str, Any]) -> int | None:
    for container in _comment_count_containers(data):
        for key in (
            "comment_count",
            "comments_count",
            "total_comment_count",
            "total_comments",
            "comment_total",
            "count",
        ):
            total = _as_optional_int(container.get(key))
            if total is not None:
                return total
    return None


def _comment_count_containers(data: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield data
    for key in ("media", "item", "post"):
        value = data.get(key)
        if isinstance(value, dict):
            yield value
    items = data.get("items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        yield items[0]


def _clean_card_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"^[\s>*+-]+", "", text.strip())
    return " ".join(text.split())


def _comment_from_raw(raw: dict[str, Any]) -> VideoComment | None:
    text = _clean_comment_text(
        str(raw.get("text") or raw.get("comment") or raw.get("content") or "")
    )
    if not text:
        return None

    replies = raw.get("replies")
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    reply_count = _as_int(
        raw.get("reply_count")
        or raw.get("replies_count")
        or raw.get("comment_count")
        or raw.get("child_comment_count")
        or (len(replies) if isinstance(replies, list) else None)
        or len(_raw_child_comments(raw))
    )
    author = str(
        raw.get("author") or user.get("username") or raw.get("author_id") or user.get("pk") or ""
    )
    author_url = str(raw.get("author_url") or "")
    if not author_url and user.get("username"):
        author_url = f"https://www.instagram.com/{user['username']}/"
    return VideoComment(
        text=text,
        author=author,
        author_url=author_url,
        comment_url=str(raw.get("comment_url") or ""),
        like_count=_as_int(
            raw.get("like_count") or raw.get("likes") or raw.get("comment_like_count")
        ),
        reply_count=reply_count,
        comment_id=str(raw.get("id") or raw.get("pk") or raw.get("comment_id") or ""),
        parent_id=str(
            raw.get("parent") or raw.get("parent_id") or raw.get("parent_comment_id") or ""
        ),
    )


def _clean_comment_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _format_comment_for_prompt(comment: VideoComment, *, max_chars: int) -> str:
    author = comment.author or "unknown"
    text = _truncate(comment.text, max_chars)
    return f"作者={author}; 点赞={comment.like_count}; 子评论={comment.reply_count}; 原文={text}"


def _truncate(text: str, max_chars: int) -> str:
    return text[: max_chars - 3].rstrip() + "..." if len(text) > max_chars else text


def _as_int(value: object) -> int:
    return _as_optional_int(value) or 0


def _as_optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(float(str(value).replace(",", ""))))
    except (TypeError, ValueError):
        return None


class _YtDlpCommentLogger:
    def debug(self, msg: str) -> None:
        logger.debug("yt-dlp comments: %s", msg)

    def info(self, msg: str) -> None:
        logger.info("yt-dlp comments: %s", msg)

    def warning(self, msg: str) -> None:
        logger.warning("yt-dlp comments: %s", msg)

    def error(self, msg: str) -> None:
        logger.error("yt-dlp comments: %s", msg)
