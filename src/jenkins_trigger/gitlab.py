"""GitLab API 异步客户端 (分支 commit 查询)"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from .config import GitLabInstance


class GitLabClient:
    def __init__(self, instance: GitLabInstance, timeout: float = 15.0):
        self.instance = instance
        self.base = instance.url.rstrip("/")
        headers = {"PRIVATE-TOKEN": instance.api_token} if instance.api_token else {}
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers)

    async def close(self) -> None:
        await self._client.aclose()

    async def get_branch_commit(self, repo_path: str, branch: str) -> str | None:
        """查询分支最新 commit sha; 仓库或分支不存在(404)时返回 None"""
        project = quote(repo_path, safe="")
        ref = quote(branch, safe="")
        resp = await self._client.get(
            f"{self.base}/api/v4/projects/{project}/repository/branches/{ref}"
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()["commit"]["id"]
