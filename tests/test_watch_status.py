import json

from anywhere_computer import watch_status


def test_missing_or_invalid_history_does_not_create_or_disclose_state(tmp_path):
    path = tmp_path / "absent" / "history.json"
    assert watch_status.read_watch_observation(path)["state"] == "unavailable"
    assert not path.parent.exists()
    path.parent.mkdir()
    for content in (b"secret broken JSON", b"x" * 9000):
        path.write_bytes(content)
        result = watch_status.read_watch_observation(path)
        assert result["state"] == "unreadable"
        assert "secret" not in json.dumps(result)
        assert path.read_bytes() == content


def test_failed_atomic_update_preserves_previous_observation(tmp_path, monkeypatch):
    path = tmp_path / "watch.json"
    watch_status.save_watch_observation(path, "restart_wait", 1, 5, 7)
    previous = path.read_bytes()

    def interrupted(*args):
        raise OSError("synthetic interruption")

    monkeypatch.setattr(watch_status.os, "replace", interrupted)
    try:
        watch_status.save_watch_observation(path, "restart_limit", 5, 5, 7)
    except OSError:
        pass
    else:
        raise AssertionError("Injected replace failure was not reached")
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]
