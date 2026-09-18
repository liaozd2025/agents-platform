# 工具审批默认模式改为完全信任

状态：archived
类型：feature
Owner：backend/package/yuxi/agents/tool_approval.py

本记录保存已退役方案的历史决定与证据。当前行为由 [PI 退役决策](../implemented/2026-09-18-retire-pi-executor.md) 取代。

## 问题

系统默认的「请求审批」模式会在写文件、编辑文件、执行命令和 PI 沙箱委派前逐条中断等待用户确认，默认体验与产品诉求不一致。需要的效果是：未做选择的会话直接自动执行敏感工具，同时保留用户主动切回逐条确认的能力。

## 决策

系统级默认值 `DEFAULT_TOOL_APPROVAL_MODE` 由 `default` 改为 `always_trust`，并作为唯一的默认来源。优先级不变：请求级 `tool_approval_mode` > Agent 配置 `context.tool_approval_mode` > 会话 metadata > `DEFAULT_TOOL_APPROVAL_MODE`。Chatbot graph 与 SubAgent graph 中 `getattr(context, "tool_approval_mode", ...)` 的硬编码 `"default"` 回退改为引用同一常量，SubAgent 工具过滤中间件的构造默认值同步跟随。

前端 `DEFAULT_TOOL_APPROVAL_MODE` 与 `ToolApprovalModeSelector` 的 prop 默认值同步为 `always_trust`，`currentOption` 的回退从 `options[0]` 改为按默认值查找。`TOOL_APPROVAL_MODES` 仍保留 `default` 与 `always_trust` 两项，选择器与后端审批中间件装配逻辑不删除，用户可手动切回。

存量会话 metadata、存量 Agent `config_json` 与浏览器 `localStorage` 中显式保存的 `default` 保持原样：本次只改变默认值，不追认也不覆盖历史选择。

## 替代方案

- 仅改前端默认选中项、后端保持 `default`：前后端默认分裂，未携带 `tool_approval_mode` 的调用路径（invocation 路由、PI 委派、legacy resume）仍走审批。
- 忽略存量 thread/Agent/`localStorage` 中的 `default`，一律按完全信任执行：会覆盖用户此前主动选择的逐条确认，超出「调整默认值」的范围。
- 删除「请求审批」选项与 `HumanInTheLoopMiddleware` 装配：同时删除安全退回路径与 Project 内写入豁免逻辑，改动面远大于默认值语义。
- 只改 Agent 配置字段默认值：会话 metadata 优先级更高，未配置 Agent 的既有会话不受影响，默认体验不统一。

## 后果

未显式选择审批模式的会话、新建会话和未配置 `tool_approval_mode` 的 Agent 默认自动执行 `write_file`、`edit_file`、`execute` 与 `pi_sandbox`，Project 外写入不再有默认中断。选择「请求审批」后行为完全不变：Project Workdir 内写入自动放行，Project 外写入、非法路径与 `execute` 继续中断；SubAgent 的敏感工具隐藏集合由模式无关的常量决定，不受本次改动影响。审批只决定是否中断，不改变 Sandbox Backend 的文件权限，`always_trust` 不产生额外文件系统能力。接受的代价是新用户默认不再被高风险命令拦下，需要显式切回逐条确认。

## 验证

- `docker compose -p yuxi exec -T api uv run --no-sync --group test pytest test/unit/agents/test_tool_approval.py test/unit/agents/test_subagent_tool_filter.py test/unit/services/test_agent_run_service.py -q`：67 passed；覆盖审批中间件装配、SubAgent 工具过滤、`resolve_agent_run_tool_approval_mode` 三级优先级与 legacy `input_payload` 缺字段时的默认回退。
- `docker compose -p yuxi exec -T web node --test src/utils/__tests__/toolApproval.test.js`：`toolApproval: all assertions passed`；覆盖 `resolveToolApprovalMode` 在各来源缺失时落到新默认值。
- `docker compose -p yuxi exec -T web npx eslint src/utils/toolApproval.js src/components/ToolApprovalModeSelector.vue src/utils/__tests__/toolApproval.test.js --max-warnings=0`：通过。
- `python3 scripts/verify_engineering_contracts.py`：通过（92 decisions）。
- `python3 -m py_compile`（4 个后端改动文件）与改动文件 UTF-8 / 行宽（≤120）检查：通过。
- 运行态探针（api 容器内 `python -c`）：`DEFAULT = always_trust`、`resolve_agent_run_tool_approval_mode(None, None) = always_trust`、`resolve_agent_run_tool_approval_mode(None, "default") = default`（显式配置仍优先）、`create_tool_approval_middleware("always_trust") = None`。
- Ruff（沿用 `backend/pyproject.toml` 配置，5 个改动文件）：`ruff check` → All checks passed；`ruff format --check` → 5 files already formatted。首次提交曾在 `agents/context.py` 引入 `E501 122 > 120`（描述行含中文，按双宽计算），已修正。
- CI 基线：`Ruff Format & Lint`、`Backend unit tests`、`Runtime System Tests`、`Deploy VitePress site to Pages` 在 `main` 上长期失败，失败文件与用例不在本变更范围内；与本变更相关的 `Lint, unit and production build`、`Owner-local decisions and gate wiring`、`PowerShell security environment contract` 通过。
- 真实 worker/SSE 链路的审批中断与默认模式端到端验收：Not run，残余风险限于运行态默认值投影。
