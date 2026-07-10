import logging

from src.log import setup


def test_setup_suppresses_http_client_request_urls(tmp_path, caplog) -> None:
    root_logger = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    previous_root_level = root_logger.level
    previous_httpx_level = httpx_logger.level
    previous_httpcore_level = httpcore_logger.level
    try:
        setup(level="DEBUG", log_dir=str(tmp_path))

        assert httpx_logger.level == logging.WARNING
        assert httpcore_logger.level == logging.WARNING
        httpx_logger.info("GET https://example.test/video?token=secret")
        assert "token=secret" not in caplog.text
    finally:
        root_logger.setLevel(previous_root_level)
        httpx_logger.setLevel(previous_httpx_level)
        httpcore_logger.setLevel(previous_httpcore_level)
