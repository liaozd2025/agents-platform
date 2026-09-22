"""独立恢复进程：真实调用扫描并记录池连接峰值。"""

import asyncio
import json
import sys
import time
from pathlib import Path

from sqlalchemy import event
from yuxi.services.agent_request_queue_service import recover_pending_dispatches
from yuxi.services.run_queue_service import close_queue_clients
from yuxi.storage.postgres.manager import pg_manager


async def main():
    """按测试屏障扫描三轮，保留真实连接、事务和 Redis 发布。"""
    directory, index = Path(sys.argv[1]), sys.argv[2]
    pg_manager.initialize()
    counts = {"active": 0, "peak": 0}

    @event.listens_for(pg_manager.async_engine.sync_engine, "checkout")
    def checkout(*args):
        """独立于扫描实现记录实际借出连接。"""
        counts["active"] += 1
        counts["peak"] = max(counts["peak"], counts["active"])

    @event.listens_for(pg_manager.async_engine.sync_engine, "checkin")
    def checkin(*args):
        """归还连接后同步活动计数。"""
        counts["active"] -= 1

    (directory / f"ready-{index}").touch()
    try:
        for phase in ("locked", "unlocked", "next"):
            while not (directory / phase).exists():
                await asyncio.sleep(0.02)
            started = time.monotonic()
            await recover_pending_dispatches()
            result = counts | {"elapsed_seconds": time.monotonic() - started}
            (directory / f"{phase}-{index}.json").write_text(json.dumps(result))
    finally:
        await close_queue_clients()
        await pg_manager.async_engine.dispose()
        await pg_manager.langgraph_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
