import logging

from src.ytdlp_options import (
    YtDlpSignalLogger,
    looks_like_cookie_invalidation,
    looks_like_rate_limit,
)


def test_looks_like_cookie_invalidation_matches_known_signals() -> None:
    assert looks_like_cookie_invalidation(
        "WARNING: [youtube] The provided YouTube account cookies are no longer valid"
    )
    assert looks_like_cookie_invalidation(
        "They are either expired or have been rotated in the browser"
    )
    assert looks_like_cookie_invalidation("Sign in to confirm you're not a bot")
    assert not looks_like_cookie_invalidation("HTTP Error 404: Not Found")


def test_looks_like_rate_limit_matches_known_signals() -> None:
    assert looks_like_rate_limit("This content isn't available, try again later")
    assert looks_like_rate_limit("The account is possibly rate-limited")
    assert not looks_like_rate_limit("Video unavailable")


def test_signal_logger_records_signals_and_forwards(caplog) -> None:
    delegate = logging.getLogger("test-ytdlp-signal")
    signal_logger = YtDlpSignalLogger(delegate, prefix="yt-dlp test: ")

    assert signal_logger.cookie_invalid is False
    assert signal_logger.rate_limited is False

    with caplog.at_level(logging.DEBUG, logger="test-ytdlp-signal"):
        signal_logger.debug("[youtube] abc: Downloading webpage")
        signal_logger.warning("The account cookies are no longer valid")
        signal_logger.error("This content isn't available, try again later")

    assert signal_logger.cookie_invalid is True
    assert signal_logger.rate_limited is True
    assert any(
        record.getMessage() == "yt-dlp test: The account cookies are no longer valid"
        for record in caplog.records
    )


def test_signal_logger_ignores_ordinary_messages() -> None:
    signal_logger = YtDlpSignalLogger(logging.getLogger("test-ytdlp-signal"))

    signal_logger.warning("Falling back to generic extractor")
    signal_logger.error("HTTP Error 403: Forbidden")

    assert signal_logger.cookie_invalid is False
    assert signal_logger.rate_limited is False
