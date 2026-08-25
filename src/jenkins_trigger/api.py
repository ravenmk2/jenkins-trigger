"""常规 API 端 (端口 8080, 预留)"""

from __future__ import annotations

from fastapi import FastAPI

from .executor import Executor


def create_api_app(executor: Executor) -> FastAPI:
    app = FastAPI(title="jenkins-trigger api")

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/status")
    async def status() -> dict:
        return {"jobs": [state.snapshot() for state in executor.states.values()]}

    return app
