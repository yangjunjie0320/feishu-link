# feishu-link

Feishu link assistant that listens to messages visible to a Feishu custom app, extracts links, fetches metadata, replies with compact interactive cards, and then tries to append downloadable short videos.

The project is designed for personal or team link collection workflows: send a supported link in Feishu, get a readable card first, and get the original video only when it can be downloaded and uploaded safely.

## Features

- Feishu WebSocket listener through a custom app.
- Compact interactive cards with cover image, platform label, title, author or channel, duration, and source button.
- Multi-platform parsing for YouTube, Instagram, TikTok, Bilibili, X/Twitter, and normal web pages.
- Video metrics when available: views, likes, comments, and reposts.
- Optional short-video append. Videos up to 180 seconds are downloaded, converted to Feishu-friendly MP4, uploaded, and sent after the card.
- **BibiGPT Integration**: Mention the bot (`@bot`) with a YouTube or Bilibili link to receive an AI-generated video summary via BibiGPT web, browser, or OpenAPI mode.
- Manual download command: mention the bot and send `下载 <link>` to force video download instead of summarization, without the automatic short-video duration cap.
- Card action buttons: supported video cards include `总结视频` and `下载视频` actions for one-click summary or manual download.
- Optional title translation through DeepSeek. Non-Chinese titles are translated and shown together with the original title.
- Unified Netscape cookie file for platforms that require login state (like Instagram, X, and BibiGPT).
- Explicit logging for parse, download, upload, and send failures. Card delivery is prioritized over video delivery.

## Supported Platforms

| Platform | Card metadata | Image or cover | Stats | Video append |
|----------|---------------|----------------|-------|--------------|
| YouTube / Shorts | Title, channel, duration | Yes | Views, likes, comments when available | Yes, when <= 180 seconds and downloadable |
| Instagram | Caption/title, author, duration | Yes | Likes, comments, views when available | Yes, for accessible reels/videos |
| TikTok | Caption/title, author, duration | Yes | Views, likes, comments, reposts when available | Yes, when enabled and downloadable |
| Bilibili | Title, UP, duration | Yes | Views, likes, comments, reposts when available | Yes, when <= 180 seconds and downloadable |
| X / Twitter | Text summary, author, duration | Yes | Likes, comments, reposts when available | Yes, for accessible media posts |
| Web pages | Open Graph title, description, site | Yes | No | No |

Douyin is intentionally not parsed by default. If Feishu itself expands a Douyin message, this service still ignores Douyin URLs unless you explicitly add support later.

## Quick Start

### 1. Create a Feishu custom app

Create a custom app in Feishu Open Platform, then configure:

- App credentials: `app_id`, `app_secret`
- Event subscription: `im.message.receive_v1` and card action callbacks (`card.action.trigger`)
- Message permissions for receiving and sending messages
- The conversations where the app should be added

The service does not filter by sender. Any message visible to the app can trigger parsing.

### 2. Create runtime config

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` and set at least:

```yaml
app_id: "cli_xxx"
app_secret: "xxx"
mode: "A"
```

Use `mode: A` to reply in the original message thread. Use `mode: B` to send cards and videos to an archive chat:

```yaml
mode: "B"
archive_chat_id: "oc_xxx"
```

`config.yaml` is intentionally ignored by Git.

### 3. Run with Docker

```bash
docker compose up -d
docker compose logs -f
```

The container mounts:

- `./config.yaml` as read-only runtime config
- `./cookies` as optional read-only platform cookie directory
- `./logs` as writable log directory
- `./browser-data` as writable Chromium profile storage for BibiGPT browser mode

### 4. Run locally for development

```bash
uv sync --extra dev
uv run python main.py --config config.yaml
```

When you see the WebSocket long-connection log, send a supported link in a Feishu conversation where the app can receive events.

## Run Without Docker

Docker is recommended for production because it pins Python, `ffmpeg`, `ffprobe`, `deno`, and runtime mounts in one place. If you do not want to use Docker, run the same `main.py` entry point with `uv`.

### 1. Install system tools

Install Python 3.12, `uv`, `ffmpeg`, `ffprobe`, and `deno`.

macOS:

```bash
brew install python@3.12 uv ffmpeg deno
```

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y python3.12 ffmpeg unzip curl
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -fsSL https://deno.land/install.sh | sh
```

Verify:

```bash
uv --version
ffmpeg -version
ffprobe -version
deno --version
```

### 2. Install Python dependencies

```bash
uv sync
```

For development and tests:

```bash
uv sync --extra dev
```

### 3. Prepare config and cookies

```bash
cp config.example.yaml config.yaml
mkdir -p cookies logs
```

Set real credentials in `config.yaml`. If you use cookie files outside Docker, configure local paths:

```yaml
platform_cookie_files:
  instagram: "cookies/instagram.txt"
  tiktok: "cookies/tiktok.txt"
  youtube: "cookies/youtube.txt"
  bilibili: "cookies/bilibili.txt"
  x: "cookies/x.txt"
```

### 4. Start the service

```bash
uv run python main.py --config config.yaml
```

Keep the process running with your preferred process manager, for example `systemd`, `supervisor`, `launchd`, or `tmux`. The service still uses `main.py` as the only runtime entry point.

## Configuration

See `config.example.yaml` for the full reference. Common settings:

| Key | Default | Description |
|-----|---------|-------------|
| `app_id` | Required | Feishu custom app ID |
| `app_secret` | Required | Feishu custom app secret |
| `mode` | `A` | `A` replies to the original message, `B` sends to archive chat |
| `archive_chat_id` | Empty | Required when `mode` is `B` |
| `youtube_api_key` | Empty | Optional YouTube Data API key for stable YouTube metadata |
| `link_blacklist` | `[]` | Regex URL patterns to ignore |
| `enable_video_send` | `true` | Whether to try appending short videos after cards |
| `max_video_duration_seconds` | `180` | Max duration for automatic video append; manual downloads ignore this artificial cap |
| `max_video_file_mb` | Config example | Max uploaded video size |
| `allowed_video_platforms` | Config example | Platforms allowed for video append |
| `cookie_file` | `cookies/cookies.txt` | Unified Netscape cookie file path |
| `bibigpt_access_mode` | `web` | `web` uses the normal BibiGPT web app quota via tRPC; `browser` sends the same request from persisted Chromium; `api` uses OpenAPI-style chat completions |
| `bibigpt_base_url` | `https://bibigpt.co` | Base URL for BibiGPT. Use `https://aitodo.co/zh` for the international/overseas route. |
| `bibigpt_timeout` | `120.0` | Timeout in seconds for BibiGPT summary requests |
| `bibigpt_browser_profile_dir` | `/app/browser-data/bibigpt` | Chromium profile path for BibiGPT browser mode |
| `bibigpt_browser_headless` | `true` | Run Chromium headless in browser mode |
| `bibigpt_browser_timeout` | `120.0` | Timeout in seconds for browser startup, navigation, and summary fetch |
| `bibigpt_default_prompt` | Default | Prompt to send to BibiGPT |
| `deepseek_api_key` | Empty | Enables title translation when configured |
| `deepseek_base_url` | DeepSeek API | Optional compatible API endpoint |
| `enable_title_translation` | `false` | Translate non-Chinese titles when true |

All settings can also be provided through environment variables prefixed with `FEISHU_LINK_`.

## Cookies

Some platforms restrict metadata or video download for anonymous requests. The `cookie_file` setting is optional and should point to a single Netscape format file exported by extensions like Cookie-Editor. This file can contain cookies for multiple domains (e.g., YouTube, X, Instagram, BibiGPT).

Recommended layout:

```text
cookies/
  cookies.txt
```

Example config:

```yaml
cookie_file: "/etc/feishu-link/cookies/cookies.txt"
```

Only provide cookies for accounts you control. Cookie files are ignored by Git and should not be shared.

## Dependency Mirrors

The project config sets `uv` to TUNA PyPI:

```toml
[[tool.uv.index]]
name = "tuna"
url = "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/"
default = true
```

The Dockerfile also switches Debian apt sources to TUNA and installs `uv` from TUNA PyPI. TUNA currently documents its Docker mirror as a Docker CE package repository, not a Docker Hub registry mirror, so the base image pull still depends on Docker Hub or your local Docker daemon mirror. Deno and Playwright browser binaries are also downloaded from their upstream release hosts.

## Title Translation

Title translation is optional. When enabled, the service calls DeepSeek only if the detected title does not contain Chinese text.

```yaml
enable_title_translation: true
deepseek_api_key: "sk-xxx"
deepseek_model: "deepseek-chat"
```

Cards display both the Chinese translation and the original title. Translation failures are logged and do not block card sending.

## Automatic Video Sending

The service always sends the card first. After that it tries video sending only when all conditions are met:

1. The URL is recognized as a video platform.
2. Duration is known and no longer than `max_video_duration_seconds`.
3. A downloadable media candidate is available.
4. Authentication requirements are satisfied by configured cookies if needed.
5. The downloaded and converted MP4 is within `max_video_file_mb`.
6. Feishu upload and message send both succeed.

Before upload, videos are converted to MP4 with H.264 video, AAC audio, `yuv420p`, and faststart metadata. The uploader probes the converted file duration with `ffprobe` and sends duration in milliseconds so Feishu can generate a correct preview.

If any video step fails, the service logs the reason and does not send an extra failure message to the chat.

## Manual Downloads

Mention the bot and start the message with `下载` followed by a link to force video download and send:

```text
@bot 下载 https://youtu.be/example
```

Manual downloads bypass BibiGPT summaries and the automatic short-video duration cap. If the link cannot be parsed as a downloadable video, has no downloadable candidate, has a known candidate file larger than `max_video_file_mb`, produces a final converted MP4 larger than `max_video_file_mb`, or fails Feishu upload/send, the bot replies with the failure reason.

Supported video cards also include action buttons:

- `总结视频` triggers the same BibiGPT summary path as mentioning the bot with a YouTube or Bilibili URL.
- `下载视频` triggers the same manual-download path and sends the result as a reply to the card message.

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

## Troubleshooting

### Card button click returns code 200340

Feishu returns `200340` when the app has no valid card callback route. In the Feishu Open Platform, open the app and check:

1. `开发配置` -> `事件与回调` uses the same subscription mode as the bot runtime.
2. `card.action.trigger` (`卡片回传交互`) is added to the subscribed callbacks/events.
3. The bot's interactive card capability is enabled.
4. If the app requires versioning, create and publish a new app version after changing callback settings.

The code registers this callback through the Python SDK as `p2.card.action.trigger`; that `p2.` prefix is SDK-internal and should not be typed into the Feishu console.

### Card is sent but video is not sent

Check logs for an explicit reason. Common causes are unknown duration, duration over the configured limit, missing cookies, platform rate limits, download failure, file size over limit, upload failure, or Feishu send failure.

### Instagram or X parsing fails

Try providing platform cookies in Netscape format. Private, deleted, region-restricted, or login-gated posts may still fail even with cookies.

### BibiGPT summary fails with a cookie error

For the aitodo overseas route, set `bibigpt_base_url: "https://aitodo.co/zh"`. Start with `bibigpt_access_mode: "web"` when a complete Netscape cookie export works. If direct HTTP returns server errors, use `bibigpt_access_mode: "browser"` so the request is sent from a persisted Chromium profile in `browser-data/`.

Browser mode can import valid cookies, but if the local cookie file is incomplete or corrupted it skips that file and relies on the Chromium profile. Do not paste only part of split Supabase auth cookies like `*-auth-token.0` / `*-auth-token.1`; export the whole `aitodo.co` cookie set again when seeding from a file.

### Feishu video preview shows the wrong duration

Make sure `ffmpeg` and `ffprobe` are available in the runtime image and that uploaded videos are converted before sending. The Docker image includes these tools.

### The service ignores a link

Check `link_blacklist`, platform support, and URL extraction logs. Some apps paste text without a plain `http` URL; the service can only parse links that appear in the message payload or Feishu rich-text link fields.

## Repository Hygiene

The repository intentionally excludes runtime secrets and local agent files:

- `config.yaml`
- `cookies/`
- `logs/`
- `.env`
- IDE directories
- local agent instructions
- `DESIGN.md`

Use `config.example.yaml` as the public template and keep real credentials only in local runtime config or deployment secrets.

## Architecture

The main runtime entry point is `main.py`. Core modules:

- `src/listener.py`: Feishu WebSocket event listener
- `src/url_extract.py`: URL extraction from text and rich-text messages
- `src/dispatch.py`: parser dispatch and fallback behavior
- `src/parsers/`: platform and metadata parsers
- `src/card.py`: interactive card rendering
- `src/image_uploader.py`: cover upload helper
- `src/media_downloader.py`: short-video download and conversion
- `src/sender.py`: Feishu card, image, and video sending
- `src/translator.py`: DeepSeek title translation
- `src/pipeline.py`: card-first, video-second orchestration
