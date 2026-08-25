import base64
import hashlib
import hmac
import time
from urllib.parse import quote_plus

import httpx
import pytest
import respx

from jenkins_trigger.config import DingTalkBot, JenkinsInstance, RepoConfig
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


async def test_trigger_params(jenkins_client):
    with respx.mock(base_url="https://jk.example.com") as mock:
        mock.get("/crumbIssuer/api/json").respond(404)
        route = mock.post("/job/backend/job/app/buildWithParameters").respond(
            201, headers={"Location": "https://jk.example.com/queue/item/42/"}
        )
        queue_url = await jenkins_client.trigger(
            RepoConfig(path="g/a", job="backend/app"), "master"
        )
    assert queue_url == "https://jk.example.com/queue/item/42/"
    assert route.calls[0].request.url.params["BRANCH"] == "master"
    await jenkins_client.close()


async def test_trigger_multibranch(jenkins_client):
    with respx.mock(base_url="https://jk.example.com") as mock:
        mock.get("/crumbIssuer/api/json").respond(404)
        route = mock.post("/job/app/job/feature%2Fx/build").respond(
            201, headers={"Location": "https://jk.example.com/queue/item/1/"}
        )
        await jenkins_client.trigger(
            RepoConfig(path="g/a", job="app", trigger="multibranch"), "feature/x"
        )
    assert route.called
    await jenkins_client.close()


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
        await jenkins_client.trigger(RepoConfig(path="g/a", job="app"), "master")
    assert route.calls[0].request.headers["Jenkins-Crumb"] == "abc"
    await jenkins_client.close()
