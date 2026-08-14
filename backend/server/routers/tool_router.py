from fastapi import APIRouter, Depends

from yuxi.agents.toolkits.service import get_tool_metadata
from yuxi.permissions.authorization import AuthorizationContext
from server.utils.auth_middleware import require_permission

tools = APIRouter(prefix="/system/tools", tags=["tools"])


@tools.get("")
async def list_tools(
    category: str = None,
    _authorization: AuthorizationContext = Depends(require_permission("tool:manage")),
):
    """获取工具列表"""
    return {"success": True, "data": get_tool_metadata(category)}


@tools.get("/options")
async def get_tool_options(
    _authorization: AuthorizationContext = Depends(require_permission("tool:manage")),
):
    """获取工具选项（前端下拉框用）"""
    all_tools = get_tool_metadata()
    return {"success": True, "data": [{"label": t["name"], "value": t["slug"]} for t in all_tools]}
