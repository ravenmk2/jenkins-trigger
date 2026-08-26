"""钉钉机器人通知客户端"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import quote_plus

import httpx
from loguru import logger

from .config import DingTalkBot
from .jenkins import BuildResult
from .planner import PlanItem

WEBHOOK_URL = "https://oapi.dingtalk.com/robot/send"


def sign(secret: str) -> tuple[str, str]:
    """钉钉加签: 返回 (timestamp, sign)"""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    return timestamp, quote_plus(base64.b64encode(digest))


class DingTalkClient:
    def __init__(self, bot: DingTalkBot, timeout: float = 15.0):
        self.bot = bot
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def send_markdown(self, title: str, text: str) -> None:
        params = {"access_token": self.bot.token}
        if self.bot.secret:
            timestamp, signature = sign(self.bot.secret)
            params.update({"timestamp": timestamp, "sign": signature})
        resp = await self._client.post(
            WEBHOOK_URL,
            params=params,
            json={"msgtype": "markdown", "markdown": {"title": title, "text": text}},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"DingTalk send failed: {data}")
        logger.info("DingTalk notification sent: {}", title)

    async def send_build_started(self, job_name: str, plan_id: str,
                                 plan: list[PlanItem]) -> None:
        title = f"Jenkins Build Started: {job_name}"
        lines = [f"### {title}", "", f"Plan ID: `{plan_id}`", ""]
        lines += [f"- {p.item.name} ({p.reason})" for p in plan]
        await self.send_markdown(title, "\n".join(lines))

    async def send_build_summary(self, job_name: str, plan_id: str,
                                 results: list[BuildResult]) -> None:
        title = f"Jenkins Build Finished: {job_name}"
        lines = [f"### {title}", "", f"Plan ID: `{plan_id}`", ""]
        for r in results:
            icon = "✅" if r.ok else "❌"
            number = f"#{r.build_number}" if r.build_number else "-"
            lines.append(f"- {icon} {r.name} {number}")
        await self.send_markdown(title, "\n".join(lines))
