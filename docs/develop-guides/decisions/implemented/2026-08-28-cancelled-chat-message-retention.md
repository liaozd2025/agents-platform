# 取消生成后保留对话消息

状态：implemented
类型：bug-fix
Owner：backend/yuxi/modules/agents/services/checkpoint_messages.py

## 当前迁移状态

2026-11-02：取消后的部分输出由新 checkpoint message 服务保存；公开历史由 public_items 投影读取。旧 Request/Conversation 协议与本文原验证属于历史版本。 以下原测试结果只对应原实现；本次验证以实际执行记录为准。

## 问题

用户在回答生成过程中点击停止后，已发送的问题和已生成的半截回答不应从对话中消失。

## 决策

- 历史接口继续隐藏尚未送达的 `queued`、`rejected` 用户消息，但保留已送达后取消执行的 `cancelled` 用户消息。
- 流式生成收到取消或连接中断时，若已有回答内容，则将其作为绑定当前 Run/Request 的 assistant 消息持久化，并标记 `error_type=cancelled`。
- 前端停止操作保留当前 Run 的中断上下文，让本地已渲染内容平滑过渡到刷新后的历史。

## 替代方案

- 仅在前端保留临时消息：刷新页面后内容丢失，不能满足对话历史一致性。
- 取消后删除整条 Run：会同时丢失用户已发送的问题，不符合主流 AI 产品体验。

## 后果

取消后的用户消息和部分 assistant 回复可在当前页面及重新加载历史后继续查看；未开始执行的排队/拒绝消息仍不会出现在历史中。

## 验证

会话历史单元测试覆盖 `cancelled` 用户消息保留，流式取消回归测试覆盖部分 assistant 回复持久化。
