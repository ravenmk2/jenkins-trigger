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
        self.started: list[tuple] = []
        self.summaries: list[tuple] = []

    async def send_build_started(self, job_name, exec_id, triggers):
        self.started.append((job_name, exec_id, triggers))

    async def send_build_summary(self, job_name, exec_id, results):
        self.summaries.append((job_name, exec_id, results))


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
    executor.mark_pending(job, "g/a", "master")
    first = executor.states["job1"].execute_at
    first_exec_id = executor.states["job1"].exec_id
    time.sleep(0.01)
    executor.mark_pending(job, "g/b", "master")
    state = executor.states["job1"]
    assert state.status == STATUS_PENDING
    assert state.execute_at > first  # 执行时间被刷新
    assert state.exec_id == first_exec_id  # 同一轮 exec_id 不变
    assert state.triggers == {("g/a", "master"), ("g/b", "master")}  # 多仓库累积


async def test_exec_id_propagates_to_run_logs(monkeypatch):
    """进入待执行时生成 exec_id(日期+流水号), 本轮执行的日志都带它"""
    import re

    from loguru import logger

    executor, _, _ = make_executor(monkeypatch)
    job = executor.config.jobs["job1"]
    seen = []
    handler_id = logger.add(
        lambda m: seen.append(m.record["extra"].get("exec_id", "-")), level="INFO"
    )
    try:
        executor.mark_pending(job, "g/a", "master")
        state = executor.states["job1"]
        assert state.exec_id is not None
        assert re.fullmatch(r"\d{8}-\d{3}", state.exec_id)
        await executor._run(state)
    finally:
        logger.remove(handler_id)
    # mark_pending 与 _run 内的日志都带同一个 exec_id, 且无占位符
    assert seen and set(seen) == {state.exec_id}


def test_exec_id_daily_sequence(monkeypatch):
    """流水号当日递增, 跨天重新计数"""
    executor, _, _ = make_executor(monkeypatch)
    today = time.strftime("%Y%m%d")
    assert executor._next_exec_id() == f"{today}-001"
    assert executor._next_exec_id() == f"{today}-002"
    # 模拟跨天: 计数日期与今天不同则重新计数
    executor._exec_date = "20000101"
    assert executor._next_exec_id() == f"{today}-001"


async def test_make_plan_includes_failed(monkeypatch):
    executor, _, _ = make_executor(monkeypatch, last_results={"jk/c": "FAILURE", "jk/b": "SUCCESS"})
    job = executor.config.jobs["job1"]
    # push g/a master: 计划起点只有 a(p2); c(p1) 上次失败也加入, 按优先级排序
    plan = await executor._make_plan(job, {("g/a", "master")})
    assert [(i.repo, i.job) for i in plan] == [("g/c", "jk/c"), ("g/a", "jk/a")]
    # c 全部成功时不加入
    executor2, _, _ = make_executor(monkeypatch, last_results={"jk/c": "SUCCESS"})
    plan2 = await executor2._make_plan(job, {("g/a", "master")})
    assert [(i.repo, i.job) for i in plan2] == [("g/a", "jk/a")]


async def test_same_repo_different_jobs_both_in_plan(monkeypatch):
    """同一仓库绑定多个 Jenkins Job: 都进入计划; 完全相同项去重"""
    executor, _, _ = make_executor(monkeypatch)
    job = executor.config.jobs["job1"]
    job.items.append(ItemConfig(repo="g/a", job="jk/a-deploy", priority=1))
    job.items.append(ItemConfig(repo="g/a", job="jk/a", priority=3))  # 重复项, 应去重
    plan = await executor._make_plan(job, {("g/a", "master")})
    assert [(i.repo, i.job) for i in plan] == [("g/a", "jk/a-deploy"), ("g/a", "jk/a")]
    # push 其它仓库时 g/a 的执行项不进入计划
    plan2 = await executor._make_plan(job, {("g/b", "master")})
    assert [(i.repo, i.job) for i in plan2] == [("g/b", "jk/b")]
    # 同一窗口 push 多个仓库: 都进入计划
    plan3 = await executor._make_plan(job, {("g/a", "master"), ("g/b", "master")})
    assert [(i.repo, i.job) for i in plan3] == [
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
    executor.mark_pending(job, "g/a", "master")
    state = executor.states["job1"]
    state.execute_at = time.monotonic()  # 立即到期
    await executor._run(state)
    assert state.status == STATUS_IDLE
    assert fake_jenkins.ran == [("g/a", "master")]  # 只跑 push 仓库的执行项
    # 开始 + 结束各一条通知, 都带 exec_id
    assert fake_dingtalk.started == [("任务一", state.exec_id, [("g/a", "master")])]
    assert len(fake_dingtalk.summaries) == 1
    assert fake_dingtalk.summaries[0][1] == state.exec_id
    assert len(state.last_results) == 1


async def test_scheduler_picks_up_due_jobs(monkeypatch):
    import jenkins_trigger.executor as mod

    monkeypatch.setattr(mod, "SCHEDULER_INTERVAL", 0.01)
    executor, fake, _ = make_executor(monkeypatch)
    job = executor.config.jobs["job1"]
    job.delay = 0
    scheduler = asyncio.create_task(executor._scheduler())
    try:
        executor.mark_pending(job, "g/a", "master")
        await asyncio.wait_for(fake.started.setdefault("g/a", asyncio.Event()).wait(), 2)
    finally:
        scheduler.cancel()


async def test_remark_while_running_triggers_second_run(monkeypatch):
    """执行中收到事件: 重新标记待执行, 当前执行完成后再跑一轮"""
    import jenkins_trigger.executor as mod

    monkeypatch.setattr(mod, "SCHEDULER_INTERVAL", 0.01)
    executor, fake, _ = make_executor(monkeypatch)
    job = executor.config.jobs["job1"]
    job.delay = 0
    state = executor.states["job1"]

    gate = asyncio.Event()  # 卡住第一次执行, 模拟构建进行中
    second_done = asyncio.Event()

    async def run_item(item, branch):
        fake.ran.append((item.repo, branch))
        if len(fake.ran) == 1:
            await gate.wait()
        if len(fake.ran) == 2:
            second_done.set()
        return BuildResult(repo_path=item.repo, job=item.job, branch=branch,
                           build_number=1, result="SUCCESS")

    fake.run_item = run_item
    scheduler = asyncio.create_task(executor._scheduler())
    try:
        executor.mark_pending(job, "g/a", "master")
        while not fake.ran:  # 等第一轮开始
            await asyncio.sleep(0.005)
        assert state.status == "执行中"
        executor.mark_pending(job, "g/b", "master")  # 执行中收到事件
        assert state.status == STATUS_PENDING  # 被重新标记
        gate.set()  # 第一轮结束
        await asyncio.wait_for(second_done.wait(), 2)
        assert fake.ran == [("g/a", "master"), ("g/b", "master")]  # 接力执行
    finally:
        scheduler.cancel()
