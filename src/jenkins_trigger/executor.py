"""任务执行器: 事件匹配、状态机(待执行/执行中)、按优先级编排执行"""

from __future__ import annotations

import asyncio
import time
from itertools import groupby

from loguru import logger
from pydantic import BaseModel, Field

from .config import AppConfig, ItemConfig, JobConfig
from .dingtalk import DingTalkClient
from .jenkins import BuildResult, JenkinsClient

STATUS_IDLE = "空闲"
STATUS_PENDING = "待执行"
STATUS_RUNNING = "执行中"

# 视为"失败"需重新触发的一次构建结果
FAILED_RESULTS = {"FAILURE", "ABORTED"}

SCHEDULER_INTERVAL = 1.0


class PushEvent(BaseModel):
    gitlab_id: str
    repo_path: str  # 带 group 的路径
    branch: str


class JobState(BaseModel):
    job: JobConfig
    status: str = STATUS_IDLE
    execute_at: float | None = None  # time.monotonic()
    exec_id: str | None = None  # 执行 ID: 一轮执行一个(日期+当日流水号)
    triggers: set[tuple[str, str]] = Field(default_factory=set)  # 待执行的 (仓库, 分支) 集合
    last_results: list[dict] = Field(default_factory=list)

    def snapshot(self) -> dict:
        return {
            "id": self.job.id,
            "name": self.job.name,
            "status": self.status,
            "exec_id": self.exec_id,
            "execute_in_seconds": round(max(0, self.execute_at - time.monotonic()), 1)
            if self.status == STATUS_PENDING and self.execute_at
            else None,
            "triggers": sorted(f"{repo} ({branch})" for repo, branch in self.triggers),
            "last_results": self.last_results,
        }


class Executor:
    def __init__(self, config: AppConfig):
        self.config = config
        self.queue: asyncio.Queue[PushEvent] = asyncio.Queue()
        self.states: dict[str, JobState] = {
            job_id: JobState(job=job) for job_id, job in config.jobs.items()
        }
        self._running: set[str] = set()
        self._jenkins: dict[str, JenkinsClient] = {}
        self._dingtalk: dict[str, DingTalkClient] = {}
        self._tasks: list[asyncio.Task] = []
        self._exec_date = ""
        self._exec_seq = 0

    def _next_exec_id(self) -> str:
        """执行 ID: 日期 + 当日流水号(3位), 跨天重新计数"""
        today = time.strftime("%Y%m%d")
        if today != self._exec_date:
            self._exec_date = today
            self._exec_seq = 0
        self._exec_seq += 1
        return f"{today}-{self._exec_seq:03d}"

    def jenkins_client(self, instance_id: str) -> JenkinsClient:
        if instance_id not in self._jenkins:
            self._jenkins[instance_id] = JenkinsClient(self.config.jenkins[instance_id])
        return self._jenkins[instance_id]

    def dingtalk_client(self, bot_id: str) -> DingTalkClient:
        if bot_id not in self._dingtalk:
            self._dingtalk[bot_id] = DingTalkClient(self.config.dingtalk_bots[bot_id])
        return self._dingtalk[bot_id]

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._worker(), name="event-worker"),
            asyncio.create_task(self._scheduler(), name="scheduler"),
        ]
        logger.info("Executor 已启动, 共 {} 个任务", len(self.states))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for client in (*self._jenkins.values(), *self._dingtalk.values()):
            await client.close()

    # ---- 事件处理 ----

    async def enqueue(self, event: PushEvent) -> None:
        await self.queue.put(event)

    async def _worker(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                self._handle_event(event)
            except Exception:
                logger.exception("处理 Push 事件失败: {}", event)

    def _handle_event(self, event: PushEvent) -> None:
        matched = self.match_jobs(event)
        if not matched:
            logger.debug("无任务匹配: {} {} {}", event.gitlab_id, event.repo_path, event.branch)
            return
        for job in matched:
            self.mark_pending(job, event.repo_path, event.branch)

    def match_jobs(self, event: PushEvent) -> list[JobConfig]:
        """按 GitLab 实例 + 仓库路径 + 分支(仓库分支覆盖默认分支)匹配任务"""
        matched = []
        for job in self.config.jobs.values():
            if job.gitlab != event.gitlab_id:
                continue
            if any(
                item.repo == event.repo_path and job.branch_of(item) == event.branch
                for item in job.items
            ):
                matched.append(job)
        return matched

    def mark_pending(self, job: JobConfig, repo: str, branch: str) -> None:
        """标记为待执行并刷新执行时间(当前时间+延迟), 实现去抖

        去抖窗口内的多个 (仓库, 分支) 累积到 triggers, 本轮一并执行;
        每一轮(非待执行状态下的标记)生成新的 exec_id。
        """
        state = self.states[job.id]
        if state.status != STATUS_PENDING:
            state.exec_id = self._next_exec_id()
        state.status = STATUS_PENDING
        state.execute_at = time.monotonic() + job.delay
        state.triggers.add((repo, branch))
        logger.bind(exec_id=state.exec_id).info(
            "任务 {} 标记为待执行, {} 秒后执行 ({} {})", job.id, job.delay, repo, branch
        )

    # ---- 调度与执行 ----

    async def _scheduler(self) -> None:
        while True:
            now = time.monotonic()
            for job_id, state in self.states.items():
                if (
                    state.status == STATUS_PENDING
                    and state.execute_at is not None
                    and state.execute_at <= now
                    and job_id not in self._running
                ):
                    asyncio.create_task(self._run(state), name=f"run-{job_id}")
            await asyncio.sleep(SCHEDULER_INTERVAL)

    async def _run(self, state: JobState) -> None:
        job = state.job
        # 取走本轮触发集合并清空: 执行期间新到的事件进入下一轮
        triggers = set(state.triggers)
        state.triggers.clear()
        self._running.add(job.id)
        state.status = STATUS_RUNNING
        # 本轮执行内所有日志(含 jenkins/dingtalk 模块)都带 exec_id
        with logger.contextualize(exec_id=state.exec_id or "-"):
            logger.info("任务 {} 开始执行 ({})", job.id, ", ".join(f"{r} {b}" for r, b in sorted(triggers)))
            try:
                await self._notify_started(job, state, triggers)
                plan = await self._make_plan(job, triggers)
                if not plan:
                    logger.warning("任务 {} 执行计划为空, 跳过", job.id)
                    return
                results = await self._execute_plan(job, plan)
                state.last_results = [
                    {"repo": r.repo_path, "branch": r.branch, "job": r.job,
                     "build_number": r.build_number, "result": r.result, "error": r.error}
                    for r in results
                ]
                await self._notify(job, state, results)
            except Exception:
                logger.exception("任务 {} 执行异常", job.id)
            finally:
                self._running.discard(job.id)
                if state.status == STATUS_RUNNING:
                    state.status = STATUS_IDLE
                    state.execute_at = None

    async def _make_plan(self, job: JobConfig, triggers: set[tuple[str, str]]) -> list[ItemConfig]:
        """制定执行计划: 触发集合内 (仓库, 分支) 的执行项 + 上次构建失败的执行项

        以 (repo, job) 去重: 同一仓库可绑定多个 Jenkins Job, 都会进入计划;
        完全相同的 (repo, job) 视为重复配置, 去重。
        """
        jenkins = self.jenkins_client(job.jenkins)
        plan: dict[tuple[str, str], ItemConfig] = {
            (item.repo, item.job): item
            for item in job.items
            if (item.repo, job.branch_of(item)) in triggers
        }
        for item in job.items:
            key = (item.repo, item.job)
            if key in plan:
                continue
            try:
                result = await jenkins.get_last_build_result(item.job)
            except Exception as exc:  # noqa: BLE001 - 查询失败不阻塞计划
                logger.warning("查询 {} 最后构建状态失败: {}", item.job, exc)
                continue
            if result in FAILED_RESULTS:
                logger.info("仓库 {} 上次构建为 {}, 加入执行计划", item.repo, result)
                plan[key] = item
        return sorted(plan.values(), key=lambda i: i.priority)

    async def _execute_plan(self, job: JobConfig, plan: list[ItemConfig]) -> list[BuildResult]:
        """按优先级执行: 同优先级并行, 不同优先级按值从小到大串行"""
        jenkins = self.jenkins_client(job.jenkins)
        results: list[BuildResult] = []
        for priority, group in groupby(plan, key=lambda i: i.priority):
            items = list(group)
            logger.info("任务 {} 执行优先级 {} 的 {} 个执行项", job.id, priority, len(items))
            group_results = await asyncio.gather(
                *(jenkins.run_item(item, job.branch_of(item)) for item in items)
            )
            results.extend(group_results)
        return results

    async def _notify_started(self, job: JobConfig, state: JobState,
                              triggers: set[tuple[str, str]]) -> None:
        """开始执行的通知(在任何 Jenkins API 调用之前)"""
        if not job.dingtalk:
            return
        try:
            await self.dingtalk_client(job.dingtalk).send_build_started(
                job.name, state.exec_id or "-", sorted(triggers)
            )
        except Exception:
            logger.exception("任务 {} 钉钉开始通知发送失败", job.id)

    async def _notify(self, job: JobConfig, state: JobState, results: list[BuildResult]) -> None:
        if not job.dingtalk:
            return
        try:
            await self.dingtalk_client(job.dingtalk).send_build_summary(
                job.name, state.exec_id or "-", results
            )
        except Exception:
            logger.exception("任务 {} 钉钉通知发送失败", job.id)
