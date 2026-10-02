"""Export local stdio registration without changing a client or starting an agent."""

import json
from pathlib import Path
from typing import Literal

from .runtime_launch import python_module_command

MCPConfigFormat = Literal['json', 'toml']


def render_mcp_config(directory: Path, output_format: MCPConfigFormat = 'json') -> str:
    if not directory.is_absolute():
        raise ValueError('MCP configuration requires an absolute state directory')
    command = python_module_command('anywhere_computer', 'mcp', '--state-dir', str(directory))
    entry = {'command': command[0], 'args': command[1:]}
    if output_format == 'json':
        return json.dumps({'mcpServers': {'anywhere-computer': entry}},
                          ensure_ascii=False, indent=2)
    if output_format == 'toml':
        # JSON strings/arrays use compatible escapes here; never interpolate a
        # local path into an unquoted TOML value or a shell command.
        return ('[mcp_servers.anywhere-computer]\n'
                f'command = {json.dumps(command[0], ensure_ascii=False)}\n'
                f'args = {json.dumps(command[1:], ensure_ascii=False)}')
    raise ValueError('Choose json or toml for the MCP configuration')
