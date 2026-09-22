# Sandbox 直连执行认证

状态：implemented
类型：bug-fix
Owner：docker/sandbox_provisioner/app.py

## 问题

隔离 OrbStack 实验中，不同用户、不同 Docker 网络的 Sandbox 可以直接请求对方执行 API 并写文件。管理服务认证没有覆盖这个入口。现有 [AIO 认证能力](https://github.com/agent-infra/sandbox/blob/main/README.md)支持 API Key，但所测 1.11.0 镜像的网关对以静态扩展名结尾的 GET 请求免认证；查询参数末尾添加 .json 后匿名下载成功，单独启用密钥不足以保护文件。

## 决策

每个 sandbox_id 使用 provisioner 主密钥通过带用途前缀的 HMAC-SHA256 派生独立 API Key，仅注入其对应 Sandbox。主密钥不进入 Sandbox。管理代理与健康检查使用目标实例的 API Key，调用方不能覆盖；SANDBOX_API_KEY 和 JWT_PUBLIC_KEY 由 provisioner 最终覆盖，不受用户或全局 Sandbox 环境影响。

启动入口将镜像网关收敛为所有路径都需认证，再运行原启动脚本；不支持预期模板的镜像显式启动失败。Docker 与 Kubernetes 都装配同一入口与认证环境。创建、发现和代理前回读实际容器/Pod 的入口与环境，缺少完整策略的旧实例返回 409。策略检查不立即终止或重建实例，既有闲置回收仍然适用。升级前必须停止接收新任务、等待任务结束并排空旧实例；409 不会关闭旧实例本身的直连接口。

## 替代方案

仅设置独立 Docker bridge 已有真实旁路证据。仅设置 API Key 留下文件下载旁路。额外自建代理服务重复了镜像已有认证能力并增加部署组件。派生密钥避免新增凭据存储；主密钥轮换时原实例需要排空重建。同一 sandbox_id 的派生密钥保持稳定，不承诺每次重建轮换。

## 后果

需要排空旧 Sandbox 后启用新策略，不能将更新 provisioner 等同于修复仍存活的旧实例。自定义镜像必须保留 AIO 的认证模板和原启动脚本；不兼容时失败，不能静默无认证启动。一个实例中的代码可以读取该实例自身密钥，但不能推导另一 sandbox_id 的密钥。该措施保护执行和文件 HTTP 入口，不承诺防御容器逃逸、Docker 管理员、宿主 root 或其他内网服务访问。

## 验证

[真实双沙箱 E2E](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/e2e/sandbox/test_direct_auth.py)通过产品 provisioner 创建不同 uid、不同 network 的两个实例。合法代理执行写文件并下载回读；A 内以无凭据和 A 自己的凭据访问 B 的命令与伪装下载路径。修复后四次请求均为 401，B 文件字节保持不变；重新创建请求复用同一 generation。恢复旧 app 后四次请求均为 200，独立回读 B 文件为 compromised；只保留 API Key 而恢复旧网关入口时，命令拒绝但两次下载仍为 200。

[供给层 unit](https://github.com/liaozd2025/agents-platform/blob/main/backend/test/unit/backends/test_sandbox_provisioner_config.py)验证独立密钥、用户和全局环境不能覆盖、健康检查和代理使用目标密钥、Docker/Kubernetes 拒绝旧策略且不立即销毁，以及不兼容模板不能启动原入口或修改模板。

```bash
docker compose exec -T api uv run --group test pytest test/unit/backends/test_sandbox_provisioner_config.py -q
docker compose exec -T api uv run --group test pytest --confcutdir=test/e2e/sandbox test/e2e/sandbox/test_direct_auth.py -q
```

E2E 需要独立 Docker 测试环境：TEST_SANDBOX_PROVISIONER_URL、TEST_SANDBOX_PREFIX、合成 SANDBOX_PROVISIONER_TOKEN，测试 runner 需要 Docker SDK 和 daemon 访问；只给测试 runner 和 provisioner 挂管理 socket。测试在 API 容器内执行，Docker SDK 复用已有 provisioner 镜像中的同版本包，未新增应用依赖。缺少 E2E 环境会显式跳过，不计为通过；本次已完整执行。--confcutdir 排除面向全应用账号和其他 Sandbox 的清理 fixture，本测试只删除自己创建的随机实例。

真实验证覆盖当前 OrbStack、默认 AIO 1.11.0 的 core 配置。监听回读仅 8080 对外，内部服务绑定 loopback；Kubernetes 只验证 Pod 装配与策略逻辑，没有真实集群网络验收。Linux Docker、browser/full 配置、生产网络与容量不在本次通过结论内。没有提交、发布或生产部署。
