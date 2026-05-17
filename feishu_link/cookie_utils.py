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
