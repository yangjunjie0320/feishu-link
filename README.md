# feishu-link

Feishu "link secretary": monitors your own outgoing messages via WebSocket, extracts URLs, fetches metadata, and replies with a structured interactive card.

No custom Feishu app required. lark-cli uses its own built-in credentials; you only need to log in with your Feishu account.

## Quick start

### 1. Install and authenticate lark-cli

```bash
npm install -g @larksuite/cli
lark-cli auth login --recommend
```

A browser window opens for Feishu OAuth. Log in with your own account. Credentials are stored at `~/.config/@larksuite/cli/`.

Verify it worked:

```bash
lark-cli auth status   # should show your open_id
```

### 2. Find your archive chat ID

```bash
lark-cli im +chats-list --format table
```

Pick the group or self-chat you want to use as the archive channel. Copy its `chat_id` (`oc_xxx`).

### 3. Create your config

```bash
cp config.example.yaml config.yaml
```

Set `archive_chat_id` in `config.yaml`. Everything else has sensible defaults.

### 4. Run locally

```bash
uv sync
uv run python main.py --config config.yaml
```

When you see `lark-cli event consumer ready`, the daemon is listening. Send a message containing a URL from your own Feishu account — the archive channel should receive an interactive card shortly after.

### 5. Run with Docker (production)

```bash
docker compose up -d
docker compose logs -f
```

The container mounts:
- `./config.yaml` — runtime config (read-only)
- `~/.config/@larksuite/cli` — lark-cli credentials (read-only)
- `./logs` — writable log directory

## Configuration reference

See `config.example.yaml` for all options. Key settings:

| Key | Default | Description |
|-----|---------|-------------|
| `mode` | `B` | `A` = thread reply in original chat; `B` = send to archive channel |
| `archive_chat_id` | — | Required for mode B (`oc_xxx`) |
| `youtube_api_key` | — | Optional; enables duration + channel metadata for YouTube links |
| `link_blacklist` | `[]` | Regex list of URL patterns to ignore |

All settings can also be set via environment variables prefixed with `FEISHU_LINK_`. See `.env.example`.

## Development

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run pyright
```

## Architecture

See `DESIGN.md` for the full specification.
