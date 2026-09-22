"""仅为 E2E 在正式启动完成后、领取 job 前设置可控屏障。"""

import asyncio
from pathlib import Path

from server.worker_main import main
from yuxi.services.run_worker import WorkerSettings

startup = WorkerSettings.on_startup


async def wait_for_database_lock(ctx):
    """保留真实启动、恢复和 heartbeat，让测试先取得准备阶段的数据库表锁。"""
    await startup(ctx)
    Path("/state/worker-ready").touch()
    while not Path("/state/start-worker").exists():
        await asyncio.sleep(0.1)


if __name__ == "__main__":
    WorkerSettings.on_startup = wait_for_database_lock
    main()
