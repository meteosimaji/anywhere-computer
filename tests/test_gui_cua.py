"""Explicit Cua adapter contracts, including scoping before provider data is exposed."""

import asyncio
import copy
import json
import time

import pytest
from pydantic import ValidationError

from anywhere_computer.gui_mcp import GUIMCP, GUIClick, GUIKey, GUIObserve, GUIType

SID = "a" * 32
UNTRUSTED_IMAGE = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg=="
)


class CuaPeer:
    def __init__(self):
        self.entries = {SID: object(), "b" * 32: object()}
        self.calls = []
        self.generation = 0
        self.damage = lambda value: value
        self.contract_missing = None

    def status(self, session_id, *, owner):
        if owner != "owner" or session_id not in self.entries:
            raise ValueError("Not owned")

    async def tools(self, session_id, *, owner, name=None, **kwargs):
        self.status(session_id, owner=owner)
        properties = {
            "pid": {"type": "integer"},
            "window_id": {"type": "integer"},
            "snapshot_id": {"type": "string"},
            "element_token": {"type": "string"},
            "delivery_mode": {"type": "string", "enum": ["background", "foreground"]},
            "scope": {"type": "string", "enum": ["window", "desktop"]},
            "include_screenshot": {"type": "boolean"},
            "max_elements": {"type": "integer"},
            "max_depth": {"type": "integer"},
            "text": {"type": "string"},
            "value": {"type": "string"},
            "key": {"type": "string"},
            "modifiers": {"type": "array"},
        }
        properties.pop(self.contract_missing, None)
        return {"tools": [{"name": name, "inputSchema": {"properties": properties}}]}

    async def call(self, session_id, name, arguments, *, owner):
        self.status(session_id, owner=owner)
        self.calls.append((name, copy.deepcopy(arguments)))
        if name != "get_window_state":
            return {
                "content": [{"type": "text", "text": "provider acknowledged"}],
                "isError": False,
                "structuredContent": {
                    "effect": "confirmed",
                    "route": "accessibility",
                    "delivery": {"mode": "background"},
                },
            }
        self.generation += 1
        snapshot = f"s{self.generation:08x}"
        payload = {
            "pid": 123,
            "window_id": 42,
            "app_name": "Editor",
            "window_title": "Trial",
            "snapshot_id": snapshot,
            "elements_complete": True,
            "element_count": 5,
            "total_element_count": 5,
            "returned_element_count": 5,
            "elements": [
                {
                    "element_index": 0,
                    "element_token": f"{snapshot}:0",
                    "role": "AXWindow",
                    "label": "Trial",
                    "depth": 0,
                },
                {
                    "element_index": 1,
                    "element_token": f"{snapshot}:1",
                    "role": "AXTextArea",
                    "value": "日本語 42 ✅",
                    "label": "Body",
                    "depth": 1,
                    "parent_index": 0,
                },
                {
                    "element_index": 2,
                    "element_token": f"{snapshot}:2",
                    "role": "AXButton",
                    "label": "Apply",
                    "depth": 1,
                    "parent_index": 0,
                    "actions": ["AXPress"],
                },
                {
                    "element_index": 3,
                    "element_token": f"{snapshot}:3",
                    "role": "AXMenuBar",
                    "label": "private unrelated menu",
                    "depth": 0,
                },
                {
                    "element_index": 4,
                    "element_token": f"{snapshot}:4",
                    "role": "AXMenuItem",
                    "label": "private unrelated filename",
                    "depth": 1,
                    "parent_index": 3,
                },
            ],
            "tree_markdown": "private unrelated menu and filename must never be exposed",
        }
        payload = self.damage(payload)
        return {
            "content": [{"type": "text", "text": payload["tree_markdown"]},
                        {"type": "image", "data": UNTRUSTED_IMAGE, "mimeType": "image/png"}],
            "isError": False,
            "structuredContent": payload,
        }


def observation(**kwargs):
    return GUIObserve(session_id=SID, provider="cua", pid=123, window_id=42, app="Editor", **kwargs)


async def test_scoped_structured_observation_excludes_global_menus():
    peer = CuaPeer()
    gui = GUIMCP(peer)
    result = await gui.observe(observation(), owner="owner")
    assert result["action_ready"] is True
    assert result["provider"] == "cua" and result["pid"] == 123
    assert "private unrelated" not in json.dumps(result)
    assert UNTRUSTED_IMAGE not in json.dumps(result)
    assert {row["element_id"] for row in result["elements"]} == {
        "s00000001:0",
        "s00000001:1",
        "s00000001:2",
    }
    assert result["elements"][1]["value"] == "日本語 42 ✅"
    assert peer.calls == [
        (
            "get_window_state",
            {
                "pid": 123,
                "window_id": 42,
                "include_screenshot": False,
                "max_elements": 2000,
                "max_depth": 25,
            },
        )
    ]
    with pytest.raises(ValueError, match="Element"):
        await gui.act(
            GUIClick(
                session_id=SID, observation_id=result["observation_id"], element_id="s00000001:4"
            ),
            owner="owner",
        )
    assert len(peer.calls) == 1


@pytest.mark.parametrize("kind", ["type", "replace", "click", "key"])
async def test_exact_target_action_and_consumption(kind):
    peer = CuaPeer()
    gui = GUIMCP(peer)
    result = await gui.observe(observation(), owner="owner")
    common = {
        "session_id": SID,
        "observation_id": result["observation_id"],
        "element_id": "s00000001:1",
    }
    if kind == "click":
        common["element_id"] = "s00000001:2"
        action = GUIClick(**common)
        expected_name, extra = "click", {"scope": "window", "delivery_mode": "background"}
    elif kind == "key":
        action = GUIKey(**common, keys=["CMD", "ENTER"])
        expected_name, extra = (
            "press_key",
            {
                "scope": "window",
                "delivery_mode": "background",
                "key": "return",
                "modifiers": ["cmd"],
            },
        )
    else:
        action = GUIType(**common, text="日本語 43 ✅", clear=kind == "replace")
        expected_name = "set_value" if kind == "replace" else "type_text"
        extra = (
            {"value": "日本語 43 ✅"}
            if kind == "replace"
            else {"text": "日本語 43 ✅", "scope": "window", "delivery_mode": "background"}
        )
    acted = await gui.act(action, owner="owner")
    assert peer.calls[-1] == (
        expected_name,
        {
            "pid": 123,
            "window_id": 42,
            "snapshot_id": "s00000001",
            "element_token": common["element_id"],
            **extra,
        },
    )
    assert acted["postcondition_verified"] is False
    assert acted["observation_consumed"] is True
    assert acted["structured_content"]["effect"] == "confirmed"
    with pytest.raises(ValueError, match="missing"):
        await gui.act(action, owner="owner")
    assert len(peer.calls) == 2


@pytest.mark.parametrize(
    "damage",
    [
        "pid",
        "window",
        "app",
        "incomplete",
        "truncated",
        "snapshot",
        "token",
        "indices",
        "parent",
        "cycle",
        "roots",
        "degraded",
    ],
)
async def test_invalid_observation_cannot_authorize_input(damage):
    peer = CuaPeer()

    def corrupt(value):
        if damage == "pid":
            value["pid"] = 124
        if damage == "window":
            value["window_id"] = 43
        if damage == "app":
            value["app_name"] = "Other"
        if damage == "incomplete":
            value["elements_complete"] = False
        if damage == "truncated":
            value["truncated"] = True
        if damage == "snapshot":
            value["snapshot_id"] = "not a snapshot"
        if damage == "token":
            value["elements"][2]["element_token"] = value["elements"][1]["element_token"]
        if damage == "indices":
            value["elements"][2]["element_index"] = 1
        if damage == "parent":
            value["elements"][1]["parent_index"] = 900
        if damage == "cycle":
            value["elements"][1]["parent_index"] = 1
        if damage == "roots":
            value["elements"][3]["role"] = "AXWindow"
        if damage == "degraded":
            value["degraded"] = True
        return value

    peer.damage = corrupt
    gui = GUIMCP(peer)
    result = await gui.observe(observation(), owner="owner")
    assert result["action_ready"] is False
    assert "observation_id" not in result and not gui.observations
    assert "private unrelated" not in json.dumps(result)
    assert UNTRUSTED_IMAGE not in json.dumps(result)
    assert [name for name, _ in peer.calls] == ["get_window_state"]


async def test_projected_query_only_authorizes_visible_elements():
    peer = CuaPeer()
    gui = GUIMCP(peer)
    seen = await gui.observe(observation(query="日本語"), owner="owner")
    assert {r["element_id"] for r in seen["elements"]} == {"s00000001:0", "s00000001:1"}
    with pytest.raises(ValueError, match="Element"):
        await gui.act(
            GUIClick(
                session_id=SID, observation_id=seen["observation_id"], element_id="s00000001:2"
            ),
            owner="owner",
        )
    assert len(peer.calls) == 1


@pytest.mark.parametrize("kind", ["missing_field", "non_text", "return", "missing_key_field"])
async def test_ambiguous_or_compound_input_is_not_dispatched(kind):
    peer = CuaPeer()
    gui = GUIMCP(peer)
    seen = await gui.observe(observation(), owner="owner")
    common = {"session_id": SID, "observation_id": seen["observation_id"]}
    if kind == "missing_key_field":
        action = GUIKey(**common, keys=["return"])
    else:
        action = GUIType(
            **common,
            text="do not send",
            element_id=None
            if kind == "missing_field"
            else "s00000001:2"
            if kind == "non_text"
            else "s00000001:1",
            press_return=kind == "return",
        )
    with pytest.raises(ValueError):
        await gui.act(action, owner="owner")
    assert len(peer.calls) == 1


async def test_schema_incompatibility_never_uses_another_provider():
    peer = CuaPeer()
    peer.contract_missing = "window_id"
    gui = GUIMCP(peer)
    with pytest.raises(ValueError, match="contract"):
        await gui.observe(observation(), owner="owner")
    assert not peer.calls


async def test_owner_staleness_and_global_serialization():
    peer = CuaPeer()
    gui = GUIMCP(peer)
    seen = await gui.observe(observation(), owner="owner")
    action = GUIType(
        session_id=SID, observation_id=seen["observation_id"], element_id="s00000001:1", text="test"
    )
    with pytest.raises(ValueError, match="another connection"):
        await gui.act(action, owner="other")
    gui.observations[SID].created = time.monotonic() - 61
    with pytest.raises(ValueError, match="stale"):
        await gui.act(action, owner="owner")
    seen = await gui.observe(observation(), owner="owner")
    action.observation_id = seen["observation_id"]
    action.element_id = "s00000002:1"
    entered, release = asyncio.Event(), asyncio.Event()
    original = peer.call

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    peer.call = delayed
    task = asyncio.create_task(gui.act(action, owner="owner"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(ValueError, match="busy"):
            await gui.observe(observation(), owner="owner")
    finally:
        release.set()
        await task
    assert not gui.observations


def test_provider_selection_is_explicit_and_validated():
    assert GUIObserve(session_id=SID, app="Editor", window_id=42).provider == "peekaboo"
    for extra in [
        {"provider": "cua"},
        {"provider": "peekaboo", "pid": 123},
        {"provider": "automatic"},
        {"provider": "peekaboo", "query": "body"},
    ]:
        with pytest.raises(ValidationError):
            GUIObserve(session_id=SID, app="Editor", window_id=42, **extra)
