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

    mode: Mode = Mode.B
    archive_chat_id: str = ""  # required for mode B
    self_open_id: str = ""    # populated at runtime from lark-cli auth status

    youtube_api_key: str = ""
    link_blacklist: list[str] = []

    log_level: str = "INFO"
    log_dir: str = "logs"

    lark_cli_bin: str = "lark-cli"
    request_timeout: float = 10.0
    send_retry_attempts: int = 3

    _blacklist_patterns: list[re.Pattern[str]] = []

    @field_validator("link_blacklist", mode="before")
    @classmethod
    def _parse_blacklist(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v  # type: ignore[return-value]

    @model_validator(mode="after")
    def _compile_patterns(self) -> Settings:
        self._blacklist_patterns = [re.compile(p) for p in self.link_blacklist]
        return self

    def is_blacklisted(self, url: str) -> bool:
        return any(p.search(url) for p in self._blacklist_patterns)

    @classmethod
    def from_yaml(cls, path: str | Path) -> Settings:
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        return cls(**data)
