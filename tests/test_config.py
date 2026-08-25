import pytest

from jenkins_trigger.config import load_config


def test_load_example_config():
    config = load_config("example")
    assert "main" in config.gitlabs
    assert "main" in config.jenkins
    assert "ops" in config.dingtalk_bots

    job = config.jobs["example"]
    assert job.name == "示例发布任务"
    assert job.delay == 60
    assert len(job.repos) == 3
    # 分支覆盖
    assert job.branch_of(job.repos[0]) == "master"
    assert job.branch_of(job.repos[2]) == "release"
    # 触发方式
    assert job.repos[0].trigger == "params"
    assert job.repos[2].trigger == "multibranch"


def test_invalid_reference(tmp_path):
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "bad.toml").write_text(
        'name = "bad"\ngitlab = "nope"\njenkins = "nope"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="GitLab"):
        load_config(tmp_path)


def test_empty_config_dir(tmp_path):
    config = load_config(tmp_path)
    assert not config.jobs
