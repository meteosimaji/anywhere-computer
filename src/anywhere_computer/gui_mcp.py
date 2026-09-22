"""Typed Peekaboo operations over an explicitly opened, owner-scoped MCP session."""

import asyncio
import re
import time
import uuid
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .direct_mcp import DirectMCPOutcomeUnknown
from .direct_mcp_sessions import DirectMCPSessions
from .mcp_results import normalize_tool_result
from .models import DirectMCPSessionId


def normalized(raw: dict[str, JsonValue]) -> dict[str, JsonValue]:
    try:
        return normalize_tool_result(raw)
    except ValueError:
        raise DirectMCPOutcomeUnknown('GUI result invalid after dispatch; inspect before retry') \
            from None


class GUIObserve(DirectMCPSessionId):
    window_id: int = Field(ge=1, le=4294967295, description=(
        'Required target window. Use the selected server window tool with action=list '
        'and the app to discover its ID. Observes without app focus and pins actions '
        'to the snapshot. Requires a compatible server; never uses foreground input.'
    ))
    app: str = Field(min_length=1, max_length=256, description=(
        'Exact running app name accepted by the selected Peekaboo server. Names may be '
        'localized (for example 計算機). If absent, inspect its app tool and use the '
        'name returned by launch; this operation does not launch applications.'
    ))


class GUIAction(DirectMCPSessionId):
    observation_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class GUIClick(GUIAction):
    element_id: str = Field(min_length=1, max_length=128)


class GUIType(GUIAction):
    text: str = Field(min_length=1, max_length=10000)
    press_return: bool = False
    element_id: str | None = Field(default=None, min_length=1, max_length=128,
        description='Optional observed input element, available in exact-window mode.')
    clear: bool = Field(default=False,
        description='Replace existing text in exact-window mode; otherwise append/type.')


class GUIKey(GUIAction):
    keys: list[str] = Field(min_length=1, max_length=8, description=(
        'One key chord, case-insensitive: cmd, shift, alt, option, ctrl, fn, a-z, 0-9, '
        'space, return (alias enter), tab, escape (alias esc), delete, '
        'arrow_up/down/left/right, f1-f12. Example: ["escape"] or ["cmd", "a"].'
    ))


class Bounds(BaseModel):
    model_config = ConfigDict(extra='ignore', allow_inf_nan=False)
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class ImageSize(BaseModel):
    model_config = ConfigDict(extra='ignore', allow_inf_nan=False)
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class CoordinateContext(BaseModel):
    model_config = ConfigDict(extra='ignore', allow_inf_nan=False)
    version: Literal[1]
    logical_space: Literal['global_display_points']
    origin: Literal['top_left']
    logical_bounds: Bounds
    delivered_image_size: ImageSize
    reference_id: str = Field(min_length=1, max_length=128)


@dataclass
class Observation:
    owner: str | None
    identity: str
    app: str
    snapshot: str
    elements: frozenset[str]
    created: float
    window_id: int


class GUIMCP:
    """One current observation per session; actions consume it before dispatch."""

    def __init__(self, sessions: DirectMCPSessions) -> None:
        self.sessions = sessions
        self.observations: dict[str, Observation] = {}
        self._interaction = asyncio.Lock()

    async def _require_exact_window(self, session_id: str, owner: str | None) -> None:
        # Peekaboo 4.3.4 accepts an explicit window for see and a snapshot for each
        # input tool. Check all four schemas: older providers can ignore arguments.
        fields = {
            'see': {'window_id': 'integer', 'app_target': 'string'},
            'click': {'snapshot': 'string', 'on': 'string'},
            'type': {'snapshot': 'string', 'text': 'string', 'clear': 'boolean',
                     'on': 'string'},
            'press': {'snapshot': 'string', 'keys': 'array'},
        }
        def compatible(properties: JsonValue, required: dict[str, str]) -> bool:
            if not isinstance(properties, dict):
                return False
            for key, expected in required.items():
                field = properties.get(key)
                if not isinstance(field, dict) or field.get('type') != expected:
                    return False
            return True

        for tool_name, required in fields.items():
            page = await self.sessions.tools(session_id, owner=owner, name=tool_name)
            found = False
            cursors: set[str] = set()
            for _ in range(16):
                rows = page.get('tools')
                if not isinstance(rows, list):
                    raise ValueError('Selected MCP catalog has no tools list')
                for row in rows:
                    if not isinstance(row, dict) or row.get('name') != tool_name:
                        continue
                    if found:
                        raise ValueError(f'Duplicate {tool_name} contract in selected MCP catalog')
                    schema = row.get('inputSchema')
                    properties = schema.get('properties') if isinstance(schema, dict) else None
                    if not compatible(properties, required):
                        raise ValueError(
                            f'Selected MCP server lacks snapshot-bound {tool_name}')
                    found = True
                cursor = page.get('nextCursor')
                if not isinstance(cursor, str) or not cursor:
                    break
                if cursor in cursors:
                    raise ValueError('Selected MCP catalog cursor repeated')
                cursors.add(cursor)
                page = await self.sessions.tools(
                    session_id, owner=owner, name=tool_name, cursor=cursor)
            else:
                raise ValueError('Selected MCP catalog exceeded pagination limit')
            if not found:
                raise ValueError(f'Snapshot-bound {tool_name} contract unavailable')

    async def observe(self, args: GUIObserve, *, owner: str | None) -> dict[str, JsonValue]:
        if self._interaction.locked():
            raise ValueError('GUI is busy; observe again after the current interaction finishes')
        async with self._interaction:
            return await self._observe(args, owner=owner)

    async def _observe(self, args: GUIObserve, *, owner: str | None) -> dict[str, JsonValue]:
        self.sessions.status(args.session_id, owner=owner)
        self.observations.pop(args.session_id, None)
        await self._require_exact_window(args.session_id, owner)
        capture: dict[str, JsonValue] = {'app_target': args.app, 'window_id': args.window_id}
        raw = await self.sessions.call(
            args.session_id, 'see', capture, owner=owner,
        )
        result = normalized(raw)
        if result['is_error']:
            return {**result, 'action_ready': False, 'reason': 'provider_observation_failed'}
        if result.get('truncated'):
            return {**result, 'action_ready': False, 'reason': 'observation_truncated'}
        # Only visible, bounded evidence may authorize input; never parse omitted text.
        content = result.get('content')
        texts: list[str] = []
        if isinstance(content, list):
            for row in content:
                value = row.get('text') if isinstance(row, dict) else None
                if isinstance(value, str):
                    texts.append(value)
        text = '\n'.join(texts)
        applications = re.findall(r'^Application: (.+)$', text, re.MULTILINE)
        if applications != [args.app]:
            return {**result, 'action_ready': False,
                    'reason': 'observed_application_mismatch'}
        snapshots = re.findall(r'^Snapshot ID: ([A-Za-z0-9_-]{1,128})$', text, re.MULTILINE)
        if not snapshots:
            return {**result, 'action_ready': False, 'reason': 'snapshot_reference_unavailable'}
        if len(snapshots) != 1:
            return {**result, 'action_ready': False, 'reason': 'snapshot_reference_ambiguous'}
        snapshot = snapshots[0]
        metadata = raw.get('_meta')
        if not isinstance(metadata, dict) or (
            metadata.get('snapshot_id') is not None
            and metadata.get('snapshot_id') != snapshot
        ):
            return {**result, 'action_ready': False,
                    'reason': 'snapshot_reference_mismatch'}
        # Peekaboo 4.3.4's MCP response projects its exact target into
        # _meta.target_receipt. The CLI JSON's observation.target is a different
        # envelope and must not be assumed to survive the MCP bridge.
        target = metadata.get('target_receipt')
        window_id = target.get('window_id') if isinstance(target, dict) else None
        if type(window_id) is not int:
            return {**result, 'action_ready': False,
                    'reason': 'observed_window_unavailable'}
        if window_id != args.window_id:
            return {**result, 'action_ready': False,
                    'reason': 'observed_window_mismatch'}
        element_ids = [match[1] for line in text.splitlines()
                       if '[not actionable]' not in line
                       and (match := re.match(r'^\s+([A-Za-z0-9_]+) - ', line))]
        if len(element_ids) != len(set(element_ids)):
            return {**result, 'action_ready': False, 'reason': 'element_reference_ambiguous'}
        context: JsonValue = None
        coordinate_status = 'unavailable'
        if isinstance(metadata, dict) and metadata.get('coordinate_context') is not None:
            try:
                parsed = CoordinateContext.model_validate(metadata['coordinate_context'])
                if parsed.reference_id != snapshot:
                    raise ValueError('Snapshot mismatch')
                context = parsed.model_dump(mode='json')
                coordinate_status = 'validated'
            except ValueError:
                return {**result, 'action_ready': False,
                        'reason': 'coordinate_reference_mismatch'}
        observation = Observation(
            owner, uuid.uuid4().hex, args.app, snapshot,
            frozenset(element_ids), time.monotonic(), args.window_id,
        )
        # Bound history to currently registered sessions; no screenshots are stored here.
        self.observations = {key: value for key, value in self.observations.items()
                             if key in self.sessions.entries and
                             time.monotonic() - value.created < 60}
        self.observations[args.session_id] = observation
        return {**result, 'observation_id': observation.identity, 'app': args.app,
                'window_id': args.window_id,
                'snapshot': snapshot, 'action_ready': True, 'expires_in_seconds': 60,
                'coordinate_context': context, 'coordinate_status': coordinate_status,
                'coordinate_actions_supported': False}

    async def act(self, args: GUIAction, *, owner: str | None) -> dict[str, JsonValue]:
        if self._interaction.locked():
            raise ValueError('GUI is busy; observe again after the current interaction finishes')
        async with self._interaction:
            return await self._act(args, owner=owner)

    async def _act(self, args: GUIAction, *, owner: str | None) -> dict[str, JsonValue]:
        observation = self.observations.get(args.session_id)
        if (observation is None or observation.owner != owner
                or observation.identity != args.observation_id
                or time.monotonic() - observation.created >= 60):
            raise ValueError('GUI observation is missing, stale or belongs to another connection')
        parameters: dict[str, JsonValue]
        if isinstance(args, GUIClick):
            if args.element_id not in observation.elements:
                raise ValueError('Element is not present in the selected observation')
            name, parameters = 'click', {'on': args.element_id, 'snapshot': observation.snapshot}
        elif isinstance(args, GUIType):
            name = 'type'
            if args.press_return:
                raise ValueError('Use gui_key after observation for exact-window Return')
            if args.element_id is not None and args.element_id not in observation.elements:
                raise ValueError('Element is not present in the selected observation')
            parameters = {'text': args.text, 'clear': args.clear,
                          'snapshot': observation.snapshot}
            if args.element_id is not None:
                parameters['on'] = args.element_id
        elif isinstance(args, GUIKey):
            aliases = {'esc': 'escape', 'enter': 'return'}
            keys = [aliases.get(key.lower(), key.lower()) for key in args.keys]
            if any(not re.fullmatch(
                r'(?:cmd|shift|alt|option|ctrl|fn|[a-z0-9]|space|return|tab|escape|delete|'
                r'arrow_(?:up|down|left|right)|f(?:[1-9]|1[0-2]))', key,
            ) for key in keys):
                raise ValueError('Unsupported GUI key name; see keys schema for supported names')
            primary = [key for key in keys
                       if key not in {'cmd', 'shift', 'alt', 'option', 'ctrl', 'fn'}]
            if len(primary) != 1:
                raise ValueError('Exact-window key chord requires one primary key')
            names = {'return': 'Return', 'escape': 'Escape', 'delete': 'BackSpace',
                     'arrow_up': 'Up', 'arrow_down': 'Down', 'arrow_left': 'Left',
                     'arrow_right': 'Right', 'tab': 'Tab'}
            name, parameters = 'press', {'snapshot': observation.snapshot,
                'keys': ['+'.join(names.get(key, key) for key in keys)]}
        else:
            raise ValueError('Unsupported GUI action')
        # Consume before the first effect. A response failure must not replay input.
        # Keyboard focus is desktop-global, including across provider sessions.
        # Other observations must not survive an input that may change that focus.
        self.observations.clear()
        raw = await self.sessions.call(args.session_id, name, parameters, owner=owner)
        return {**normalized(raw), 'observation_consumed': True,
                'app': observation.app, 'stage': 'action_result',
                'window_id': observation.window_id,
                'focus_may_change_externally': None,
                'postcondition_verified': False,
                'next_action': 'Observe the same window to verify the intended effect; '
                    'do not repeat input solely because the provider acknowledged it.'}
