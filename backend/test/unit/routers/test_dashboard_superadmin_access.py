"""Dashboard HTTP 权限依赖覆盖。"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from yuxi.api.routers.dashboard import dashboard


def _bound_permission(call):
    """读取 require_permission 的固定权限标识。"""
    for cell in call.__closure__ or ():
        if isinstance(cell.cell_contents, str):
            return cell.cell_contents
    return None


def _dependencies(dependant):
    """包含 scoped service 的嵌套权限依赖。"""
    for child in dependant.dependencies:
        yield child
        yield from _dependencies(child)


def test_every_dashboard_route_has_function_permission():
    for route in dashboard.routes:
        if isinstance(route, APIRoute):
            required = "system_config:manage" if route.path.endswith("/stats/vms") else "dashboard:view"
            assert required in {_bound_permission(dependency.call) for dependency in _dependencies(route.dependant)}, route.path


@pytest.mark.asyncio
async def test_dashboard_function_permission_rejects_ordinary_login():
    route = next(route for route in dashboard.routes if isinstance(route, APIRoute) and route.path.endswith("/stats"))
    dependency = next(item.call for item in _dependencies(route.dependant) if _bound_permission(item.call) == "dashboard:view")
    with pytest.raises(HTTPException) as error:
        await dependency(SimpleNamespace(has_permission=lambda _: False))
    assert error.value.status_code == 403
