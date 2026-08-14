import assert from "node:assert/strict";
import test from "node:test";

import {
  getAssignableScopeTypes,
  getDataScopeLabel,
  getRoleAuditActionLabel,
  groupRolePermissions,
  resetRoleAssignmentScope,
} from "../../src/utils/roleOverview.js";

test("角色权限按服务端目录分组并忽略未选权限", () => {
  const catalog = [
    { key: "user:read", name: "查看用户", group: "用户与组织" },
    { key: "user:create", name: "创建用户", group: "用户与组织" },
    { key: "dashboard:view", name: "查看 Dashboard", group: "统计分析" },
  ];

  assert.deepEqual(
    groupRolePermissions(catalog, ["dashboard:view", "user:read"]),
    [
      {
        label: "用户与组织",
        permissions: [
          { key: "user:read", name: "查看用户", group: "用户与组织" },
        ],
      },
      {
        label: "统计分析",
        permissions: [
          { key: "dashboard:view", name: "查看 Dashboard", group: "统计分析" },
        ],
      },
    ],
  );
});

test("数据范围使用服务端目录中的中文名称", () => {
  const scopes = [
    { key: "self", label: "仅本人" },
    { key: "all", label: "全部数据" },
  ];

  assert.equal(getDataScopeLabel(scopes, "all"), "全部数据");
  assert.equal(getDataScopeLabel(scopes, "missing"), "未知范围");
});

test("角色安全审计动作使用中文名称并保留未知动作", () => {
  assert.equal(getRoleAuditActionLabel("role.update"), "修改角色");
  assert.equal(getRoleAuditActionLabel("role.future"), "role.future");
});

test("角色分配只提供不超过默认范围的选项", () => {
  const scopes = ["none", "self", "organization_and_descendants", "all"].map(
    (key) => ({ key }),
  );

  assert.deepEqual(
    getAssignableScopeTypes("self", scopes).map((scope) => scope.key),
    ["none", "self"],
  );
  assert.deepEqual(
    getAssignableScopeTypes("all", scopes).map((scope) => scope.key),
    scopes.map((scope) => scope.key),
  );
  assert.deepEqual(
    getAssignableScopeTypes(
      "selected_organizations_and_descendants",
      scopes,
      [
        { id: 1, parent_id: null },
        { id: 2, parent_id: 1 },
        { id: 3, parent_id: 1 },
      ],
      3,
      [2],
    ).map((scope) => scope.key),
    ["none"],
  );
});

test("选择超级管理员会清空旧范围并移除其他角色", () => {
  const assignments = [
    { role_id: 1, scope_mode: "inherit", override_department_ids: [] },
    {
      role_id: 2,
      scope_mode: "override",
      override_scope_type: "all",
      override_department_ids: [3],
    },
  ];

  assert.deepEqual(resetRoleAssignmentScope(assignments, 1, true), [
    {
      role_id: 2,
      scope_mode: "inherit",
      override_scope_type: null,
      override_department_ids: [],
    },
  ]);
});
