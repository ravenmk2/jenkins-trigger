"""cron 定时触发: 每 (任务, cron 表达式) 一个协程, 到点标记待执行 (服务器本地时区)"""

from __future__ import annotations

import asyncio
from datetime import datetime

from croniter import croniter
from loguru import logger

from .config import AppConfig, JobConfig
from .executor import Executor


class CronScheduler:
    def __init__(self, executor: Executor, config: AppConfig):
        self._executor = executor
        self._jobs = [job for job in config.jobs.values() if job.crons]
        self._tasks: list[asyncio.Task] = []

    @staticmethod
    def next_delay(expr: str, base: datetime | None = None) -> float:
        """距下次触发的秒数"""
        base = base or datetime.now()
        next_at = croniter(expr, base).get_next(datetime)
        return max(0.0, (next_at - base).total_seconds())

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._cron_loop(job, expr), name=f"cron-{job.id}")
            for job in self._jobs
            for expr in job.crons
        ]
        if self._tasks:
            logger.info("Cron scheduler started with {} entries", len(self._tasks))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _cron_loop(self, job: JobConfig, expr: str) -> None:
        while True:
            try:
                await asyncio.sleep(self.next_delay(expr))
                self._executor.mark_scheduled(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 异常不退出, 下一轮重新计算触发时间
                logger.exception("Job {} cron '{}' scheduling failed", job.id, expr)
