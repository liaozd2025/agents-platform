# 图谱抽取超时可配置并补全 DashScope 内置模型

状态：implemented
类型：bug-fix
Owner：`backend/package/yuxi/knowledge/graphs/extractors/llm.py`、`backend/package/yuxi/models/providers/builtin.py`

## 问题

知识图谱抽取在推理类模型上必然失败：单次请求超时固定为 60 秒，而推理模型先输出思考过程再给结果，遇到长 chunk（3000+ 字符）时 60 秒必然超时，表现为「抽取失败」并在重试 3 次后放弃。同时默认对话模型未在 `alibaba-cn` 内置 provider 中登记，模型发现结果缺少 `qwen3.7-*`，链路报「未找到模型」。

## 决策

1. 单次抽取的模型请求超时改为可配置项 `extractor_options.model_timeout`，缺省 300 秒。
2. 非法配置（非数字或 ≤0）在选项校验阶段直接抛 `ValueError`，不再静默回退默认值。
3. `alibaba-cn` 内置 provider 补登记 `qwen3.7-max` / `qwen3.7-plus` / `qwen3.7-flash` 对话模型。
4. `text-embedding-v4` 单批条数收敛到 10，规避 DashScope 兼容模式的 `InvalidParameter` 400。

## 替代方案

- 只把固定超时从 60 秒放宽到 300 秒：不同知识库的 chunk 长度与模型差异大，更长的输入仍会重现。
- 保留静默回退：配置写错时问题推迟到抽取阶段才暴露，且难以从日志定位。
- 在调用侧包一层重试：重复消耗并发额度与模型成本，且不解决单次请求本身的超时。

## 后果

- 新增长期维护表面：`extractor_options.model_timeout` 是知识库级配置，需要与文档、校验共同维护。
- 失败语义变化：非法配置由静默回退改为显式报错，历史配置若含非法值会在校验阶段直接失败。
- 超时放宽后单次抽取占用模型连接的时间变长，并发上限较高时需注意连接与额度占用。

## 验证

- `docker exec fix-test-workflow-api-1 python -m pytest test/unit/graphs/test_llm_graph_extractor_timeout.py -q` → 9 passed（缺省、非数字、≤0 三个分支与合法覆盖）。
- `docker exec fix-test-workflow-api-1 python -m pytest test/unit/repositories/test_user_repository.py test/unit/graphs test/unit/routers -q` → 155 passed。
- 未验证：未对真实 DashScope 发起调用，300 秒与 `batch_size=10` 的取值依据是线上错误表现，缺少 provider 侧复现证据；「未找到模型」仅有内置 provider 表的代码级证据，未做端到端抽取验证。
