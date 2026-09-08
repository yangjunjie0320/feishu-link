# 小红书图文协议与验收

所有时间为北京时间，2026-09-08。

## 最终调用协议

1. 原生页面只解析 `window.__INITIAL_STATE__.note.noteDetailMap[noteId].note`，验证目标 ID、type=normal，并提取真实标题、作者和首图。只处理 JSON 与未加引号的 undefined，不执行页面脚本。
2. BibiGPT `content.info` 使用签名访问 URL 与 `skipSubtitleTask=true`，校验 id/dbId/url、非空 images、无 audioUrl/playUrl。
3. POST `/api/trpc/contentPipeline.summarize?batch=1`，请求正文为 `{"0":{"json":{"url":"签名访问URL","promptConfig":{...}}}}`。沿用现有 Cookie/独立浏览器传输，promptConfig 设置 isRefresh=true、showTimestamp=false、showEmoji=false。提示词要求结合全部图片与正文，按笔记称呼来源，不生成视频时间轴。
4. 校验返回 id 与目标 dbId 一致且 summaryText 非空。不进入视频字幕等待，不调用 chapterSummary，不回退到旧缓存或未经核验的纯文案总结。

公开前端 build `9MrHaSvwxqFWQ6pYt4ebT` 的 `1fu43hr0zbk9c.js` 任务处理器使用 contentPipeline.fetch / contentPipeline.summarize；后者请求 url/promptConfig、读取 summaryText。最终图文客户端只取 metadata 后直接总结，没有复用视频字幕准备状态机。

## 实验证据

- 教授笔记：内容 ID `0f790168-4d9b-4d67-81ca-5638029249cf`、笔记 ID `6a9dfe020000000026038898`，《一个大学教授与新生家长的对话》，启航合伙人，六图。
- 新样例：内容 ID `5b0b9f6b-4000-41ff-8340-934e9b794201`、笔记 ID `6a9d5ea4000000000b003011`，《我愿称其为量化交易的神》，开心的麻薯，六图。
- 用户 Chrome 网页“一键总结”成功生成量化笔记，摘要出现首图中存在、正文未提及的 Black-Litterman 与 Bootstrap，证明有读图。但网页默认仍使用视频措辞和 0:00–0:05 时间戳，并附带纯文案章节。
- 23:36–23:40 远端独立临时快照的 contentPipeline.summarize 实验两条均成功，正文长度分别为 1392、2681 字符；开启上述图文提示词后没有视频时间轴。
- 教授笔记响应包含“最初读师范学院、不想当老师而考研，博士毕业后仍当老师”以及“有机化学不是我教”，分别目视核对原笔记第三、六张图片；正文只有大学教育与自觉性的概述，没有这些事实。模型图片编号与原笔记顺序有偏差，不能把编号当作准确定位。
- 量化笔记响应包含目录中的 Black-Litterman、Bootstrap、多因子定价模型，首图已目视核实。正文仅兴趣表达与话题标签。

脱敏响应见 `tests/fixtures/xiaohongshu/bibigpt-detail.json`、`professor-dialogue-summary.json`、`quant-research-summary.json`。元数据访问签名、作者 ID 与图片地址已脱敏；生成结果不含账号凭据。隔离实验报告在控制台 `maintain/reports/xiaohongshu-20260908-231334/pipeline-results/`。

## 排查过程中修正的判断

23:10–23:15 曾根据结果页重新总结组件 `1ewug1axyv9ew.js` 测试 `/api/summarize/default`，Cookie-only 和补齐同源会话头后均返回 HTTP 500 HTML。该入口不能等同于首页任务队列“一键总结”，不能据此宣称 BibiGPT 整体故障。Chrome 工具未提供网络抓包能力，上述调用路径依据来自公开前端源码及远端实测，不是 Chrome 请求抓包。

最终实现已移除未验证的 SSE 分支和新增会话头处理，保留现有传输。原始失败记录保留在控制台两个隔离报告目录中。旧教授笔记保存结果仅有视频主题与时间轴，当时不能作为图文理解证据。

## 发布验收

- 最终客户端在独立快照 20260908-234301 两样例重跑均成功，耗时 75.92 / 88.73 秒，均返回图片独有事实且无视频时间轴。
- 23:46–23:47 在既有 MediaMind 测试群真实上传首图、发送标题卡，通过隔离 Pipeline 调用按钮回调处理器，发送图文总结卡，工作提示清除。无章节卡；归档命中 1 条，BibiGPT 链接写入成功，全程 57.99 秒。此项是实际发卡与程序触发回调，不冒充人工点击或生产 WebSocket 入站验收。
- `uv run pytest -q`：1129 passed、1 skipped。`uv run ruff check .`、`git diff --check` 均通过。测试数量减少来自移除弃用的 SSE/会话头测试，新增空结果和错误内容 ID 的协议校验回归。
- 用户已授权隔离验收后 commit、push、部署。本机不启动服务；发布前版本为 1fbb59cb81c136a4b43b71eb68028b30bff1c982。部署后仍需运行 maintain/verify.sh remote。
