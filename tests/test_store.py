import json

from jenkins_trigger.store import StateStore


def test_get_set_commit_persists(tmp_path):
    store = StateStore(tmp_path)
    assert store.get_commit("job1", "g/a", "master") is None
    store.set_commit("job1", "g/a", "master", "sha1")
    assert store.get_commit("job1", "g/a", "master") == "sha1"

    # 重新加载后仍在
    store2 = StateStore(tmp_path)
    assert store2.get_commit("job1", "g/a", "master") == "sha1"


def test_file_is_three_level_nested(tmp_path):
    store = StateStore(tmp_path)
    store.set_commit("job1", "g/a", "master", "sha1")
    store.set_commit("job1", "g/a", "release", "sha2")
    data = json.loads((tmp_path / "jobs" / "job1.json").read_text(encoding="utf-8"))
    assert data == {"commits": {"g/a": {"master": "sha1", "release": "sha2"}}}


def test_jobs_use_separate_files(tmp_path):
    store = StateStore(tmp_path)
    store.set_commit("job1", "g/a", "master", "sha1")
    store.set_commit("job2", "g/b", "master", "sha3")
    assert (tmp_path / "jobs" / "job1.json").exists()
    assert (tmp_path / "jobs" / "job2.json").exists()
    assert store.get_commit("job2", "g/a", "master") is None


def test_corrupted_file_tolerated(tmp_path):
    (tmp_path / "jobs").mkdir(parents=True)
    (tmp_path / "jobs" / "bad.json").write_text("{not json", encoding="utf-8")
    store = StateStore(tmp_path)
    assert store.get_commit("bad", "g/a", "master") is None
    # 损坏文件不影响写入
    store.set_commit("bad", "g/a", "master", "sha1")
    assert StateStore(tmp_path).get_commit("bad", "g/a", "master") == "sha1"


def test_atomic_write_leaves_no_tmp(tmp_path):
    store = StateStore(tmp_path)
    store.set_commit("job1", "g/a", "master", "sha1")
    assert list((tmp_path / "jobs").glob("*.tmp")) == []


def test_missing_dir_loads_empty(tmp_path):
    store = StateStore(tmp_path / "nonexistent")
    assert store.get_commit("job1", "g/a", "master") is None
