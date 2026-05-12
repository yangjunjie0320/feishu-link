# feishu-link

Feishu "link secretary": monitors your own outgoing messages via WebSocket, extracts URLs, fetches metadata, and replies with a structured interactive card.

## Prerequisites

- Docker and docker-compose
- `lark-cli` authenticated on the host machine (credentials are mounted into the container)

## Setup

### 1. Authenticate lark-cli on the host

```
npm install -g @larksuite/cli
lark-cli auth login --recommend
```

Follow the browser prompt to log in with your Feishu account. Credentials are stored at `~/.config/@larksuite/cli/`.

### 2. Create your config

```
cp config.example.yaml config.yaml
```

Edit `config.yaml`:
- Set `mode: B` and `archive_chat_id` to the `oc_xxx` ID of your archive chat (recommended), or `mode: A` for thread replies in the original conversation.
- Optionally add a `youtube_api_key` for full YouTube metadata (title, duration, channel).

### 3. Run with Docker

```
docker compose up -d
docker compose logs -f
```

The container mounts:
- `./config.yaml` — runtime config (read-only)
- `~/.config/@larksuite/cli` — lark-cli credentials (read-only)
- `./logs` — writable log directory

## Running locally (without Docker)

```
npm install -g @larksuite/cli
uv sync
uv run python main.py --config config.yaml
```

## Development

```
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run pyright
```

## Environment variables

All settings can be overridden via environment variables prefixed with `FEISHU_LINK_`. See `.env.example`.

## Architecture

See `DESIGN.md` for the full specification.
