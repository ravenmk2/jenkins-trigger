import asyncio
from datetime import datetime

from jenkins_trigger.config import AppConfig, JobConfig
from jenkins_trigger.cron import CronScheduler
from jenkins_trigger.executor import STATUS_PENDING, Executor
from jenkins_trigger.store import StateStore


def make_config(crons) -> AppConfig:
    job = JobConfig(id="job1", name="任务一", gitlab="gl", jenkins="jk", crons=crons)
    return AppConfig(jobs={"job1": job})


def test_next_delay():
    base = datetime(2026, 8, 26, 7, 59, 0)
    assert CronScheduler.next_delay("0 8 * * *", base) == 60.0
    # 每分钟的 cron: 距下一分钟 30 秒
    assert CronScheduler.next_delay("* * * * *", datetime(2026, 8, 26, 7, 59, 30)) == 30.0


async def test_cron_marks_job_scheduled(tmp_path, monkeypatch):
    """cron 到点 → mark_scheduled, 任务进入待执行且立即到期"""
    monkeypatch.setattr(CronScheduler, "next_delay", staticmethod(lambda *a, **k: 0.01))
    config = make_config(["0 8 * * *"])
    executor = Executor(config, StateStore(tmp_path))
    scheduler = CronScheduler(executor, config)
    await scheduler.start()
    try:
        state = executor.states["job1"]
        for _ in range(200):
            if state.status == STATUS_PENDING:
                break
            await asyncio.sleep(0.01)
        assert state.status == STATUS_PENDING
        assert state.plan_id  # 新一轮生成了计划 ID
        assert state.execute_at is not None
    finally:
        await scheduler.stop()


async def test_no_crons_no_tasks(tmp_path):
    config = make_config([])
    executor = Executor(config, StateStore(tmp_path))
    scheduler = CronScheduler(executor, config)
    await scheduler.start()
    assert scheduler._tasks == []
    await scheduler.stop()
