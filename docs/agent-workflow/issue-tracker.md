# Issue tracker：GitHub

本仓库的 issue 与 spec 都放在 GitHub Issues，统一用 `gh` CLI 操作。

仓库：`liaozd2025/agents-platform`（`origin`，原名 `liaozd2025/Yuxi`，旧名仍可重定向访问）。

> **每条 `gh` 命令都必须显式写 `-R liaozd2025/agents-platform`。**
> `upstream` 指向 `xerrors/Yuxi`（上游开源仓库），而 `gh` 的自动推断会优先落到 `upstream`——实测 `gh label list` 列出的是上游的标签、`gh label create` 直接打到了上游仓库。省掉 `-R` 不是省事，是把写操作发到别人的仓库。

## 约定

所有命令均以 `-R liaozd2025/agents-platform` 开头，下表省略不写。

- **创建**：`gh issue create --title "..." --body-file <path>`，正文较长时用文件，避免 heredoc 转义问题
- **读取**：`gh issue view <number> --comments`
- **列表**：`gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`，按需加 `--label` / `--state`
- **评论**：`gh issue comment <number> --body "..."`
- **标签**：`gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **关闭**：`gh issue close <number> --comment "..."`

## Pull requests 是否作为需求来源

**PRs as a request surface: no.** _(若本仓库把外部 PR 也当作需求入口，改成 `yes`；`/triage` 会读这个开关。)_

GitHub 的 issue 与 PR 共用一个编号空间，裸 `#42` 可能是任一种。先 `gh pr view 42`，失败再回退 `gh issue view 42`。

## 当 skill 说「publish to the issue tracker」

创建一个 GitHub issue。

## 当 skill 说「fetch the relevant ticket」

执行 `gh issue view <number> --comments`。

## 阻塞关系（blocking edges）

优先用 GitHub 原生的 issue dependencies（UI 可见）：

```bash
# <blocker-db-id> 是阻塞方的数字 database id，不是 #number、也不是 node_id
gh api repos/liaozd2025/agents-platform/issues/<n> --jq .id
gh api --method POST repos/liaozd2025/agents-platform/issues/<child>/dependencies/blocked_by \
  -F issue_id=<blocker-db-id>
```

若该 API 不可用，退化为在子 issue 正文顶部写 `Blocked by: #<n>, #<n>`。
**所有阻塞方都关闭后，该 ticket 才算解锁。**

## 本仓库的补充约定

- **语言**：issue 标题与正文用**中文**（与仓库其余文档一致）。标签字符串、命令片段保持英文原样。
- **提交**：由 issue 派生的提交遵循 [CLAUDE.md](../../CLAUDE.md) 的提交规范——Conventional Commits + 中文说明。
- **术语**：优先使用 [ARCHITECTURE.md](../../ARCHITECTURE.md) 中已有的模块与边界名称，不要另造同义词。
