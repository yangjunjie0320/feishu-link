import pytest

from feishu_link.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        self_open_id="ou_self",
        archive_chat_id="oc_archive",
        log_level="DEBUG",
    )
