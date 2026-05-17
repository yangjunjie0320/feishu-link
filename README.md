# feishu-link

Feishu "link secretary": monitors messages visible to a Feishu custom app via WebSocket, extracts URLs, fetches metadata, and replies with a structured interactive card.

This project runs through a Feishu custom app. Configure the app credentials, subscribe to message events, and add the app to the conversations you want it to process.

## Quick start

### 1. Create and configure a Feishu custom app

Create a custom app in Feishu Open Platform, enable the permissions needed to receive and send messages, subscribe to `im.message.receive_v1`, and copy the app credentials:

- `app_id`
- `app_secret`

Add the app to the conversations whose messages should be processed. The service does not filter by sender.

### 2. Create your config

```bash
cp config.example.yaml config.yaml
```

Set `app_id` and `app_secret` in `config.yaml`. If you use `mode: B`, also set `archive_chat_id`.

### 3. Run locally

```bash
uv sync
uv run python main.py --config config.yaml
```

When you see `WebSocket long connection started`, the daemon is listening. Send a message containing a URL in a conversation where the app can receive events; the archive channel should receive an interactive card shortly after.

### 4. Run with Docker (production)

```bash
docker compose up -d
docker compose logs -f
```

The container mounts:
- `./config.yaml` — runtime config (read-only)
- `./logs` — writable log directory

## Configuration reference

See `config.example.yaml` for all options. Key settings:

| Key | Default | Description |
|-----|---------|-------------|
| `app_id` | — | Feishu custom app ID |
| `app_secret` | — | Feishu custom app secret |
| `mode` | `A` | `A` = thread reply in original chat; `B` = send to archive channel |
| `archive_chat_id` | — | Required only for mode B (`oc_xxx`) |
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

To send one real card to Feishu for integration verification:

```bash
FEISHU_LINK_INTEGRATION_SEND=1 uv run --extra dev pytest tests/test_integration_feishu_send.py
```

For `mode: A`, provide a real message ID to reply to:

```bash
FEISHU_LINK_INTEGRATION_SEND=1 FEISHU_LINK_INTEGRATION_MESSAGE_ID=om_xxx uv run --extra dev pytest tests/test_integration_feishu_send.py
```

## Architecture

See `DESIGN.md` for the full specification.
