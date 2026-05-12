FROM python:3.12-slim

# Install Node.js (for lark-cli) and system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install lark-cli globally
RUN npm install -g @larksuite/cli

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install Python deps first (cache layer)
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev

# Copy source
COPY main.py ./
COPY feishu_link/ feishu_link/

RUN mkdir -p logs

# Mount points expected at runtime:
#   /etc/feishu-link/config.yaml  (read-only config)
#   /root/.config/@larksuite/cli/ (lark-cli credentials)
#   /app/logs/                    (writable log volume)

CMD ["uv", "run", "python", "main.py", "--config", "/etc/feishu-link/config.yaml"]
