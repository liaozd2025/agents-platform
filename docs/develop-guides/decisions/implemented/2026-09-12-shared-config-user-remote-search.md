# 共享配置候选用户改为按关键字远程检索

状态：implemented
类型：feature
Owner：backend/server/routers/auth_router.py

## 问题

共享配置浮层的候选用户在打开时一次性拉取管理域内全量用户（`limit` 默认 1000），在用户量较大的组织里既慢又难用：首屏请求体积大、渲染卡顿，且没有筛选手段。

## 决策

1. `/api/auth/users/access-options` 新增 `keyword`、`skip`、`limit` 查询参数，授权范围过滤与关键字匹配都在 SQL 层完成，不再先全量载入再内存切片。
2. 单次返回条数上限由 1000 收敛为 100，总数通过 `X-Total-Count` 响应头返回，调用方按关键字继续检索。
3. 前端候选用户列表改为按关键字远程检索，搜索框加 300ms 防抖与请求序号控制，丢弃过期响应。
4. 已勾选但不在当前结果中的用户与搜索结果合并展示，保证切换关键字后仍可取消勾选。

## 替代方案

- 保留一次性全量拉取、只做前端过滤：不解决首屏体积与响应时间，用户量继续增长后仍会退化。
- 服务端返回全量、前端改虚拟滚动：需要引入虚拟列表依赖，且没有降低传输与 SQL 扫描成本。
- 保持 `limit` 上限 1000：契约上无法阻止调用方一次拉取全量，等于没有收敛。

## 后果

- 对外契约收紧：`limit` 上限 100、`skip` 语义由内存切片变为 SQL 分页、新增 `X-Total-Count` 响应头。已排查调用点，`getUserAccessOptions` 目前被 `ShareConfigForm.vue` 与 `DataBaseInfoView.vue` 引用。
- 跨域场景依赖 CORS `expose_headers` 暴露 `X-Total-Count`；该配置已存在于 `backend/server/main.py` 并有单测覆盖。
- 前端需长期维护"已选项与搜索结果合并展示"的逻辑，否则切换关键字会丢失勾选状态。

## 验证

- `docker exec fix-test-workflow-api-1 python -m pytest test/unit/routers/test_auth_access_options_pagination.py -q` → 3 passed（`limit > 100` 被拒、`X-Total-Count` 与关键字透传、组织节点不可达返回 404）。
- `docker exec fix-test-workflow-api-1 python -m pytest test/unit/repositories/test_user_repository.py test/unit/graphs test/unit/routers -q` → 155 passed。
- `eslint`（改动文件）与 `vite build`（31.76s）通过。
- 未验证：`test/integration/api/test_auth_router.py` 因环境缺少 `TEST_USERNAME` / `TEST_PASSWORD` 未执行；前端未做真实浏览器交互验证。
