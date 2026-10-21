"""产品事实表（Fact Layer）独立路由。

产品事实表是关系型数据的独立模块，与知识库体系解耦：
- 路径不依赖 kb_id；当前阶段对全部登录用户开放读写（暂不启用功能权限）；
- 持久化全部在 fact_layer_repository，router 只做请求转换与状态码映射；
- 与知识库的配合只发生在上层问答编排（事实表优先、检索兜底），管理层面互不隶属。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from server.utils.auth_middleware import get_authorization_context
from yuxi.permissions.authorization import AuthorizationContext
from yuxi.repositories import fact_layer_repository as fact_repo
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User

fact_layer = APIRouter(prefix="/fact-layer", tags=["fact-layer"])


async def _require_login(
    authorization: AuthorizationContext = Depends(get_authorization_context),
) -> User:
    """仅需登录即可访问；产品事实表当前对全部登录用户开放读写。"""
    return authorization.user


class FactLayerUpdateRequest(BaseModel):
    """产品事实表编辑请求：事实项 -> 新值；空字符串表示清空该值"""

    updates: dict[str, str | None]


class FactLayerCreateRequest(BaseModel):
    """新增产品行请求：product_id 为产品名，values 为可选的初始事实值"""

    product_id: str
    values: dict[str, str | None] = {}


@fact_layer.get("")
async def get_fact_layer_table(
    product: str = Query("", description="产品名过滤（模糊匹配）"),
    current_user: User = Depends(_require_login),
):
    """读取产品事实表（Fact Layer）现值数据（宽表：一个产品一行，每个事实项一列）。"""
    async with pg_manager.get_async_session_context() as session:
        data = await fact_repo.list_fact_layer(session, product)
    return {**data, "total": len(data["rows"])}


@fact_layer.patch("/{product_id}")
async def update_fact_layer_row(
    product_id: str,
    request: FactLayerUpdateRequest,
    current_user: User = Depends(_require_login),
):
    """编辑产品事实表（Fact Layer）宽表中某个产品的现值（updates 为 {事实项: 新值}）。"""
    updates = {k.strip(): v for k, v in (request.updates or {}).items() if k.strip()}
    if not updates:
        raise HTTPException(status_code=400, detail="没有要更新的事实项")
    if len(updates) > 50:
        raise HTTPException(status_code=400, detail="单次更新的事实项过多")

    try:
        async with pg_manager.get_async_session_context() as session:
            fact_items = await fact_repo.update_fact_row(session, product_id, updates)
    except fact_repo.FactLayerNotFoundError:
        raise HTTPException(status_code=404, detail=f"产品「{product_id}」不存在") from None
    except fact_repo.FactLayerValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception:
        raise HTTPException(status_code=500, detail="更新产品事实表失败，请稍后重试") from None

    return {"updated": True, "product_id": product_id, "fact_items": fact_items}


@fact_layer.post("")
async def create_fact_layer_row(
    request: FactLayerCreateRequest,
    current_user: User = Depends(_require_login),
):
    """在产品事实表（Fact Layer）宽表中新增一个产品行（当前对全部登录用户开放）。"""
    product_id = (request.product_id or "").strip()
    if not product_id:
        raise HTTPException(status_code=400, detail="产品名不能为空")
    if len(product_id) > 255:
        raise HTTPException(status_code=400, detail="产品名过长")

    values = {k.strip(): v for k, v in (request.values or {}).items() if k.strip()}
    if len(values) > 50:
        raise HTTPException(status_code=400, detail="初始事实项过多")

    try:
        async with pg_manager.get_async_session_context() as session:
            await fact_repo.create_fact_row(session, product_id, values)
    except fact_repo.FactLayerConflictError:
        raise HTTPException(status_code=409, detail=f"产品「{product_id}」已存在") from None
    except fact_repo.FactLayerValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception:
        raise HTTPException(status_code=500, detail="新增产品失败，请稍后重试") from None

    return {"created": True, "product_id": product_id}


@fact_layer.delete("/{product_id}")
async def delete_fact_layer_row(
    product_id: str,
    current_user: User = Depends(_require_login),
):
    """删除产品事实表（Fact Layer）中的一个产品行（当前对全部登录用户开放）。"""
    try:
        async with pg_manager.get_async_session_context() as session:
            await fact_repo.delete_fact_row(session, product_id)
    except fact_repo.FactLayerNotFoundError:
        raise HTTPException(status_code=404, detail=f"产品「{product_id}」不存在") from None
    except Exception:
        raise HTTPException(status_code=500, detail="删除产品失败，请稍后重试") from None

    return {"deleted": True, "product_id": product_id}
