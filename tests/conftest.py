import pytest

from src.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        archive_chat_id="oc_archive",
        log_level="DEBUG",
    )


@pytest.fixture(autouse=True)
def _clear_tiktok_quota():
    """The TikTok cooldown is module-level state; tests must not inherit it."""
    from src.tiktok_comments import reset_fetch_quota

    reset_fetch_quota()
    yield
    reset_fetch_quota()
