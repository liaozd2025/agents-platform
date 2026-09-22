# 长聊天分批展示与流式渲染合并

状态：implemented
类型：bug-fix
Owner：web/src/components/AgentChatComponent.vue

## 问题

长历史一次挂载全部消息，流式 Markdown 随每帧文本变化重做转换及 DOM 替换。#94 的 500 条各 1k 历史与 100k 累计文本样本已复现输入阻塞；主聊天卸载还需要验证后台线程资源关闭。

## 决策

主聊天及子线程消息列表先展示最近 20 个显示条目，通过“查看更早消息”每次增加 20 个；完整历史仍保留在权威消息状态，引用、导出和运行归属不截断。流式 Markdown 合并到至多每 100ms 一次渲染，非流式和终态立即刷新最后全文。卸载主聊天时清理所有线程的订阅与 smoother；useAgentThreadState 的 disposed 边界阻止迟到请求、finally 和重连回调重新创建状态。

## 替代方案

虚拟列表引入动态高度和滚动定位职责，当前分批查看已满足目标。把完整 Markdown 移到 Worker 需要拆分依赖 DOM 的净化与预览，成本更高。流式改纯文本会改变阅读语义，故保持完整 Markdown 渲染并合并更新。

## 后果
分批展示会增加查看早期消息的点击；历史数据仍保留在内存，不承诺总历史内存有界。单次极大 Markdown 的转换成本仍随正文增长，100ms 合并不等于生产延迟 SLA。仅使用合成数据和本地模型，浏览器指标注明构建、设备及测量边界。

## 验证
| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
| --- | --- | --- | --- | --- | --- |
| 500 条各 1k 历史初次展示仍可输入，旧历史可继续查看 | 全部挂载造成主线程长任务 | 主聊天及 ThreadMessageList 的显示窗口 | 原组件探针和真实主路由 Chrome，DOM/输入/内存回读 | 原实现同负载基线 | Passed |
| 100k 累计文本流式更新降低重复转换，结束全文完整 | 每帧累计 Markdown 重渲染或尾部丢失 | MarkdownPreview 与现有 stream smoother | 真浏览器合成协议，最终 DOM 与源文本核对 | 旧实现同样本；固定时钟下的真实 DOM 连续更新、终态及卸载 | Passed |
| 切换可继续查看，卸载后所有线程资源释放 | 只清理当前线程，后台订阅继续 | useAgentThreadState 与主聊天卸载 | 双线程订阅/缓冲与 DOM 销毁检查 | 移除 disposed guard 后，迟到 active-run 响应再次打开订阅 | Passed |

组件探针复用 #94 的 500 条各 1k 历史与 100k 累计文本，production build + Chrome headless shell 147：历史输入命令往返从 1113–1148ms 降至 61–66ms，DOM 节点 56513 降至 2274；累计输出从 10.29s 仍未完成降至 1.59s 完成，采样堆峰约 50.3MiB 降至 25.7MiB。

主路由使用真实认证/API 与 Vite dev + Chrome 153，消息由浏览器注入合成协议，性能结果不等同后端业务 E2E。相同三次 500 条历史基线输入往返 2232–2707ms，修复样本 82–427ms；DOM 90845 降至 3967；50ms 采样堆峰 168–186MiB 降至 102–105MiB。100k 加 30 次增量从 12.68s 降至 2.13s，最大输入命令往返 558ms 降至 376ms，堆峰 149MiB 降至 130MiB。末尾大块渲染仍有可见成本；这些值是本机对照，不是 INP 或生产 SLA，采样可能漏掉更短峰值。

`bash backend/test/e2e/chat/run_render.sh <API_IMAGE> <PROVISIONER_IMAGE>` 进入真实主路由，断言最近 20 条、逐批展开全部 500 条、独立去 Markdown 标记后的完整 DOM、切换后窗口重置、卸载后的空线程状态及四个订阅终止。固定时钟下直接挂载 MarkdownPreview，连续十次更新只保留一个定时器，释放后显示最新文本，终态即时刷新，卸载清理未触发定时器；原实现的 DOM 提前变为最后文本，守卫拒绝。脚本接入 Runtime System Tests，截图包含浅色与深色移动视口。

`web/test/unit/agentRequestQueue.test.js` 用真实 composable 和延迟 API 响应验证卸载竞态；移除 disposed guard 时因重新开启一次订阅而失败，修复后为零，threadStates 保持空。相关窗口 unit 保留完整源数据引用及跨轮次顺序。生产及付费 provider：Not run。

本地最终检查：前端 lint、454 项 unit 与 build 通过；后端全量 unit 2344 passed / 53 skipped；工程契约及其 63 项 unit、docs build、shell 语法和 diff 检查通过。远端 CI 的实际结果由对应 PR 记录。
