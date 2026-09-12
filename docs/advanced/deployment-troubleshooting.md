# 部署故障排查

本页记录 Docker Compose 生产部署中，API、worker 或 `storage-migrator` 启动失败时的最小排查流程。命令均在项目目录执行，并显式指定 `.env.prod`。

## 先看服务状态

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml ps
docker logs --tail 200 ai-agents-platform-storage-migrator-1
docker logs --tail 200 api-prod
```

`storage-migrator` 是一次性迁移服务。迁移成功后显示 `Exited` 且退出码为 `0` 属于正常状态；API、worker 和 sandbox-provisioner 会等待它成功后再启动。

## `exit 127` 或入口脚本错误

### 判断含义

`exit 127` 表示容器启动命令未找到或无法执行。若日志包含以下内容，优先检查 API 镜像入口，而不是数据库：

```text
exec /usr/local/bin/yuxi-entrypoint: no such file or directory
```

```text
/usr/local/bin/yuxi-entrypoint: Is a directory
```

Windows 工作区生成的脚本还可能带 CRLF 换行，导致 shebang 被系统识别为不存在。正确镜像中入口必须是普通文件，第一行应为 `#!/bin/sh`。

### 检查镜像和容器实际配置

```bash
docker inspect ai-agents-platform-storage-migrator-1 \
  --format '镜像={{.Image}} 入口={{json .Config.Entrypoint}} 命令={{json .Config.Cmd}}'
docker image inspect yuxi-api:0.7.2 \
  --format '镜像ID={{.Id}} 入口={{json .Config.Entrypoint}}'
docker run --rm --entrypoint /bin/sh yuxi-api:0.7.2 \
  -c 'command -v uv; uv --version; ls -l /usr/local/bin/yuxi-entrypoint; head -n 1 /usr/local/bin/yuxi-entrypoint'
```

如果入口路径是目录、文件不存在，或 `uv` 不存在，说明当前标签不是可用镜像。重新导入正确的 API 镜像包，并用 SHA256 校验上传文件：

```bash
sha256sum ai-agents-platform-api-image-0.7.2.tar
docker load -i ai-agents-platform-api-image-0.7.2.tar
```

### 清理旧容器引用

镜像标签更新后，已存在的 `storage-migrator` 容器可能仍引用旧镜像。只删除运行容器不会删除数据库卷：

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml rm -sf api worker storage-migrator
```

如果入口脚本仍被按错误的 shell 形式执行，使用已经验证过的兼容覆盖文件：

```bash
docker compose \
  --env-file .env.prod \
  -f docker-compose.prod.yml \
  up -d --no-build
```

不要使用 `docker compose down -v`，否则会删除数据库和对象存储卷。

## 验证是否恢复

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml ps
docker exec api-prod curl --fail http://localhost:5050/api/system/health
docker exec api-prod curl --fail http://localhost:5050/api/system/ready
```

`/api/system/health` 只表示进程存活；`/api/system/ready` 返回 `status=ready` 且 `degraded=false`，才表示 API、PostgreSQL、Redis、worker 等启动依赖已经就绪。健康检查有刷新间隔，刚启动时短暂显示 `unhealthy` 可以等待一个检查周期后复核。

## 常见路径问题

- `.env.prod` 必须位于 Compose 文件所在目录，且启动命令使用 `--env-file .env.prod`。
- 新源码包不包含 `.env.prod`，更新源码时应保留服务器原配置。
- `/home/ai-agents-platform` 和 `/opt/ai-agents-platform` 是两个不同的部署目录；镜像包、Compose 文件和 `.env.prod` 必须来自同一目录。
- 离线服务器启动使用 `up -d --no-build`，不要让 Compose 尝试重新拉取或构建镜像。
