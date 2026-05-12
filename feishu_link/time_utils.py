from datetime import UTC, datetime

from dateutil import tz

_BEIJING = tz.gettz("Asia/Shanghai")
_US_EASTERN = tz.gettz("America/New_York")


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def to_beijing(dt: datetime) -> datetime:
    return dt.astimezone(_BEIJING)


def to_us_eastern(dt: datetime) -> datetime:
    return dt.astimezone(_US_EASTERN)


def format_beijing(dt: datetime, fmt: str = "%Y-%m-%d %H:%M CST") -> str:
    return to_beijing(dt).strftime(fmt)
