from fastapi.testclient import TestClient

from jenkins_trigger.config import load_config
from jenkins_trigger.executor import Executor
from jenkins_trigger.store import StateStore
from jenkins_trigger.webhook import create_webhook_app

PUSH_PAYLOAD = {
    "object_kind": "push",
    "event_name": "push",
    "ref": "refs/heads/master",
    "project": {"path_with_namespace": "group/project-a"},
}

WEBHOOK_URL = "/webhook/gitlab/main"


def _make_client(tmp_path):
    config = load_config("example")
    executor = Executor(config, StateStore(tmp_path))
    return TestClient(create_webhook_app(config, executor)), executor


def test_unknown_instance_rejected(tmp_path):
    client, _ = _make_client(tmp_path)
    resp = client.post(
        "/webhook/gitlab/nope",
        json=PUSH_PAYLOAD,
        headers={"X-Gitlab-Token": "change-me", "X-Gitlab-Event": "Push Hook"},
    )
    assert resp.status_code == 404


def test_invalid_token_rejected(tmp_path):
    client, _ = _make_client(tmp_path)
    resp = client.post(
        WEBHOOK_URL,
        json=PUSH_PAYLOAD,
        headers={"X-Gitlab-Token": "wrong", "X-Gitlab-Event": "Push Hook"},
    )
    assert resp.status_code == 401


def test_non_push_event_ignored(tmp_path):
    client, _ = _make_client(tmp_path)
    resp = client.post(
        WEBHOOK_URL,
        json={"object_kind": "merge_request", "event_type": "merge_request"},
        headers={"X-Gitlab-Token": "change-me", "X-Gitlab-Event": "Merge Request Hook"},
    )
    assert resp.status_code == 202


def test_system_hook_push_queued(tmp_path):
    """System Hook(MR 合并等)的 push 事件同样入队, 不看 header 类型"""
    client, executor = _make_client(tmp_path)
    resp = client.post(
        WEBHOOK_URL,
        json=PUSH_PAYLOAD,
        headers={"X-Gitlab-Token": "change-me", "X-Gitlab-Event": "System Hook"},
    )
    assert resp.status_code == 200
    event = executor.queue.get_nowait()
    assert event.repo_path == "group/project-a"
    assert event.branch == "master"


def test_system_hook_non_push_ignored(tmp_path):
    client, _ = _make_client(tmp_path)
    resp = client.post(
        WEBHOOK_URL,
        json={"object_kind": "merge_request", "event_type": "merge_request"},
        headers={"X-Gitlab-Token": "change-me", "X-Gitlab-Event": "System Hook"},
    )
    assert resp.status_code == 202


def test_push_event_queued(tmp_path):
    client, executor = _make_client(tmp_path)
    resp = client.post(
        WEBHOOK_URL,
        json=PUSH_PAYLOAD,
        headers={"X-Gitlab-Token": "change-me", "X-Gitlab-Event": "Push Hook"},
    )
    assert resp.status_code == 200
    event = executor.queue.get_nowait()
    assert event.gitlab_id == "main"
    assert event.repo_path == "group/project-a"
    assert event.branch == "master"
    assert event.author == ""  # payload 无 user_name 时为空


def test_push_event_carries_author(tmp_path):
    """payload 带 user_name 时, 事件携带推送人"""
    client, executor = _make_client(tmp_path)
    resp = client.post(
        WEBHOOK_URL,
        json={**PUSH_PAYLOAD, "user_name": "张三"},
        headers={"X-Gitlab-Token": "change-me", "X-Gitlab-Event": "Push Hook"},
    )
    assert resp.status_code == 200
    assert executor.queue.get_nowait().author == "张三"
