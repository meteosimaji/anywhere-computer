"""Explicit, window-scoped projection of a Cua MCP observation and actions."""

import re
from typing import TYPE_CHECKING, cast

from pydantic import JsonValue

from .mcp_results import normalize_tool_result

if TYPE_CHECKING:
    from .direct_mcp_sessions import DirectMCPSessions
    from .gui_mcp import GUIAction, GUIObserve, Observation


_CONTRACTS = {
    'get_window_state': {'pid': 'integer', 'window_id': 'integer',
                         'include_screenshot': 'boolean', 'max_elements': 'integer',
                         'max_depth': 'integer'},
    'click': {'pid': 'integer', 'window_id': 'integer', 'snapshot_id': 'string',
              'element_token': 'string', 'scope': 'string', 'delivery_mode': 'string'},
    'type_text': {'pid': 'integer', 'window_id': 'integer', 'snapshot_id': 'string',
                  'element_token': 'string', 'text': 'string', 'scope': 'string',
                  'delivery_mode': 'string'},
    'set_value': {'pid': 'integer', 'window_id': 'integer', 'snapshot_id': 'string',
                  'element_token': 'string', 'value': 'string'},
    'press_key': {'pid': 'integer', 'window_id': 'integer', 'snapshot_id': 'string',
                  'element_token': 'string', 'key': 'string', 'modifiers': 'array',
                  'scope': 'string', 'delivery_mode': 'string'},
}


async def _require_contracts(sessions: 'DirectMCPSessions', session_id: str,
                             owner: str | None) -> None:
    for name, fields in _CONTRACTS.items():
        page = await sessions.tools(session_id, owner=owner, name=name)
        rows = page.get('tools')
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError(f'Cua {name} contract unavailable')
        row = rows[0]
        schema = row.get('inputSchema')
        properties = schema.get('properties') if isinstance(schema, dict) else None
        if row.get('name') != name or not isinstance(properties, dict) or any(
            not isinstance(field := properties.get(key), dict)
            or field.get('type') != kind for key, kind in fields.items()
        ):
            raise ValueError(f'Cua {name} contract incompatible')
        for field in ('scope', 'delivery_mode'):
            if field in fields:
                definition = properties[field]
                choices = definition.get('enum') if isinstance(definition, dict) else None
                expected = 'window' if field == 'scope' else 'background'
                if not isinstance(choices, list) or expected not in choices:
                    raise ValueError(f'Cua {name} contract cannot pin {field}')


def _project(value: object, args: 'GUIObserve') -> tuple[dict[str, JsonValue], str,
                                                         set[str]] | None:
    if not isinstance(value, dict) or value.get('pid') != args.pid or (
        value.get('window_id') != args.window_id or value.get('app_name') != args.app
    ):
        return None
    snapshot = value.get('snapshot_id')
    if not isinstance(snapshot, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', snapshot):
        return None
    rows = value.get('elements')
    if (value.get('elements_complete') is not True or value.get('truncated') is True
            or value.get('degraded') is True or not isinstance(rows, list)
            or not 1 <= len(rows) <= 2000 or any(
                type(value.get(key)) is not int or value[key] != len(rows)
                for key in ('element_count', 'total_element_count', 'returned_element_count'))):
        return None
    by_index: dict[int, dict[str, JsonValue]] = {}
    tokens: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            return None
        index, token = item.get('element_index'), item.get('element_token')
        if (type(index) is not int or index < 0 or index in by_index
                or not isinstance(token, str) or token != f'{snapshot}:{index}'
                or token in tokens or not isinstance(item.get('role'), str)
                or not isinstance(item.get('depth'), int)):
            return None
        by_index[index] = item
        tokens.add(token)
    roots = [i for i, item in by_index.items() if item['role'] == 'AXWindow']
    if len(roots) != 1:
        return None
    root = roots[0]
    for index, item in by_index.items():
        parent = item.get('parent_index')
        if parent is None:
            if item.get('depth') != 0:
                return None
            continue
        if type(parent) is not int or parent not in by_index or parent == index:
            return None
        parent_depth = by_index[parent]['depth']
        if type(parent_depth) is not int or item['depth'] != parent_depth + 1:
            return None
        seen = {index}
        cursor = parent
        while cursor in by_index and by_index[cursor].get('parent_index') is not None:
            if cursor in seen:
                return None
            seen.add(cursor)
            next_index = by_index[cursor]['parent_index']
            if type(next_index) is not int:
                return None
            cursor = next_index
        if cursor in seen:
            return None
    descendants = {root}
    for index in sorted(by_index):
        cursor = index
        while cursor in by_index and cursor != root:
            parent = by_index[cursor].get('parent_index')
            if type(parent) is not int:
                break
            cursor = parent
        if cursor == root:
            descendants.add(index)
    query = args.query.casefold() if args.query else None
    selected = {root}
    if query is None:
        selected = descendants
    else:
        for index in descendants:
            item = by_index[index]
            if any(query in str(item.get(key, '')).casefold()
                   for key in ('role', 'label', 'value')):
                selected.add(index)
                ancestor = item.get('parent_index')
                while type(ancestor) is int and ancestor in descendants:
                    selected.add(ancestor)
                    ancestor = by_index[ancestor].get('parent_index')
    projected: list[JsonValue] = []
    allowed = {'element_index', 'role', 'label', 'value', 'depth',
               'parent_index', 'actions'}
    for index in sorted(selected):
        item = by_index[index]
        projected.append(cast(dict[str, JsonValue],
                              {'element_id': item['element_token'],
                               **{key: item[key] for key in allowed if key in item}}))
    result: dict[str, JsonValue] = {
        'provider': 'cua', 'pid': args.pid, 'window_id': args.window_id,
        'app': args.app, 'snapshot': snapshot, 'elements': projected,
        'action_ready': True}
    return (result, snapshot,
            {str(by_index[index]['element_token']) for index in selected})


async def observe_cua(sessions: 'DirectMCPSessions', args: 'GUIObserve',
                      owner: str | None) -> tuple[dict[str, JsonValue], str | None, set[str]]:
    await _require_contracts(sessions, args.session_id, owner)
    raw = await sessions.call(args.session_id, 'get_window_state', {
        'pid': args.pid, 'window_id': args.window_id, 'include_screenshot': False,
        'max_elements': 2000, 'max_depth': 25}, owner=owner)
    result = normalize_tool_result(raw)
    base: dict[str, JsonValue] = {'provider': 'cua', 'pid': args.pid,
                                  'window_id': args.window_id, 'app': args.app,
                                  'action_ready': False}
    if result['is_error'] or result['truncated']:
        return {**base, 'reason': 'provider_observation_failed'}, None, set()
    projected = _project(result.get('structured_content'), args)
    if projected is None:
        return {**base, 'reason': 'invalid_scoped_observation'}, None, set()
    clean, snapshot, elements = projected
    return {**base, **clean}, snapshot, elements


def act_cua(args: 'GUIAction', observation: 'Observation') -> tuple[str, dict[str, JsonValue]]:
    from .gui_mcp import GUIClick, GUIKey, GUIType

    base: dict[str, JsonValue] = {'pid': observation.pid, 'window_id': observation.window_id,
                                  'snapshot_id': observation.snapshot}
    if isinstance(args, GUIClick):
        if args.element_id not in observation.elements:
            raise ValueError('Element is not present in the selected observation')
        return 'click', {**base, 'element_token': args.element_id,
                         'scope': 'window', 'delivery_mode': 'background'}
    if isinstance(args, GUIType):
        if (args.press_return or args.element_id is None
                or args.element_id not in observation.elements):
            raise ValueError('Cua typing requires one observed text element and no Return')
        if observation.roles is None or observation.roles.get(args.element_id) not in {
            'AXTextArea', 'AXTextField', 'AXSearchField', 'AXComboBox'}:
            raise ValueError('Cua typing requires an observed text element')
        # set_value is an exact AX replacement; type_text sends text to that same target.
        name = 'set_value' if args.clear else 'type_text'
        params: dict[str, JsonValue] = {'value': args.text} if args.clear else {
            'text': args.text, 'scope': 'window', 'delivery_mode': 'background'}
        return name, {**base, 'element_token': args.element_id, **params}
    if isinstance(args, GUIKey):
        if args.element_id is None or args.element_id not in observation.elements:
            raise ValueError('Cua key input requires an observed element target')
        aliases = {'enter': 'return', 'esc': 'escape'}
        keys = [aliases.get(key.lower(), key.lower()) for key in args.keys]
        if any(not re.fullmatch(
            r'(?:cmd|shift|alt|option|ctrl|fn|[a-z0-9]|space|return|tab|escape|delete|'
            r'arrow_(?:up|down|left|right)|f(?:[1-9]|1[0-2]))', key,
        ) for key in keys):
            raise ValueError('Unsupported Cua key name')
        modifiers = [key for key in keys if key in {'cmd', 'shift', 'alt', 'option', 'ctrl', 'fn'}]
        primary = [key for key in keys if key not in modifiers]
        if len(primary) != 1 or len(modifiers) != len(set(modifiers)):
            raise ValueError('Cua key chord requires one primary key')
        return 'press_key', {**base, 'element_token': args.element_id,
                             'key': primary[0], 'modifiers': cast(list[JsonValue], modifiers),
                             'scope': 'window', 'delivery_mode': 'background'}
    raise ValueError('Unsupported GUI action')
