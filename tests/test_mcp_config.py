import asyncio
import json
import os
import site
import subprocess
import sys
import tomllib
import venv
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from anywhere_computer import cli
from anywhere_computer.connection import exchange


@pytest.mark.parametrize('output_format', ['json', 'toml'])
def test_export_keeps_installation_and_state_without_starting(
    tmp_path, monkeypatch, capsys, output_format,
):
    state = tmp_path / 'state 日本語 with "quotes"'
    executable = str(tmp_path / 'installation with spaces' / 'python')
    monkeypatch.setattr(sys, 'executable', executable)
    monkeypatch.setattr(sys, 'argv', ['anywhere', 'mcp-config', '--state-dir', str(state),
                                     '--format', output_format])
    monkeypatch.setattr(cli, 'ensure_agent', lambda *a, **k: pytest.fail('Unexpected start'))
    cli.main()
    output = capsys.readouterr().out
    parsed = json.loads(output) if output_format == 'json' else tomllib.loads(output)
    entry = parsed['mcpServers' if output_format == 'json' else 'mcp_servers']['anywhere-computer']
    assert entry == {
        'command': executable,
        'args': ['-B', '-I', '-X', 'utf8', '-m', 'anywhere_computer',
                 'mcp', '--state-dir', str(state)],
    }
    assert not state.exists()
    assert 'env' not in entry


@pytest.mark.parametrize('options', [
    ['mcp-config', '--scope', 'files_read'],
    ['mcp-config', '--resource', 'https://example.com/mcp'],
    ['mcp-config', '--profile', 'a-private-profile'],
    ['status', '--format', 'toml'],
])
def test_export_rejects_unrelated_or_private_options(monkeypatch, capsys, options):
    monkeypatch.setattr(sys, 'argv', ['anywhere', *options])
    monkeypatch.setattr(cli, 'ensure_agent', lambda *a, **k: pytest.fail('Unexpected start'))
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2
    assert capsys.readouterr().out == ''


async def test_exported_configuration_runs_real_mcp_without_path_or_workspace_imports(tmp_path):
    state = tmp_path / 'agent state 日本語'
    credential = 'synthetic-mcp-config-credential'
    # Install only fixture credential hooks in a disposable interpreter's trusted
    # site. The exported command, CLI, engine and MCP transport remain real; -I
    # stays enabled and no live OS credential store is read or changed.
    environment = tmp_path / 'installation with spaces'
    venv.EnvBuilder(with_pip=False, symlinks=sys.platform != 'win32').create(environment)
    interpreter = environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    fixture_site = Path(subprocess.check_output([
        str(interpreter), '-I', '-c', "import sysconfig; print(sysconfig.get_path('purelib'))",
    ], text=True).strip())
    fixture_site.mkdir(parents=True, exist_ok=True)
    (fixture_site / 'test-dependencies.pth').write_text(
        '\n'.join([str(Path(__file__).parents[1] / 'src'), *site.getsitepackages()]) + '\n',
        encoding='utf-8',
    )
    (fixture_site / 'sitecustomize.py').write_text(f'''
from pathlib import Path
from anywhere_computer import credentials
def fixture_credential(directory, *, create=False):
    assert directory == Path({str(state)!r}), 'Fixture credential requested for another state'
    return {credential!r}
credentials.local_credential = fixture_credential
''', encoding='utf-8')
    hostile = tmp_path / 'unrelated client workspace'
    hostile.mkdir()
    marker = hostile / 'unexpected-import'
    (hostile / 'anywhere_computer.py').write_text(
        f'from pathlib import Path\nPath({str(marker)!r}).touch()\n'
        'raise RuntimeError("wrong code")',
        encoding='utf-8',
    )
    env = dict(os.environ, PATH=str(tmp_path / 'empty-path'), PYTHONPATH=str(hostile),
               ANYWHERE_STATE_DIR=str(tmp_path / 'wrong-state'))
    generated = subprocess.run(
        [str(interpreter), '-B', '-I', '-m', 'anywhere_computer', 'mcp-config',
         '--state-dir', str(state)],
        cwd=hostile, env=env, check=True, text=True, capture_output=True, timeout=15,
    )
    config = json.loads(generated.stdout)['mcpServers']['anywhere-computer']
    assert config['command'] == str(interpreter)
    assert not state.exists()
    parameters = StdioServerParameters(**config, cwd=str(hostile), env=env)
    try:
        async with asyncio.timeout(45):
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    initialized = await client.initialize()
                    assert initialized.serverInfo.name == 'anywhere-computer'
                    tools = await client.list_tools()
                    assert {'computer_status', 'files_read', 'operations_get'} <= {
                        tool.name for tool in tools.tools
                    }
                    status = await client.call_tool('computer_status', {})
                    assert not status.isError
                    assert status.structuredContent['data']['state'] == 'ready'
                    sample = tmp_path / 'sample 日本語.txt'
                    sample.write_text('generated configuration works', encoding='utf-8')
                    read = await client.call_tool('files_read', {'path': str(sample)})
                    assert not read.isError
                    assert read.structuredContent['data']['text'] == sample.read_text('utf-8')
        assert not marker.exists()
        assert not (tmp_path / 'wrong-state').exists()
    finally:
        if (state / 'agent.json').exists():
            stopped = await exchange(state, '__stop', credential=credential)
            assert stopped.state == 'completed'
