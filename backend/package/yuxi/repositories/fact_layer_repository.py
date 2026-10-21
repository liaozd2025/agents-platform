"""产品事实表（Fact Layer）持久化 Repository。

事实表是独立的 PostgreSQL 宽表（一个产品一行，每个事实项一列），
与知识库体系解耦；本模块是它唯一的持久化出口，router 不得直接执行 SQL。
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.utils import logger

_FACT_TABLE = "fact_layer_current"
_NON_FACT_COLUMNS = {"product_id", "sources", "updated_at"}


class FactLayerNotFoundError(KeyError):
    """指定的产品在事实表中不存在。"""


class FactLayerValidationError(ValueError):
    """事实项不在表中或参数不合法。"""


class FactLayerConflictError(Exception):
    """产品已存在等冲突。"""


def _quote_pg_ident(name: str) -> str:
    """把列名安全地包成带引号的 PostgreSQL 标识符。"""
    return '"' + name.replace('"', '""') + '"'


async def _load_fact_columns(session: AsyncSession) -> list[str]:
    """读取事实表当前的全部事实项列名（按表内顺序）。"""
    result = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{_FACT_TABLE}' "
            "AND column_name NOT IN ('product_id', 'sources', 'updated_at') "
            "ORDER BY ordinal_position"
        )
    )
    return [row[0] for row in result]


async def list_fact_layer(session: AsyncSession, product: str) -> dict:
    """读取事实表现值数据；表不存在或查询异常时返回空结果。"""
    try:
        columns = await _load_fact_columns(session)
        result = await session.execute(
            text(
                f"SELECT * FROM {_FACT_TABLE} "
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
        logger.warning("读取产品事实表失败，按空表返回", exc_info=True)
        return {"columns": [], "rows": []}
    return {"columns": columns, "rows": rows}


async def update_fact_row(session: AsyncSession, product_id: str, updates: dict[str, str | None]) -> list[str]:
    """更新某产品行的事实项现值；空字符串表示清空该格。

    返回实际更新的事实项列表；产品不存在抛 FactLayerNotFoundError，
    事实项不在表中抛 FactLayerValidationError。
    """
    valid_columns = set(await _load_fact_columns(session))
    unknown = [name for name in updates if name not in valid_columns]
    if unknown:
        raise FactLayerValidationError(f"事实表中不存在这些事实项: {'、'.join(unknown)}")

    set_clause = ", ".join(f"{_quote_pg_ident(name)} = NULLIF(:v{i}, '')" for i, name in enumerate(updates))
    params = {f"v{i}": ("" if value is None else str(value)) for i, value in enumerate(updates.values())}
    params["pid"] = product_id
    result = await session.execute(
        text(f"UPDATE {_FACT_TABLE} SET {set_clause}, updated_at = now() WHERE product_id = :pid RETURNING product_id"),
        params,
    )
    if result.rowcount == 0:
        raise FactLayerNotFoundError(product_id)
    return list(updates)


async def create_fact_row(session: AsyncSession, product_id: str, values: dict[str, str | None]) -> None:
    """新增一个产品行；产品已存在抛 FactLayerConflictError，事实项不合法抛 FactLayerValidationError。"""
    valid_columns = set(await _load_fact_columns(session))
    unknown = [name for name in values if name not in valid_columns]
    if unknown:
        raise FactLayerValidationError(f"事实表中不存在这些事实项: {'、'.join(unknown)}")

    if values:
        col_list = ", ".join(_quote_pg_ident(name) for name in values)
        placeholders = ", ".join(f":v{i}" for i in range(len(values)))
        params = {f"v{i}": ("" if value is None else str(value)) for i, value in enumerate(values.values())}
        params["pid"] = product_id
        sql = f"INSERT INTO {_FACT_TABLE} (product_id, {col_list}) VALUES (:pid, {placeholders})"
    else:
        params = {"pid": product_id}
        sql = f"INSERT INTO {_FACT_TABLE} (product_id) VALUES (:pid)"

    try:
        await session.execute(text(sql), params)
    except Exception as exc:
        if "duplicate key" in str(exc).lower():
            raise FactLayerConflictError(f"产品「{product_id}」已存在") from exc
        raise


async def delete_fact_row(session: AsyncSession, product_id: str) -> None:
    """删除一个产品行；不存在抛 FactLayerNotFoundError。"""
    result = await session.execute(
        text(f"DELETE FROM {_FACT_TABLE} WHERE product_id = :pid RETURNING product_id"),
        {"pid": product_id},
    )
    if result.rowcount == 0:
        raise FactLayerNotFoundError(product_id)
