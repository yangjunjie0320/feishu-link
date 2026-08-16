from __future__ import annotations

import httpx

from src.http_errors import describe_request_error


def test_describe_names_the_type_when_message_is_empty() -> None:
    """The whole point: httpx timeouts raise with no text at all."""
    assert describe_request_error(httpx.ConnectTimeout("")) == "ConnectTimeout"
    assert describe_request_error(httpx.ReadTimeout("   ")) == "ReadTimeout"


def test_describe_keeps_the_message_when_there_is_one() -> None:
    described = describe_request_error(httpx.ConnectError("[Errno 61] Connection refused"))
    assert described == "ConnectError: [Errno 61] Connection refused"
