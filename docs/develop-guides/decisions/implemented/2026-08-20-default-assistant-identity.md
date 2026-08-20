# 默认聊天助手使用九典身份

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/agents/buildin/chatbot/prompt.py

## 问题

默认聊天系统提示词仍将助手描述为“语析”，导致用户询问助手身份时可能得到旧品牌回复。

## 决策

默认聊天系统提示词将助手身份声明为“九典AI助手”，并明确要求在用户询问“你是谁”时只回答“我是九典AI助手”，不补充其他内容。其余回答规范、文件系统约束和自定义 Agent 系统提示词拼接顺序保持不变。

## 替代方案

- 在聊天服务中拦截身份问题并返回固定消息：会为一个默认身份文案增加特殊请求分支，并绕过正常 Agent 回复链路。
- 只修改前端品牌配置：不会改变模型接收的默认身份设定。

## 后果

默认聊天助手不再使用“语析”作为身份回复。管理员或用户显式配置的 Agent 系统提示词仍按现有规则追加并生效；API、持久化和运行流程不变。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 默认系统提示词使用九典身份且不包含“语析” | 默认聊天仍回答旧品牌身份，或身份回答附加其他内容 | `backend/package/yuxi/agents/buildin/chatbot/prompt.py` | `docker exec api-dev uv run --group test pytest test/unit/agents/test_chatbot_prompt.py -q` | 恢复旧身份声明后，`test_default_chatbot_identifies_as_jiudian_ai_assistant` 因缺少九典身份并出现“语析”而失败 | Passed |
