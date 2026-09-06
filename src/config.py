from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Mode(StrEnum):
    A = "A"  # thread reply in original conversation
    B = "B"  # post to archive channel


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FEISHU_LINK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_id: str = ""
    app_secret: str = ""

    mode: Mode = Mode.A
    archive_chat_id: str = ""  # required for mode B
    cookie_file: str = "cookies/cookies.txt"
    platform_cookie_files: dict[str, str] = {}

    # BibiGPT integration
    bibigpt_access_mode: str = "web"
    bibigpt_base_url: str = "https://aitodo.co/zh"
    bibigpt_model: str = "openai/gpt-5.5"
    bibigpt_timeout: float = 120.0
    bibigpt_browser_profile_dir: str = "browser-data/bibigpt"
    bibigpt_browser_headless: bool = True
    bibigpt_browser_timeout: float = 120.0
    bibigpt_default_prompt: str = ""
    bibigpt_cookie_writeback: bool = True
    # In-process reuse of default-prompt summaries and back-off after failures,
    # so a re-click never re-triggers bilibili's risk control. 0 disables.
    bibigpt_summary_cache_ttl_seconds: int = 1800
    bibigpt_failure_cooldown_seconds: int = 300
    # Browser mode: prepare Bilibili/TikTok/Douyin videos with the desktop protocol,
    # observe subtitle readiness, then summarize once. Also handles risk control
    # encountered during legacy recovery. Does not use the browser's local queue.
    bibigpt_web_queue_enabled: bool = True
    bibigpt_web_queue_poll_seconds: int = 45
    bibigpt_web_queue_wait_seconds: int = 600
    # Legacy setting accepted for configuration compatibility. The content
    # pipeline always summarizes with our prompt/model once; no extra generation.
    bibigpt_web_queue_regenerate: bool = False

    # Cookie auto-refresh. Source "chrome" extracts from the system Chrome's
    # cookie store (login state maintained by the human using that browser);
    # "browser_profile" is the legacy Playwright persistent-profile path.
    cookie_refresh_enabled: bool = True
    cookie_refresh_source: str = "chrome"
    cookie_refresh_chrome_profile: str = ""
    cookie_refresh_platforms: list[str] = ["bilibili"]
    cookie_refresh_profile_dir: str = "browser-data/cookies"
    cookie_refresh_browser_headless: bool = True
    cookie_refresh_browser_channel: str = ""
    cookie_refresh_browser_timeout: float = 60.0
    cookie_refresh_stale_before_seconds: int = 86400
    cookie_refresh_min_interval_seconds: int = 600
    # Auxiliary pre-emptive trigger: refresh when the cookie file is older than
    # this even if its nominal expiry is far off (0 disables).
    cookie_refresh_max_age_seconds: int = 43200
    # Cooldown for the reactive (failure-driven) refresh path.
    cookie_refresh_reactive_cooldown_seconds: int = 60

    # Bitable archive: append one row per successfully sent link card.
    bitable_enabled: bool = False
    bitable_app_token: str = ""
    bitable_table_id: str = ""

    # Daily report: digest card of the day's archived links.
    report_enabled: bool = False
    report_time: str = "22:00"
    report_timezone: str = "Asia/Shanghai"
    report_chat_id: str = ""  # empty falls back to archive_chat_id
    report_chat_ids: list[str] = []  # extra chats that also receive the report

    youtube_api_key: str = ""
    link_allowlist: list[str] = []
    link_blacklist: list[str] = []

    log_level: str = "INFO"
    log_dir: str = "logs"

    request_timeout: float = 10.0
    send_retry_attempts: int = 3
    card_prepare_timeout: float = Field(default=60.0, gt=0)
    card_parse_timeout: float = Field(default=45.0, gt=0)
    card_enrichment_timeout: float = Field(default=10.0, gt=0)
    card_parse_concurrency: int = Field(default=4, ge=1)
    card_platform_concurrency: int = Field(default=2, ge=1)
    card_cache_ttl_seconds: float = Field(default=600.0, ge=0)
    card_cache_capacity: int = Field(default=256, ge=1)
    card_browser_timeout: float = Field(default=30.0, gt=0)
    card_browser_profile_dir: str = "browser-data/cards"
    card_browser_channel: str = "chrome"
    card_browser_headless: bool = True
    card_browser_concurrency: int = Field(default=2, ge=1)
    card_browser_platform_concurrency: int = Field(default=1, ge=1)
    card_send_timeout: float = Field(default=30.0, gt=0)
    card_send_attempt_timeout: float = Field(default=10.0, gt=0)
    media_metadata_timeout: float = Field(default=60.0, gt=0)
    media_metadata_concurrency: int = Field(default=2, ge=1)
    video_append_enabled: bool = True
    max_video_duration_seconds: int = 180
    max_video_file_mb: int = 30
    allowed_video_platforms: list[str] = ["bilibili", "instagram", "tiktok", "youtube", "x"]
    video_temp_dir: str = "/tmp/feishu-link"

    title_translation_enabled: bool = False
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_thinking_enabled: bool = True
    deepseek_reasoning_effort: str | None = None
    translation_timeout: float = 10.0
    summary_rewrite_timeout: float = 120.0
    comment_analysis_max_comments: int = 500
    comment_analysis_prompt_comments: int = 200
    comment_analysis_timeout: float = 120.0
    comment_fetch_timeout: float = 90.0

    # TikTok comments come from an in-page fetch inside a Playwright Chromium
    # page: the web API is signed by the page's webmssdk.js, so requests must
    # originate from the page context. Browser startup makes it slower than the
    # httpx-based platforms, hence its own wall-clock budget.
    tiktok_comment_fetch_enabled: bool = True
    tiktok_comment_browser_profile_dir: str = "browser-data/tiktok"
    tiktok_comment_browser_headless: bool = True
    # Real Chrome, not Playwright's bundled Chromium: TikTok serves the latter
    # empty comment bodies. Combined with Chrome's new headless mode.
    tiktok_comment_browser_channel: str = "chrome"
    # Route only TikTok through a residential egress. Datacenter/VPN IPs score
    # 10-30% success against TikTok versus 85-95% for residential, which is why
    # the same code succeeds intermittently on one egress and reliably on
    # another. Empty means use the machine's default route.
    tiktok_comment_proxy: str = ""
    # Skip images/video/fonts: only the comment JSON is needed, and over a
    # proxied hop those resources dominate page load time.
    tiktok_comment_block_media: bool = True
    # Self-imposed cooldown. TikTok degrades clients that fetch too often,
    # and a night of rapid retries is what turned a working path into hours
    # of empty bodies. Normal use is a few fetches a day, so this only ever
    # bites during debugging.
    tiktok_comment_max_per_window: int = 5
    tiktok_comment_window_seconds: float = 3600.0
    tiktok_comment_browser_timeout: float = 45.0
    tiktok_comment_fetch_timeout: float = 120.0
    # Timing is a narrow window: the Comments tab renders before React binds
    # its handler (early clicks silently do nothing), while waiting too long
    # lets the feed autoplay on to the next video.
    tiktok_comment_tab_settle_seconds: float = 10.0
    tiktok_comment_tab_click_attempts: int = 3
    tiktok_comment_load_timeout: float = 7.0
    tiktok_comment_max_scrolls: int = 25
    tiktok_comment_scroll_delay: float = 1.2

    _allowlist_patterns: list[re.Pattern[str]] = []
    _blacklist_patterns: list[re.Pattern[str]] = []

    @field_validator("link_allowlist", mode="before")
    @classmethod
    def _parse_allowlist(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v  # type: ignore[return-value]

    @field_validator("link_blacklist", mode="before")
    @classmethod
    def _parse_blacklist(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v  # type: ignore[return-value]

    @field_validator("allowed_video_platforms", "cookie_refresh_platforms", mode="before")
    @classmethod
    def _parse_allowed_video_platforms(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v  # type: ignore[return-value]

    @field_validator("report_time")
    @classmethod
    def _parse_report_time(cls, v: Any) -> str:
        value = str(v).strip()
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", value):
            raise ValueError("report_time must be HH:MM (24-hour)")
        return value

    @field_validator("bibigpt_access_mode")
    @classmethod
    def _parse_bibigpt_access_mode(cls, v: Any) -> str:
        mode = str(v or "web").strip().lower()
        if mode not in {"web", "browser"}:
            raise ValueError("bibigpt_access_mode must be 'web' or 'browser'")
        return mode

    @model_validator(mode="after")
    def _compile_patterns(self) -> Settings:
        self._allowlist_patterns = [re.compile(p) for p in self.link_allowlist]
        self._blacklist_patterns = [re.compile(p) for p in self.link_blacklist]
        self.allowed_video_platforms = [
            platform.strip().lower()
            for platform in self.allowed_video_platforms
            if platform.strip()
        ]
        self.cookie_refresh_platforms = [
            platform.strip().lower()
            for platform in self.cookie_refresh_platforms
            if platform.strip()
        ]
        return self

    def is_allowed(self, url: str) -> bool:
        if not self._allowlist_patterns:
            return True
        return any(p.search(url) for p in self._allowlist_patterns)

    def is_blacklisted(self, url: str) -> bool:
        return any(p.search(url) for p in self._blacklist_patterns)

    def effective_report_chat_ids(self) -> list[str]:
        """Report targets in config order, de-duplicated; falls back to the
        archive chat when neither report_chat_id nor report_chat_ids is set."""
        seen: set[str] = set()
        targets: list[str] = []
        for chat_id in [self.report_chat_id, *self.report_chat_ids]:
            chat_id = chat_id.strip()
            if chat_id and chat_id not in seen:
                seen.add(chat_id)
                targets.append(chat_id)
        if not targets and self.archive_chat_id:
            targets.append(self.archive_chat_id)
        return targets

    def comment_fetch_timeout_for(self, platform: str) -> float:
        """Wall-clock budget for one platform's comment fetch.

        TikTok pays for a Chromium launch and page load before the first
        request, so it gets its own budget instead of raising the global one and
        slowing down every other platform's failure feedback.
        """
        if platform.strip().lower() == "tiktok" and self.tiktok_comment_fetch_timeout > 0:
            return self.tiktok_comment_fetch_timeout
        return self.comment_fetch_timeout

    def cookie_file_for_platform(self, platform: str) -> str:
        normalized = platform.strip().lower()
        configured = self.platform_cookie_files.get(normalized, "")
        if configured:
            return configured

        candidate = Path("cookies") / f"{normalized}.txt"
        if candidate.exists():
            return str(candidate)

        # Douyin has an independent session; never treat the generic legacy
        # cookie file as its login state.
        if normalized == "douyin":
            return ""

        if self.cookie_file and Path(self.cookie_file).exists():
            return self.cookie_file

        return ""

    @classmethod
    def from_yaml(cls, path: str | Path) -> Settings:
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        return cls(**data)
