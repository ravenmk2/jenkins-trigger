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
from .jenkins import BuildResult, job_url_path
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
        # 标题用固定文案, Job/Plan 放正文列表(列表项保证换行)
        title = "Jenkins Build Started"
        lines = [f"### {title}", "", f"- Job: {job_name}", f"- Plan: {plan_id}"]
        lines += [f"- ⏳ {p.item.name} ({p.reason})" for p in plan]
        await self.send_markdown(title, "\n".join(lines))

    async def send_build_summary(self, job_name: str, plan_id: str,
                                 results: list[BuildResult], jenkins_url: str) -> None:
        title = "Jenkins Build Finished"
        lines = [f"### {title}", "", f"- Job: {job_name}", f"- Plan: {plan_id}"]
        base = jenkins_url.rstrip("/")
        for r in results:
            icon = "✅" if r.ok else "❌"
            label = f"{r.name} #{r.build_number}" if r.build_number else r.name
            if r.ok:
                lines.append(f"- {icon} {label}")
            else:
                # 失败项渲染为 Jenkins 链接: 有构建号链到该次构建, 否则链到 Job 页
                url = f"{base}/{job_url_path(r.job)}/"
                if r.build_number:
                    url += f"{r.build_number}/"
                lines.append(f"- {icon} [{label}]({url})")
        await self.send_markdown(title, "\n".join(lines))
