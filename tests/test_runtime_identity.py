import pytest

from anywhere_computer.connection import ensure_agent
from anywhere_computer.models import Reply
from anywhere_computer.runtime_identity import runtime_identity


def test_runtime_identity_is_stable_and_content_addressed():
    assert len(runtime_identity()) == 64
    assert runtime_identity() == runtime_identity()


def test_existing_matching_agent_is_reused(tmp_path, monkeypatch):
    calls = []

    async def exchange(directory, tool, **kwargs):
        calls.append(tool)
        return Reply(
            operation_id="0" * 32,
            state="completed",
            data={
                "runtime_id": runtime_identity(),
                "active_sessions": 0,
                "active_operations": 0,
            },
        )

    monkeypatch.setattr(
        "anywhere_computer.connection.local_credential", lambda *a, **kw: "test-only"
    )
    monkeypatch.setattr("anywhere_computer.connection.exchange", exchange)
    assert ensure_agent(tmp_path)["runtime_id"] == runtime_identity()
    assert calls == ["__status"]


def test_build_upgrade_refuses_to_stop_active_work(tmp_path, monkeypatch):
    calls = []

    async def exchange(directory, tool, **kwargs):
        calls.append(tool)
        return Reply(
            operation_id="0" * 32,
            state="completed",
            data={
                "runtime_id": "previous",
                "active_sessions": 1,
                "active_operations": 0,
            },
        )

    monkeypatch.setattr(
        "anywhere_computer.connection.local_credential", lambda *a, **kw: "test-only"
    )
    monkeypatch.setattr("anywhere_computer.connection.exchange", exchange)
    with pytest.raises(RuntimeError, match="active work"):
        ensure_agent(tmp_path, replace_idle=True)
    assert calls == ["__status"]


@pytest.mark.parametrize('busy', [False, True])
def test_connector_reuses_compatible_other_build_without_replacing_it(tmp_path, monkeypatch, busy):
    from anywhere_computer.runtime_identity import ENGINE_API_VERSION

    calls = []

    async def exchange(directory, tool, **kwargs):
        calls.append(tool)
        return Reply(operation_id='0' * 32, state='completed', data={
            'runtime_id': 'another-installed-build', 'engine_api_version': ENGINE_API_VERSION,
            'update_blocked': busy, 'active_sessions': int(busy),
        })

    monkeypatch.setattr('anywhere_computer.connection.local_credential', lambda *a, **kw: 'fixture')
    monkeypatch.setattr('anywhere_computer.connection.exchange', exchange)
    result = ensure_agent(tmp_path, replace_idle=False)
    assert result['runtime_id'] == 'another-installed-build'
    assert calls == ['__status']


@pytest.mark.parametrize('api', [None, 2, True, '1'])
def test_connector_rejects_unknown_protocol_without_stopping_server(tmp_path, monkeypatch, api):
    calls = []

    async def exchange(directory, tool, **kwargs):
        calls.append(tool)
        return Reply(operation_id='0' * 32, state='completed', data={
            'runtime_id': 'another-installed-build', 'engine_api_version': api,
        })

    monkeypatch.setattr('anywhere_computer.connection.local_credential', lambda *a, **kw: 'fixture')
    monkeypatch.setattr('anywhere_computer.connection.exchange', exchange)
    with pytest.raises(RuntimeError, match='API'):
        ensure_agent(tmp_path, replace_idle=False)
    assert calls == ['__status']
