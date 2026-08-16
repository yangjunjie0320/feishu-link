from __future__ import annotations


def describe_request_error(exc: Exception) -> str:
    """Render a transport exception so it stays diagnosable in logs.

    httpx transport errors frequently carry an empty message -- ConnectTimeout,
    ReadTimeout and RemoteProtocolError all raise with no text -- which turned
    "request error: {e}" into "request error: " and left nothing to act on.
    Production hit this on 10 Instagram Reel parses in one week.
    """
    message = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {message}" if message else name
