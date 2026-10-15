from __future__ import annotations

import httpx

_TRANSIENT_TYPES = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
)

# Transport failures as they surface in yt-dlp stderr, httpx reprs and
# Playwright errors. HTTP status errors (403/404/429) are deliberately absent:
# retrying those after a pause does not help.
_TRANSIENT_MARKERS = (
    "timed out",
    "connection reset",
    "connection refused",
    "connection aborted",
    "unexpected_eof",
    "eof occurred",
    "connecterror",
    "connecttimeout",
    "readtimeout",
    "proxyerror",
    "unable to connect to proxy",
    "temporary failure in name resolution",
    "nodename nor servname",
    "network is unreachable",
    "err_connection",
    "err_proxy",
    "err_timed_out",
)


def is_transient_network_error(exc: BaseException) -> bool:
    """True when exc (or anything in its cause chain) is a transport-level blip.

    The mac mini reaches Instagram/YouTube through a local proxy that drops out
    for a minute every few weeks; failures inside that window are worth one
    delayed retry, everything else is not.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _TRANSIENT_TYPES):
            return True
        text = f"{type(current).__name__}: {current}".lower()
        if any(marker in text for marker in _TRANSIENT_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


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
