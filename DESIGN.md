# 飞书随身秘书：链接自动解析卡片

## 背景

用户在飞书中发送链接时，希望系统自动识别并将链接转换为结构化卡片，实现「发出即入库、发出即展示」的随身秘书体验。整个流程无需用户额外操作，感知上链接发出去就自动变成好看的卡片。

---

## 目标

只处理用户本人发出的消息，识别其中的链接，自动抓取元数据，回复一条结构化卡片。不干预他人消息，不影响原始聊天记录。

---

## 技术方案

### 核心组件

**飞书 CLI（lark-cli）**
通过 WebSocket 实时监听消息事件，以用户身份（--as user）运行，天然拥有用户本人的消息权限。

**链接解析器**
根据链接类型调用不同解析策略，提取标题、描述、封面图、来源域名等元数据。

**卡片发送器**
将元数据组装成飞书消息卡片（interactive 类型），回复到原会话或写入专属归档频道。

---

### 整体流程

```
用户在飞书客户端发送一条消息
            ↓
lark-cli WebSocket 收到消息事件
            ↓
过滤：sender_id == 用户本人？
    否 → 丢弃
    是 → 正则匹配消息中的 URL
              没有链接 → 丢弃
              有链接 → 进入解析流程
                        ↓
                  识别链接类型
                  （YouTube / 文章 / 其他）
                        ↓
                  抓取元数据
                  （标题、封面、描述、时长）
                        ↓
                  组装飞书消息卡片
                        ↓
                  lark-cli 发送卡片到目标会话
```

---

### 链接类型支持

| 类型 | 解析方式 | 抓取字段 |
|------|---------|---------|
| YouTube | YouTube Data API v3 | 标题、封面、时长、频道 |
| 网页文章 | Open Graph / HTML meta | 标题、描述、封面图、站点名 |
| 其他 URL | 基础 HTML 解析 | 标题、域名 |

---

### 卡片结构设计

```
┌─────────────────────────────────────┐
│  [封面图]                            │
│                                     │
│  标题（最多两行）                     │
│  来源网站 · 时长或发布时间            │
│                                     │
│  描述摘要（最多三行）                  │
│                                     │
│  [ 打开链接 ]                        │
└─────────────────────────────────────┘
```

---

### 卡片发送位置

提供两种模式，可通过配置切换：

**模式 A：原会话回复**
在用户发消息的同一个会话里，以 Thread 形式回复在原消息下面。用户发出链接，卡片紧接着出现，视觉上像自动展开。

**模式 B：归档到专属频道**
卡片发送到一个专属的私聊或群组（如「我的链接库」），原会话完全不受影响。适合不想打扰他人的场景。

---

## 工具链与依赖

### lark-cli（`@larksuite/cli`）

- 安装方式：`npm install -g @larksuite/cli`，在 Docker 容器内随 Node.js 一同安装。
- 身份认证：`lark-cli auth login --recommend`，凭据存储于 `~/.config/@larksuite/cli/`，通过卷挂载注入容器。
- 监听命令：`lark-cli event consume im.message.receive_v1 --as user`，输出为 NDJSON，每行一个事件对象。
  - 就绪信号：子进程在 stderr 打印 `[event] ready event_key=im.message.receive_v1` 后方可开始读取 stdout。
  - 关闭信号：关闭 stdin（或传 `--max-events` / `--timeout`）触发优雅退出。
  - 事件字段：`sender_id`（open_id）、`message_type`、`content`、`chat_id`、`chat_type`、`message_id`。
- 回复命令（模式 A，Thread 回复）：
  ```
  lark-cli im +messages-reply --message-id <om_xxx> --msg-type interactive \
      --content '<card-json>' --reply-in-thread --as user
  ```
- 发送命令（模式 B，归档频道）：
  ```
  lark-cli im +messages-send --chat-id <oc_xxx> --msg-type interactive \
      --content '<card-json>' --as user
  ```
- 获取自身 open_id（启动时查询一次）：`lark-cli auth status --format json`。

### Python 依赖（`uv` 管理）

运行时：`httpx`、`beautifulsoup4`、`lxml`、`pydantic`、`pydantic-settings`、`pyyaml`、`python-dateutil`、`tenacity`；可选：`google-api-python-client`（YouTube Data API v3）。

开发时：`pytest`、`pytest-asyncio`、`respx`、`ruff`、`pyright`。

### 链接解析器错误策略（`ParserError`）

- HTTP 请求失败或非 2xx 状态码 → 抛出 `ParserError(url, status_code, reason)`，禁止静默跳过。
- OG meta 解析失败 → 降级至 fallback 解析器（仅提取 `<title>` 和域名）。
- fallback 也失败 → 抛出 `ParserError`，pipeline 捕获后在日志中以 ERROR 级别记录完整上下文，继续处理同一消息的其他 URL。
- 发送失败 → tenacity 重试（3 次，退避 1 s/3 s/9 s），耗尽后记录 CRITICAL 并继续，不终止监听进程。

## 实现计划

### 第一阶段：核心监听与解析

目标：跑通主流程，能够识别自己发出的链接并回复卡片。

1. 配置 lark-cli，完成用户身份授权
2. 实现 WebSocket 事件监听脚本（`lark-cli event consume` 子进程封装）
3. 实现 sender_id 过滤逻辑（对比启动时查询的自身 open_id）
4. 实现 URL 正则提取（兼容飞书富文本格式）
5. 实现 Open Graph / HTML meta 解析，含 fallback 降级
6. 实现飞书 interactive 卡片组装（标题、描述、打开链接按钮）
7. 实现卡片发送（模式 A：Thread 回复；模式 B：归档频道）

交付物：可在 Docker 容器中运行的 Python 服务（`python main.py`）

---

### 第二阶段：YouTube 专项支持

目标：YouTube 链接展示封面图、时长、频道名。

1. 接入 YouTube Data API v3
2. 从链接中提取 video_id
3. 拉取视频详情并渲染卡片

---

### 第三阶段：稳定性与配置化

目标：长期可靠运行，支持个性化配置。

1. 进程守护（pm2 或 systemd）
2. 配置文件：目标会话 ID、模式 A/B 切换、链接黑名单
3. 错误处理：解析失败时静默跳过，不发送损坏卡片
4. 日志：记录每次解析结果，方便回溯

---

## 关键约束

**监听范围**
CLI 以用户身份运行，只能监听机器人或用户账号在场的会话。用户与他人的私聊若未授权，无法监听。建议的覆盖范围：将机器人拉入常用群组，或使用用户身份授权。

**YouTube 在国内不可直接访问**
YouTube Data API 需要代理或在境外服务器运行。如果本地没有代理，可以降级为抓取 YouTube 页面的 og:title 和 og:image，无需 API Key，但无法获取时长。

**飞书卡片 interactive 类型**
接收他人发来的 interactive 卡片时，CLI 暂不支持紧凑解析，但发送 interactive 卡片完全正常，不影响本方案。

---

## 后续可扩展方向

- 自动打标签（根据域名或关键词分类）
- 写入飞书多维表格，建立个人链接知识库
- 每周汇总推送，生成「本周收藏」摘要
- 支持更多链接类型（GitHub、Twitter、微信文章）
