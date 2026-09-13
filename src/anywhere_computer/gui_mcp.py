"""Typed Peekaboo operations over an explicitly opened, owner-scoped MCP session."""

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
    app: str = Field(min_length=1, max_length=256)


class GUIAction(DirectMCPSessionId):
    observation_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class GUIClick(GUIAction):
    element_id: str = Field(min_length=1, max_length=128)


class GUIType(GUIAction):
    text: str = Field(min_length=1, max_length=10000)
    press_return: bool = False


class GUIKey(GUIAction):
    keys: list[str] = Field(min_length=1, max_length=8)


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


class GUIMCP:
    """One current observation per session; actions consume it before dispatch."""

    def __init__(self, sessions: DirectMCPSessions) -> None:
        self.sessions = sessions
        self.observations: dict[str, Observation] = {}

    async def observe(self, args: GUIObserve, *, owner: str | None) -> dict[str, JsonValue]:
        self.sessions.status(args.session_id, owner=owner)
        self.observations.pop(args.session_id, None)
        raw = await self.sessions.call(
            args.session_id, 'see', {'app_target': args.app}, owner=owner,
        )
        result = normalized(raw)
        if result['is_error']:
            return result
        content = raw.get('content')
        texts: list[str] = []
        if isinstance(content, list):
            for row in content:
                value = row.get('text') if isinstance(row, dict) else None
                if isinstance(value, str):
                    texts.append(value)
        text = '\n'.join(texts)
        match = re.search(r'^Snapshot ID: ([A-Za-z0-9_-]{1,128})$', text, re.MULTILINE)
        if match is None:
            return {**result, 'action_ready': False, 'reason': 'snapshot_reference_unavailable'}
        snapshot = match[1]
        context: JsonValue = None
        metadata = raw.get('_meta')
        coordinate_status = 'unavailable'
        if isinstance(metadata, dict) and metadata.get('coordinate_context') is not None:
            try:
                parsed = CoordinateContext.model_validate(metadata['coordinate_context'])
                if parsed.reference_id != snapshot:
                    raise ValueError('Snapshot mismatch')
                context = parsed.model_dump(mode='json')
                coordinate_status = 'validated'
            except ValueError:
                coordinate_status = 'unsupported_or_invalid'
        observation = Observation(
            owner, uuid.uuid4().hex, args.app, snapshot,
            frozenset(match[1] for line in text.splitlines()
                      if '[not actionable]' not in line
                      and (match := re.match(r'^\s+([A-Za-z0-9_]+) - ', line))),
            time.monotonic(),
        )
        # Bound history to currently registered sessions; no screenshots are stored here.
        self.observations = {key: value for key, value in self.observations.items()
                             if key in self.sessions.entries and
                             time.monotonic() - value.created < 60}
        self.observations[args.session_id] = observation
        return {**result, 'observation_id': observation.identity, 'app': args.app,
                'snapshot': snapshot, 'action_ready': True, 'expires_in_seconds': 60,
                'coordinate_context': context, 'coordinate_status': coordinate_status,
                'coordinate_actions_supported': False}

    async def act(self, args: GUIAction, *, owner: str | None) -> dict[str, JsonValue]:
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
            name, parameters = 'type', {'text': args.text, 'press_return': args.press_return,
                                        'snapshot': observation.snapshot}
        elif isinstance(args, GUIKey):
            if any(not re.fullmatch(
                r'(?:cmd|shift|alt|option|ctrl|fn|[a-z0-9]|space|return|tab|escape|delete|'
                r'arrow_(?:up|down|left|right)|f(?:[1-9]|1[0-2]))', key,
            ) for key in args.keys):
                raise ValueError('Unsupported GUI key name')
            name, parameters = 'hotkey', {'keys': ','.join(args.keys)}
        else:
            raise ValueError('Unsupported GUI action')
        # Consume before the first effect. A response failure must not replay input.
        del self.observations[args.session_id]
        if name in {'type', 'hotkey'}:
            focused = await self.sessions.call(args.session_id, 'app',
                {'action': 'focus', 'name': observation.app}, owner=owner)
            focus_result = normalized(focused)
            if focus_result['is_error']:
                return {**focus_result, 'input_dispatched': False, 'stage': 'focus'}
        raw = await self.sessions.call(args.session_id, name, parameters, owner=owner)
        return {**normalized(raw), 'observation_consumed': True,
                'app': observation.app, 'stage': 'action_result',
                'focus_may_change_externally': name in {'type', 'hotkey'}}
