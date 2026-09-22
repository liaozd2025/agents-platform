# Runtime CI 前置与行为检查接线

状态：implemented
类型：testing
Owner：.github/workflows/system-tests.yml

## 问题

Runtime CI 的 Docker Hub MinIO 拉取失败使后续运行链路测试全部跳过。文档构建提前调用 Pages 配置，仓库没有 Pages 站点时无法进入构建。CLI 行为和真实浏览器恢复缺少 PR 接线，文件安全测试辅助函数又把缺失智能体标识的成功响应标为跳过。现有工程检查器未覆盖若干实际必要步骤与测试辅助文件的触发范围。

## 决策

Compose 和隔离验收使用 Quay 的同版本 MinIO，并固定与原镜像一致的多架构摘要。安装脚本预拉同版本 tag，以兼容现有加速器的重新打标签流程；启动仍由 Compose 的摘要约束。镜像版本与数据格式保持原样；变更只解决可获取性，不构成 MinIO 升级或安全维护承诺。

文档 workflow 的 build job 直接安装依赖并构建，Pages 配置和写权限属于部署 job。独立 CLI workflow 在 PR、分支推送和版本 tag 上执行现有测试；发布仍单独手动触发。后端、Web 和 Ruff 检查支持手动选择分支运行，以便在合入前验收测试分支。Runtime job 复用已经构建的 API/provisioner 镜像，在另一个一次性 Compose project 中运行现有 Chrome 恢复测试并保留截图。隔离运行脚本显式补拉缺失的基础镜像，不依赖开发机器的镜像缓存。Runtime job 也从解析后的 Compose 读取实际 Sandbox 镜像并先行拉取，使外部镜像下载不占用 Agent 场景的终态等待时间。确定性 Agent 测试整组共用一个临时供应商，结束后统一删除，避免逐例删建同一配置触发跨进程缓存空窗；该 fixture 不验证模型配置故障恢复，后者属于 #125。失败日志保留完整时间戳以定位首次失败和后续连带错误。

文件安全辅助函数对缺失标识直接失败，真实 HTTP 文件安全测试进入 Runtime 选集。工程检查器沿用现有 workflow 契约，补齐 PI 历史、OA 资料、DurableTask、文件安全、浏览器、文档、CLI 以及相关触发路径；DurableTask 的准备、重启和断言各有独立步骤，保持原先执行顺序。一次性 API 测试显式将 entrypoint 设为 uv，防止服务固定启动入口吞掉 pytest 命令。

## 替代方案

保留本地缓存或吞掉拉取失败无法验证干净 runner。为浏览器新增一套测试框架和完整独立构建会重复当前可用验收脚本。把 Pages 配置作为构建前置会让未开通部署的仓库失去文档检查。

## 后果

必要检查的失败继续阻断所在 workflow。分支保护、required checks 与生产配置不在此决定范围内。Web 或 CLI 变更会触发 Runtime 检查，增加 CI 耗时；浏览器脚本依赖 runner 的 Chrome、Node 和 Python，依赖缺失明确失败。容器生成的合成凭据文件保留 0600，所有者改为宿主测试用户；结束时容器停止后归还一次性目录所有权，使 Linux 非 root runner 可以完整清理。

## 验证

删除必要步骤和缩窄 PR 路径的负向测试由现有 checker 拒绝。恢复构建前 Pages 配置会使 workflow 回归测试失败；恢复 helper 的 skip 会使错误响应回归失败，正常响应仍可创建线程。文件安全通过真实 HTTP 验证拒绝结果与宿主文件未被覆盖。Chrome 恢复验收读取最终 DOM，并独立查询 PostgreSQL 中的 Request、Run 和 Message。

本地冷缓存验证使用新建 BuildKit 内容存储，完整下载 Linux amd64 MinIO 镜像层后执行版本检查。冷下载后的对象写入与回读通过。该证据仅证明镜像可冷获取和运行；应用测试使用依赖匹配的本地 API 镜像。本地验证与 GitHub 干净 runner 的整套 workflow 分开记录。测试分支的首轮远端检查中，工程契约、后端、Web、Ruff、CLI 与文档构建通过；Runtime 已通过冷构建、readiness、事务及 Run 审计检查，但确定性 Agent 组出现 Sandbox 操作超时与模型缓存空窗，后续步骤未执行。完整远端通过前不能关闭 #117；本决定不代表生产上线。
