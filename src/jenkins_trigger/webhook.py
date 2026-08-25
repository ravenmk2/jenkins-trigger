"""GitLab Webhook 接收端 (端口 8081)"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from .config import AppConfig
from .executor import Executor, PushEvent


def create_webhook_app(config: AppConfig, executor: Executor) -> FastAPI:
    app = FastAPI(title="jenkins-trigger webhook", docs_url=None, redoc_url=None)

    # 路径中的 instance_id 定位 GitLab 实例, 再校验 X-Gitlab-Token
    instances = config.gitlabs

    @app.post("/webhook/gitlab/{instance_id}")
    async def gitlab_webhook(instance_id: str, request: Request) -> JSONResponse:
        instance = instances.get(instance_id)
        if not instance:
            return JSONResponse({"detail": f"unknown instance: {instance_id}"}, status_code=404)
        if request.headers.get("X-Gitlab-Token", "") != instance.webhook_token:
            logger.warning("Webhook Token 校验失败: {} ({})", instance_id, request.client)
            return JSONResponse({"detail": "invalid token"}, status_code=401)

        event_type = request.headers.get("X-Gitlab-Event", "")
        if event_type != "Push Hook":
            return JSONResponse({"detail": f"ignored event: {event_type}"}, status_code=202)

        payload = await request.json()
        ref = payload.get("ref", "")
        branch = ref.removeprefix("refs/heads/")
        repo_path = (payload.get("project") or {}).get("path_with_namespace", "")
        if not branch or not repo_path:
            return JSONResponse({"detail": "missing ref or project path"}, status_code=400)

        await executor.enqueue(PushEvent(gitlab_id=instance_id, repo_path=repo_path, branch=branch))
        logger.info("收到 Push 事件: {} {} {}", instance_id, repo_path, branch)
        return JSONResponse({"status": "queued"})

    return app
