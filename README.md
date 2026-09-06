# feishu-link

Feishu link assistant that listens to messages visible to a Feishu custom app, extracts links, fetches metadata, replies with compact interactive cards, and then tries to append downloadable short videos.

The project is designed for personal or team link collection workflows: send a supported link in Feishu, get a readable card first, and get the original video only when it can be downloaded and uploaded safely.

## Features

- Feishu WebSocket listener through a custom app.
- Compact interactive cards with cover image, platform label, title, author or channel, duration, and source button.
- Multi-platform parsing for YouTube, Instagram, TikTok, Bilibili, X/Twitter, and normal web pages.
- Video metrics when available: views, likes, comments, and reposts.
- Optional short-video append. Videos up to 180 seconds are downloaded, converted to Feishu-friendly MP4, uploaded, and sent after the card.
- **BibiGPT Integration**: Mention the bot (`@bot`) with a YouTube or Bilibili link to receive an AI-generated summary followed by BibiGPT's timeline chapter summary in collapsed Feishu cards.
- Manual download command: mention the bot and send `下载 <link>` to force video download instead of summarization, without the automatic short-video duration cap.
- Card action buttons: supported video cards include `总结视频`, `分析评论`, and `下载视频` actions.
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

### 3. Run locally for development

```bash
uv sync --extra dev
uv run python main.py --config config.yaml
```

When you see the WebSocket long-connection log, send a supported link in a Feishu conversation where the app can receive events.

## Run in production (native + launchd)

The service runs natively, with no container. `uv` manages Python and resolves dependencies from `pyproject.toml`; `ffmpeg`, `ffprobe`, and `deno` must be on `PATH`. Production uses a macOS launchd LaunchAgent so the process restarts on crash and survives reconnects. Playwright is pinned to a known browser build so browser-mode summaries and cookie refresh do not require a fresh Chromium download on every dependency refresh.

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

Set real credentials in `config.yaml`. If you use cookie files, configure local paths:

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

For production on macOS, run it under a launchd LaunchAgent: point `ProgramArguments` at `.venv/bin/python main.py --config config.yaml`, set `WorkingDirectory` to the project root, put `/opt/homebrew/bin` on the agent's `PATH` (launchd does not source your shell profile, and `yt-dlp` needs `deno`/`ffmpeg`), and enable `KeepAlive` so it restarts on crash. The service uses `main.py` as the only runtime entry point.

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
| `max_video_file_mb` | `30` | Max uploaded video size; Feishu `im/v1/files` rejects files over 30 MB |
| `allowed_video_platforms` | Config example | Platforms allowed for video append |
| `cookie_file` | `cookies/cookies.txt` | Unified Netscape cookie file path |
| `bibigpt_access_mode` | `web` | `web` uses tRPC over HTTP; `browser` reuses Chromium login cookies in isolated request pages and uses the desktop content pipeline for Bilibili |
| `bibigpt_base_url` | `https://aitodo.co/zh` | Base URL for BibiGPT |
| `bibigpt_timeout` | `120.0` | Timeout in seconds for BibiGPT summary and chapter-summary requests |
| `bibigpt_browser_profile_dir` | `browser-data/bibigpt` | Chromium profile path for BibiGPT browser mode |
| `bibigpt_browser_headless` | `true` | Run Chromium headless in browser mode |
| `bibigpt_browser_timeout` | `120.0` | Total request budget including profile-lock waiting, startup, navigation, and fetch; browser cleanup may add a bounded delay |
| `bibigpt_web_queue_enabled` | `true` | Use the desktop server content pipeline for Bilibili in browser mode, also as recovery for risk-control errors |
| `bibigpt_web_queue_poll_seconds` | `45` | Interval between server subtitle-status checks |
| `bibigpt_web_queue_wait_seconds` | `600` | Content-preparation wait budget; expiry reports pending rather than claiming the server task failed |
| `bibigpt_default_prompt` | Empty | Optional default custom prompt. Leave empty to use BibiGPT's built-in prompt |
| `deepseek_api_key` | Empty | Enables title translation, BibiGPT summary rewriting, and faithful Chinese formatting of BibiGPT chapter summaries; chapter formatting falls back to the BibiGPT original when unavailable |
| `deepseek_base_url` | DeepSeek API | Optional compatible API endpoint |
| `deepseek_model` | `deepseek-v4-flash` | DeepSeek model used for translation, summary rewriting, comment analysis, and chapter-summary formatting; explicit configuration overrides this default |
| `enable_title_translation` | `false` | Translate non-Chinese titles when true |
| `cookie_refresh_enabled` | `true` | Refresh expiring platform cookies from a persistent Chromium profile |
| `cookie_refresh_platforms` | `["bilibili"]` | Platforms whose cookies are refreshed before yt-dlp parse/download |
| `cookie_refresh_profile_dir` | `browser-data/cookies` | Parent directory for per-platform persistent browser profiles |
| `comment_analysis_max_comments` | `200` | Max comments to collect per on-demand comment analysis action; runtime is hard-capped at 200 |
| `comment_analysis_prompt_comments` | `120` | Max collected comments included in the LLM prompt |
| `comment_analysis_timeout` | `120.0` | Timeout in seconds for the comment analysis LLM call |

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
cookie_file: "cookies/cookies.txt"
```

For refreshable platforms, prefer per-platform files such as `cookies/bilibili.txt`.
Run `python main.py --browser-login bilibili` once to seed the persistent profile
before relying on automatic refresh. X and Instagram can also be refreshed after
logging in with `--browser-login x` / `--browser-login instagram` and adding them
to `cookie_refresh_platforms`.

Only provide cookies for accounts you control. Cookie files are ignored by Git and should not be shared.

## Dependency Mirrors

The project does not commit `uv.lock` and does not pin a package index in `pyproject.toml`. Deployment scripts remove any locally generated `uv.lock` before `uv sync`, so dependencies are resolved from the current `pyproject.toml` constraints each time.

Use the default upstream PyPI, or choose a mirror per machine/per run:

```bash
UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/ uv sync
```

Deno and Playwright browser binaries are downloaded from their upstream release hosts.

## Title Translation

Title translation is optional. When enabled, the service calls DeepSeek only if the detected title does not contain Chinese text.

```yaml
enable_title_translation: true
deepseek_api_key: "sk-xxx"
deepseek_model: "deepseek-v4-flash"
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

Before upload, the downloader prefers the lowest available video quality to stay within Feishu upload limits, then converts videos to MP4 with H.264 video, AAC audio, `yuv420p`, faststart metadata, and a bitrate budget derived from `max_video_file_mb` when duration is known. The uploader probes the converted file duration with `ffprobe` and sends duration in milliseconds so Feishu can generate a correct preview.

If any video step fails, the service logs the reason and does not send an extra failure message to the chat.

## Manual Downloads

Mention the bot and start the message with `下载` followed by a link to force video download and send:

```text
@bot 下载 https://youtu.be/example
```

Manual downloads bypass BibiGPT summaries and the automatic short-video duration cap. They also use the lowest available video quality and size-targeted conversion to reduce upload failures. If the link cannot be parsed as a downloadable video, has no downloadable candidate, produces a final converted MP4 larger than `max_video_file_mb`, or fails Feishu upload/send, the bot replies with the failure reason.

New link cards include only the action buttons supported by the platform:

- `总结视频` triggers the same BibiGPT summary-and-chapter-summary path as mentioning the bot with a YouTube or Bilibili URL.
- `分析评论` fetches up to 200 comments, ranks comments by likes and replies, translates the top 3, and sends a fixed-template comment analysis card with total-comment and sample counts.

Cards omit the action row when neither feature is supported. The `打开链接` and `下载视频` buttons are no longer included in new cards; existing cards keep their original layout. Summary work uses Feishu's `Typing` reaction; comment analysis uses `THINKING`. When both run on one message, each reaction remains until its corresponding work finishes.

BibiGPT summary output is treated as draft material. When `deepseek_api_key` is configured, the bot sends that output through DeepSeek with fixed Chinese Markdown output requirements before rendering the Feishu card. After the summary card succeeds, the bot requests BibiGPT's `timeline` chapter summary using the returned content ID and appends a collapsed `字幕总结` card. DeepSeek may translate and proofread the chapter introduction, titles, and summaries, but timestamps remain code-owned and raw subtitles are never sent or used as a fallback. If the BibiGPT account cannot generate chapter summaries, the original summary remains available and the bot reports that the chapter summary is unavailable.

In browser mode, Bilibili requests first prepare content through BibiGPT's desktop server protocol, confirm a server content ID, and observe subtitle readiness before requesting a summary. Content preparation uses the desktop's `content.info` fallback after a recoverable pipeline failure and selects the matching summary endpoint. Request pages share login cookies without restoring the browser profile's old local task queue. Temporary summary errors receive bounded cache-permitting recovery; waiting expiry retains the content link and does not trigger the failure cooldown. See the [investigation and verification limits](docs/bibigpt-reliability.md). The legacy `bibigpt_web_queue_regenerate` setting remains accepted but no longer triggers an extra generation.

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

### BibiGPT summary is sent but the chapter summary is unavailable

The ordinary summary is intentionally sent first. The chapter request reuses its content ID and the existing BibiGPT login state, but chapter summaries may consume BibiGPT quota or require a paid account. Check logs for the lookup status and reason. The service does not fall back to a complete transcript, start a transcription task, or extract subtitles with yt-dlp.

### Feishu video preview shows the wrong duration

Make sure `ffmpeg` and `ffprobe` are available on `PATH` and that uploaded videos are converted before sending.

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
