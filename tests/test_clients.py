import base64
import hashlib
import hmac
import time
from urllib.parse import quote_plus

import httpx
import pytest
import respx

from jenkins_trigger.config import DingTalkBot, JenkinsInstance, ItemConfig
from jenkins_trigger.dingtalk import sign
from jenkins_trigger.jenkins import JenkinsClient, job_url_path


def test_job_url_path():
    assert job_url_path("folder/job-a") == "job/folder/job/job-a"
    assert job_url_path("job-a") == "job/job-a"
    assert job_url_path("a b/c") == "job/a%20b/job/c"


def test_dingtalk_sign():
    timestamp, signature = sign("SEC123")
    expected = hmac.new(
        b"SEC123", f"{timestamp}\nSEC123".encode(), hashlib.sha256
    ).digest()
    assert signature == quote_plus(base64.b64encode(expected))
    assert abs(int(timestamp) - round(time.time() * 1000)) < 5000


@pytest.fixture
def jenkins_client():
    instance = JenkinsInstance(
        id="jk", url="https://jk.example.com", username="u", token="t"
    )
    return JenkinsClient(instance)


async def test_trigger_parameterized(jenkins_client):
    """参数化构建: build_params 原样透传, 分支按需显式写"""
    with respx.mock(base_url="https://jk.example.com") as mock:
        mock.get("/crumbIssuer/api/json").respond(404)
        route = mock.post("/job/backend/job/app/buildWithParameters").respond(
            201, headers={"Location": "https://jk.example.com/queue/item/42/"}
        )
        queue_url = await jenkins_client.trigger(
            ItemConfig(repo="g/a", job="backend/app", parameterized=True,
                       build_params={"BRANCH": "master", "ENV": "prod"}),
            "master",
        )
    assert queue_url == "https://jk.example.com/queue/item/42/"
    query = route.calls[0].request.url.params
    assert query["BRANCH"] == "master"
    assert query["ENV"] == "prod"
    await jenkins_client.close()


async def test_trigger_plain(jenkins_client):
    """非参数化(默认): 纯触发, 不带参数"""
    with respx.mock(base_url="https://jk.example.com") as mock:
        mock.get("/crumbIssuer/api/json").respond(404)
        route = mock.post("/job/app/build").respond(
            201, headers={"Location": "https://jk.example.com/queue/item/2/"}
        )
        await jenkins_client.trigger(ItemConfig(repo="g/a", job="app"), "master")
    assert route.called
    assert not route.calls[0].request.url.params
    await jenkins_client.close()


async def test_trigger_parameterized_without_params(jenkins_client):
    """parameterized=true 且无 build_params: 走 buildWithParameters 但不带参数"""
    with respx.mock(base_url="https://jk.example.com") as mock:
        mock.get("/crumbIssuer/api/json").respond(404)
        route = mock.post("/job/app/buildWithParameters").respond(
            201, headers={"Location": "https://jk.example.com/queue/item/3/"}
        )
        await jenkins_client.trigger(
            ItemConfig(repo="g/a", job="app", parameterized=True), "master"
        )
    assert route.called
    assert not route.calls[0].request.url.params
    await jenkins_client.close()


async def test_build_notifications():
    from jenkins_trigger.dingtalk import DingTalkClient
    from jenkins_trigger.jenkins import BuildResult

    client = DingTalkClient(DingTalkBot(id="dt", token="t"))
    sent = []

    async def fake_send(title, text):
        sent.append((title, text))

    client.send_markdown = fake_send

    await client.send_build_started("发布任务", "20260825-001",
                                    [("g/a", "master"), ("g/b", "release")])
    title, text = sent[0]
    assert title == "Jenkins 构建开始: 发布任务"
    assert "20260825-001" in text
    assert "- `g/a` (master)" in text
    assert "- `g/b` (release)" in text

    results = [
        BuildResult(repo_path="g/a", job="backend/app", branch="master",
                    build_number=42, result="SUCCESS"),
        BuildResult(repo_path="g/b", job="backend/b", branch="master", error="timeout"),
    ]
    await client.send_build_summary("发布任务", "20260825-001", results)
    title, text = sent[1]
    assert title == "Jenkins 构建结束: 发布任务"
    assert "20260825-001" in text
    assert "- ✅ backend/app #42 SUCCESS" in text
    assert "- ❌ backend/b - timeout" in text
    assert "g/a" not in text  # 列表项不含仓库/分支信息
    await client.close()


async def test_get_last_build_result(jenkins_client):
    with respx.mock(base_url="https://jk.example.com") as mock:
        mock.get("/job/app/lastBuild/api/json").respond(
            200, json={"building": False, "result": "FAILURE"}
        )
        result = await jenkins_client.get_last_build_result("app")
    assert result == "FAILURE"
    await jenkins_client.close()


async def test_wait_for_build_number(jenkins_client):
    with respx.mock() as mock:
        mock.get("https://jk.example.com/queue/item/42/api/json").respond(
            200, json={"executable": {"number": 7, "url": "https://jk.example.com/job/app/7/"}}
        )
        url, number = await jenkins_client.wait_for_build_number(
            "https://jk.example.com/queue/item/42/"
        )
    assert number == 7
    await jenkins_client.close()


async def test_trigger_uses_crumb(jenkins_client):
    with respx.mock(base_url="https://jk.example.com") as mock:
        mock.get("/crumbIssuer/api/json").respond(
            200, json={"crumbRequestField": "Jenkins-Crumb", "crumb": "abc"}
        )
        route = mock.post("/job/app/buildWithParameters").respond(
            201, headers={"Location": "https://jk.example.com/queue/item/1/"}
        )
        await jenkins_client.trigger(ItemConfig(repo="g/a", job="app", parameterized=True), "master")
    assert route.calls[0].request.headers["Jenkins-Crumb"] == "abc"
    await jenkins_client.close()
