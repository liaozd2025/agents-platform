"""组织快照的真实身份归属与撤销边界。"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from test.support.sqlite import create_utc_sqlite_engine
from yuxi.infrastructure.postgres.base import Base
from yuxi.infrastructure.postgres.models import load_models
from yuxi.modules.identity.models import Department, User
from yuxi.modules.identity.services.organization_snapshot import get_user_organization_snapshot


@pytest_asyncio.fixture
async def snapshot_db():
    """每例使用独立关系库验证 owning lookup，而非模拟查询结果。"""
    load_models()
    engine = create_utc_sqlite_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        department = Department(name="Owner organization", path="/1/10/")
        owner = User(uid="owner", username="owner", password_hash="test", department=department)
        db.add_all([department, owner])
        await db.flush()
        end_user = User(
            uid="end-user",
            username="end-user",
            password_hash="!disabled",
            user_kind="end_user",
            app_id="app",
            end_user_id="external",
            owner_user_id=owner.id,
        )
        db.add(end_user)
        await db.commit()
        yield db, owner, end_user
    await engine.dispose()


@pytest.mark.asyncio
async def test_end_user_snapshot_uses_current_owner_department(snapshot_db):
    db, owner, end_user = snapshot_db
    expected = await get_user_organization_snapshot(db, uid=owner.uid)
    actual = await get_user_organization_snapshot(db, uid=end_user.uid)
    assert actual == expected
    assert actual["organization_path_snapshot"] == "/1/10/"
    assert actual["organization_snapshot_inferred"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "deleted"])
async def test_invalid_owner_cannot_create_anonymous_organization_snapshot(snapshot_db, state):
    db, owner, end_user = snapshot_db
    if state == "missing":
        end_user.owner_user_id = owner.id + 999
    else:
        owner.is_deleted = True
    await db.commit()
    with pytest.raises(ValueError, match="组织快照对应的用户"):
        await get_user_organization_snapshot(db, uid=end_user.uid)
