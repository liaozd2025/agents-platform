"""仅挂载真实认证路由、使用测试独立 Schema 的多副本应用。"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.api.dependencies.auth import get_db
from yuxi.api.errors import domain_error_handler
from yuxi.api.routers.identity.auth import auth
from yuxi.infrastructure.redis import close_async_redis_client
from yuxi.shared.errors import DomainError

engine = create_async_engine(os.environ["POSTGRES_URL"], connect_args={"server_settings": {"search_path": os.environ["YUXI_TEST_SCHEMA"]}})
factory = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """只释放该测试副本自己的连接。"""
    yield
    await close_async_redis_client()
    await engine.dispose()


async def request_db():
    """请求借用独立 Schema，提交由真实服务用例负责。"""
    async with factory() as db:
        yield db


app = FastAPI(lifespan=lifespan)
app.add_exception_handler(DomainError, domain_error_handler)
app.dependency_overrides[get_db] = request_db
app.include_router(auth, prefix="/api")
