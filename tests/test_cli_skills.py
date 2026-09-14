import json
import sys
import uuid

import pytest

from anywhere_computer import cli
from anywhere_computer.engine import Engine
from anywhere_computer.models import Request


def test_cli_skill_registration_uses_persistent_engine_settings(tmp_path, monkeypatch, capsys):
    state = tmp_path / 'state'
    collection = tmp_path / 'skills with spaces 日本語'
    collection.mkdir()

    async def exchange(directory, tool, arguments=None):
        assert directory == state
        engine = Engine(directory)
        try:
            return await engine.execute(Request(operation_id=uuid.uuid4().hex,
                                                tool=tool, arguments=arguments or {}))
        finally:
            await engine.close()

    monkeypatch.setattr(cli, 'exchange', exchange)
    monkeypatch.setattr(cli, 'ensure_agent', lambda *a, **k: pytest.fail('Unexpected start'))

    def run(*options):
        monkeypatch.setattr(sys, 'argv', ['anywhere', 'skills-configure',
                                         '--state-dir', str(state), *options])
        cli.main()
        return json.loads(capsys.readouterr().out)['data']

    assert run('--skill-root', str(collection))['skill_roots'] == [str(collection)]
    assert run()['skill_roots'] == [str(collection)]
    with pytest.raises(SystemExit) as error:
        run('--skill-root', 'relative')
    assert error.value.code == 1
    capsys.readouterr()
    assert run()['skill_roots'] == [str(collection)]
    assert run('--clear-skill-roots')['skill_roots'] == []


@pytest.mark.parametrize('arguments', [
    ['status', '--skill-root', '/example'],
    ['skills-configure', '--skill-root', '/example', '--clear-skill-roots'],
])
def test_skill_options_reject_ambiguous_commands(monkeypatch, arguments):
    monkeypatch.setattr(sys, 'argv', ['anywhere', *arguments])
    monkeypatch.setattr(cli, 'exchange', lambda *a, **k: pytest.fail('Unexpected request'))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
