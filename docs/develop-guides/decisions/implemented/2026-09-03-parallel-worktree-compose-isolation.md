# 并行 worktree Compose 隔离

状态：implemented
类型：feature
Owner：docker-compose.yml

## 问题

开发 Compose 固定容器名、网络名、宿主端口和 `docker/volumes` 状态目录。两个 worktree 同时启动时会争用同一组宿主资源，也可能让不同代码或 Schema 的进程写入同一份持久数据。

## 决策

Compose project 名拥有容器、网络和本地镜像身份，`YUXI_STATE_DIR` 拥有 PostgreSQL、Redis、MinIO、Milvus、Neo4j、用户文件和 Skill 投影等状态根，各服务宿主端口使用 `YUXI_*_PORT` 变量。默认值继续对应单 worktree 的既有端口和 `./docker/volumes`，不配置槽位时仍可直接运行 `docker compose up -d`。

删除开发环境固定 `container_name` 和 `APP_DOCKER_NETWORK` 网络名，让 Compose 按 project 生成身份。命令、测试和排障文档只按 service 名操作。Redis 端口优先读取 `YUXI_REDIS_PORT`，未设置时兼容旧 `REDIS_HOST_PORT`；其他新增槽位变量没有旧别名。

生产 Compose 保持独立容器和镜像身份，不复用开发槽位。并行 worktree 必须使用不同 project、状态根和宿主端口；Schema 不兼容时不能共享或伪造版本，状态目录与 volume 未经授权不得删除。

## 替代方案

- 只修改宿主端口：数据库和文件卷仍会被不同代码同时写入，拒绝。
- 继续固定容器名并要求人工停旧环境：无法支持并行运行，也不能防止误用同一状态目录，拒绝。
- 为每个 worktree 复制一份 Compose 文件：配置会独立漂移，拒绝。

## 后果

默认开发环境的访问端口和数据位置不变。自定义 `APP_DOCKER_NETWORK` 不再控制开发网络名；需要并行运行时配置 `COMPOSE_PROJECT_NAME`。旧 `REDIS_HOST_PORT` 继续生效，但新配置统一使用 `YUXI_REDIS_PORT`。运行中的旧固定名容器不会被本次代码自动替换，切换到新 Compose 身份时需按部署窗口重建对应服务并保留状态目录。

## 验证

- Compose 槽位单元测试验证容器、网络、镜像、状态根和全部宿主端口均可隔离，并保留只读 OA 授权文件共享；负控恢复固定资源后失败。
- 开发 Compose config 与使用占位凭据的生产 Compose config 通过。
- 工程契约、文档构建通过；未实际同时启动第二个 worktree 槽位。
