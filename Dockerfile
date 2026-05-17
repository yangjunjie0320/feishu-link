FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev

COPY main.py ./
COPY feishu_link/ feishu_link/

RUN mkdir -p logs

CMD ["uv", "run", "python", "main.py", "--config", "/etc/feishu-link/config.yaml"]
