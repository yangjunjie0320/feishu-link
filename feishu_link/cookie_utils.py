from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def temporary_cookie_file(cookie_file: str) -> Iterator[str]:
    if not cookie_file:
        yield ""
        return

    source = Path(cookie_file)
    with tempfile.TemporaryDirectory(prefix="feishu-link-cookies-") as temp_dir:
        target = Path(temp_dir) / source.name
        shutil.copy2(source, target)
        yield str(target)


def cookie_header_from_netscape_file(cookie_file: str) -> str:
    if not cookie_file:
        return ""

    path = Path(cookie_file)
    if not path.exists():
        return ""

    pairs: list[str] = []
    with path.open(encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#HttpOnly_"):
                line = line.removeprefix("#HttpOnly_")
            elif line.startswith("#"):
                continue

            parts = line.split("\t")
            if len(parts) < 7:
                continue
            name = parts[5].strip()
            value = parts[6].strip()
            if name:
                pairs.append(f"{name}={value}")

    return "; ".join(pairs)
