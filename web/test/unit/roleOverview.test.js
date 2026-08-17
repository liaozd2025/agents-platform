import assert from "node:assert/strict";
import test from "node:test";

import {
  getAssignableScopeTypes,
  getDataScopeLabel,
  groupRolePermissions,
  resetRoleAssignmentScope,
  serializeRoleMembersCsv,
  updateRolePermissionSelection,
} from "../../src/utils/roleOverview.js";

test("角色成员导出为带表头且正确转义的 CSV", () => {
  assert.equal(
    serializeRoleMembersCsv([{ username: '张"三', uid: "zhang,san" }]),
    '成员,账号\n"张""三","zhang,san"',
  );
});

test("角色权限按服务端目录组成菜单与操作层级", () => {
  const catalog = [
    { key: "dashboard:view", name: "概览 Dashboard", group: "统计分析", parent_key: null },
    { key: "user:read", name: "用户管理", group: "组织与权限", parent_key: null },
    { key: "user:create", name: "创建用户", group: "组织与权限", parent_key: "user:read" },
    { key: "user:update", name: "修改用户", group: "组织与权限", parent_key: "user:read" },
  ];

  assert.deepEqual(
    groupRolePermissions(catalog, ["dashboard:view", "user:read", "user:create"]),
    [
      {
        label: "统计分析",
        menus: [
          {
            ...catalog[0],
            granted: true,
            granted_operation_count: 0,
            operation_count: 0,
            operations: [],
          },
        ],
      },
      {
        label: "组织与权限",
        menus: [
          {
            ...catalog[1],
            granted: true,
            granted_operation_count: 1,
            operation_count: 2,
            operations: [
              { ...catalog[2], granted: true },
              { ...catalog[3], granted: false },
            ],
          },
        ],
      },
    ],
  );
  assert.deepEqual(groupRolePermissions(catalog, ["user:create"], true), [
    {
      label: "组织与权限",
      menus: [
        {
          ...catalog[1],
          granted: false,
          granted_operation_count: 1,
          operation_count: 2,
          operations: [{ ...catalog[2], granted: true }],
        },
      ],
    },
  ]);
});

test("菜单与操作权限选择保持父子联动", () => {
  const catalog = [
    { key: "user:read", parent_key: null },
    { key: "user:create", parent_key: "user:read" },
    { key: "user:update", parent_key: "user:read" },
  ];

  assert.deepEqual(updateRolePermissionSelection([], catalog, "user:create", true), [
    "user:read",
    "user:create",
  ]);
  assert.deepEqual(
    updateRolePermissionSelection(
      ["user:read", "user:create", "user:update"],
      catalog,
      "user:read",
      false,
    ),
    [],
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

test("非超级管理员角色选项遵守服务端转授约束", () => {
  const scopes = ["none", "self", "organization_and_descendants", "all"].map(
    (key) => ({ key }),
  );
  const departments = [
    { id: 1, parent_id: null },
    { id: 2, parent_id: 1 },
    { id: 3, parent_id: 2 },
    { id: 4, parent_id: 1 },
  ];
  const constraints = {
    override_scope_types: ["none", "self", "selected_organizations_and_descendants"],
    override_department_ids: [2, 3],
  };

  assert.deepEqual(
    getAssignableScopeTypes("all", scopes, departments, 2, [], constraints).map(
      (scope) => scope.key,
    ),
    ["none", "self"],
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
