import logging

from main import _handle_safely


class _BoomPipeline:
    async def handle(self, event) -> None:
        raise RuntimeError("boom")


async def test_handle_safely_swallows_and_logs(caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="main"):
        # Must not raise: a fire-and-forget task failure cannot crash the loop.
        await _handle_safely(_BoomPipeline(), object())
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert any("unhandled error handling event" in r.getMessage() for r in caplog.records)
