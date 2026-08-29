# 新会话固定使用 DeepSeek 默认模型

状态：implemented
类型：bug-fix
Owner：web/src/utils/conversationModel.js

## 问题

独立登录或 iframe 新建会话时，智能体和系统配置异步加载会让模型选择从可用模型回跳到已停用的 Qwen，导致首次发送请求失败。

## 决策

前端按“用户手动选择、Conversation 已绑定模型、`siliconflow-cn:deepseek-ai/DeepSeek-V4-Flash`”解析当前展示和请求模型。新会话不继承异步加载的智能体或系统默认模型，模型选择器也不自动选择接口列表第一项。

## 替代方案

不继续把 DeepSeek 作为临时兜底，因为智能体配置加载后仍会覆盖它；也不修改已有 Conversation 的模型绑定，避免改变历史会话的明确选择。

## 后果

独立登录和 iframe 的新会话默认模型保持一致。已有会话绑定和用户手动选择继续优先，后续如需更换平台默认模型，应修改 `web/src/utils/conversationModel.js` 的默认模型常量并同步回归测试。

## 验证

- `docker compose run --rm --no-deps -v "${PWD}\web\test:/app/test:ro" web node --test test/unit/conversationModelBinding.test.js` 验证选择顺序、固定 DeepSeek 默认值、异步配置不覆盖默认值，以及不再自动选择列表第一项。
- `docker compose exec -T web pnpm run lint:check` 与 `docker compose exec -T web pnpm run build` 验证前端静态检查和编译通过。
- 恢复智能体或系统配置优先级，或重新启用 `auto-select-first` 时，相关回归测试失败。
