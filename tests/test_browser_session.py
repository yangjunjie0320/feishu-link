from __future__ import annotations

import signal
import subprocess
from pathlib import Path

import pytest

from src import browser_session


def _ps_line(pid: int, ppid: int, etime: str, command: str) -> str:
    return f"{pid:>5} {ppid:>5} {etime:>12} {command}"


def _fake_ps(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ps"], returncode=0, stdout="\n".join(lines))

    monkeypatch.setattr(browser_session.subprocess, "run", fake_run)


def _record_kills(monkeypatch: pytest.MonkeyPatch, alive: set[int]) -> list[tuple[int, int]]:
    """Record os.kill calls; signal 0 probes report pids in `alive` as running."""
    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        if sig == 0 and pid not in alive:
            raise ProcessLookupError(pid)
        if sig == signal.SIGTERM:
            alive.discard(pid)

    monkeypatch.setattr(browser_session.os, "kill", fake_kill)
    return calls


def test_orphan_pids_matches_only_adopted_processes_for_this_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = Path("browser-data/tiktok")
    _fake_ps(
        monkeypatch,
        [
            _ps_line(101, 1, "21-10:45", "/chrome --user-data-dir=browser-data/tiktok --headless"),
            # live parent: another instance or a probe may still be using it
            _ps_line(102, 900, "01:00", "/chrome --user-data-dir=browser-data/tiktok"),
            # different profile that shares the prefix
            _ps_line(103, 1, "05:00", "/chrome --user-data-dir=browser-data/tiktok-probe"),
            # unrelated profile
            _ps_line(104, 1, "05:00", "/chrome --user-data-dir=browser-data/bibigpt"),
        ],
    )

    assert browser_session._orphan_pids(profile) == [(101, "21-10:45")]


def test_orphan_pids_matches_resolved_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = tmp_path / "profile"
    _fake_ps(
        monkeypatch, [_ps_line(201, 1, "03:00", f"/chrome --user-data-dir={profile.resolve()}")]
    )

    assert browser_session._orphan_pids(profile) == [(201, "03:00")]


def test_orphan_pids_survives_ps_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("ps unavailable")

    monkeypatch.setattr(browser_session.subprocess, "run", fake_run)

    with caplog.at_level("WARNING"):
        assert browser_session._orphan_pids(Path("browser-data/tiktok")) == []
    assert "Failed to list processes" in caplog.text


async def test_reap_orphans_escalates_to_sigkill_when_sigterm_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = Path("browser-data/tiktok")
    _fake_ps(
        monkeypatch,
        [
            _ps_line(301, 1, "10:00", "/chrome --user-data-dir=browser-data/tiktok"),
            _ps_line(302, 1, "10:00", "/chrome --user-data-dir=browser-data/tiktok"),
        ],
    )
    # 301 dies on SIGTERM; 302 ignores it and must be killed.
    alive = {301, 302}
    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        if sig == 0 and pid not in alive:
            raise ProcessLookupError(pid)
        if sig == signal.SIGTERM and pid == 301:
            alive.discard(pid)

    monkeypatch.setattr(browser_session.os, "kill", fake_kill)
    monkeypatch.setattr(browser_session, "_ORPHAN_TERM_GRACE_SECONDS", 0)

    await browser_session._reap_orphans(profile)

    assert (301, signal.SIGTERM) in calls
    assert (302, signal.SIGTERM) in calls
    assert (302, signal.SIGKILL) in calls
    assert (301, signal.SIGKILL) not in calls


async def test_reap_orphans_is_noop_without_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_ps(monkeypatch, [_ps_line(401, 1, "10:00", "/chrome --user-data-dir=other")])
    calls = _record_kills(monkeypatch, set())

    await browser_session._reap_orphans(Path("browser-data/tiktok"))

    assert calls == []


async def test_reap_orphans_ignores_kill_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_ps(
        monkeypatch, [_ps_line(501, 1, "10:00", "/chrome --user-data-dir=browser-data/tiktok")]
    )

    def fake_kill(pid: int, sig: int) -> None:
        raise PermissionError("not permitted")

    monkeypatch.setattr(browser_session.os, "kill", fake_kill)
    monkeypatch.setattr(browser_session, "_ORPHAN_TERM_GRACE_SECONDS", 0)

    await browser_session._reap_orphans(Path("browser-data/tiktok"))
