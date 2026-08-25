import asyncio
import time

import pytest

from jenkins_trigger.config import (
    AppConfig,
    DingTalkBot,
    GitLabInstance,
    ItemConfig,
    JenkinsInstance,
    JobConfig,
)
from jenkins_trigger.executor import (
    STATUS_IDLE,
    STATUS_PENDING,
    Executor,
    PushEvent,
)
from jenkins_trigger.jenkins import BuildResult


def make_config() -> AppConfig:
    gitlab = GitLabInstance(id="gl", url="https://gl.example.com", webhook_token="t")
    jenkins = JenkinsInstance(id="jk", url="https://jk.example.com", username="u", token="t")
    bot = DingTalkBot(id="dt", token="t")
    job = JobConfig(
        id="job1", name="任务一", gitlab="gl", jenkins="jk", dingtalk="dt",
        delay=10, default_branch="master",
        items=[
            ItemConfig(repo="g/a", job="jk/a", priority=2),
            ItemConfig(repo="g/b", job="jk/b", priority=1),
            ItemConfig(repo="g/c", job="jk/c", branch="release", priority=1),
        ],
    )
    return AppConfig(
        gitlabs={"gl": gitlab}, jenkins={"jk": jenkins},
        dingtalk_bots={"dt": bot}, jobs={"job1": job},
    )


class FakeJenkins:
    def __init__(self, last_results: dict[str, str | None] | None = None):
        self.last_results = last_results or {}
        self.ran: list[tuple[str, str]] = []
        self.started: dict[str, asyncio.Event] = {}

    async def get_last_build_result(self, job_path):
        return self.last_results.get(job_path)

    async def run_item(self, item, branch):
        event = self.started.setdefault(item.repo, asyncio.Event())
        event.set()
        await asyncio.sleep(0)  # 让出事件循环
        self.ran.append((item.repo, branch))
        return BuildResult(repo_path=item.repo, job=item.job, branch=branch,
                           build_number=1, result="SUCCESS")


class FakeDingTalk:
    def __init__(self):
        self.messages: list[tuple[str, list]] = []

    async def send_build_summary(self, job_name, results):
        self.messages.append((job_name, results))


def make_executor(monkeypatch, last_results=None) -> tuple[Executor, FakeJenkins, FakeDingTalk]:
    executor = Executor(make_config())
    fake_jenkins = FakeJenkins(last_results)
    fake_dingtalk = FakeDingTalk()
    monkeypatch.setattr(executor, "jenkins_client", lambda _: fake_jenkins)
    monkeypatch.setattr(executor, "dingtalk_client", lambda _: fake_dingtalk)
    return executor, fake_jenkins, fake_dingtalk


def test_match_jobs(monkeypatch):
    executor, _, _ = make_executor(monkeypatch)
    job = executor.config.jobs["job1"]
    event = lambda gitlab_id, repo_path, branch: PushEvent(  # noqa: E731
        gitlab_id=gitlab_id, repo_path=repo_path, branch=branch
    )
    assert executor.match_jobs(event("gl", "g/a", "master")) == [job]
    # 仓库 c 覆盖了分支
    assert executor.match_jobs(event("gl", "g/c", "release")) == [job]
    assert executor.match_jobs(event("gl", "g/c", "master")) == []
    # 不同 GitLab 实例 / 未配置仓库
    assert executor.match_jobs(event("other", "g/a", "master")) == []
    assert executor.match_jobs(event("gl", "g/unknown", "master")) == []


def test_mark_pending_debounce(monkeypatch):
    executor, _, _ = make_executor(monkeypatch)
    job = executor.config.jobs["job1"]
    executor.mark_pending(job, "master")
    first = executor.states["job1"].execute_at
    time.sleep(0.01)
    executor.mark_pending(job, "master")
    state = executor.states["job1"]
    assert state.status == STATUS_PENDING
    assert state.execute_at > first  # 执行时间被刷新


async def test_make_plan_includes_failed(monkeypatch):
    executor, _, _ = make_executor(monkeypatch, last_results={"jk/c": "FAILURE", "jk/b": "SUCCESS"})
    job = executor.config.jobs["job1"]
    # master 推送: b(p1), a(p2) 匹配; c(p1) 上次失败也加入, 按优先级排序
    plan = await executor._make_plan(job, "master")
    assert [i.repo for i in plan] == ["g/b", "g/c", "g/a"]
    # c 全部成功时不加入
    executor2, _, _ = make_executor(monkeypatch, last_results={"jk/c": "SUCCESS"})
    plan2 = await executor2._make_plan(job, "master")
    assert [i.repo for i in plan2] == ["g/b", "g/a"]


async def test_same_repo_different_jobs_both_in_plan(monkeypatch):
    """同一仓库绑定多个 Jenkins Job: 都进入计划; 完全相同项去重"""
    executor, _, _ = make_executor(monkeypatch)
    job = executor.config.jobs["job1"]
    job.items.append(ItemConfig(repo="g/a", job="jk/a-deploy", priority=1))
    job.items.append(ItemConfig(repo="g/a", job="jk/a", priority=3))  # 重复项, 应去重
    plan = await executor._make_plan(job, "master")
    assert [(i.repo, i.job) for i in plan] == [
        ("g/b", "jk/b"), ("g/a", "jk/a-deploy"), ("g/a", "jk/a"),
    ]


async def test_execute_plan_priority_order(monkeypatch):
    executor, fake, _ = make_executor(monkeypatch)
    job = executor.config.jobs["job1"]
    plan = [job.items[1], job.items[0]]  # b(p1) 先于 a(p2)
    await executor._execute_plan(job, plan)
    assert fake.ran == [("g/b", "master"), ("g/a", "master")]


async def test_same_priority_runs_parallel(monkeypatch):
    executor, fake, _ = make_executor(monkeypatch)
    job = executor.config.jobs["job1"]
    # b 和 c 都是 priority=1, 各自分支不同
    plan = [job.items[1], job.items[2]]
    results = await executor._execute_plan(job, plan)
    assert sorted(fake.ran) == [("g/b", "master"), ("g/c", "release")]
    assert all(r.ok for r in results)


async def test_run_end_to_end_notifies(monkeypatch):
    executor, fake_jenkins, fake_dingtalk = make_executor(monkeypatch)
    job = executor.config.jobs["job1"]
    executor.mark_pending(job, "master")
    state = executor.states["job1"]
    state.execute_at = time.monotonic()  # 立即到期
    await executor._run(state)
    assert state.status == STATUS_IDLE
    assert len(fake_jenkins.ran) == 2  # master 匹配 a, b
    assert len(fake_dingtalk.messages) == 1
    assert len(state.last_results) == 2


async def test_scheduler_picks_up_due_jobs(monkeypatch):
    import jenkins_trigger.executor as mod

    monkeypatch.setattr(mod, "SCHEDULER_INTERVAL", 0.01)
    executor, fake, _ = make_executor(monkeypatch)
    job = executor.config.jobs["job1"]
    job.delay = 0
    scheduler = asyncio.create_task(executor._scheduler())
    try:
        executor.mark_pending(job, "master")
        await asyncio.wait_for(fake.started.setdefault("g/b", asyncio.Event()).wait(), 2)
    finally:
        scheduler.cancel()
