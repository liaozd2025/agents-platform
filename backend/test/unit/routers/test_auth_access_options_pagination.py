"""共享配置候选用户接口的分页与关键字契约测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.routers import auth_router
from server.routers.auth_router import auth
from server.utils.auth_middleware import get_authorization_context, get_db

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _build_client(monkeypatch: pytest.MonkeyPatch, page_result):
    """构造只挂载 auth 路由的应用，并把管理域查询替换为可观测的假实现。

    page_result 为 None 时用于模拟组织节点不可达的分支。
    """

    captured: dict[str, object] = {}

    async def fake_list_authorized_users_page(authorization, permission_key, **kwargs):
        # 记录入参，用于断言关键字与分页参数确实透传到仓储层。
        captured["permission_key"] = permission_key
        captured.update(kwargs)
        return page_result

    monkeypatch.setattr(auth_router, "list_authorized_users_page", fake_list_authorized_users_page)

    app = FastAPI()
    app.include_router(auth, prefix="/api")

    async def override_db():
        return None

    async def override_authorization():
        # has_permission 恒为 True，跳过 user:read 判定，聚焦分页契约本身。
        return SimpleNamespace(user=SimpleNamespace(id=1), has_permission=lambda key: True)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_authorization_context] = override_authorization
    return app, captured


def _visible_rows() -> list[tuple[SimpleNamespace, str]]:
    return [(SimpleNamespace(uid="u1", username="张三", department_id=1), "默认部门")]


async def test_limit_above_upper_bound_is_rejected(monkeypatch) -> None:
    """limit 超过 100 必须被拒，避免调用方重新退化成一次性全量拉取。"""

    app, captured = _build_client(monkeypatch, (_visible_rows(), 7))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/auth/users/access-options", params={"limit": 101})

    assert response.status_code == 422
    # 参数校验失败时不应触碰仓储层。
    assert captured == {}


async def test_total_count_header_and_keyword_are_forwarded(monkeypatch) -> None:
    """总数经 X-Total-Count 回传，keyword 与分页参数透传到仓储层。"""

    app, captured = _build_client(monkeypatch, (_visible_rows(), 7))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/auth/users/access-options", params={"keyword": "张", "skip": 0, "limit": 100}
        )

    assert response.status_code == 200, response.text
    assert response.headers["X-Total-Count"] == "7"
    assert response.json() == [
        {"uid": "u1", "username": "张三", "department_id": 1, "department_name": "默认部门"}
    ]
    assert captured["permission_key"] == "user:read"
    assert captured["keyword"] == "张"
    assert captured["limit"] == 100
    assert captured["skip"] == 0


async def test_unreachable_department_returns_404(monkeypatch) -> None:
    """组织节点不可达时返回 404，而不是回退成管理域内的全量候选用户。"""

    app, _captured = _build_client(monkeypatch, None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/auth/users/access-options", params={"department_id": 999}
        )

    assert response.status_code == 404
