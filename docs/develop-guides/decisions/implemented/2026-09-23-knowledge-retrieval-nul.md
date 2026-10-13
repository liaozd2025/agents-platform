# 知识库检索结果清理 NUL 字符

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/knowledge/manager.py

## 问题

知识库检索可能返回 U+0000。它进入模型上下文、引用原文及消息 JSON 元数据后，会令 PostgreSQL 提取 JSON 文本字段时报错，导致会话历史接口返回 500。

## 决策

在 `KnowledgeBaseManager.retrieve` 和 `aquery` 的共享返回边界递归移除检索结果中的 U+0000，覆盖聊天自动检索、Agent 工具、MCP 和知识库查询接口；来源文章在解析元数据和 URL 编码前清理原文，来源附件补充后再次清理新增元数据。清理字典键若造成字段重名则显式失败，避免静默覆盖。

## 替代方案

- 只在引用展示时清理：模型上下文和保存的 `knowledge_sources` 仍可能含有 U+0000。
- 改写历史 JSON 查询或全表清洗旧记录：掩盖输入缺陷，扩大查询复杂度或数据变更范围。现有已坏记录需在部署时单独、精确地修复。

## 后果

检索返回的 U+0000 会从模型输入、工具结果、查询接口及来源片段中去除。已经持久化的异常记录不会由此自动修改。

## 验证

`test_aquery_removes_nul_from_provider_result` 覆盖知识库查询接口；`test_retrieve_removes_nul_from_shared_provider_result` 覆盖共享检索结果的正文、嵌套元数据和字段名；`test_retrieval_removes_nul_from_content_and_citation_metadata` 覆盖来源补充、文章标题和引用原文；`test_source_reference_removes_nul_before_encoding_oa_url` 验证解码后的 OA 任务号、标题和文章类型；字段名碰撞有负向测试。`test/integration/services/test_knowledge_source_message.py` 在真实 PostgreSQL 上恢复未清理 JSON 的 `22P05`，验证清理后历史字段提取及普通消息、模型审计消息的来源回读。验证环境使用隔离 PostgreSQL 16，不连接共享数据库。
