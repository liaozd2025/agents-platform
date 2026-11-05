"""用独立 PostgreSQL 读取证明模型缓存刷新发生在提交之后。"""

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import yuxi.api.routers.models as router
from yuxi.modules.models.tables import ModelProvider

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.self_managed_resources]


async def test_provider_refresh_observes_committed_values_in_another_session(monkeypatch):
    """真实更新用例和事务提交完成后，刷新边界才能从独立连接读到新配置。"""
    schema = "pytest_provider_publish_" + uuid4().hex
    admin = create_async_engine(os.environ["POSTGRES_URL"])
    engine = create_async_engine(os.environ["POSTGRES_URL"], connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    observed = []

    async def refresh():
        """替换缓存输出边界，使用另一连接作为提交事实的独立 oracle。"""
        async with factory() as reader:
            observed.append(await reader.scalar(select(ModelProvider.enabled_models).where(ModelProvider.provider_id == "fixture")))

    monkeypatch.setattr(router, "_refresh_model_cache", refresh)
    try:
        async with admin.begin() as db:
            await db.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as db:
            await db.run_sync(ModelProvider.__table__.create)
        async with factory() as db:
            db.add(
                ModelProvider(
                    provider_id="fixture",
                    display_name="Fixture",
                    base_url="https://fixture.invalid/v1",
                    capabilities=["chat"],
                    enabled_models=[{"id": "old-model", "type": "chat"}],
                )
            )
            await db.commit()
        async with factory() as writer:
            result = await router.update_provider(
                "fixture", router.ModelProviderPayload(enabled_models=[]), current_user=SimpleNamespace(username="fixture-admin"), db=writer
            )
        assert result["success"] is True
        assert observed == [[]]
        async with factory() as reader:
            provider = await reader.scalar(select(ModelProvider).where(ModelProvider.provider_id == "fixture"))
            assert provider.enabled_models == []
            assert provider.updated_by == "fixture-admin"
    finally:
        await engine.dispose()
        async with admin.begin() as db:
            await db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()
