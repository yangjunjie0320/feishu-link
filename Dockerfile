FROM python:3.12-slim

ARG DENO_VERSION=2.6.2

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN python -c "import platform, urllib.request, zipfile; from pathlib import Path; version='${DENO_VERSION}'; machine=platform.machine().lower(); target='aarch64-unknown-linux-gnu' if machine in ('aarch64', 'arm64') else 'x86_64-unknown-linux-gnu'; url='https://github.com/denoland/deno/releases/download/v{}/deno-{}.zip'.format(version, target); archive=Path('/tmp/deno.zip'); urllib.request.urlretrieve(url, archive); zipfile.ZipFile(archive).extractall('/usr/local/bin'); Path('/usr/local/bin/deno').chmod(0o755); archive.unlink()"

RUN deno --version

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev

COPY main.py ./
COPY src/ src/

RUN mkdir -p logs

CMD ["uv", "run", "python", "main.py", "--config", "/etc/feishu-link/config.yaml"]
