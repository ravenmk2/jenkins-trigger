"""配置加载: config/gitlab.toml, config/jenkins.toml, config/dingtalk.toml, config/jobs/<job-id>.toml"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class GitLabInstance(BaseModel):
    id: str
    name: str = ""
    url: str
    webhook_token: str


class JenkinsInstance(BaseModel):
    id: str
    name: str = ""
    url: str  # 例: https://jenkins.example.com
    username: str
    token: str  # Jenkins API Token


class DingTalkBot(BaseModel):
    id: str
    name: str = ""
    token: str  # access_token
    secret: str = ""  # 加签密钥, 可为空


class ItemConfig(BaseModel):
    """任务的执行项: Git 仓库与 Jenkins Job 的绑定"""

    repo: str  # 带 group 的仓库路径, 例: group/project-a
    job: str  # Jenkins Job 路径, 例: folder/job-a
    branch: str | None = None  # 触发分支, 覆盖任务默认分支
    priority: int = 100  # 值越小优先级越高, 同优先级并行执行
    parameterized: bool = False  # 参数化构建(buildWithParameters); False 则纯触发(/build)
    build_params: dict[str, str] = Field(default_factory=dict)  # 构建参数, 分支需要时显式写

    @model_validator(mode="after")
    def _check_build_params(self) -> ItemConfig:
        # 非参数化构建按定义不带参数, 配了 build_params 属于配置错误, 启动即报
        if not self.parameterized and self.build_params:
            raise ValueError(f"仓库 {self.repo}: parameterized=false 不支持 build_params")
        return self


class JobConfig(BaseModel):
    id: str = ""  # 文件名 (不含 .toml)
    name: str
    gitlab: str  # GitLabInstance.id
    jenkins: str  # JenkinsInstance.id
    dingtalk: str | None = None  # DingTalkBot.id, 为空则不通知
    delay: int = 0  # 执行延迟(秒)
    default_branch: str = "master"  # 默认触发分支
    items: list[ItemConfig] = Field(default_factory=list)

    def branch_of(self, item: ItemConfig) -> str:
        return item.branch or self.default_branch


class AppConfig(BaseModel):
    gitlabs: dict[str, GitLabInstance] = Field(default_factory=dict)
    jenkins: dict[str, JenkinsInstance] = Field(default_factory=dict)
    dingtalk_bots: dict[str, DingTalkBot] = Field(default_factory=dict)
    jobs: dict[str, JobConfig] = Field(default_factory=dict)


def _load_instances(path: Path, model: type[BaseModel], key: str) -> dict[str, BaseModel]:
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    instances = {item["id"]: model.model_validate(item) for item in data.get(key, [])}
    return instances


def load_config(config_dir: str | Path = "config") -> AppConfig:
    config_dir = Path(config_dir)
    gitlabs = _load_instances(config_dir / "gitlab.toml", GitLabInstance, "instances")
    jenkins = _load_instances(config_dir / "jenkins.toml", JenkinsInstance, "instances")
    bots = _load_instances(config_dir / "dingtalk.toml", DingTalkBot, "bots")

    jobs: dict[str, JobConfig] = {}
    jobs_dir = config_dir / "jobs"
    if jobs_dir.is_dir():
        for file in sorted(jobs_dir.glob("*.toml")):
            job = JobConfig.model_validate(tomllib.loads(file.read_text(encoding="utf-8")))
            job.id = file.stem
            if job.gitlab not in gitlabs:
                raise ValueError(f"任务 {job.id}: 未找到 GitLab 实例 '{job.gitlab}'")
            if job.jenkins not in jenkins:
                raise ValueError(f"任务 {job.id}: 未找到 Jenkins 实例 '{job.jenkins}'")
            if job.dingtalk and job.dingtalk not in bots:
                raise ValueError(f"任务 {job.id}: 未找到钉钉机器人 '{job.dingtalk}'")
            jobs[job.id] = job

    return AppConfig(gitlabs=gitlabs, jenkins=jenkins, dingtalk_bots=bots, jobs=jobs)
