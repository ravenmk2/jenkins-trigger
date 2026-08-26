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
    assert job.crons == ["0 8 * * *", "0 20 * * *"]
    assert len(job.items) == 3
    # 显示名
    assert [i.name for i in job.items] == ["后端服务 A", "后端服务 B", "前端应用"]
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


def test_item_name_required():
    from pydantic import ValidationError

    from jenkins_trigger.config import ItemConfig

    with pytest.raises(ValidationError, match="name"):
        ItemConfig(repo="g/a", job="a")


def test_invalid_cron_rejected():
    from pydantic import ValidationError

    from jenkins_trigger.config import JobConfig

    with pytest.raises(ValidationError, match="cron"):
        JobConfig(name="j", gitlab="g", jenkins="j", crons=["not a cron"])


def test_non_parameterized_rejects_build_params():
    from pydantic import ValidationError

    from jenkins_trigger.config import ItemConfig

    with pytest.raises(ValidationError, match="parameterized"):
        ItemConfig(name="a", repo="g/a", job="a", build_params={"X": "1"})


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
