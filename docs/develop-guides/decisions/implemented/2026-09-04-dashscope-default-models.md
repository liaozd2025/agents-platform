# 默认模型切换为 DashScope

状态：implemented
类型：feature
Owner：backend/package/yuxi/config/options.py

## 问题

部署已改用 `DASHSCOPE_API_KEY`，但前后端的代码兜底值仍指向已停用的 SiliconFlow DeepSeek，空配置或新会话可能因此回退到旧模型。

## 决策

新会话和系统配置默认使用 `alibaba-cn:qwen3.7-max`；Embedding 使用 `alibaba-cn:text-embedding-v4`；Rerank 使用 `alibaba-cn:qwen3-rerank`。已有会话保存的模型不被批量改写，继续按会话绑定使用。

## 替代方案

只修改页面默认值无法覆盖后端新会话和空配置路径，因此不采用。批量改写历史会话会改变既有行为，也不采用。

## 后果

新部署无需额外修改代码即可与 DashScope Key 配套工作；已存在的数据库配置仍由管理员配置值优先，历史会话保持原模型绑定。

## 验证

- 前端 `conversationModelBinding.test.js`：4 项通过。
- 后端 `test_options.py`：9 项通过。
- `options.py` Python 编译通过。
- 当前 PostgreSQL 系统配置和 `/api/system/ready` 均确认 DashScope 配置生效。
