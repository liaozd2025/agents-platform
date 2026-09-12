# H5 历史会话迁移操作手册

本文用于把旧 H5 平台 `jd-ai` 数据库中的“九典 AI 助手”历史会话迁移到 Yuxi。迁移目标智能体为 `default-chatbot`，用户身份按 `oa:ZD:<OA账号>` 映射。

迁移数据写入目标环境的 PostgreSQL，不会进入 Git。首次部署到测试、预发布或生产环境时，每套独立数据库都需要分别执行一次迁移；正常更新代码、重新构建镜像或重启容器不需要重新执行全量迁移。

## 迁移范围与限制

当前工具迁移：

- 会话标题、状态、置顶状态和创建时间。
- 用户消息与助手消息。
- 推理内容、引用、来源 ID、trace ID 等扩展元数据。
- 附件元数据，但不复制附件文件实体。

当前工具不迁移 LangGraph checkpoint、Agent Run、工具调用记录、H5 附件文件实体和其他 H5 应用数据。迁移后的历史会话以查看为主。

脚本支持幂等重跑：已迁移会话会被跳过，不会重复创建。但是，已迁移会话后来在 H5 中新增的消息不会自动追加；当前增量运行只适合补充新建会话。

## 前置条件

执行前确认：

1. 服务器能访问 H5 MySQL 的主机和端口。
2. H5 数据库账号至少具备 `jd-ai` 相关表的只读权限。
3. Yuxi PostgreSQL、Redis 和 API 容器正常运行。
4. 目标 Agent `default-chatbot` 已存在。
5. PostgreSQL 使用持久化数据卷或独立数据库，并已建立备份策略。

不要把真实数据库密码、失败记录中的业务内容或数据库导出文件提交到 Git。

## 配置源数据库连接

在服务器项目根目录的 `.env` 中增加以下配置，并替换占位值：

```dotenv
H5_MYSQL_HOST=<H5数据库地址>
H5_MYSQL_PORT=3306
H5_MYSQL_DATABASE=jd-ai
H5_MYSQL_USER=<只读账号>
H5_MYSQL_PASSWORD=<只读密码>
```

如果 API 容器已经启动，只重建 API 以加载新配置：

```bash
docker compose up -d --no-deps --force-recreate api
```

检查配置是否已进入容器。以下命令只输出是否配置，不输出账号密码：

```bash
docker compose exec -T api sh -lc '
for key in H5_MYSQL_HOST H5_MYSQL_PORT H5_MYSQL_DATABASE H5_MYSQL_USER H5_MYSQL_PASSWORD; do
  eval "value=\${$key}"
  if [ -n "$value" ]; then echo "$key=configured"; else echo "$key=missing"; fi
done
'
```

## 确定固定截止时间

每批迁移必须使用固定的 `--cutoff`，避免源库持续写入时出现会话与消息范围不一致。推荐使用 ISO 格式，避免 Shell 对空格参数的转义差异：

```text
2026-08-25T23:59:59
```

预检、试迁移和该批全量迁移必须使用同一个截止时间。

## 首次迁移流程

### 1. 只读预检

先预检 100 条会话。默认不写 PostgreSQL：

```bash
docker compose exec -T api sh -lc '
python scripts/migrate_jd_ai_h5_conversations.py \
  --cutoff <固定截止时间> \
  --limit-conversations 100 \
  --failure-file /tmp/h5-migration-preview-failures.json
'
```

重点核对日志中的：

- `source_conversations`：读取的源会话数。
- `source_messages`：这些会话对应的源消息数。
- `prepared_conversations`、`prepared_messages`：通过校验、准备写入的数量。
- `failures`：失败数量。

查看失败清单：

```bash
docker compose exec -T api sh -lc 'cat /tmp/h5-migration-preview-failures.json'
```

### 2. 小批量试迁移

预检通过后，使用同一截止时间试迁移少量会话：

```bash
docker compose exec -T api sh -lc '
python scripts/migrate_jd_ai_h5_conversations.py \
  --cutoff <固定截止时间> \
  --limit-conversations 5 \
  --apply \
  --failure-file /tmp/h5-migration-trial-failures.json
'
```

试迁移完成后，使用对应 OA 账号登录 Yuxi，确认会话标题、消息顺序、消息内容和用户归属正确。

### 3. 全量迁移

试迁移确认无误后，去掉 `--limit-conversations`，仍使用同一截止时间：

```bash
docker compose exec -T api sh -lc '
python scripts/migrate_jd_ai_h5_conversations.py \
  --cutoff <固定截止时间> \
  --apply \
  --failure-file /tmp/h5-migration-full-failures.json
'
```

数据量较大时，可将任务放到容器后台并记录日志：

```bash
docker compose exec -T api sh -lc '
nohup python scripts/migrate_jd_ai_h5_conversations.py \
  --cutoff <固定截止时间> \
  --apply \
  --failure-file /tmp/h5-migration-full-failures.json \
  >/tmp/h5-migration-full.log 2>&1 </dev/null &
'
```

查看进度：

```bash
docker compose exec -T api sh -lc 'tail -n 50 /tmp/h5-migration-full.log'
```

确认日志出现“迁移完成”后再验收结果。

## 迁移结果验证

### 页面验证

使用已迁移用户的 OA 账号登录并检查：

1. 会话列表中能看到历史会话。
2. 点击会话会请求 `GET /api/chat/thread/<thread_id>/history`。
3. 有消息的会话能够回显用户问题和助手回答。
4. 消息顺序和内容与 H5 一致。
5. 新建会话和正常对话不受影响。

部分源会话本身可能没有消息，点击后显示空会话属于正常情况。

### PostgreSQL 验证

统计迁移会话和用户：

```sql
SELECT
  COUNT(*) AS conversation_count,
  COUNT(DISTINCT uid) AS user_count
FROM conversations
WHERE extra_metadata->'migration'->>'source' = 'jd-ai-h5';
```

统计迁移消息：

```sql
SELECT COUNT(*) AS message_count
FROM messages AS m
JOIN conversations AS c ON c.id = m.conversation_id
WHERE c.extra_metadata->'migration'->>'source' = 'jd-ai-h5';
```

抽查指定 OA 账号：

```sql
SELECT thread_id, title, created_at, updated_at
FROM conversations
WHERE uid = 'oa:ZD:<OA账号>'
  AND extra_metadata->'migration'->>'source' = 'jd-ai-h5'
ORDER BY created_at DESC
LIMIT 20;
```

## 后续同步新建会话

H5 产生新会话后，可以选取新的固定截止时间，重复“只读预检 → 小批量试迁移 → 全量迁移”流程。脚本会幂等跳过已迁移会话，只创建尚未迁移的新会话。

```bash
docker compose exec -T api sh -lc '
python scripts/migrate_jd_ai_h5_conversations.py \
  --cutoff <新的固定截止时间> \
  --apply \
  --failure-file /tmp/h5-migration-incremental-failures.json
'
```

每批次应使用独立的失败清单和日志文件。当前脚本没有“起始时间”参数，因此增量运行仍会读取截止时间前的会话，再依靠来源元数据和 `thread_id` 跳过已迁移项。

如果业务要求同步“旧会话中后来新增的消息”，不要直接依赖上述命令，需要先扩展消息级增量迁移：按 `source_message_id` 去重并向既有会话追加消息。

## 失败处理与安全重跑

脚本按会话独立事务写入：单条会话失败会回滚该会话并继续处理其他会话。修复源数据或迁移规则后，可以使用同一截止时间安全重跑。

常见失败包括：

- H5 用户缺少 OA 账号，无法生成稳定 UID。
- 源消息类型不支持。
- 用户消息或助手消息内容为空。
- 目标库存在相同 `thread_id`，但不是本迁移来源创建的数据。

失败清单可能包含源会话 ID 和错误原因，应按业务敏感数据管理，不要提交到 Git。

## 数据持久化与版本更新

迁移结果保存在服务器 PostgreSQL 中。只要数据库和数据卷保留，拉取新代码、重新构建镜像和重启容器都不会删除迁移数据，也不需要重复执行首次全量迁移。

禁止在没有备份和明确确认的情况下执行：

```bash
docker compose down -v
```

同时避免删除 PostgreSQL 数据卷、清空业务表、切换到空数据库或运行破坏性初始化脚本。迁移完成后应立即做一次 PostgreSQL 备份，并纳入定期备份和恢复演练。
