# Yuxi Fork 与官网同步迭代操作手册

> 适用仓库：`https://github.com/liaozd2025/Yuxi`
>
> 官方仓库：`https://github.com/xerrors/Yuxi`
>
> 目标：在自己的 Fork 上持续开发，同时安全、可追踪地同步官方 `main`。

## 1. 远端和分支职责

本地 Git 保持两个远端：

```text
origin    https://github.com/liaozd2025/Yuxi   自己的 Fork，用于推送
upstream  https://github.com/xerrors/Yuxi      官方仓库，只用于获取更新
```

推荐分支关系：

```text
upstream/main
      ↓ 快进同步
sync/upstream-main   官方代码镜像，只允许同步官网
      ↓ 合并
main                 你的产品核心分支
      ↓ 创建
feat/*               多智能体独立功能分支
```

约定：

- `sync/upstream-main` 只反映官方 `main`，不放自己的功能。
- `main` 是你的本地产品主线，可以包含企业功能和私有改动。
- `feat/*`、`fix/*`、`docs/*` 从你的 `main` 创建。
- 不向 `upstream` 推送；官方 PR 分支从 `sync/upstream-main` 创建。

这套模型适合长期维护自己的产品版本：日常开发围绕 `main`，官网更新先进入同步分支，再经过检查合入 `main`。

## 2. 现有克隆的一次性配置

如果现有克隆把官方仓库配置成了 `origin`，先确认工作区：

```bash
cd /Users/ddddup/Codebase/work/Yuxi
git status --short --branch
git remote -v
```

如果 `git status` 显示本地改动，不要直接切换或覆盖。需要保留的改动应提交到专用分支；暂时不准备提交的改动可以临时保存：

```bash
git stash push --include-untracked -m "切换 Fork 远端前保存本地改动"
```

然后将当前远端改成推荐结构：

```bash
git remote rename origin upstream
git remote add origin https://github.com/liaozd2025/Yuxi.git
git fetch --prune upstream
git fetch --prune origin
git remote -v
```

预期结果：

```text
origin    https://github.com/liaozd2025/Yuxi.git
upstream  https://github.com/xerrors/Yuxi.git
```

如果 `origin` 已经是自己的 Fork，不要重复 `rename`，只需补充官方远端：

```bash
git remote add upstream https://github.com/xerrors/Yuxi.git
git fetch --prune upstream
```

## 3. 创建官方同步分支

同步分支必须从官方最新代码创建，并保持干净。先确认当前工作区没有需要处理的本地改动：

```bash
git status --short --branch
git fetch --prune upstream
```

第一次创建：

```bash
git switch main
git switch -c sync/upstream-main upstream/main
git push origin sync/upstream-main
# 拉取来源是 upstream，推送目标是 origin
git config branch.sync/upstream-main.remote upstream
git config branch.sync/upstream-main.merge refs/heads/main
git config branch.sync/upstream-main.pushRemote origin
git switch main
```

以后更新官方同步分支：

```bash
git switch sync/upstream-main
git fetch upstream
git merge --ff-only upstream/main
git push origin sync/upstream-main
```

`sync/upstream-main` 的提交应只来自 `upstream/main`。如果 `--ff-only` 失败，说明同步分支被写入了额外提交，先检查，不要使用 `reset --hard` 静默覆盖：

```bash
git log --oneline --left-right sync/upstream-main...upstream/main
git status --short --branch
```

## 4. Vibe Coding 与多智能体 worktree

主工作区只负责同步、集成和最终验证。每个智能体使用独立 worktree，不共享一个正在修改的目录。

```bash
mkdir -p /Users/ddddup/Codebase/work/Yuxi-worktrees
git worktree add \
  /Users/ddddup/Codebase/work/Yuxi-worktrees/feat-enterprise-sso \
  -b feat/enterprise-sso \
  main
```

给每个智能体明确以下信息：

- worktree 的绝对路径和分支名。
- 目标、允许修改的目录和明确不应修改的文件。
- 最小验收标准和必须运行的测试。
- 完成后提交的范围和未验证风险。

建议并行开发这些内容：

- 不同页面、不同 API 和相互独立的测试。
- 不同业务模块，且不共享数据库迁移和锁文件。

建议串行处理这些内容：

- 同一个核心文件、数据库模型和迁移。
- `docker-compose.yml`、Dockerfile、依赖锁文件。
- 需要统一产品决策的架构和权限边界。

智能体完成后，在自己的 worktree 中检查、测试、提交并推送：

```bash
git status --short --branch
git diff --check
git add <changed-files>
git commit -m "feat: 增加企业单点登录"
git push -u origin feat/enterprise-sso
```

智能体的完成消息只是开发结果，不代表已经集成。集成前仍要从主工作区重新检查提交、diff 和测试结果。

## 5. 开发自己的功能

每个功能从同步后的 `main` 创建独立分支：

```bash
git switch main
git switch -c feat/<功能名称>
```

示例：

```bash
git switch -c feat/enterprise-sso
```

开发过程中只提交当前功能相关文件：

```bash
git status
git diff
git add <changed-files>
git commit -m "feat: 增加企业单点登录"
```

推送到自己的 Fork：

```bash
git push -u origin feat/enterprise-sso
```

功能分支不要直接推送到 `upstream`，也不要把多个无关功能混在同一个分支中。

## 6. 日常同步官网并更新自己的 `main`

先更新官方同步分支：

```bash
git switch sync/upstream-main
git fetch upstream
git merge --ff-only upstream/main
git push origin sync/upstream-main
```

确认官方更新后，再合入你的产品主线：

```bash
git switch main
git merge --no-ff sync/upstream-main
git push origin main
```

`main` 是你的产品分支，因此不再要求它能够直接对 `upstream/main` 使用 `--ff-only`。合并官方更新后，需要按改动范围重新测试。

如果某个个人独占的功能分支需要先吸收新的本地 `main`，可以 rebase：

```bash
git switch feat/<功能名称>
git rebase main
git push --force-with-lease origin feat/<功能名称>
```

多人共用的功能分支不要 rebase，改用普通合并。

## 7. 集成智能体功能

建议使用一个专门的集成 worktree，逐个接收智能体分支：

```bash
git worktree add \
  /Users/ddddup/Codebase/work/Yuxi-worktrees/integration-main \
  -b integration/main \
  main

cd /Users/ddddup/Codebase/work/Yuxi-worktrees/integration-main
git merge --no-ff feat/enterprise-sso
git merge --no-ff feat/knowledge-search
```

每合并一个分支，都完成一次相关测试和 `git diff --check`。确认集成分支可用后，再快进你的产品 `main`：

```bash
cd /Users/ddddup/Codebase/work/Yuxi
git switch main
git merge --ff-only integration/main
git push origin main
```

如果不需要独立的集成 worktree，也可以直接在 `main` 中按同样顺序逐个合并；不要把多个智能体分支一次性合并后再排查问题。

## 8. 发生冲突时怎么处理

查看冲突文件：

```bash
git status
git diff --name-only --diff-filter=U
```

逐个文件理解官方改动和本地业务改动后再处理。完成后：

```bash
git add <resolved-files>
git commit                         # merge 场景
git rebase --continue              # rebase 场景
```

如果还没有判断清楚，不要强行保留某一方，可以安全退出：

```bash
git merge --abort
# 或
git rebase --abort
```

重点检查以下高风险区域：

- `backend/` 的路由、服务、仓储、权限和数据库迁移。
- `web/` 的 API、状态管理和核心页面。
- `docker-compose.yml`、Dockerfile、`.env.template`。
- `backend/uv.lock`、`web/pnpm-lock.yaml` 等依赖锁文件。

不要用“全部选择 ours/theirs”代替理解业务行为。冲突解决后必须跑对应测试。

## 9. 每次同步或合并后的验证

先检查补丁：

```bash
git diff --check
git status --short --branch
git log --oneline --decorate -5
```

Yuxi 使用 Docker Compose。默认 Compose 只由集成 worktree 负责，避免多个智能体同时占用相同端口、数据库和数据卷：

```bash
cd /Users/ddddup/Codebase/work/Yuxi-worktrees/integration-main
docker compose up -d
docker ps
docker logs api-dev --tail 100
```

按改动范围执行最小验证：

```bash
# 后端单元测试
docker compose exec api uv run --group test pytest test/unit -m "not slow"

# 后端集成测试
docker compose exec api uv run --group test pytest test/integration

# 前端检查
docker compose exec web pnpm run lint
docker compose exec web pnpm run test:unit
docker compose exec web pnpm run build
```

如果修改了 Dockerfile、依赖或构建配置，使用：

```bash
docker compose up -d --build
```

智能体可以在自己的 worktree 做局部静态检查和单元测试；合并后的 API、数据库、前端和关键链路测试必须在集成 worktree 重新执行。

未执行的测试要在提交或发布记录中如实说明，不把容器启动成功当成代码验证完成。

## 10. 提交、PR 和发布边界

提交前：

```bash
git status
git diff
git diff --check
```

不要提交以下内容：

- `.env`、密码、Token、API Key。
- 本地数据库、上传文件、运行输出和构建产物。
- 与当前功能无关的格式化或重构。

如果功能有机会回馈官方：

- 从 `sync/upstream-main` 创建专门的 `feat/upstream-*` 分支，再向 `xerrors/Yuxi:main` 创建 PR。
- PR 只包含一个清晰主题。
- 按官方 [贡献指南](../develop-guides/contributing.md) 填写测试、影响范围和未验证风险。

如果功能只服务自己的业务：

- 不需要创建官方 PR。
- 保留在自己的 `main` 及对应功能分支。
- 继续更新 `sync/upstream-main`，再把它合入自己的 `main`。

## 11. 最短日常清单

```bash
# 同步官网
git status --short --branch
git switch sync/upstream-main
git fetch upstream
git merge --ff-only upstream/main
git push origin sync/upstream-main

# 合入自己的产品主线
git switch main
git merge --no-ff sync/upstream-main
git push origin main

# 开发功能
git worktree add \
  /Users/ddddup/Codebase/work/Yuxi-worktrees/feat-<功能名称> \
  -b feat/<功能名称> \
  main
# 修改、测试、提交
git push -u origin feat/<功能名称>

# 最终集成和部署
cd /Users/ddddup/Codebase/work/Yuxi-worktrees/integration-main
git merge --no-ff feat/<功能名称>
docker compose up -d --build
```

遇到任何同步异常，先保留现场：

```bash
git status
git branch -vv
git remote -v
git log --oneline --decorate --graph --all -20
```

不要在未确认目标分支和改动归属前执行 `git reset --hard`、批量删除分支或强制覆盖远端。
