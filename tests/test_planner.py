from jenkins_trigger.config import ItemConfig, JobConfig
from jenkins_trigger.planner import (
    REASON_CHANGED,
    REASON_PUSHED,
    REASON_RETRY,
    Planner,
)
from jenkins_trigger.store import StateStore


def make_job() -> JobConfig:
    return JobConfig(
        id="job1", name="任务一", gitlab="gl", jenkins="jk",
        delay=10, default_branch="master",
        items=[
            ItemConfig(name="A", repo="g/a", job="jk/a", stage=2),
            ItemConfig(name="B", repo="g/b", job="jk/b", stage=1),
            ItemConfig(name="C", repo="g/c", job="jk/c", branch="release", stage=1),
        ],
    )


class FakeJenkins:
    def __init__(self, last_results: dict[str, str | None] | None = None, fail: bool = False):
        self.last_results = last_results or {}
        self.fail = fail

    async def get_last_build_result(self, job_path):
        if self.fail:
            raise RuntimeError("boom")
        return self.last_results.get(job_path)


class FakeGitLab:
    def __init__(self, commits: dict | None = None, fail_repos: tuple = ()):
        self.commits = commits or {}  # (repo, branch) -> sha
        self.fail_repos = set(fail_repos)

    async def get_branch_commit(self, repo, branch):
        if repo in self.fail_repos:
            raise RuntimeError("boom")
        return self.commits.get((repo, branch))


def make_planner(tmp_path, gitlab_commits=None) -> tuple[Planner, StateStore, FakeGitLab]:
    store = StateStore(tmp_path)
    gitlab = FakeGitLab(gitlab_commits)
    return Planner(store), store, gitlab


async def test_pushed_repo_in_plan(tmp_path):
    planner, _, gitlab = make_planner(tmp_path)
    job = make_job()
    plan = await planner.make_plan(job, {("g/a", "master")}, FakeJenkins(), gitlab)
    assert [(p.item.repo, p.reason) for p in plan] == [("g/a", REASON_PUSHED)]


async def test_failed_build_retries(tmp_path):
    planner, _, gitlab = make_planner(tmp_path)
    job = make_job()
    plan = await planner.make_plan(
        job, {("g/a", "master")}, FakeJenkins({"jk/c": "FAILURE"}), gitlab
    )
    assert [(p.item.repo, p.reason) for p in plan] == [
        ("g/c", REASON_RETRY), ("g/a", REASON_PUSHED),
    ]


async def test_changed_commit_in_plan(tmp_path):
    planner, store, gitlab = make_planner(
        tmp_path, {("g/b", "master"): "new-sha", ("g/c", "release"): "same-sha"}
    )
    store.set_commit("job1", "g/b", "master", "old-sha")
    store.set_commit("job1", "g/c", "release", "same-sha")
    job = make_job()
    plan = await planner.make_plan(job, set(), FakeJenkins(), gitlab)
    assert [(p.item.repo, p.reason) for p in plan] == [("g/b", REASON_CHANGED)]
    assert plan[0].commit == "new-sha"


async def test_no_record_writes_baseline_without_build(tmp_path):
    planner, store, gitlab = make_planner(tmp_path, {("g/a", "master"): "sha-a"})
    job = make_job()
    plan = await planner.make_plan(job, set(), FakeJenkins(), gitlab)
    assert plan == []  # 仅记录基线, 不构建
    assert store.get_commit("job1", "g/a", "master") == "sha-a"


async def test_reason_precedence(tmp_path):
    """Pushed 覆盖 Changed/Retry; Changed 覆盖 Retry"""
    planner, store, gitlab = make_planner(
        tmp_path, {("g/a", "master"): "sha-a", ("g/c", "release"): "sha-c"}
    )
    store.set_commit("job1", "g/a", "master", "old-a")
    store.set_commit("job1", "g/c", "release", "old-c")
    job = make_job()
    # a: push + changed + failed → Pushed; c: changed + failed → Changed
    plan = await planner.make_plan(
        job, {("g/a", "master")},
        FakeJenkins({"jk/a": "FAILURE", "jk/c": "ABORTED"}), gitlab,
    )
    reasons = {p.item.repo: p.reason for p in plan}
    assert reasons == {"g/a": REASON_PUSHED, "g/c": REASON_CHANGED}


async def test_changed_skipped_without_gitlab_client(tmp_path):
    """未配 api_token (gitlab=None): 整步跳过, 且已有记录不受影响"""
    planner, store, _ = make_planner(tmp_path)
    store.set_commit("job1", "g/b", "master", "old-sha")
    job = make_job()
    plan = await planner.make_plan(job, set(), FakeJenkins(), None)
    assert plan == []


async def test_query_failures_tolerated(tmp_path):
    planner, _, gitlab = make_planner(tmp_path, gitlab_commits={("g/b", "master"): "sha-b"})
    gitlab.fail_repos.add("g/a")
    job = make_job()
    # GitLab 查 a 失败 + Jenkins 全部查询失败: 不阻塞, b 写基线
    plan = await planner.make_plan(job, set(), FakeJenkins(fail=True), gitlab)
    assert plan == []


async def test_same_repo_different_jobs_both_in_plan(tmp_path):
    """同一仓库绑定多个 Jenkins Job: 都进入计划; 完全相同项去重"""
    planner, _, gitlab = make_planner(tmp_path)
    job = make_job()
    job.items.append(ItemConfig(name="A2", repo="g/a", job="jk/a-deploy", stage=1))
    job.items.append(ItemConfig(name="A3", repo="g/a", job="jk/a", stage=3))  # 重复项, 应去重
    plan = await planner.make_plan(job, {("g/a", "master")}, FakeJenkins(), gitlab)
    assert [(p.item.repo, p.item.job) for p in plan] == [("g/a", "jk/a-deploy"), ("g/a", "jk/a")]
