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
    STATUS_PLANNING,
    STATUS_RUNNING,
    Executor,
    PushEvent,
)
from jenkins_trigger.jenkins import BuildResult
from jenkins_trigger.planner import REASON_PUSHED, PlanItem
from jenkins_trigger.store import StateStore


def make_config() -> AppConfig:
    gitlab = GitLabInstance(
        id="gl", url="https://gl.example.com", webhook_token="t", api_token="pat"
    )
    jenkins = JenkinsInstance(id="jk", url="https://jk.example.com", username="u", token="t")
    bot = DingTalkBot(id="dt", token="t")
    job = JobConfig(
        id="job1", name="任务一", gitlab="gl", jenkins="jk", dingtalk="dt",
        delay=10, default_branch="master",
        items=[
            ItemConfig(name="A", repo="g/a", job="jk/a", stage=2),
            ItemConfig(name="B", repo="g/b", job="jk/b", stage=1),
            ItemConfig(name="C", repo="g/c", job="jk/c", branch="release", stage=1),
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
                           name=item.name, build_number=1, result="SUCCESS")


class FakeGitLab:
    def __init__(self, commits: dict | None = None):
        self.commits = commits or {}  # (repo, branch) -> sha

    async def get_branch_commit(self, repo, branch):
        return self.commits.get((repo, branch))


class FakeDingTalk:
    def __init__(self):
        self.started: list[tuple] = []
        self.summaries: list[tuple] = []

    async def send_build_started(self, job_name, plan_id, plan):
        self.started.append((job_name, plan_id, [(p.item.name, p.reason) for p in plan]))

    async def send_build_summary(self, job_name, plan_id, results):
        self.summaries.append((job_name, plan_id, results))


def make_executor(monkeypatch, tmp_path, last_results=None, gitlab_commits=None):
    executor = Executor(make_config(), StateStore(tmp_path))
    fake_jenkins = FakeJenkins(last_results)
    fake_gitlab = FakeGitLab(gitlab_commits)
    fake_dingtalk = FakeDingTalk()
    monkeypatch.setattr(executor, "jenkins_client", lambda _: fake_jenkins)
    monkeypatch.setattr(executor, "gitlab_client", lambda _: fake_gitlab)
    monkeypatch.setattr(executor, "dingtalk_client", lambda _: fake_dingtalk)
    return executor, fake_jenkins, fake_gitlab, fake_dingtalk


def plan_of(items, job) -> list[PlanItem]:
    return [PlanItem(item=i, branch=job.branch_of(i), reason=REASON_PUSHED) for i in items]


def test_match_jobs(monkeypatch, tmp_path):
    executor, *_ = make_executor(monkeypatch, tmp_path)
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


def test_mark_pending_debounce(monkeypatch, tmp_path):
    executor, *_ = make_executor(monkeypatch, tmp_path)
    job = executor.config.jobs["job1"]
    executor.mark_pending(job, "g/a", "master")
    first = executor.states["job1"].execute_at
    first_plan_id = executor.states["job1"].plan_id
    time.sleep(0.01)
    executor.mark_pending(job, "g/b", "master")
    state = executor.states["job1"]
    assert state.status == STATUS_PENDING
    assert state.execute_at > first  # 执行时间被刷新
    assert state.plan_id == first_plan_id  # 同一轮 plan_id 不变
    assert state.triggers == {("g/a", "master"), ("g/b", "master")}  # 多仓库累积


def test_mark_scheduled_immediate(monkeypatch, tmp_path):
    """cron 触发: 立即到期, 无 triggers"""
    executor, *_ = make_executor(monkeypatch, tmp_path)
    job = executor.config.jobs["job1"]
    before = time.monotonic()
    executor.mark_scheduled(job)
    state = executor.states["job1"]
    assert state.status == STATUS_PENDING
    assert state.plan_id
    assert before - 0.1 <= state.execute_at <= time.monotonic() + 0.1
    assert state.triggers == set()


async def test_plan_id_propagates_to_run_logs(monkeypatch, tmp_path):
    """plan_id 格式 YYMMDD-HHMMSS(两位年, 补 0), 本轮执行的日志都带它"""
    import re

    from loguru import logger

    executor, *_ = make_executor(monkeypatch, tmp_path)
    job = executor.config.jobs["job1"]
    seen = []
    handler_id = logger.add(
        lambda m: seen.append(m.record["extra"].get("plan_id", "-")), level="INFO"
    )
    try:
        executor.mark_pending(job, "g/a", "master")
        state = executor.states["job1"]
        assert state.plan_id is not None
        assert re.fullmatch(r"\d{6}-\d{6}", state.plan_id)
        await executor._run(state)
    finally:
        logger.remove(handler_id)
    # mark_pending 与 _run 内的日志都带同一个 plan_id, 且无占位符
    assert seen and set(seen) == {state.plan_id}


async def test_empty_plan_back_to_idle_without_notify(monkeypatch, tmp_path):
    """cron 触发且无变更/失败: 计划为空, 不发通知, 回到空闲"""
    executor, fake_jenkins, _, fake_dingtalk = make_executor(monkeypatch, tmp_path)
    job = executor.config.jobs["job1"]
    executor.mark_scheduled(job)
    state = executor.states["job1"]
    await executor._run(state)
    assert state.status == STATUS_IDLE
    assert fake_jenkins.ran == []
    assert fake_dingtalk.started == [] and fake_dingtalk.summaries == []


async def test_execute_plan_stage_order(monkeypatch, tmp_path):
    executor, fake, *_ = make_executor(monkeypatch, tmp_path)
    job = executor.config.jobs["job1"]
    plan = plan_of([job.items[1], job.items[0]], job)  # b(stage 1) 先于 a(stage 2)
    await executor._execute_plan(job, plan)
    assert fake.ran == [("g/b", "master"), ("g/a", "master")]


async def test_same_stage_runs_parallel(monkeypatch, tmp_path):
    executor, fake, *_ = make_executor(monkeypatch, tmp_path)
    job = executor.config.jobs["job1"]
    # b 和 c 都是 stage=1, 各自分支不同
    results = await executor._execute_plan(job, plan_of([job.items[1], job.items[2]], job))
    assert sorted(fake.ran) == [("g/b", "master"), ("g/c", "release")]
    assert all(r.ok for r in results)


async def test_run_end_to_end_notifies(monkeypatch, tmp_path):
    executor, fake_jenkins, _, fake_dingtalk = make_executor(monkeypatch, tmp_path)
    job = executor.config.jobs["job1"]
    executor.mark_pending(job, "g/a", "master")
    state = executor.states["job1"]
    state.execute_at = time.monotonic()  # 立即到期
    await executor._run(state)
    assert state.status == STATUS_IDLE
    assert fake_jenkins.ran == [("g/a", "master")]  # 只跑 push 仓库的执行项
    # 开始 + 结束各一条通知, 都带 plan_id; 开始通知列出 name 与 reason
    assert fake_dingtalk.started == [("任务一", state.plan_id, [("A", "Pushed")])]
    assert len(fake_dingtalk.summaries) == 1
    assert fake_dingtalk.summaries[0][1] == state.plan_id
    assert state.last_results[0]["name"] == "A"


async def test_push_author_propagates_to_notification(monkeypatch, tmp_path):
    """带作者的 push: 记录到 trigger_authors, 开始通知的 reason 标注作者, 本轮结束后清空"""
    executor, _, _, fake_dingtalk = make_executor(monkeypatch, tmp_path)
    job = executor.config.jobs["job1"]
    executor.mark_pending(job, "g/a", "master", "张三")
    state = executor.states["job1"]
    assert state.trigger_authors == {("g/a", "master"): "张三"}
    await executor._run(state)
    assert state.trigger_authors == {}
    assert fake_dingtalk.started == [("任务一", state.plan_id, [("A", "Pushed by 张三")])]


async def test_commit_updated_after_successful_build(monkeypatch, tmp_path):
    executor, *_ = make_executor(
        monkeypatch, tmp_path, gitlab_commits={("g/a", "master"): "sha-new"}
    )
    executor.store.set_commit("job1", "g/a", "master", "sha-old")
    job = executor.config.jobs["job1"]
    executor.mark_pending(job, "g/a", "master")
    await executor._run(executor.states["job1"])
    assert executor.store.get_commit("job1", "g/a", "master") == "sha-new"


async def test_commit_kept_when_build_errors(monkeypatch, tmp_path):
    """构建异常: 不回写 commit 记录, 下轮 Changed 自动重试"""
    executor, fake_jenkins, *_ = make_executor(
        monkeypatch, tmp_path, gitlab_commits={("g/a", "master"): "sha-new"}
    )
    executor.store.set_commit("job1", "g/a", "master", "sha-old")

    async def run_item(item, branch):
        return BuildResult(repo_path=item.repo, job=item.job, branch=branch,
                           name=item.name, error="boom")

    fake_jenkins.run_item = run_item
    job = executor.config.jobs["job1"]
    executor.mark_pending(job, "g/a", "master")
    await executor._run(executor.states["job1"])
    assert executor.store.get_commit("job1", "g/a", "master") == "sha-old"


async def test_planning_state_visible(monkeypatch, tmp_path):
    """计划生成期间状态为计划中"""
    executor, _, fake_gitlab, _ = make_executor(monkeypatch, tmp_path)
    gate = asyncio.Event()

    async def get_branch_commit(repo, branch):
        await gate.wait()
        return None

    fake_gitlab.get_branch_commit = get_branch_commit
    job = executor.config.jobs["job1"]
    state = executor.states["job1"]
    executor.mark_pending(job, "g/a", "master")
    run_task = asyncio.create_task(executor._run(state))
    try:
        for _ in range(200):
            if state.status == STATUS_PLANNING:
                break
            await asyncio.sleep(0.005)
        assert state.status == STATUS_PLANNING
    finally:
        gate.set()
        await run_task
    assert state.status == STATUS_IDLE


async def test_dispatcher_picks_up_due_jobs(monkeypatch, tmp_path):
    import jenkins_trigger.executor as mod

    monkeypatch.setattr(mod, "DISPATCH_INTERVAL", 0.01)
    executor, fake, *_ = make_executor(monkeypatch, tmp_path)
    job = executor.config.jobs["job1"]
    job.delay = 0
    dispatcher = asyncio.create_task(executor._dispatcher())
    try:
        executor.mark_pending(job, "g/a", "master")
        await asyncio.wait_for(fake.started.setdefault("g/a", asyncio.Event()).wait(), 2)
    finally:
        dispatcher.cancel()


async def test_remark_while_running_triggers_second_run(monkeypatch, tmp_path):
    """执行中收到事件: 重新标记待执行, 当前执行完成后再跑一轮"""
    import jenkins_trigger.executor as mod

    monkeypatch.setattr(mod, "DISPATCH_INTERVAL", 0.01)
    executor, fake, *_ = make_executor(monkeypatch, tmp_path)
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
                           name=item.name, build_number=1, result="SUCCESS")

    fake.run_item = run_item
    dispatcher = asyncio.create_task(executor._dispatcher())
    try:
        executor.mark_pending(job, "g/a", "master")
        while not fake.ran:  # 等第一轮开始
            await asyncio.sleep(0.005)
        assert state.status == STATUS_RUNNING
        executor.mark_pending(job, "g/b", "master")  # 执行中收到事件
        assert state.status == STATUS_PENDING  # 被重新标记
        gate.set()  # 第一轮结束
        await asyncio.wait_for(second_done.wait(), 2)
        assert fake.ran == [("g/a", "master"), ("g/b", "master")]  # 接力执行
    finally:
        dispatcher.cancel()


async def test_backfill_baselines(monkeypatch, tmp_path):
    """启动补齐: 缺失的补录, 已有的保留, 查询无结果的跳过"""
    executor = Executor(make_config(), StateStore(tmp_path))
    fake_gitlab = FakeGitLab({("g/a", "master"): "sha-a", ("g/c", "release"): "sha-c"})
    monkeypatch.setattr(executor, "gitlab_client", lambda _: fake_gitlab)
    executor.store.set_commit("job1", "g/a", "master", "existing")
    await executor._backfill_baselines()
    assert executor.store.get_commit("job1", "g/a", "master") == "existing"  # 已有保留
    assert executor.store.get_commit("job1", "g/c", "release") == "sha-c"  # 缺失补齐
    assert executor.store.get_commit("job1", "g/b", "master") is None  # 无结果跳过


async def test_backfill_skips_without_api_token(tmp_path):
    """实例未配 api_token: 跳过补齐, 不报错"""
    config = make_config()
    config.gitlabs["gl"].api_token = ""
    executor = Executor(config, StateStore(tmp_path))
    await executor._backfill_baselines()
    assert executor.store.get_commit("job1", "g/a", "master") is None
    assert executor.gitlab_client("gl") is None
