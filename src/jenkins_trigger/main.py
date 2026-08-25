"""入口: 同进程启动 Webhook(8081) 与 API(8080) 两个服务"""

from __future__ import annotations

import asyncio
import os
import sys

import uvicorn
from loguru import logger

from .api import create_api_app
from .config import load_config
from .executor import Executor
from .webhook import create_webhook_app


async def serve() -> None:
    logger.remove()
    logger.add(sys.stderr, level=os.getenv("LOG_LEVEL", "INFO"))

    config_dir = os.getenv("JENKINS_TRIGGER_CONFIG", "config")
    config = load_config(config_dir)
    logger.info(
        "配置已加载: {} GitLab / {} Jenkins / {} 钉钉机器人 / {} 任务",
        len(config.gitlabs), len(config.jenkins), len(config.dingtalk_bots), len(config.jobs),
    )

    executor = Executor(config)
    await executor.start()

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
        await executor.stop()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
