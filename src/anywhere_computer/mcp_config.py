"""Export local stdio registration without changing a client or starting an agent."""

import json
from pathlib import Path
from typing import Literal

from .runtime_launch import python_module_command

MCPConfigFormat = Literal['json', 'toml']


def _toml_string(value: str) -> str:
    # JSON handles quotes, backslashes and control escapes shared with TOML.
    # TOML requires one Unicode scalar escape for non-BMP characters, rather
    # than JSON's UTF-16 surrogate pair. ASCII also survives legacy stdout.
    if any(0xd800 <= ord(character) <= 0xdfff for character in value):
        raise ValueError('MCP TOML paths require valid Unicode scalar values')
    quoted = json.dumps(value, ensure_ascii=False)
    return ''.join(character if ord(character) < 127 else
                   f'\\u{ord(character):04x}' if ord(character) <= 0xffff else
                   f'\\U{ord(character):08x}' for character in quoted)


def render_mcp_config(directory: Path, output_format: MCPConfigFormat = 'json') -> str:
    if not directory.is_absolute():
        raise ValueError('MCP configuration requires an absolute state directory')
    command = python_module_command('anywhere_computer', 'mcp', '--state-dir', str(directory))
    entry = {'command': command[0], 'args': command[1:]}
    if output_format == 'json':
        return json.dumps({'mcpServers': {'anywhere-computer': entry}}, indent=2)
    if output_format == 'toml':
        # Never interpolate a local path into an unquoted value or shell command.
        arguments = ', '.join(_toml_string(argument) for argument in command[1:])
        return ('[mcp_servers.anywhere-computer]\n'
                f'command = {_toml_string(command[0])}\n'
                f'args = [{arguments}]')
    raise ValueError('Choose json or toml for the MCP configuration')
