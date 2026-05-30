from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import field_validator, model_validator
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
    bibigpt_browser_profile_dir: str = "/app/browser-data/bibigpt"
    bibigpt_browser_headless: bool = True
    bibigpt_browser_timeout: float = 120.0
    bibigpt_default_prompt: str = ""

    youtube_api_key: str = ""
    link_allowlist: list[str] = []
    link_blacklist: list[str] = []

    log_level: str = "INFO"
    log_dir: str = "logs"

    request_timeout: float = 10.0
    send_retry_attempts: int = 3
    video_append_enabled: bool = True
    max_video_duration_seconds: int = 180
    max_video_file_mb: int = 30
    allowed_video_platforms: list[str] = ["bilibili", "instagram", "tiktok", "youtube", "x"]
    video_temp_dir: str = "/tmp/feishu-link"

    title_translation_enabled: bool = False
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    translation_timeout: float = 10.0
    summary_rewrite_timeout: float = 120.0
    comment_analysis_max_comments: int = 500
    comment_analysis_prompt_comments: int = 200
    comment_analysis_timeout: float = 120.0

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

    @field_validator("allowed_video_platforms", mode="before")
    @classmethod
    def _parse_allowed_video_platforms(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v  # type: ignore[return-value]

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
        return self

    def is_allowed(self, url: str) -> bool:
        if not self._allowlist_patterns:
            return True
        return any(p.search(url) for p in self._allowlist_patterns)

    def is_blacklisted(self, url: str) -> bool:
        return any(p.search(url) for p in self._blacklist_patterns)

    def cookie_file_for_platform(self, platform: str) -> str:
        normalized = platform.strip().lower()
        configured = self.platform_cookie_files.get(normalized, "")
        if configured:
            return configured

        if self.cookie_file and Path(self.cookie_file).exists():
            return self.cookie_file

        for cookie_dir in (Path("/etc/feishu-link/cookies"), Path("cookies")):
            candidate = cookie_dir / f"{normalized}.txt"
            if candidate.exists():
                return str(candidate)

        return ""

    @classmethod
    def from_yaml(cls, path: str | Path) -> Settings:
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        return cls(**data)
