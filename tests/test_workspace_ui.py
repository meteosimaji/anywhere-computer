import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from anywhere_computer.engine import Engine
from anywhere_computer.mcp_server import MCPSession
from anywhere_computer.workspace_ui import UI_EXTENSION, UI_MIME, workspace_resource


async def initialize(session, *, ui=True):
    response = await session.handle({
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'clientInfo': {'name': 'ui-test', 'version': '1'},
            'capabilities': {'extensions': {UI_EXTENSION: {'mimeTypes': [UI_MIME]}}} if ui else {},
        },
    })
    await session.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    return response['result']


async def rpc(session, method, **params):
    return await session.handle({'jsonrpc': '2.0', 'id': uuid.uuid4().hex,
                                 'method': method, 'params': params})


@pytest.fixture
async def ui_session(tmp_path):
    engine = Engine(tmp_path / 'engine')
    allowed = set(engine.tools)
    async def catalog():
        return engine.catalog(frozenset(allowed))
    session = MCPSession(catalog, engine.execute)
    try:
        yield session, engine, allowed
    finally:
        await engine.close()


async def test_ui_negotiation_resource_and_render_only_contract(ui_session, tmp_path):
    session, engine, _ = ui_session
    initialized = await initialize(session)
    assert initialized['capabilities']['resources']['subscribe'] is False
    tools = (await rpc(session, 'tools/list'))['result']['tools']
    render = next(tool for tool in tools if tool['name'] == 'workspace_open')
    uri = render['_meta']['ui']['resourceUri']
    resources = (await rpc(session, 'resources/list'))['result']['resources']
    assert resources[0]['uri'] == uri and 'text' not in resources[0]
    asset = (await rpc(session, 'resources/read', uri=uri))['result']['contents'][0]
    assert asset['mimeType'] == UI_MIME
    assert asset['_meta']['ui']['csp']['connectDomains'] == []
    assert '<script>' in asset['text'] and 'ui/initialize' in asset['text']
    target = tmp_path / 'does-not-exist.txt'
    output = (await rpc(session, 'tools/call', name='workspace_open',
                        arguments={'path': str(target)}))['result']
    assert output['structuredContent']['state'] == 'completed'
    assert output['structuredContent']['data']['workspace']['path'] == str(target)
    assert not target.exists()
    assert 'files_write' in output['_meta']['workspaceTools']
    assert 'terminal_start' not in output['_meta']['workspaceTools']
    assert 'devices_call' not in output['_meta']['workspaceTools']
    assert len(engine.tools) == 61


async def test_text_only_host_keeps_useful_tools_without_ui_metadata(ui_session):
    session, _, _ = ui_session
    result = await initialize(session, ui=False)
    assert 'resources' not in result['capabilities']
    tools = (await rpc(session, 'tools/list'))['result']['tools']
    assert all('_meta' not in tool for tool in tools)
    assert (await rpc(session, 'resources/read', uri=workspace_resource()['uri']))['error']
    output = (await rpc(session, 'tools/call', name='workspace_open', arguments={}))['result']
    assert output['structuredContent']['state'] == 'completed'
    assert 'files_read' in output['structuredContent']['data']['instructions']
    assert '_meta' not in output


async def test_resource_access_tracks_tool_grant_and_rejects_arbitrary_paths(ui_session):
    session, _, allowed = ui_session
    await initialize(session)
    uri = workspace_resource()['uri']
    for invalid in ('file:///etc/passwd', 'ui://anywhere-computer/../../etc/passwd', uri + '?x=1'):
        assert (await rpc(session, 'resources/read', uri=invalid))['error']
    allowed.remove('workspace_open')
    assert (await rpc(session, 'resources/list'))['result']['resources'] == []
    assert (await rpc(session, 'resources/read', uri=uri))['error']
    assert (await rpc(session, 'tools/call', name='workspace_open', arguments={}))['error']


def test_workspace_mutation_recovery_in_javascript():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the development-only UI script test")
    root = Path(__file__).resolve().parents[1]
    try:
        result = subprocess.run(
            [node, str(root / "tests/workspace_ui_state.cjs"),
             str(root / "src/anywhere_computer/web/workspace.html")],
            capture_output=True, text=True, encoding="utf-8", timeout=15, check=True,
        )
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
        # TimeoutExpired may retain bytes even when text=True. Fixture output
        # contains only test phases and assertion diagnostics, not user data.
        captured = []
        for stream in (error.stdout, error.stderr):
            if isinstance(stream, bytes):
                stream = stream.decode("utf-8", errors="replace")
            captured.append((stream or "<no output>")[-4000:])
        pytest.fail(f"Workspace Node fixture failed: {error}\n"
                    f"stdout:\n{captured[0]}\nstderr:\n{captured[1]}", pytrace=False)
    assert "workspace mutation recovery: passed" in result.stdout
