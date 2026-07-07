# Cookie 工作方式笔记（2026-07-08）

## 一句话

各平台登录态的真相源是**远端 mac mini 上日常使用的 Google Chrome**。服务需要 cookie 时，直接从 Chrome 的本地 cookie 库解密读取，不再自己开浏览器登录。

## 数据流

```
你在 mac mini 的 Chrome 里登录 B站/YouTube/X/Instagram
        ↓（登录态由你平时用 Chrome 自然维持，网站前端 JS 自动续期）
Chrome 把 cookie 加密存在本地库（钥匙串里的密钥解密）
        ↓（yt-dlp 解析/下载前，若平台 cookie 缺失或临期）
服务用 yt_dlp.cookies.extract_cookies_from_browser("chrome") 提取
        ↓ 按平台域过滤 + 校验必需 cookie（SESSDATA / SAPISID 等）
原子写入 cookies/{platform}.txt
        ↓ 不变
temporary_cookie_file → yt-dlp 解析/下载/评论抓取
```

配置：`cookie_refresh_source: "chrome"`（默认）。`cookie_refresh_platforms` 里列出的平台才会刷新（当前 bilibili/x/instagram/youtube）。进程内按 `cookie_refresh_min_interval_seconds` 节流。

## 你要做的维护

- **保持登录**：偶尔在 mac mini 的 Chrome 里打开这些网站、保持登录即可。长期不开、登录过期了，就再登一次。没有任何命令要跑。
- **一次性授权**：首次提取时 macOS 会弹钥匙串授权（python 要读 "Chrome Safe Storage"），选**始终允许**。已完成。

## 验证是否正常（在 mac mini 桌面终端跑）

```bash
cd /Users/openclaw/workspace/feishu-link && uv run python -c "
from src.cookie_refresh import _PROFILES, _extract_chrome_cookies_sync, _platform_cookies, _has_required_cookies
raw = _extract_chrome_cookies_sync('')
for name, prof in _PROFILES.items():
    print(name, 'logged-in' if _has_required_cookies(_platform_cookies(raw, prof), prof) else 'NOT-logged-in')"
```

需要 `logged-in` 的平台显示 `logged-in` 即正常。

注意：**必须在桌面终端跑**。SSH 会话够不到钥匙串，会报 `no key found` / 全部解密失败，是假阴性。服务本身在 launchd `gui/502` 域，和桌面同一图形会话，能正常解密（已实测：日志出现 `Refreshed N cookies for <platform> from Chrome`）。

## 已知隐患 / 坑

- **钥匙串授权绑 python 二进制**：`uv sync` 重建 venv 后可能要重新授权一次；服务后台提取失败只记 WARNING、继续用旧 cookie，不阻断。到桌面重授权即可恢复。
- **Chrome 大版本改 cookie 加密格式**会破提取，升级 yt-dlp 可解。
- **B站 SESSDATA 硬过期**只能靠你重新在 Chrome 登录，无法自动续（前端只在你实际使用时续期）。
- **后备路径** `cookie_refresh_source: "browser_profile"`：老的 Playwright 持久 profile 方案（`--browser-login <platform>` 登录），仅在 Chrome 提取不可用时用；对 B站硬过期依然无解。

## 待修（与 cookie 无关，但同期发现）

「分析评论」在 YouTube 评论抓取失败时会长时间重试并**阻塞整个消息处理**，一条卡住 = 所有新消息无响应。需加超时上限 + 不阻塞主循环。
