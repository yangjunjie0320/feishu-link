from datetime import UTC, datetime

from src.time_utils import format_beijing, now_utc, to_beijing, to_us_eastern


def test_now_utc_is_utc() -> None:
    dt = now_utc()
    assert dt.tzinfo == UTC


def test_to_beijing_offset() -> None:
    utc = datetime(2024, 1, 15, 8, 0, tzinfo=UTC)
    cst = to_beijing(utc)
    assert cst.hour == 16  # UTC+8


def test_to_us_eastern_dst() -> None:
    # Summer (EDT = UTC-4)
    utc = datetime(2024, 7, 4, 12, 0, tzinfo=UTC)
    eastern = to_us_eastern(utc)
    assert eastern.hour == 8

    # Winter (EST = UTC-5)
    utc_winter = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
    eastern_winter = to_us_eastern(utc_winter)
    assert eastern_winter.hour == 7


def test_format_beijing() -> None:
    utc = datetime(2024, 6, 1, 3, 30, tzinfo=UTC)
    result = format_beijing(utc)
    assert "11:30" in result
    assert "CST" in result
