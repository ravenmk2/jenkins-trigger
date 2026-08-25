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
    assert len(job.items) == 3
    # 分支覆盖
    assert job.branch_of(job.items[0]) == "master"
    assert job.branch_of(job.items[2]) == "release"
    # 触发方式
    assert job.items[0].parameterized is True
    assert job.items[1].parameterized is False  # 默认值
    assert job.items[2].parameterized is False
    # 构建参数(分支显式写)
    assert job.items[0].build_params == {"BRANCH": "master", "ENV": "prod"}
    assert job.items[1].build_params == {}


def test_non_parameterized_rejects_build_params():
    from pydantic import ValidationError

    from jenkins_trigger.config import ItemConfig

    with pytest.raises(ValidationError, match="parameterized"):
        ItemConfig(repo="g/a", job="a", build_params={"X": "1"})


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
