"""任务执行器: 事件匹配、状态机(空闲/待执行/计划中/执行中)、按 stage 分层编排执行

并发语义:
- 同一 Job 任何时刻只有一轮执行; 计划中/执行中收到新触发 → 重新标记待执行,
  当前轮结束后由 dispatcher 接力拉起第二轮。
- 不同 Job 之间完全并行: dispatcher 为每个到期 Job 各起独立 asyncio task。
"""

from __future__ import annotations

import asyncio
import time
from itertools import groupby

from loguru import logger
from pydantic import BaseModel, Field

from .config import AppConfig, ItemConfig, JobConfig
from .dingtalk import DingTalkClient
from .gitlab import GitLabClient
from .jenkins import BuildResult, JenkinsClient
from .planner import PlanItem, Planner
from .store import StateStore

STATUS_IDLE = "空闲"
STATUS_PENDING = "待执行"
STATUS_PLANNING = "计划中"
STATUS_RUNNING = "执行中"

DISPATCH_INTERVAL = 1.0


class PushEvent(BaseModel):
    gitlab_id: str
    repo_path: str  # 带 group 的路径
    branch: str
    author: str = ""  # 推送人 (GitLab user_name), 用于通知中标注触发作者


class JobState(BaseModel):
    job: JobConfig
    status: str = STATUS_IDLE
    execute_at: float | None = None  # time.monotonic()
    plan_id: str | None = None  # 计划 ID: 一轮执行一个 (YYMMDD-HHMMSS, 本地时间)
    triggers: set[tuple[str, str]] = Field(default_factory=set)  # 待执行的 (仓库, 分支) 集合
    trigger_authors: dict[tuple[str, str], str] = Field(default_factory=dict)  # 各触发的推送人
    last_results: list[dict] = Field(default_factory=list)

    def snapshot(self) -> dict:
        return {
            "id": self.job.id,
            "name": self.job.name,
            "status": self.status,
            "plan_id": self.plan_id,
            "execute_in_seconds": round(max(0, self.execute_at - time.monotonic()), 1)
            if self.status == STATUS_PENDING and self.execute_at
            else None,
            "triggers": sorted(f"{repo} ({branch})" for repo, branch in self.triggers),
            "last_results": self.last_results,
        }


class Executor:
    def __init__(self, config: AppConfig, store: StateStore):
        self.config = config
        self.store = store
        self.planner = Planner(store)
        self.queue: asyncio.Queue[PushEvent] = asyncio.Queue()
        self.states: dict[str, JobState] = {
            job_id: JobState(job=job) for job_id, job in config.jobs.items()
        }
        self._running: set[str] = set()
        self._jenkins: dict[str, JenkinsClient] = {}
        self._gitlab: dict[str, GitLabClient] = {}
        self._dingtalk: dict[str, DingTalkClient] = {}
        self._tasks: list[asyncio.Task] = []

    @staticmethod
    def _new_plan_id() -> str:
        """计划 ID: 本地时间 YYMMDD-HHMMSS (两位年, 其余两位补 0)"""
        return time.strftime("%y%m%d-%H%M%S")

    def jenkins_client(self, instance_id: str) -> JenkinsClient:
        if instance_id not in self._jenkins:
            self._jenkins[instance_id] = JenkinsClient(self.config.jenkins[instance_id])
        return self._jenkins[instance_id]

    def gitlab_client(self, instance_id: str) -> GitLabClient | None:
        """实例配了 api_token 才有客户端; 否则返回 None (跳过 Changed 检测)"""
        instance = self.config.gitlabs[instance_id]
        if not instance.api_token:
            return None
        if instance_id not in self._gitlab:
            self._gitlab[instance_id] = GitLabClient(instance)
        return self._gitlab[instance_id]

    def dingtalk_client(self, bot_id: str) -> DingTalkClient:
        if bot_id not in self._dingtalk:
            self._dingtalk[bot_id] = DingTalkClient(self.config.dingtalk_bots[bot_id])
        return self._dingtalk[bot_id]

    async def start(self) -> None:
        await self._backfill_baselines()
        self._tasks = [
            asyncio.create_task(self._worker(), name="event-worker"),
            asyncio.create_task(self._dispatcher(), name="dispatcher"),
        ]
        logger.info("Executor started with {} jobs", len(self.states))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for client in (*self._jenkins.values(), *self._gitlab.values(), *self._dingtalk.values()):
            await client.close()

    # ---- 启动基线补齐 ----

    async def _backfill_baselines(self) -> None:
        """启动时补齐所有 Job 所有仓库的 commit 记录 (仅补缺失, 已有记录保留)"""
        tasks = []
        for job in self.config.jobs.values():
            gitlab = self.gitlab_client(job.gitlab)
            if gitlab is None:
                if job.items:
                    logger.warning(
                        "Job {}: GitLab instance has no api_token, "
                        "skipping baseline backfill and Changed detection", job.id
                    )
                continue
            tasks += [self._backfill_item(job, gitlab, item) for item in job.items]
        if tasks:
            await asyncio.gather(*tasks)

    async def _backfill_item(self, job: JobConfig, gitlab: GitLabClient, item: ItemConfig) -> None:
        branch = job.branch_of(item)
        if self.store.get_commit(job.id, item.repo, branch) is not None:
            return
        try:
            sha = await gitlab.get_branch_commit(item.repo, branch)
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻塞启动
            logger.warning("Job {}: failed to backfill baseline for {} ({}): {}",
                           job.id, item.repo, branch, exc)
            return
        if sha:
            self.store.set_commit(job.id, item.repo, branch, sha)
            logger.info("Job {} baseline backfilled: {} ({}) = {}", job.id, item.repo, branch, sha[:8])

    # ---- 事件处理 ----

    async def enqueue(self, event: PushEvent) -> None:
        await self.queue.put(event)

    async def _worker(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                self._handle_event(event)
            except Exception:
                logger.exception("Failed to handle push event: {}", event)

    def _handle_event(self, event: PushEvent) -> None:
        matched = self.match_jobs(event)
        if not matched:
            logger.debug("No job matched: {} {} {}", event.gitlab_id, event.repo_path, event.branch)
            return
        for job in matched:
            self.mark_pending(job, event.repo_path, event.branch, event.author)

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

    def mark_pending(self, job: JobConfig, repo: str, branch: str, author: str = "") -> None:
        """Push 触发: 标记待执行并刷新执行时间(当前时间+延迟), 实现去抖

        去抖窗口内的多个 (仓库, 分支) 累积到 triggers, 本轮一并执行。
        """
        state = self.states[job.id]
        execute_at = time.monotonic() + job.delay
        self._mark(state, execute_at)
        state.triggers.add((repo, branch))
        if author:
            state.trigger_authors[(repo, branch)] = author
        # execute_at 是 monotonic 时间, 换算为墙上时间便于阅读; 日志行首时间戳为同一天, 只打到时分秒
        wall_at = time.strftime("%H:%M:%S", time.localtime(time.time() + job.delay))
        logger.bind(plan_id=state.plan_id).info(
            "Job {} marked pending, execute at {} (in {}s) ({} {})",
            job.id, wall_at, job.delay, repo, branch,
        )

    def mark_scheduled(self, job: JobConfig) -> None:
        """cron 触发: 到点立即执行, 不走延迟"""
        state = self.states[job.id]
        self._mark(state, time.monotonic())
        logger.bind(plan_id=state.plan_id).info("Job {} cron fired, marked pending", job.id)

    def _mark(self, state: JobState, execute_at: float) -> None:
        # 每一轮(非待执行状态下的标记)生成新的 plan_id
        if state.status != STATUS_PENDING:
            state.plan_id = self._new_plan_id()
        state.status = STATUS_PENDING
        state.execute_at = execute_at

    # ---- 调度与执行 ----

    async def _dispatcher(self) -> None:
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
            await asyncio.sleep(DISPATCH_INTERVAL)

    async def _run(self, state: JobState) -> None:
        job = state.job
        # 取走本轮触发集合并清空: 执行期间新到的事件进入下一轮
        triggers = set(state.triggers)
        authors = dict(state.trigger_authors)
        state.triggers.clear()
        state.trigger_authors.clear()
        self._running.add(job.id)
        state.status = STATUS_PLANNING
        # 本轮执行内所有日志(含 planner/jenkins/dingtalk 模块)都带 plan_id
        with logger.contextualize(plan_id=state.plan_id or "-"):
            trigger_desc = ", ".join(f"{r} {b}" for r, b in sorted(triggers)) or "cron"
            logger.info("Job {} started ({})", job.id, trigger_desc)
            try:
                plan = await self.planner.make_plan(
                    job, triggers,
                    self.jenkins_client(job.jenkins), self.gitlab_client(job.gitlab),
                    authors,
                )
                if not plan:
                    logger.info("Job {} plan is empty, skipped", job.id)
                    return
                await self._notify_started(job, state, plan)
                state.status = STATUS_RUNNING
                results = await self._execute_plan(job, plan)
                self._update_commits(job, plan, results)
                state.last_results = [
                    {"name": r.name, "repo": r.repo_path, "branch": r.branch, "job": r.job,
                     "build_number": r.build_number, "result": r.result, "error": r.error}
                    for r in results
                ]
                await self._notify(job, state, results)
            except Exception:
                logger.exception("Job {} execution failed", job.id)
            finally:
                self._running.discard(job.id)
                if state.status in (STATUS_PLANNING, STATUS_RUNNING):
                    state.status = STATUS_IDLE
                    state.execute_at = None

    async def _execute_plan(self, job: JobConfig, plan: list[PlanItem]) -> list[BuildResult]:
        """按 stage 分层执行: 同 stage 并行, 不同 stage 按值从小到大串行"""
        jenkins = self.jenkins_client(job.jenkins)
        results: list[BuildResult] = []
        for stage, group in groupby(plan, key=lambda p: p.item.stage):
            items = list(group)
            logger.info("Job {} executing stage {} with {} items", job.id, stage, len(items))
            group_results = await asyncio.gather(
                *(jenkins.run_item(p.item, p.branch) for p in items)
            )
            results.extend(group_results)
        return results

    def _update_commits(
        self, job: JobConfig, plan: list[PlanItem], results: list[BuildResult]
    ) -> None:
        """构建正常结束的执行项回写 commit 记录; 异常的不更新, 下轮 Changed 自动重试"""
        by_key = {(r.repo_path, r.job): r for r in results}
        for p in plan:
            if p.commit is None:
                continue
            result = by_key.get((p.item.repo, p.item.job))
            if result and result.error is None:
                self.store.set_commit(job.id, p.item.repo, p.branch, p.commit)

    async def _notify_started(
        self, job: JobConfig, state: JobState, plan: list[PlanItem]
    ) -> None:
        """计划生成后的开始通知(列出计划项 name 与 reason)"""
        if not job.dingtalk:
            return
        try:
            await self.dingtalk_client(job.dingtalk).send_build_started(
                job.name, state.plan_id or "-", plan
            )
        except Exception:
            logger.exception("Job {}: failed to send DingTalk start notification", job.id)

    async def _notify(self, job: JobConfig, state: JobState, results: list[BuildResult]) -> None:
        if not job.dingtalk:
            return
        try:
            await self.dingtalk_client(job.dingtalk).send_build_summary(
                job.name, state.plan_id or "-", results
            )
        except Exception:
            logger.exception("Job {}: failed to send DingTalk summary notification", job.id)
