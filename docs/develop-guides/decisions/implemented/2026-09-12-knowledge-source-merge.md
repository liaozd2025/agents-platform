# 知识库来源展示与轻量启动边界

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/services/knowledge_retrieval_policy.py

## 问题

检索来源需要保留文章标题、作者和原文链接，同时兼容 LITE 启动与普通上传文件。技能分区调整需要保留搜索与既有文件预览能力。

## 决策

知识库策略保留能力门禁与非检索请求提前返回，只在真实检索时加载知识运行时和来源补充。`source_references.py` 解析文章头部元数据；chat service 将本次自动检索结果随 assistant metadata 持久化，前端按文章分组。OA 来源可打开原文，无 OA 链接的文件保留片段预览，具备库和文件 ID 的来源保留完整文件入口。

技能广场同时应用搜索与分类。Ollama 和 vLLM 使用内置 OpenAI 兼容模板且默认禁用，由管理员配置后启用。

## 替代方案

采用顶层知识运行时导入会破坏 LITE 边界；仅保留 OA 链接会使普通上传文件无法查看。两者均不满足当前 consumer。

## 后果

来源信息增加消息元数据体积，每个命中文件增加一次文章读取；单次检索内按文件复用。来源读取失败时保留文件名来源。OA 链接依赖现有站点路由，不表示已验证外部原文可访问。OA 链接的字节格式随后续变更增加了 `ecType` 参数，跳转行为由 [2026-09-12-source-navigation-embed-bridge.md](2026-09-12-source-navigation-embed-bridge.md) 拥有。

## 验证

检索策略与 LITE fresh-process 检查验证惰性导入及来源输出，chat service 检查来源元数据随消息保存。前端检查搜索无匹配结果及普通文件预览入口；完整后端、前端测试与构建结果记录在 PR。真实外部 OA 和模型服务连通性不由这些检查证明。
