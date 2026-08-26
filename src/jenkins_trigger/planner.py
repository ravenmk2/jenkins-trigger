"""执行计划生成: Retry (Jenkins 上次失败) / Changed (分支 commit 变化) / Pushed (Push 事件)

reason 优先级: Pushed > Changed > Retry (后者覆盖前者)。
commit 记录: 无记录 → 仅写基线不进计划; 有记录且不同 → Changed; 构建正常结束后回写记录。
"""

from __future__ import annotations

import asyncio

from loguru import logger
from pydantic import BaseModel

from .config import ItemConfig, JobConfig
from .gitlab import GitLabClient
from .jenkins import JenkinsClient
from .store import StateStore

REASON_RETRY = "Retry"
REASON_CHANGED = "Changed"
REASON_PUSHED = "Pushed"

# 视为"失败"需重新触发的一次构建结果
FAILED_RESULTS = {"FAILURE", "ABORTED"}


class PlanItem(BaseModel):
    item: ItemConfig
    branch: str  # 生效分支
    reason: str  # Retry / Changed / Pushed
    commit: str | None = None  # 查询到的分支 sha; 构建完成后回写 store


class Planner:
    def __init__(self, store: StateStore):
        self.store = store

    async def make_plan(
        self,
        job: JobConfig,
        triggers: set[tuple[str, str]],
        jenkins: JenkinsClient,
        gitlab: GitLabClient | None,
    ) -> list[PlanItem]:
        """制定执行计划; 以 (repo, job) 去重, 按 stage 排序"""
        plan: dict[tuple[str, str], PlanItem] = {}
        commits = await self._fetch_commits(job, gitlab) if gitlab else {}

        # 1. Changed: 分支 commit 与记录比较; 无记录仅写基线
        for item in job.items:
            branch = job.branch_of(item)
            sha = commits.get((item.repo, branch))
            if sha is None:
                continue
            recorded = self.store.get_commit(job.id, item.repo, branch)
            if recorded is None:
                logger.info("Job {} repo {} ({}): no commit record, baseline written {}",
                            job.id, item.repo, branch, sha[:8])
                self.store.set_commit(job.id, item.repo, branch, sha)
            elif recorded != sha:
                logger.info("Repo {} ({}) commit changed: {} → {}",
                            item.repo, branch, recorded[:8], sha[:8])
                plan[(item.repo, item.job)] = PlanItem(
                    item=item, branch=branch, reason=REASON_CHANGED, commit=sha
                )

        # 2. Retry: Jenkins 上次构建 FAILURE/ABORTED (不覆盖 Changed)
        for item in job.items:
            key = (item.repo, item.job)
            if key in plan:
                continue
            try:
                result = await jenkins.get_last_build_result(item.job)
            except Exception as exc:  # noqa: BLE001 - 查询失败不阻塞计划
                logger.warning("Failed to query last build result of {}: {}", item.job, exc)
                continue
            if result in FAILED_RESULTS:
                logger.info("Repo {} last build result is {}, added to plan", item.repo, result)
                branch = job.branch_of(item)
                plan[key] = PlanItem(
                    item=item, branch=branch, reason=REASON_RETRY,
                    commit=commits.get((item.repo, branch)),
                )

        # 3. Pushed: Push 事件标记的仓库 (覆盖一切)
        for item in job.items:
            branch = job.branch_of(item)
            if (item.repo, branch) in triggers:
                plan[(item.repo, item.job)] = PlanItem(
                    item=item, branch=branch, reason=REASON_PUSHED,
                    commit=commits.get((item.repo, branch)),
                )

        return sorted(plan.values(), key=lambda p: p.item.stage)

    async def _fetch_commits(
        self, job: JobConfig, gitlab: GitLabClient
    ) -> dict[tuple[str, str], str]:
        """并发查询任务内所有 item 生效分支的 commit; 单条失败仅告警跳过"""
        pairs = {(item.repo, job.branch_of(item)) for item in job.items}

        async def fetch(repo: str, branch: str) -> tuple[tuple[str, str], str | None]:
            try:
                return (repo, branch), await gitlab.get_branch_commit(repo, branch)
            except Exception as exc:  # noqa: BLE001 - 查询失败不阻塞计划
                logger.warning("Failed to query commit of {} ({}): {}", repo, branch, exc)
                return (repo, branch), None

        results = await asyncio.gather(*(fetch(repo, branch) for repo, branch in pairs))
        return {key: sha for key, sha in results if sha is not None}
