"""产品事实真值表工具模块

产品事实表（fact_layer_current 宽表）的只读查询工具。
产品确定性事实（规格/参数/数值类）的唯一权威来源，与向量检索链路相互独立。

本工具是独立注册的工具，不隶属知识库工具包，也不依赖会话挂载了哪个知识库；
与产品知识库的配合只发生在上层问答编排（事实表优先、检索兜底）。
"""

from typing import Any

from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import BaseModel, Field
from sqlalchemy import text

from yuxi.agents.toolkits.registry import tool
from yuxi.storage.postgres.manager import pg_manager
from yuxi.utils import logger

# 产品知识库名称（仅用于上层问答编排的业务判断，如检索闸门；工具本身不依赖它）
PRODUCT_KB_NAME = "产品知识库"
# 事实表（宽表：一个产品一行，每个事实项一列）
_FACT_TABLE = "fact_layer_current"
# 宽表中的非事实项列
_NON_FACT_COLUMNS = ("product_id", "sources", "updated_at")


class QueryProductFactsInput(BaseModel):
    """查询产品事实输入模型"""

    product_name: str | None = Field(
        default=None,
        description="产品名称，支持部分匹配（如'阿司匹林'可命中'阿司匹林肠溶片'）",
    )
    fact_item: str | None = Field(
        default=None,
        description="要查询的具体事实项（如'有效期'、'用法用量'、'规格（说明书【规格】栏）'）。为空时返回该产品的全部事实",
    )
    query: str | None = Field(
        default=None,
        description="兼容别名：模型可能把产品名（可含事实项）放到 query 参数里传入，等价于 product_name",
    )


def _quote_ident(name: str) -> str:
    """把列名安全地包成带引号的 PostgreSQL 标识符"""
    return '"' + name.replace('"', '""') + '"'


async def _load_fact_columns(session) -> list[str]:
    """读取事实表的全部事实项列名（按建表顺序）"""
    result = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :table_name "
            "ORDER BY ordinal_position"
        ),
        {"table_name": _FACT_TABLE},
    )
    return [row[0] for row in result if row[0] not in _NON_FACT_COLUMNS]


@tool(
    category="buildin",
    tags=["产品事实"],
    display_name="查询产品事实",
    args_schema=QueryProductFactsInput,
)
async def query_product_facts(
    product_name: str | None = None, fact_item: str | None = None, runtime: ToolRuntime = None, query: str | None = None
) -> Any:
    """查询产品事实表，获取产品确定性事实的权威当前值。

    事实表是产品事实数据的唯一权威来源，独立于知识库文档体系。
    当用户询问某个产品的确定性事实信息时，必须优先且只使用本工具查询，
    禁止改用检索知识库（query_kb）等向量检索工具。

    必须使用本工具的场景（有唯一正确答案的事实类问题）：
    - 产品的规格、成份、性状、用法用量
    - 产品的有效期、批准文号、贮藏条件、生产企业、上市许可持有人
    - 用户明确询问"某产品的某项指标是多少"

    不应使用本工具的场景：
    - 开放性、解释性、方法论类问题（如"为什么""怎么用"），改用检索知识库工具
    - 与产品事实数据无关的文档内容问题

    使用规则：
    1. 本工具返回的值即为当前权威答案，直接依据它组织回答，不要再用检索结果佐证或覆盖
    2. 若未查到对应产品或事实项，如实告知"事实表中暂无该项数据"，不要凭记忆编造数值
    3. product_name 传用户提到的产品名即可，支持部分匹配
    4. fact_item 传用户询问的事实项名称，为空时返回该产品的全部事实

    Args:
        product_name: 产品名称
        fact_item: 具体事实项，为空时返回该产品全部事实

    Returns:
        产品事实列表，每条包含事实项、当前值与来源信息
    """
    product_value = (product_name or "").strip()
    item = (fact_item or "").strip()
    alias_query = (query or "").strip()
    # 兼容模型传参走形：把产品名（可含事实项）写进 query 别名，如「乙酰半胱氨酸颗粒 有效期」
    if not product_value and alias_query:
        parts = alias_query.split()
        product_value = parts[0] if parts else ""
        if not item and len(parts) > 1:
            item = "".join(parts[1:]).strip()
    if not product_value:
        return "请提供要查询的产品名称"

    product_pattern = f"%{product_value}%"

    try:
        async with pg_manager.get_async_session_context() as session:
            fact_columns = await _load_fact_columns(session)
            if not fact_columns:
                return "产品事实表暂无数据"

            if item:
                exact = [c for c in fact_columns if c == item]
                fuzzy = [c for c in fact_columns if c != item and item in c]
                target_columns = exact or fuzzy
                if not target_columns:
                    return f"产品事实表中没有名为“{item}”的事实项。可用事实项：{'、'.join(fact_columns)}"
            else:
                target_columns = fact_columns

            select_list = ", ".join(_quote_ident(c) for c in target_columns)
            statement = text(
                f"""
                SELECT product_id, {select_list}, sources
                FROM {_FACT_TABLE}
                WHERE product_id ILIKE :product_pattern
                ORDER BY product_id
                LIMIT 20
                """
            )
            result = await session.execute(statement, {"product_pattern": product_pattern})
            rows = result.mappings().all()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"查询产品事实表失败: {exc}")
        return "产品事实查询失败，请稍后重试"

    if not rows:
        item_hint = f"事实项“{item}”" if item else "任何事实"
        return f"产品事实表中暂无“{product_name}”的{item_hint}数据"

    output = []
    for row in rows:
        sources = row.get("sources") or {}
        if item:
            for col in target_columns:
                value = row.get(col)
                if value is None:
                    continue
                source = sources.get(col) or {}
                output.append(
                    {
                        "产品": row["product_id"],
                        "事实项": col,
                        "当前值": value,
                        "来源文件": source.get("source_file"),
                        "来源等级": source.get("source_level"),
                        "生效日期": source.get("effective_date"),
                    }
                )
        else:
            facts = {}
            for col in fact_columns:
                if row.get(col) is not None:
                    facts[col] = row[col]
            source_files = sorted(
                {(meta or {}).get("source_file") for meta in sources.values() if (meta or {}).get("source_file")}
            )
            output.append(
                {
                    "产品": row["product_id"],
                    "事实": facts,
                    "来源文件": source_files,
                }
            )
    return output


async def fact_table_coverage_detail(query: str) -> dict:
    """逐项判断事实表对该事实类问题的覆盖情况。

    返回 {"matched_products", "required_items", "covered_items", "missing_items"}；
    产品不在表或问题未映射到任何事实列时各列表为空，由调用方按不覆盖处理。
    """
    detail = {"matched_products": [], "required_items": [], "covered_items": [], "missing_items": []}
    if not query or not query.strip():
        return detail
    try:
        async with pg_manager.get_async_session_context() as session:
            fact_columns = await _load_fact_columns(session)
            if not fact_columns:
                return detail
            # 产品匹配：问题文本包含表中产品名（可能命中多个，逐一校验）
            rows = await session.execute(text("SELECT product_id FROM fact_layer_current"))
            products = [r[0] for r in rows]
            matched = sorted({p for p in products if p and p in query}, key=len, reverse=True)
            if not matched:
                return detail
            # 事实项匹配：问题中提及、且能映射到表中列名的事实项（列名取括号前主干比对）
            required = []
            for col in fact_columns:
                base = col.split("（")[0].split("(")[0].strip()
                if base and base in query:
                    required.append(col)
            # 覆盖判定必须从严：问题按事实类词汇提问、但该词汇映射不到任何列时，
            # 记为缺失而不是忽略——否则「有效期和价格」这类问题会被误判全覆盖、
            # 跳过检索后无法回答未收录项（审阅 P1#3）。
            from yuxi.services.knowledge_retrieval_policy import _FACT_PATTERNS

            mapped_bases = {col.split("（")[0].split("(")[0].strip() for col in fact_columns}
            unmapped = [
                pattern
                for pattern in _FACT_PATTERNS
                if pattern in query
                and not any(pattern == base or pattern in base or base in pattern for base in mapped_bases)
            ]
            if not required and not unmapped:
                return detail
            if not required:
                # 有事实类词汇但全部映射不上列：直接判不覆盖，缺项即这些词汇。
                detail["matched_products"] = matched
                detail["missing_items"] = unmapped
                return detail
            cols_sql = ", ".join(_quote_ident(c) for c in required)
            covered = list(required)
            for pid in matched:
                res = await session.execute(
                    text(f"SELECT {cols_sql} FROM {_FACT_TABLE} WHERE product_id = :pid"),
                    {"pid": pid},
                )
                values = res.first()
                if values is None:
                    return {
                        "matched_products": [],
                        "required_items": [],
                        "covered_items": [],
                        "missing_items": [],
                    }
                for col, value in zip(required, values):
                    if (value is None or not str(value).strip()) and col in covered:
                        covered.remove(col)
            detail["matched_products"] = matched
            detail["required_items"] = required
            detail["covered_items"] = covered
            detail["missing_items"] = [c for c in required if c not in covered] + unmapped
            return detail
    except Exception:
        logger.warning("fact_table_coverage_detail 预判失败，按不覆盖处理", exc_info=True)
        return detail


async def fact_table_covers_query(query: str) -> bool:
    """判断事实表能否完整覆盖该事实类问题（检索闸门的确定性预判）。

    规则：问题文本能对应到表中的产品，且问题提及的事实项（能映射到表列名的）
    在该产品行上均有非空值，才返回 True；产品不在表、事实项为空或无法判断时
    返回 False，交回原有检索链路兜底。
    """
    detail = await fact_table_coverage_detail(query)
    return bool(detail["matched_products"]) and bool(detail["required_items"]) and not detail["missing_items"]


async def product_in_fact_table(query: str) -> bool:
    """判断问题文本中是否提到了事实表中已收录的产品（整行请求的轻量预判）。"""
    if not query or not query.strip():
        return False
    try:
        async with pg_manager.get_async_session_context() as session:
            rows = await session.execute(text("SELECT product_id FROM fact_layer_current"))
            products = [r[0] for r in rows]
            return any(p and p in query for p in products)
    except Exception:
        logger.warning("product_in_fact_table 预判失败，按未命中处理", exc_info=True)
        return False


def format_whole_row_note() -> str:
    """构造「产品基本信息类整行请求」的固定回答路径说明。"""
    return (
        "<fact_table_note>\n"
        "用户询问的是产品基本信息类综合请求，回答必须按以下固定路径组织：\n"
        "1. 先调用 query_product_facts 工具（product_name 传用户提到的产品名，fact_item 不传，"
        "即可一次获取该产品在事实表中的全部已收录事实），以返回的事实表内容为主体作答，"
        "这部分不要输出任何引用标记；\n"
        "2. 事实表中未收录或为空的项，不得编造，可依据上方 <knowledge_context> 中的检索内容补充，"
        "但必须先说明「产品事实表中未收录该项，以下内容来自产品知识库文档检索」，"
        "这部分保留来源引用标记。\n"
        "</fact_table_note>"
    )


def format_fact_table_note(detail: dict) -> str:
    """根据逐项覆盖结果构造注入消息的事实表覆盖说明。"""
    if not detail.get("matched_products"):
        return (
            "<fact_table_note>\n"
            "用户询问的产品不在产品事实表中，本次答案不是来自事实表。回答时必须先用一句话向用户说明"
            "「产品事实表中未收录该产品，以下内容来自产品知识库文档检索」，再依据上方 <knowledge_context> "
            "中的内容作答，并保留来源引用标记。\n"
            "</fact_table_note>"
        )
    if not detail.get("required_items"):
        return (
            "<fact_table_note>\n"
            "用户询问的事实项无法对应到产品事实表中的列，按事实表未收录处理。回答时必须先用一句话向用户说明"
            "「产品事实表中未收录该事实项，以下内容来自产品知识库文档检索」，再依据上方 <knowledge_context> "
            "中的内容作答，并保留来源引用标记。\n"
            "</fact_table_note>"
        )
    covered = "、".join(detail.get("covered_items") or [])
    missing = "、".join(detail.get("missing_items") or [])
    parts = ["<fact_table_note>\n用户的问题同时涉及事实表已收录与未收录的事实项，回答必须逐项区分来源："]
    if covered:
        parts.append(f"- 「{covered}」在产品事实表中已收录：直接以事实表值为准作答，这部分不要输出任何引用标记；")
    if missing:
        parts.append(
            f"- 「{missing}」在产品事实表中未收录（该项无值）：必须先说明「产品事实表中未收录该事实项，"
            "以下内容来自产品知识库文档检索」，再依据上方 <knowledge_context> 中的内容作答，这部分保留来源引用标记。"
        )
    parts.append("</fact_table_note>")
    return "\n".join(parts)
