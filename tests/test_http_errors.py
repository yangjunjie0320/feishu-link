from __future__ import annotations

import httpx

from src.http_errors import describe_request_error, is_transient_network_error


def test_describe_names_the_type_when_message_is_empty() -> None:
    """The whole point: httpx timeouts raise with no text at all."""
    assert describe_request_error(httpx.ConnectTimeout("")) == "ConnectTimeout"
    assert describe_request_error(httpx.ReadTimeout("   ")) == "ReadTimeout"


def test_describe_keeps_the_message_when_there_is_one() -> None:
    described = describe_request_error(httpx.ConnectError("[Errno 61] Connection refused"))
    assert described == "ConnectError: [Errno 61] Connection refused"


def test_transient_detects_httpx_transport_types() -> None:
    assert is_transient_network_error(httpx.ConnectTimeout(""))
    assert is_transient_network_error(httpx.ConnectError("[Errno 61] Connection refused"))


def test_transient_detects_wrapped_and_text_only_errors() -> None:
    try:
        try:
            raise httpx.ReadTimeout("")
        except httpx.ReadTimeout as inner:
            raise RuntimeError("download failed") from inner
    except RuntimeError as wrapped:
        assert is_transient_network_error(wrapped)
    assert is_transient_network_error(
        RuntimeError("Unable to download webpage: [SSL: UNEXPECTED_EOF_WHILE_READING]")
    )
    assert is_transient_network_error(RuntimeError("net::ERR_PROXY_CONNECTION_FAILED"))


def test_transient_ignores_http_status_and_content_errors() -> None:
    assert not is_transient_network_error(RuntimeError("HTTP Error 404: Not Found"))
    assert not is_transient_network_error(RuntimeError("download finished without output file"))
    assert not is_transient_network_error(RuntimeError("login required"))
