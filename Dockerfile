FROM python:3.12-slim

ARG DENO_VERSION=2.6.2
ARG UV_VERSION=0.11.12
ARG TUNA_DEBIAN_URL=http://mirrors.tuna.tsinghua.edu.cn/debian
ARG TUNA_DEBIAN_SECURITY_URL=http://mirrors.tuna.tsinghua.edu.cn/debian-security
ARG TUNA_PYPI_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/

ENV DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PIP_INDEX_URL=${TUNA_PYPI_URL} \
    UV_DEFAULT_INDEX=${TUNA_PYPI_URL}

RUN sed -i \
        -e "s|http://deb.debian.org/debian|${TUNA_DEBIAN_URL}|g" \
        -e "s|http://deb.debian.org/debian-security|${TUNA_DEBIAN_SECURITY_URL}|g" \
        -e "s|http://security.debian.org/debian-security|${TUNA_DEBIAN_SECURITY_URL}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        tini \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN python -c "import platform, urllib.request, zipfile; from pathlib import Path; version='${DENO_VERSION}'; machine=platform.machine().lower(); target='aarch64-unknown-linux-gnu' if machine in ('aarch64', 'arm64') else 'x86_64-unknown-linux-gnu'; url='https://github.com/denoland/deno/releases/download/v{}/deno-{}.zip'.format(version, target); archive=Path('/tmp/deno.zip'); urllib.request.urlretrieve(url, archive); zipfile.ZipFile(archive).extractall('/usr/local/bin'); Path('/usr/local/bin/deno').chmod(0o755); archive.unlink()"

RUN deno --version

RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev
RUN uv run python -m playwright install --with-deps chromium

COPY main.py ./
COPY src/ src/

RUN mkdir -p logs /app/browser-data /app/browser-data/cookies

# tini as PID 1 reaps zombie Chromium subprocesses left by browser sessions and
# forwards SIGTERM so the browser tree shuts down cleanly. Stale per-profile
# Singleton locks are cleared in-process before each launch (browser_session).
ENTRYPOINT ["tini", "--"]
CMD ["uv", "run", "python", "main.py", "--config", "/etc/feishu-link/config.yaml"]
