"""入口: 同进程启动 Webhook(8081) 与 API(8080) 两个服务"""

from __future__ import annotations

import asyncio
import os
import sys

import uvicorn
from loguru import logger

from .api import create_api_app
from .config import load_config
from .cron import CronScheduler
from .executor import Executor
from .store import StateStore
from .webhook import create_webhook_app


async def serve() -> None:
    logger.remove()
    logger.configure(extra={"plan_id": "-"})  # 未绑定计划 ID 的日志占位
    logger.add(
        sys.stderr,
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level>"
        " | <cyan>{extra[plan_id]}</cyan> | <cyan>{name}</cyan>:<cyan>{function}</cyan>"
        ":<cyan>{line}</cyan> - <level>{message}</level>",
    )

    config_dir = os.getenv("JENKINS_TRIGGER_CONFIG", "config")
    config = load_config(config_dir)
    logger.info(
        "Config loaded: {} GitLab / {} Jenkins / {} DingTalk bots / {} jobs",
        len(config.gitlabs), len(config.jenkins), len(config.dingtalk_bots), len(config.jobs),
    )

    data_dir = os.getenv("JENKINS_TRIGGER_DATA", "data")
    store = StateStore(data_dir)
    executor = Executor(config, store)  # start() 内先补齐 commit 基线再受理触发
    await executor.start()
    cron = CronScheduler(executor, config)
    await cron.start()

    api_host = os.getenv("API_HOST", "0.0.0.0")
    webhook_port = int(os.getenv("WEBHOOK_PORT", "8081"))
    api_port = int(os.getenv("API_PORT", "8080"))
    servers = [
        uvicorn.Server(uvicorn.Config(
            create_webhook_app(config, executor), host=api_host, port=webhook_port, log_level="warning",
        )),
        uvicorn.Server(uvicorn.Config(
            create_api_app(executor), host=api_host, port=api_port, log_level="warning",
        )),
    ]
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        await cron.stop()
        await executor.stop()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
