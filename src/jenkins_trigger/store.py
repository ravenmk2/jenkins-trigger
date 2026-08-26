"""运行数据持久化: data/jobs/<job-id>.json, 分支 commit 记录 (commits → repo → branch → sha)"""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field


class JobData(BaseModel):
    """单个任务的数据文件内容: commits 为 repo → branch → sha 三级嵌套"""

    commits: dict[str, dict[str, str]] = Field(default_factory=dict)


class StateStore:
    """按 Job 分文件持久化; set 即落盘, 原子写(先写 tmp 再 replace)

    单事件循环内使用, 无跨线程竞争, 不加锁。
    """

    def __init__(self, data_dir: str | Path = "data"):
        self._dir = Path(data_dir) / "jobs"
        self._jobs: dict[str, JobData] = {}
        self._load()

    def _load(self) -> None:
        if not self._dir.is_dir():
            return
        for file in sorted(self._dir.glob("*.json")):
            try:
                self._jobs[file.stem] = JobData.model_validate_json(file.read_text(encoding="utf-8"))
            except ValueError as exc:  # JSON 损坏或结构不符
                logger.warning("Data file {} is corrupted, treating as empty: {}", file, exc)

    def get_commit(self, job_id: str, repo: str, branch: str) -> str | None:
        """查询记录的分支 commit, 无记录返回 None"""
        data = self._jobs.get(job_id)
        if not data:
            return None
        return data.commits.get(repo, {}).get(branch)

    def set_commit(self, job_id: str, repo: str, branch: str, sha: str) -> None:
        """更新分支 commit 记录并落盘"""
        data = self._jobs.setdefault(job_id, JobData())
        data.commits.setdefault(repo, {})[branch] = sha
        self._save(job_id)

    def _save(self, job_id: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self._dir / f"{job_id}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(self._jobs[job_id].model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, target)
