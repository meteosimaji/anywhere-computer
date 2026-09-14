import sys

import pytest

from anywhere_computer import cli


@pytest.mark.parametrize('choice,kind', [('2', 'chatgpt'), ('3', 'native')])
def test_setup_routes_to_existing_remote_flow(tmp_path, monkeypatch, choice, kind):
    target = tmp_path / 'new-state'
    monkeypatch.setattr(sys, 'argv', ['anywhere', 'setup', '--state-dir', str(target)])
    monkeypatch.setattr(cli, 'has_interactive_input', lambda: True)
    monkeypatch.setattr('builtins.input', lambda _: choice)
    calls = []
    def setup(directory, *, client_kind):
        calls.append((directory, client_kind))
        return {'state': 'fixture'}
    monkeypatch.setattr(cli, 'setup_remote', setup)
    cli.main()
    assert calls == [(target, kind)]
    assert not target.exists()


def test_setup_local_uses_existing_start(tmp_path, monkeypatch):
    target = tmp_path / 'new-state'
    monkeypatch.setattr(sys, 'argv', ['anywhere', 'setup', '--state-dir', str(target)])
    monkeypatch.setattr(cli, 'has_interactive_input', lambda: True)
    monkeypatch.setattr('builtins.input', lambda _: '1')
    calls = []
    def start(directory, *, replace_idle):
        calls.append((directory, replace_idle))
        return {'state': 'ready'}
    monkeypatch.setattr(cli, 'ensure_agent', start)
    cli.main()
    assert calls == [(target, True)]


@pytest.mark.parametrize('choice', ['q', 'invalid', 'eof'])
def test_setup_cancel_or_invalid_has_no_side_effects(tmp_path, monkeypatch, choice):
    target = tmp_path / 'new-state'
    monkeypatch.setattr(sys, 'argv', ['anywhere', 'setup', '--state-dir', str(target)])
    monkeypatch.setattr(cli, 'has_interactive_input', lambda: True)
    def answer(_):
        if choice == 'eof':
            raise EOFError
        return choice
    monkeypatch.setattr('builtins.input', answer)
    monkeypatch.setattr(cli, 'ensure_agent', lambda *a, **k: pytest.fail('Unexpected start'))
    monkeypatch.setattr(cli, 'setup_remote', lambda *a, **k: pytest.fail('Unexpected setup'))
    if choice == 'invalid':
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 2
    else:
        cli.main()
    assert not target.exists()


@pytest.mark.parametrize('options', [[], ['--resource', 'https://example.com/mcp']])
def test_setup_rejects_noninteractive_or_advanced_options(tmp_path, monkeypatch, options):
    target = tmp_path / 'new-state'
    monkeypatch.setattr(sys, 'argv', ['anywhere', 'setup', '--state-dir', str(target), *options])
    monkeypatch.setattr(cli, 'has_interactive_input', lambda: False)
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('Unexpected prompt'))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert not target.exists()
