# Content cards and operations

The card path is independent from video preparation. `Dispatcher.parse_card` returns metadata, `complete`/`partial`/`unavailable`/`unsupported`, content presence, failure category and sources. `prepare_video` runs after delivery for automatic video append, the manual command and legacy download callbacks. YouTube/Bilibili summary actions remain available; verified TikTok/Douyin videos also support on-demand summaries and chapters. Platform labels link to the original share URL; titles use the canonical content URL when available. Partial and unavailable cards retain this link without adding action buttons. Douyin supports cards, archive and on-demand video summaries; photo posts have no summary action, and video download/comment actions remain disabled.

A card timeout or missing metadata does not block independent media preparation after the result card is sent. Media extraction must still confirm a video and its duration before automatic download; the 180-second and final 30-MB limits remain. Unsupported content, confirmed photo posts and explicit access restrictions are not retried through automatic download. Unavailable cards remain excluded from the archive. Manual download failures receive an explicit reply.

Instagram media extraction tries public access first. Expired login cookies can make otherwise public Reels fail with HTTP 400. Only an explicit login requirement enables one fallback with the configured cookies, within the same media preparation budget; rate limits and verification failures do not switch identities. The successful cookie mode follows the metadata into the actual download, so an anonymous success never silently reattaches stale cookies. Existing cookie behavior for other platforms is unchanged.

When Instagram omits `video_duration`, the isolated media worker preserves the exact total duration from that same video's static DASH manifest. It never estimates duration from bitrate or fragment counts, and dynamic or invalid manifests remain unknown. Fractional Instagram durations round up before the automatic duration limit is checked. The download, conversion and upload path remains unchanged.

## Sources and completeness

| Platform | Sources, in order |
| --- | --- |
| TikTok video | Official public oEmbed, page metadata, isolated Chrome |
| TikTok photo | Target post page data, isolated Chrome |
| Instagram | Existing media/info with cookies, target page, independent anonymous public page, isolated Chrome; preserve carousel `img_index` |
| YouTube | Data API when configured, public oEmbed, page, isolated Chrome |
| X/Twitter | Public oEmbed, authenticated GraphQL, page, isolated Chrome |
| Douyin | Public target page data and isolated Chrome; `/video/`, `/note/`, `v.douyin.com`, valid share pages |
| Bilibili | Existing metadata extraction and page fallback |

Invalid Instagram cookies can trigger an account checkpoint even for public content. The anonymous page source uses a separate empty cookie jar, preserving the existing login state.

Each source fills missing or placeholder fields without replacing actual content already obtained. One HTTP page supplies structured data, OG and title together. Browser extraction checks the target content ID and excludes recommendations. Domain names, slogans, login/verification pages and mismatched posts are rejected. X articles provide a title and excerpt, without a full-article promise.

TikTok photo covers accept both `displayImage` and `imageURL.urlList` on the first image only, preserving signed CDN candidates. In metrics mode, verified content also survives the browser's own cleanup timeout; caller cancellation still propagates.

Completeness requires actual text and the image expected for that post. Verified text-only posts and images originally without captions are judged by their actual content. A parsed cover URL is only a candidate: failed download/upload makes the delivered result partial. Up to three candidates retain signed query parameters and use source headers. Translation failure preserves the original.

YouTube and TikTok additionally fill missing interaction counters before sending the card. YouTube uses its configured Data API or the target watch page (no new API key required); TikTok uses the target video/photo statistics. A page read gets at most five seconds, followed by isolated Chrome when needed. Extra reads share `card_metrics_timeout` (default 15 seconds, 0 disables enrichment) within the existing 45-second parse budget, including browser admission and cleanup. Sources already read are not fetched again. Known zeroes are retained; hidden/unavailable counters stay absent. Recommendation statistics are excluded by target ID and page scope. Failure preserves the prepared content and is recorded by `card metrics` logs with filled/missing fields and elapsed time. Counts are snapshots cached with the card for ten minutes; there is no extra card update or comment-list/media extraction. Other platforms do not make extra requests for counters.

## Budgets and isolation

- Card preparation: 60 seconds total, parsing at most 45 seconds including admission/cookies, cover and translation in parallel up to 10 seconds, bounded cleanup. A single final card follows the existing work reaction.
- Card parses: global 4, per platform 2. Duplicate content joins one in-flight operation; cancellation of one waiter does not cancel another.
- Complete metadata: 10-minute, 256-entry memory cache. Callers get independent copies and their original share URL. Partial/error results are not cached; cover failure invalidates cached metadata.
- Browsers: global 2, per platform 1, independent `browser-data/cards/<platform>` profiles, up to 30 seconds including admission and cleanup. Existing summary/comment profiles remain separate.
- Media metadata: at most two subprocesses, with a timeout and process-group termination. Metadata extraction does not occupy card worker threads.
- Recovery: at most one network retry and one refresh after a clear authentication failure per parse; concurrent refreshes for one platform share work. Rate limiting, login, verification, deleted content and region restrictions have separate failure categories.
- Feishu card sends and image uploads use asynchronous requests with deadlines. Each logical send gets one UUID, reused for internal retries.

## Archive rules

Only a successfully sent card with real content is enqueued. A real caption with no cover can be archived. Placeholder-only and unavailable results cannot create records. A partial result fills only empty fields in an existing row; a complete result follows the existing update policy. Remarks and BibiGPT links are preserved. No parse-status field is added to the table.

Archive requests validate the JSON business code as well as transport success. Older production SDK versions can otherwise treat HTTP 200 business errors as success. A rejected replacement must never delete the previous record. Entries without a sender omit the People field. TikTok `is_from_webapp`/`sender_device` and Instagram `igsi` are ignored for archive comparisons; actual content selectors such as `img_index` remain significant.

For Instagram, an explicit carousel index without matching child-media data cannot use the first thumbnail as a substitute. The caption and author remain available, and the missing selected image is reported if no later source resolves it.

## Runtime configuration

`config.example.yaml` lists `card_*` budgets/concurrency, `media_metadata_*`, and the Douyin allow patterns. Production must add `douyin.com` and `iesdouyin.com` to the allowed domains and remove their old blacklist patterns. Keep Douyin out of `allowed_video_platforms`.

Public Douyin content starts anonymously. If a post requires login, sign in with the existing remote Chrome and export a Netscape cookie file to `cookies/douyin.txt`, then set `platform_cookie_files.douyin` to that path. It never inherits the generic cookie file. The cards profile is independently configurable with `card_browser_profile_dir`; browser fallback seeds only the relevant platform cookies. No paid parser, new proxy or curl-cffi is required.

## Validation and release

Run `uv run pytest -q` and `uv run ruff check .` from `code/`. Tests cover target matching, malformed responses, placeholder rejection, partial content, timeout/cancellation, cache isolation, subprocess cleanup, old SDK compatibility, send UUID reuse, archive protection and preserved video actions.

Run real probes only in a temporary code snapshot on the production host, with separate card profiles and local copies of existing cookie files. Reuse production Python/dependencies to catch SDK differences. Check at least five public samples per platform, including different supported types and historical failures, then validate final cards and archive in the existing test chat. Do not report successful Feishu delivery as successful parsing.

Before release record the production Git revision and back up `config.yaml` on that host. Push, fast-forward the production checkout, merge only the required settings and restart `gui/502/com.feishu-link.agent`. If dependencies did not change, preserve the current environment. Verify with `maintain/verify.sh remote` and runtime logs. Roll back to the recorded revision and configuration, restart the same launchd job, and retain all login state.

Logs distinguish `card parsed`, `card prepared` and `card outcome`, recording platform, completeness, cover result, delivery, duration, sources and reason. Use preparation duration for P95 and content completeness plus cover outcome for complete-card rate.
