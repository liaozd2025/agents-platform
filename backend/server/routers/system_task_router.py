from fastapi import APIRouter, Depends, HTTPException, Query

from yuxi.permissions.authorization import AuthorizationContext
from yuxi.services.task_service import tasker
from server.utils.auth_middleware import require_permission

tasks = APIRouter(prefix="/tasks", tags=["tasks"])


@tasks.get("")
async def list_tasks(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=100),
    _authorization: AuthorizationContext = Depends(require_permission("system_task:manage")),
):
    """List tasks, optionally filtered by status."""
    return await tasker.list_tasks(status=status, limit=limit)


@tasks.get("/{task_id}")
async def get_task(
    task_id: str,
    _authorization: AuthorizationContext = Depends(require_permission("system_task:manage")),
):
    """Retrieve a single task by id."""
    task = await tasker.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": task}


@tasks.post("/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    _authorization: AuthorizationContext = Depends(require_permission("system_task:manage")),
):
    """Request cancellation of a task."""
    success = await tasker.cancel_task(task_id)
    if not success:
        raise HTTPException(status_code=400, detail="Task cannot be cancelled")
    return {"task_id": task_id, "status": "cancelled"}


@tasks.delete("/{task_id}")
async def delete_task(
    task_id: str,
    _authorization: AuthorizationContext = Depends(require_permission("system_task:manage")),
):
    """Delete a task by id."""
    success = await tasker.delete_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": task_id, "status": "deleted"}
