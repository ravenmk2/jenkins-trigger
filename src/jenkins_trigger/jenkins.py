"""Jenkins API 异步客户端"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

import httpx
from loguru import logger
from pydantic import BaseModel

from .config import JenkinsInstance, RepoConfig

# 等待构建完成的默认超时(秒)与轮询间隔
BUILD_TIMEOUT = 3600
POLL_INTERVAL = 10


class BuildResult(BaseModel):
    repo_path: str
    job: str
    branch: str
    build_number: int | None = None
    result: str | None = None  # SUCCESS / FAILURE / ABORTED / ...
    error: str | None = None  # 触发/查询阶段异常

    @property
    def ok(self) -> bool:
        return self.error is None and self.result == "SUCCESS"


def job_url_path(job_path: str) -> str:
    """'folder/job-a' -> 'job/folder/job/job-a'"""
    return "/".join(f"job/{quote(seg, safe='')}" for seg in job_path.strip("/").split("/"))


class JenkinsClient:
    def __init__(self, instance: JenkinsInstance, timeout: float = 30.0):
        self.instance = instance
        self.base = instance.url.rstrip("/")
        self._client = httpx.AsyncClient(
            auth=(instance.username, instance.token),
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        self._crumb: dict[str, str] | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _post_headers(self) -> dict[str, str]:
        """获取 CSRF crumb; 未启用 CSRF 防护时返回空 headers"""
        if self._crumb is None:
            try:
                resp = await self._client.get(f"{self.base}/crumbIssuer/api/json")
                if resp.status_code == 200:
                    data = resp.json()
                    self._crumb = {data["crumbRequestField"]: data["crumb"]}
                else:
                    self._crumb = {}
            except httpx.HTTPError:
                self._crumb = {}
        return self._crumb

    async def trigger(self, repo: RepoConfig, branch: str) -> str:
        """触发构建, 返回 queue item URL"""
        path = job_url_path(repo.job)
        if repo.trigger == "multibranch":
            url = f"{self.base}/{path}/job/{quote(branch, safe='')}/build"
            params = None
        else:
            url = f"{self.base}/{path}/buildWithParameters"
            params = {repo.branch_param: branch}
        headers = await self._post_headers()
        resp = await self._client.post(url, params=params, headers=headers)
        resp.raise_for_status()
        queue_url = resp.headers.get("Location")
        if not queue_url:
            raise RuntimeError(f"Jenkins 未返回 queue URL: {repo.job}")
        logger.info("已触发 Jenkins Job {} (分支 {}), queue: {}", repo.job, branch, queue_url)
        return queue_url

    async def wait_for_build_number(self, queue_url: str, timeout: float = 300) -> tuple[str, int]:
        """轮询 queue item 直到分配 build number, 返回 (build_url, number)"""
        deadline = time.monotonic() + timeout
        api = f"{queue_url.rstrip('/')}/api/json"
        while True:
            resp = await self._client.get(api)
            if resp.status_code == 200:
                data = resp.json()
                executable = data.get("executable")
                if executable and executable.get("number") is not None:
                    return executable["url"], executable["number"]
                if data.get("cancelled"):
                    raise RuntimeError("queue item 被取消")
            if time.monotonic() > deadline:
                raise TimeoutError(f"等待 queue 分配超时: {queue_url}")
            await asyncio.sleep(3)

    async def get_last_build_result(self, job_path: str, branch: str | None = None,
                                    trigger: str = "params") -> str | None:
        """查询 Job 最后一次构建结果, 无构建记录时返回 None"""
        path = job_url_path(job_path)
        if trigger == "multibranch" and branch:
            path += f"/job/{quote(branch, safe='')}"
        resp = await self._client.get(f"{self.base}/{path}/lastBuild/api/json")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if data.get("building"):
            return "BUILDING"
        return data.get("result")

    async def wait_build_completed(self, build_url: str, timeout: float = BUILD_TIMEOUT) -> str:
        """轮询直到构建结束, 返回 result"""
        deadline = time.monotonic() + timeout
        api = f"{build_url.rstrip('/')}/api/json"
        while True:
            resp = await self._client.get(api)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("building"):
                return data.get("result") or "UNKNOWN"
            if time.monotonic() > deadline:
                raise TimeoutError(f"等待构建完成超时: {build_url}")
            await asyncio.sleep(POLL_INTERVAL)

    async def run_repo(self, repo: RepoConfig, branch: str) -> BuildResult:
        """触发并等待单个仓库的构建结果"""
        result = BuildResult(repo_path=repo.path, job=repo.job, branch=branch)
        try:
            queue_url = await self.trigger(repo, branch)
            build_url, number = await self.wait_for_build_number(queue_url)
            result.build_number = number
            result.result = await self.wait_build_completed(build_url)
        except Exception as exc:  # noqa: BLE001 - 汇总到结果, 不中断其它 Job
            logger.error("仓库 {} 构建异常: {}", repo.path, exc)
            result.error = str(exc)
        return result
