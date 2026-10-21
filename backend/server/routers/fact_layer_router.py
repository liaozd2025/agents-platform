"""产品事实表（Fact Layer）独立路由。

产品事实表是关系型数据的独立模块，与知识库体系解耦：
- 路径不依赖 kb_id；当前阶段对全部登录用户开放读写（暂不启用功能权限）；
- 与知识库的配合只发生在上层问答编排（事实表优先、检索兜底），管理层面互不隶属。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from server.utils.auth_middleware import get_authorization_context
from yuxi.permissions.authorization import AuthorizationContext
from yuxi.storage.postgres.models_business import User
from yuxi.utils import logger

fact_layer = APIRouter(prefix="/fact-layer", tags=["fact-layer"])


async def _require_login(
    authorization: AuthorizationContext = Depends(get_authorization_context),
) -> User:
    """仅需登录即可访问；产品事实表当前对全部登录用户开放读写。"""
    return authorization.user


def _quote_pg_ident(name: str) -> str:
    """把列名安全地包成带引号的 PostgreSQL 标识符"""
    return '"' + name.replace('"', '""') + '"'


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
    from sqlalchemy import text

    from yuxi.storage.postgres.manager import pg_manager

    try:
        async with pg_manager.get_async_session_context() as session:
            col_result = await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'fact_layer_current' "
                    "AND column_name NOT IN ('product_id', 'sources', 'updated_at') "
                    "ORDER BY ordinal_position"
                )
            )
            fact_columns = [row[0] for row in col_result]
            result = await session.execute(
                text(
                    "SELECT * FROM fact_layer_current "
                    "WHERE (:product = '' OR product_id ILIKE '%' || :product || '%') "
                    "ORDER BY product_id"
                ),
                {"product": (product or "").strip()},
            )
            rows = [dict(row._mapping) for row in result]
            for row in rows:
                row.pop("sources", None)
                row.pop("updated_at", None)
    except Exception:
        # 表不存在或查询异常时返回空列表，不影响其他功能
        fact_columns = []
        rows = []
    return {"columns": fact_columns, "rows": rows, "total": len(rows)}


@fact_layer.patch("/{product_id}")
async def update_fact_layer_row(
    product_id: str,
    request: FactLayerUpdateRequest,
    current_user: User = Depends(_require_login),
):
    """编辑产品事实表（Fact Layer）宽表中某个产品的现值。

    - updates 为 {事实项: 新值}；传空字符串表示清空该格
    """
    from sqlalchemy import text

    from yuxi.storage.postgres.manager import pg_manager

    updates = {k.strip(): v for k, v in (request.updates or {}).items() if k.strip()}
    if not updates:
        raise HTTPException(status_code=400, detail="没有要更新的事实项")
    if len(updates) > 50:
        raise HTTPException(status_code=400, detail="单次更新的事实项过多")

    try:
        async with pg_manager.get_async_session_context() as session:
            col_result = await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'fact_layer_current' "
                    "AND column_name NOT IN ('product_id', 'sources', 'updated_at')"
                )
            )
            valid_columns = {row[0] for row in col_result}
            unknown = [name for name in updates if name not in valid_columns]
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"事实表中不存在这些事实项: {'、'.join(unknown)}",
                )

            set_clause = ", ".join(
                f"{_quote_pg_ident(name)} = NULLIF(:v{i}, '')"
                for i, name in enumerate(updates)
            )
            params = {
                f"v{i}": ("" if value is None else str(value))
                for i, value in enumerate(updates.values())
            }
            params["pid"] = product_id
            result = await session.execute(
                text(
                    f"UPDATE fact_layer_current SET {set_clause}, updated_at = now() "
                    f"WHERE product_id = :pid RETURNING product_id"
                ),
                params,
            )
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail=f"产品「{product_id}」不存在")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"更新产品事实表失败: {exc}")
        raise HTTPException(status_code=500, detail="更新产品事实表失败，请稍后重试") from exc

    return {"updated": True, "product_id": product_id, "fact_items": list(updates)}


@fact_layer.post("")
async def create_fact_layer_row(
    request: FactLayerCreateRequest,
    current_user: User = Depends(_require_login),
):
    """在产品事实表（Fact Layer）宽表中新增一个产品行（当前对全部登录用户开放）。"""
    from sqlalchemy import text

    from yuxi.storage.postgres.manager import pg_manager

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
            col_result = await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'fact_layer_current' "
                    "AND column_name NOT IN ('product_id', 'sources', 'updated_at')"
                )
            )
            valid_columns = {row[0] for row in col_result}
            unknown = [name for name in values if name not in valid_columns]
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"事实表中不存在这些事实项: {'、'.join(unknown)}",
                )

            if values:
                col_list = ", ".join(_quote_pg_ident(name) for name in values)
                placeholders = ", ".join(f":v{i}" for i in range(len(values)))
                params = {
                    f"v{i}": ("" if value is None else str(value))
                    for i, value in enumerate(values.values())
                }
                params["pid"] = product_id
                sql = (
                    f"INSERT INTO fact_layer_current (product_id, {col_list}) "
                    f"VALUES (:pid, {placeholders}) RETURNING product_id"
                )
            else:
                params = {"pid": product_id}
                sql = "INSERT INTO fact_layer_current (product_id) VALUES (:pid) RETURNING product_id"

            result = await session.execute(text(sql), params)
            if result.rowcount == 0:
                raise HTTPException(status_code=409, detail=f"产品「{product_id}」已存在")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        if "duplicate key" in str(exc).lower():
            raise HTTPException(status_code=409, detail=f"产品「{product_id}」已存在") from exc
        logger.warning(f"新增产品事实行失败: {exc}")
        raise HTTPException(status_code=500, detail="新增产品失败，请稍后重试") from exc

    return {"created": True, "product_id": product_id}


@fact_layer.delete("/{product_id}")
async def delete_fact_layer_row(
    product_id: str,
    current_user: User = Depends(_require_login),
):
    """删除产品事实表（Fact Layer）中的一个产品行（当前对全部登录用户开放）。"""
    from sqlalchemy import text

    from yuxi.storage.postgres.manager import pg_manager

    try:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                text("DELETE FROM fact_layer_current WHERE product_id = :pid RETURNING product_id"),
                {"pid": product_id},
            )
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail=f"产品「{product_id}」不存在")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"删除产品事实行失败: {exc}")
        raise HTTPException(status_code=500, detail="删除产品失败，请稍后重试") from exc

    return {"deleted": True, "product_id": product_id}
