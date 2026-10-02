import asyncio
import json
import os
import site
import subprocess
import sys
import tomllib
import venv
from pathlib import Path

import psutil
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from anywhere_computer import cli
from anywhere_computer.connection import exchange
from anywhere_computer.mcp_config import render_mcp_config


@pytest.mark.parametrize('output_format', ['json', 'toml'])
def test_export_keeps_installation_and_state_without_starting(
    tmp_path, monkeypatch, capsys, output_format,
):
    state = tmp_path / 'state 日本語 🙂 with "quotes"\x7f'
    executable = str(tmp_path / 'installation with spaces' / 'python')
    monkeypatch.setattr(sys, 'executable', executable)
    monkeypatch.setattr(sys, 'argv', ['anywhere', 'mcp-config', '--state-dir', str(state),
                                     '--format', output_format])
    monkeypatch.setattr(cli, 'ensure_agent', lambda *a, **k: pytest.fail('Unexpected start'))
    cli.main()
    output = capsys.readouterr().out
    # Redirected Windows output may use a legacy code page. Copyable exports
    # must retain exact Unicode paths without depending on stdout's encoding.
    output.encode('ascii')
    parsed = json.loads(output) if output_format == 'json' else tomllib.loads(output)
    entry = parsed['mcpServers' if output_format == 'json' else 'mcp_servers']['anywhere-computer']
    assert entry == {
        'command': executable,
        'args': ['-B', '-I', '-X', 'utf8', '-m', 'anywhere_computer',
                 'mcp', '--state-dir', str(state)],
    }
    assert not state.exists()
    assert 'env' not in entry


def test_toml_export_rejects_invalid_unicode_before_output(tmp_path):
    with pytest.raises(ValueError, match='Unicode scalar'):
        render_mcp_config(tmp_path / 'invalid\ud800', 'toml')


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


async def test_exported_configuration_runs_real_mcp_without_path_or_workspace_imports(
    tmp_path, monkeypatch,
):
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
        cwd=hostile, env=env, text=True, capture_output=True, timeout=15,
    )
    assert generated.returncode == 0, generated.stderr[-2000:]
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
                    admitted_endpoint = json.loads((state / 'agent.json').read_text('utf-8'))
                    admitted_process = psutil.Process(admitted_endpoint['pid'])
                    assert admitted_process.create_time() == admitted_endpoint['process_started']
                    admitted_parent = admitted_process.ppid()
        assert not marker.exists()
        assert not (tmp_path / 'wrong-state').exists()
    finally:
        if (state / 'agent.json').exists():
            # Observe the actual TCP phases without replacing its transport,
            # retrying the stop, or recording request/reply bodies or secrets.
            phases = []
            real_open = asyncio.open_connection

            async def observed_open(*args, **kwargs):
                reader, writer = await real_open(*args, **kwargs)
                phases.append('connected')
                real_readline, real_drain = reader.readline, writer.drain
                real_close = writer.wait_closed

                async def observed_readline():
                    phases.append('reading_reply')
                    result = await real_readline()
                    phases.append('reply_received' if result else 'reply_missing')
                    return result

                async def observed_drain():
                    await real_drain()
                    phases.append('request_drained')

                async def observed_close():
                    phases.append('closing')
                    try:
                        await real_close()
                    except ConnectionError:
                        phases.append('close_reset')
                        raise
                    phases.append('closed')

                monkeypatch.setattr(reader, 'readline', observed_readline)
                monkeypatch.setattr(writer, 'drain', observed_drain)
                monkeypatch.setattr(writer, 'wait_closed', observed_close)
                return reader, writer

            monkeypatch.setattr(asyncio, 'open_connection', observed_open)
            try:
                stopped = await exchange(state, '__stop', credential=credential)
            except ConnectionError as error:
                error.add_note('Controlled MCP stop phases: ' + json.dumps(phases))
                try:
                    process = psutil.Process(admitted_endpoint['pid'])
                    same = process.create_time() == admitted_endpoint['process_started']
                    live = (same and process.is_running()
                            and process.status() != psutil.STATUS_ZOMBIE)
                except psutil.Error:
                    same, live = False, False
                error.add_note('Controlled MCP agent lifecycle: ' + json.dumps({
                    'parent_during_client': admitted_parent,
                    'same_process_after_client': same,
                    'live_after_client': live,
                }))
                raise
            assert stopped.state == 'completed'
