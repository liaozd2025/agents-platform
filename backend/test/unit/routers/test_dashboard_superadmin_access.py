from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from server.routers.dashboard_router import dashboard

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _required_permission(call) -> str | None:
    """读取权限依赖闭包中绑定的权限键。"""
    for cell in call.__closure__ or ():
        if isinstance(cell.cell_contents, str):
            return cell.cell_contents
    return None


async def test_dashboard_routes_require_dashboard_view_permission():
    routes = [route for route in dashboard.routes if isinstance(route, APIRoute)]

    assert routes
    for route in routes:
        assert "dashboard:view" in {
            _required_permission(dependency.call) for dependency in route.dependant.dependencies
        }


async def test_dashboard_view_dependency_rejects_missing_permission():
    route = next(route for route in dashboard.routes if isinstance(route, APIRoute))
    dependency = next(
        dependency.call
        for dependency in route.dependant.dependencies
        if _required_permission(dependency.call) == "dashboard:view"
    )

    with pytest.raises(HTTPException) as exc:
        await dependency(SimpleNamespace(has_permission=lambda _permission: False))

    assert exc.value.status_code == 403
