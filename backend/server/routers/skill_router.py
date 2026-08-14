"""Skills 管理路由"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_authorization_context, get_db, require_permission
from yuxi.agents.skills.service import (
    confirm_personal_skill_install_draft,
    confirm_skill_install_draft,
    create_skill_node,
    delete_skill,
    delete_skill_node,
    delete_skills_batch,
    delete_personal_skill,
    discard_skill_install_draft,
    export_skill_zip,
    get_manageable_skill_or_raise,
    get_management_readable_skill_or_raise,
    get_skill_dependency_options,
    get_skill_tree,
    init_builtin_skills,
    is_builtin_skill,
    list_accessible_skills,
    list_skill_cards_for_user,
    list_skills,
    list_visible_skills_for_management,
    prepare_remote_skill_install,
    prepare_skill_upload,
    read_personal_skill_file,
    read_skill_file,
    update_skill_dependencies,
    update_skill_enabled,
    update_skill_file,
    update_skill_share_config,
    user_can_manage_skill,
)
from yuxi.permissions import resolve_skill_permission
from yuxi.permissions.authorization import AuthorizationContext
from yuxi.agents.skills.remote_install import list_remote_skills, search_remote_skills
from yuxi.utils.logging_config import logger

skills = APIRouter(prefix="/system/skills", tags=["skills"])
user_skills = APIRouter(prefix="/skills", tags=["skills"])


class ShareConfigPayload(BaseModel):
    share_config: dict | None = Field(None, description="共享权限配置")


class SkillEnabledUpdateRequest(BaseModel):
    enabled: bool = Field(..., description="是否启用")


class SkillNodeCreateRequest(BaseModel):
    path: str = Field(..., description="相对 skill 根目录的路径")
    is_dir: bool = Field(False, description="是否创建目录")
    content: str | None = Field("", description="文件内容（仅文件创建时生效）")


class SkillFileUpdateRequest(BaseModel):
    path: str = Field(..., description="相对 skill 根目录的路径")
    content: str = Field(..., description="文件内容")


class SkillDependenciesUpdateRequest(BaseModel):
    tool_dependencies: list[str] = Field(default_factory=list, description="依赖的内置工具列表")
    mcp_dependencies: list[str] = Field(default_factory=list, description="依赖的 MCP 服务列表")
    skill_dependencies: list[str] = Field(default_factory=list, description="依赖的其他 skill slug 列表")


class RemoteSkillSourceRequest(BaseModel):
    source: str = Field(..., description="远程 Skill 来源，如 owner/repo 或允许的 HTTPS URL")


class RemoteSkillPrepareRequest(RemoteSkillSourceRequest):
    skills: list[str] = Field(..., description="需要安装的 skill 名称列表")


class RemoteSkillSearchRequest(BaseModel):
    query: str = Field(..., description="搜索关键字")


class SkillBatchDeleteRequest(BaseModel):
    slugs: list[str] = Field(..., max_length=50, description="需要批量删除的 skill slug 列表，最多支持 50 个")


class _DraftConfirmRequestBase(BaseModel):
    slugs: list[str] | None = Field(None, description="本次确认安装的 Skill slug")


class SkillDraftConfirmRequest(_DraftConfirmRequestBase):
    share_config: dict | None = Field(None, description="共享权限配置")


class PersonalSkillDraftConfirmRequest(_DraftConfirmRequestBase):
    pass


def _raise_from_value_error(e: ValueError) -> None:
    message = str(e)
    status_code = 404 if "不存在" in message or "无权" in message else 400
    raise HTTPException(status_code=status_code, detail=message)


def _cleanup_export_file(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"Failed to cleanup exported skill archive '{path}': {e}")


def _summarize_results(results: list[dict]) -> dict[str, int]:
    return {
        "total": len(results),
        "success": sum(1 for item in results if item.get("success")),
        "failed": sum(1 for item in results if not item.get("success")),
    }


async def require_skill_access_permission(
    authorization: AuthorizationContext = Depends(get_authorization_context),
) -> AuthorizationContext:
    """校验 Skill 使用或管理功能权限。"""

    if not any(authorization.has_permission(permission) for permission in ("skill:use", "skill:manage")):
        raise HTTPException(status_code=403, detail="缺少功能权限: skill:use")
    return authorization


def _can_manage_skill(authorization: AuthorizationContext, item) -> bool:
    """同时校验 Skill 管理功能权限与资源管理范围。"""

    if getattr(item, "source_scope", None) == "personal":
        return authorization.has_permission("skill:use") and user_can_manage_skill(authorization.user, item)
    return authorization.has_permission("skill:manage") and user_can_manage_skill(authorization.user, item)


def _serialize_skill_for_user(item, authorization: AuthorizationContext) -> dict:
    user = authorization.user
    data = item.to_dict()
    data["can_manage"] = _can_manage_skill(authorization, item)
    data["effective_permission"] = resolve_skill_permission(user, item).value
    data["is_builtin"] = is_builtin_skill(item)
    return data


def _allowed_skill_access_levels(authorization: AuthorizationContext) -> list[str]:
    if authorization.has_permission("skill:manage"):
        return ["global", "department", "user"]
    return ["user"]


@user_skills.get("")
async def list_skill_cards_route(
    refresh_personal: bool = Query(False, description="是否强制重新扫描个人 Skill"),
    authorization: AuthorizationContext = Depends(require_permission("skill:use")),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        items, snapshot = await list_skill_cards_for_user(
            db,
            current_user,
            refresh_personal=refresh_personal,
        )
        return {
            "success": True,
            "data": [_serialize_skill_for_user(item, authorization) for item in items],
            "personal_cache": {
                "scanned_at": snapshot.scanned_at,
                "from_cache": snapshot.from_cache,
            },
            "allowed_access_levels": _allowed_skill_access_levels(authorization),
        }
    except Exception as e:
        logger.error(f"Failed to list Skill cards: {e}")
        raise HTTPException(status_code=500, detail="获取 Skill 列表失败")


@user_skills.get("/accessible")
async def list_accessible_skills_route(
    authorization: AuthorizationContext = Depends(require_permission("skill:use")),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        items = await list_accessible_skills(db, current_user)
        return {"success": True, "data": [_serialize_skill_for_user(item, authorization) for item in items]}
    except Exception as e:
        logger.error(f"Failed to list accessible skills: {e}")
        raise HTTPException(status_code=500, detail="获取可访问 Skills 失败")


@user_skills.post("/import/prepare")
async def prepare_skill_upload_route(
    file: UploadFile = File(...),
    authorization: AuthorizationContext = Depends(require_skill_access_permission),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        data = await prepare_skill_upload(
            db,
            filename=file.filename or "",
            file_bytes=await file.read(),
            operator=current_user,
        )
        return {"success": True, "data": data}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to prepare skill upload: {e}")
        raise HTTPException(status_code=500, detail="解析上传 Skill 失败")


@user_skills.post("/remote/list")
async def list_remote_skills_route(
    payload: RemoteSkillSourceRequest,
    _authorization: AuthorizationContext = Depends(require_skill_access_permission),
):
    try:
        return {"success": True, "data": await list_remote_skills(payload.source)}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to list remote skills from '{payload.source}': {e}")
        raise HTTPException(status_code=500, detail="获取远程 skills 列表失败")


@user_skills.post("/remote/search")
async def search_remote_skills_route(
    payload: RemoteSkillSearchRequest,
    _authorization: AuthorizationContext = Depends(require_skill_access_permission),
):
    try:
        return {"success": True, "data": await search_remote_skills(payload.query)}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to search remote skills with query '{payload.query}': {e}")
        raise HTTPException(status_code=500, detail="搜索远程 skills 失败")


@user_skills.post("/remote/prepare")
async def prepare_remote_skills_route(
    payload: RemoteSkillPrepareRequest,
    authorization: AuthorizationContext = Depends(require_skill_access_permission),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        data = await prepare_remote_skill_install(
            db,
            source=payload.source,
            skills=payload.skills,
            operator=current_user,
        )
        return {"success": True, "data": data}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to prepare remote skills from '{payload.source}': {e}")
        raise HTTPException(status_code=500, detail="解析远程 Skills 失败")


@user_skills.post("/install-drafts/{draft_id}/confirm")
async def confirm_skill_install_draft_route(
    draft_id: str,
    payload: SkillDraftConfirmRequest,
    authorization: AuthorizationContext = Depends(require_permission("skill:manage")),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        results = await confirm_skill_install_draft(
            db,
            draft_id=draft_id,
            share_config=payload.share_config,
            slugs=payload.slugs,
            operator=current_user,
        )
        return {"success": True, "data": results, "summary": _summarize_results(results)}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to confirm skill install draft '{draft_id}': {e}")
        raise HTTPException(status_code=500, detail="确认安装 Skill 失败")


@user_skills.post("/personal/install-drafts/{draft_id}/confirm")
async def confirm_personal_skill_install_draft_route(
    draft_id: str,
    payload: PersonalSkillDraftConfirmRequest,
    authorization: AuthorizationContext = Depends(require_permission("skill:use")),
):
    current_user = authorization.user
    try:
        results = await confirm_personal_skill_install_draft(
            draft_id=draft_id,
            slugs=payload.slugs,
            operator=current_user,
        )
        return {"success": True, "data": results, "summary": _summarize_results(results)}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to confirm personal Skill draft '{draft_id}': {e}")
        raise HTTPException(status_code=500, detail="确认安装个人 Skill 失败")


@user_skills.get("/personal/{slug}/file")
async def read_personal_skill_file_route(
    slug: str,
    path: str = Query(..., description="相对 Skill 根目录的文件路径"),
    authorization: AuthorizationContext = Depends(require_permission("skill:use")),
):
    current_user = authorization.user
    try:
        return {
            "success": True,
            "data": await read_personal_skill_file(str(current_user.uid), slug, path),
        }
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to read personal Skill file '{slug}/{path}': {e}")
        raise HTTPException(status_code=500, detail="读取个人 Skill 文件失败")


@user_skills.delete("/personal/{slug}")
async def delete_personal_skill_route(
    slug: str,
    authorization: AuthorizationContext = Depends(require_permission("skill:use")),
):
    current_user = authorization.user
    try:
        snapshot = await delete_personal_skill(str(current_user.uid), slug)
        return {
            "success": True,
            "personal_cache": {
                "scanned_at": snapshot.scanned_at,
                "from_cache": snapshot.from_cache,
            },
        }
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to delete personal Skill '{slug}': {e}")
        raise HTTPException(status_code=500, detail="删除个人 Skill 失败")


@user_skills.delete("/install-drafts/{draft_id}")
async def discard_skill_install_draft_route(
    draft_id: str,
    authorization: AuthorizationContext = Depends(require_skill_access_permission),
):
    current_user = authorization.user
    try:
        await discard_skill_install_draft(draft_id=draft_id, operator=current_user)
        return {"success": True}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to discard skill install draft '{draft_id}': {e}")
        raise HTTPException(status_code=500, detail="取消安装 Skill 失败")


@skills.get("")
async def list_skills_route(
    authorization: AuthorizationContext = Depends(require_skill_access_permission),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        items = await list_visible_skills_for_management(db, current_user)
        return {
            "success": True,
            "data": [_serialize_skill_for_user(item, authorization) for item in items],
            "allowed_access_levels": _allowed_skill_access_levels(authorization),
        }
    except Exception as e:
        logger.error(f"Failed to list manageable skills: {e}")
        raise HTTPException(status_code=500, detail="获取技能列表失败")


@skills.get("/dependency-options")
async def get_skill_dependency_options_route(
    slug: str | None = Query(None, description="当前 Skill slug"),
    authorization: AuthorizationContext = Depends(require_permission("skill:manage")),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        if slug:
            await get_manageable_skill_or_raise(db, current_user, slug)
        return {"success": True, "data": await get_skill_dependency_options(db, current_user, slug)}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to get skill dependency options: {e}")
        raise HTTPException(status_code=500, detail="获取 skill 依赖选项失败")


@skills.get("/builtin")
async def list_builtin_skills_route(
    _authorization: AuthorizationContext = Depends(require_permission("skill:manage")),
    db: AsyncSession = Depends(get_db),
):
    try:
        items = [item for item in await list_skills(db) if item.source_type == "builtin"]
        return {"success": True, "data": [item.to_dict() for item in items]}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to list builtin skills: {e}")
        raise HTTPException(status_code=500, detail="获取内置 skill 列表失败")


@skills.post("/builtin/sync")
async def sync_builtin_skills_route(
    authorization: AuthorizationContext = Depends(require_permission("skill:manage")),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        items = await init_builtin_skills(db, created_by=current_user.uid)
        return {"success": True, "data": [item.to_dict() for item in items]}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to sync builtin skills: {e}")
        raise HTTPException(status_code=500, detail="同步内置 skill 失败")


@skills.put("/{slug}/share-config")
async def update_skill_share_config_route(
    slug: str,
    payload: ShareConfigPayload,
    authorization: AuthorizationContext = Depends(require_permission("skill:manage")),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        item = await update_skill_share_config(db, slug=slug, share_config=payload.share_config, operator=current_user)
        return {"success": True, "data": _serialize_skill_for_user(item, authorization)}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to update skill share config '{slug}': {e}")
        raise HTTPException(status_code=500, detail="更新 Skill 共享范围失败")


@skills.put("/{slug}/enabled")
async def update_skill_enabled_route(
    slug: str,
    payload: SkillEnabledUpdateRequest,
    authorization: AuthorizationContext = Depends(require_permission("skill:manage")),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        item = await update_skill_enabled(db, slug=slug, enabled=payload.enabled, operator=current_user)
        return {"success": True, "data": _serialize_skill_for_user(item, authorization)}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to update skill enabled '{slug}': {e}")
        raise HTTPException(status_code=500, detail="更新 Skill 启用状态失败")


@skills.get("/{slug}/tree")
async def get_skill_tree_route(
    slug: str,
    authorization: AuthorizationContext = Depends(require_skill_access_permission),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        await get_management_readable_skill_or_raise(db, current_user, slug)
        return {"success": True, "data": await get_skill_tree(db, slug)}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to get skill tree '{slug}': {e}")
        raise HTTPException(status_code=500, detail="获取技能目录树失败")


@skills.get("/{slug}/file")
async def get_skill_file_route(
    slug: str,
    path: str = Query(..., description="相对 skill 根目录路径"),
    authorization: AuthorizationContext = Depends(require_skill_access_permission),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        await get_management_readable_skill_or_raise(db, current_user, slug)
        return {"success": True, "data": await read_skill_file(db, slug, path)}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to read skill file '{slug}/{path}': {e}")
        raise HTTPException(status_code=500, detail="读取技能文件失败")


@skills.post("/{slug}/file")
async def create_skill_file_route(
    slug: str,
    payload: SkillNodeCreateRequest,
    authorization: AuthorizationContext = Depends(require_permission("skill:manage")),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        await get_manageable_skill_or_raise(db, current_user, slug)
        await create_skill_node(
            db,
            slug=slug,
            relative_path=payload.path,
            is_dir=payload.is_dir,
            content=payload.content,
            updated_by=current_user.uid,
        )
        return {"success": True}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to create skill node '{slug}/{payload.path}': {e}")
        raise HTTPException(status_code=500, detail="创建技能文件失败")


@skills.put("/{slug}/file")
async def update_skill_file_route(
    slug: str,
    payload: SkillFileUpdateRequest,
    authorization: AuthorizationContext = Depends(require_permission("skill:manage")),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        await get_manageable_skill_or_raise(db, current_user, slug)
        await update_skill_file(
            db,
            slug=slug,
            relative_path=payload.path,
            content=payload.content,
            updated_by=current_user.uid,
        )
        return {"success": True}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to update skill file '{slug}/{payload.path}': {e}")
        raise HTTPException(status_code=500, detail="更新技能文件失败")


@skills.put("/{slug}/dependencies")
async def update_skill_dependencies_route(
    slug: str,
    payload: SkillDependenciesUpdateRequest,
    authorization: AuthorizationContext = Depends(require_permission("skill:manage")),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        item = await update_skill_dependencies(
            db,
            slug=slug,
            tool_dependencies=payload.tool_dependencies,
            mcp_dependencies=payload.mcp_dependencies,
            skill_dependencies=payload.skill_dependencies,
            operator=current_user,
        )
        return {"success": True, "data": _serialize_skill_for_user(item, authorization)}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to update skill dependencies '{slug}': {e}")
        raise HTTPException(status_code=500, detail="更新 skill 依赖失败")


@skills.delete("/{slug}/file")
async def delete_skill_file_route(
    slug: str,
    path: str = Query(..., description="相对 skill 根目录路径"),
    authorization: AuthorizationContext = Depends(require_permission("skill:manage")),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        await get_manageable_skill_or_raise(db, current_user, slug)
        await delete_skill_node(db, slug=slug, relative_path=path)
        return {"success": True}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to delete skill file '{slug}/{path}': {e}")
        raise HTTPException(status_code=500, detail="删除技能文件失败")


@skills.get("/{slug}/export")
async def export_skill_route(
    slug: str,
    background_tasks: BackgroundTasks,
    authorization: AuthorizationContext = Depends(require_permission("skill:manage")),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        await get_manageable_skill_or_raise(db, current_user, slug)
        export_path, download_name = await export_skill_zip(db, slug)
        background_tasks.add_task(_cleanup_export_file, export_path)
        return FileResponse(path=export_path, media_type="application/zip", filename=download_name)
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to export skill '{slug}': {e}")
        raise HTTPException(status_code=500, detail="导出技能失败")


@skills.delete("/{slug}")
async def delete_skill_route(
    slug: str,
    authorization: AuthorizationContext = Depends(require_permission("skill:manage")),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        await get_manageable_skill_or_raise(db, current_user, slug)
        await delete_skill(db, slug=slug)
        return {"success": True}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to delete skill '{slug}': {e}")
        raise HTTPException(status_code=500, detail="删除技能失败")


@skills.post("/delete-batch")
async def delete_skills_batch_route(
    payload: SkillBatchDeleteRequest,
    authorization: AuthorizationContext = Depends(require_permission("skill:manage")),
    db: AsyncSession = Depends(get_db),
):
    current_user = authorization.user
    try:
        for slug in payload.slugs:
            await get_manageable_skill_or_raise(db, current_user, slug)
        results = await delete_skills_batch(db, slugs=payload.slugs)
        return {"success": True, "data": results, "summary": _summarize_results(results)}
    except ValueError as e:
        _raise_from_value_error(e)
    except Exception as e:
        logger.error(f"Failed to delete skills batch: {e}")
        raise HTTPException(status_code=500, detail="批量删除技能失败")
