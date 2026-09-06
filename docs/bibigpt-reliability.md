# BibiGPT 稳定性调查（2026-09-06）

时间均为北京时间。调查对象为生产 `cc9f57c`，以及当日 BibiGPT 工作台 v4.614.0 公开加载的前端脚本；不把旧 v1 开源代码当作当前服务实现。

## 已确认的问题

1. **本地排队被误当作服务器接收。** 工作台先写本地任务队列，立即返回本地 ID 并提示入队，随后才异步执行网络请求。旧代码等输入框清空/提示后马上关闭页面，未确认服务器接收。重新打开工作台又会恢复本地任务，和我们自己的同步总结轮询同时运行。
2. **轮询的并非任务状态。** `summaryBySetting` 即使 `isRefresh=false`，缓存未命中也可能执行新生成。旧代码每45秒调用它，实际日志中出现524及多次网络错误；不能据此判断后台字幕任务的状态。
3. **最后错误被初始风控覆盖。** 9月5日22:29提交的《土地收入，下降三分之二了》（BV1jCty6fEcE，9分22秒）后续六次查询均报“网络波动”，22:39等待耗尽后却把初始“平台风控”作为终态发给用户。现有日志没有其网页最终完成状态，因此不能声称网页任务失败。
4. **实际请求缺少超时。** 原先120秒配置只约束启动和导航，不约束 `page.evaluate` 内的 fetch；挂起会占住共用 profile，600秒等待预算也无法中断它。
5. **恢复路径不完整。** HTTP成功内嵌tRPC错误被误记成状态200；只有第一次出现风控才进队列，普通恢复中后来出现风控会被当作普通未就绪吞掉。

## 网页实际协议与改动

| 阶段 | 网页协议 | 客户端采用方式 |
| --- | --- | --- |
| 内容准备 | `contentPipeline.fetch`，目标subtitle | 等服务器返回detail.dbId，正常请求forceFresh=false |
| 等待字幕 | `contentPipeline.observe`，按contentId查询 | 只看subtitle.status及错误元数据；不重新提交总结 |
| 总结 | `contentPipeline.summarize` | ready后传原promptConfig/模型，读取summaryText |
| 章节 | `video.chapterSummary` | 保持现有中文timeline及展示边界 |

前端还会为界面读取字幕正文，但总结请求只需要URL和promptConfig。本项目据此省略字幕正文读取，不自行提取或总结字幕；省略这一步在请求数据依赖上成立，仍需真实账号端到端验证。与网页最终跳转及章节请求一致，章节及分享链接使用fetch返回的内容ID，不依赖总结响应是否另带id。

B站在browser模式、队列开关启用时直接采用这条流程，避免先触发一次同步风控。其他平台继续兼容原流程。字幕准备完成后，总结调用若遇超时或暂态5xx，最多三次允许缓存的补取，不重建字幕任务、不重复强制生成。等待状态、上游明确失败、认证失败和本地等待超时分别报告，日志保留阶段、contentId和最后错误；超时不宣称后台任务已失败。源平台提取所需授权与BibiGPT会话401/403分开提示。仍存在上游服务故障、登录失效和协议变化的可能，不能保证所有视频成功。

## 证据来源与限制

- [当前工作台处理器](https://aitodo.co/_next/static/chunks/0m91l1g-z7x_n.js)：提交输入、内容准备、observe状态机、最终总结。
- [当前本地队列实现](https://aitodo.co/_next/static/chunks/2kkj15h5q7c97.js)：本地任务ID、异步启动及重新打开时恢复。
- [当前入队提示实现](https://aitodo.co/_next/static/chunks/3d9e4l1vhw6u4.js)：提示早于服务器确认。
- [官方批量总结说明](https://docs.bibigpt.co/function-usage/platform-function/multi-link-batch-summarize)与[完成通知说明](https://docs.bibigpt.co/function-usage/platform-function/summary-complete-notification)：产品允许后台完成，但这不代表本地入队提示等于服务器已接收。
- [官方接口说明](https://docs.bibigpt.co/api-reference/introduction)：isRefresh控制缓存，开放API与网页并未完全一致；不擅自用另一套Bearer API替换现有账号接入。

公开前端可以确认协议与客户端先后顺序，不能确认B站风控背后的上游IP/cookie，或后台具体使用了哪种转写服务。普通网页的本地字幕提取分支要求Tauri环境，常规Chromium不会进入，因此这里采用服务端subtitle目标。

本地Python 3.13全量离线验证为495项通过，排除1项真实飞书发送测试；Ruff和git diff --check通过。验证包括协议响应驱动的测试、真实挂起协程的超时/取消测试，以及完全拦截网络的真实Chromium隔离/cookie同步试验。生产运行Python 3.14.3，尚未在该环境运行本次全套测试。

当前可连接的浏览器未登录生产账号；使用现有导出cookie做一次只读HTTP observe查询返回401，不能替代browser模式的真实账号联调。尚未通过其网页记录核验上述单个任务的最终状态，也未向生产账号提交新总结任务。本次不部署生产。
