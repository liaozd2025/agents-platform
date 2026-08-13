"""仅挂载真实认证路由的 OIDC 多副本集成测试应用。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from server.routers.auth_router import auth
from yuxi.storage.postgres.manager import pg_manager


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """为独立认证副本初始化并释放数据库连接。"""
    pg_manager.initialize()
    yield
    await pg_manager.close()


app = FastAPI(lifespan=lifespan)
app.include_router(auth, prefix="/api")
