import asyncio
import time

import pytest
from pydantic import ValidationError

from anywhere_computer.gui_mcp import GUIMCP, GUIClick, GUIKey, GUIObserve, GUIType


class Peer:
    def __init__(self):
        self.calls = []
        self.entries = {"a" * 32: object()}
        self.metadata = None
        self.app = None

    def status(self, session_id, *, owner):
        if owner != "owner":
            raise ValueError("Not owned")

    async def tools(self, session_id, *, owner, **kwargs):
        self.status(session_id, owner=owner)
        schemas = {
            "see": {"window_id": "integer", "app_target": "string"},
            "click": {"snapshot": "string", "on": "string"},
            "type": {"snapshot": "string", "text": "string", "clear": "boolean", "on": "string"},
            "press": {"snapshot": "string", "keys": "array"},
        }
        return {
            "tools": [
                {
                    "name": name,
                    "inputSchema": {
                        "properties": {
                            field: {"type": field_type}
                            for field, field_type in fields.items()
                        }
                    },
                }
                for name, fields in schemas.items()
            ]
        }

    async def call(self, session_id, name, arguments, *, owner):
        self.status(session_id, owner=owner)
        self.calls.append((name, arguments))
        if name == "see":
            self.app = arguments["app_target"]
        text = (
            f"Snapshot ID: snapshot-1\nApplication: {self.app}\n  elem_1 - button"
            if name == "see"
            else "done"
        )
        return {
            "content": [{"type": "text", "text": text}],
            "isError": False,
            "_meta": ({
                "snapshot_id": "snapshot-1",
                "target_receipt": {"window_id": arguments["window_id"]},
                **(self.metadata or {}),
            } if name == "see" else self.metadata),
        }


async def test_observation_owner_staleness_and_consumption():
    peer = Peer()
    gui = GUIMCP(peer)
    sid = "a" * 32
    seen = await gui.observe(
        GUIObserve(session_id=sid, app="Calculator", window_id=42), owner="owner"
    )
    with pytest.raises(ValueError, match="Not owned"):
        await gui.observe(GUIObserve(session_id=sid, app="Other", window_id=42), owner="other")
    assert gui.observations[sid].identity == seen["observation_id"]
    action = GUIClick(session_id=sid, observation_id=seen["observation_id"], element_id="elem_1")
    with pytest.raises(ValueError, match="another connection"):
        await gui.act(action, owner="other")
    assert len(peer.calls) == 1
    gui.observations[sid].created = time.monotonic() - 61
    with pytest.raises(ValueError, match="stale"):
        await gui.act(action, owner="owner")
    assert len(peer.calls) == 1
    seen = await gui.observe(
        GUIObserve(session_id=sid, app="Calculator", window_id=42), owner="owner"
    )
    action.observation_id = seen["observation_id"]
    await gui.act(action, owner="owner")
    assert peer.calls[-1] == ("click", {"on": "elem_1", "snapshot": "snapshot-1"})
    with pytest.raises(ValueError, match="missing"):
        await gui.act(action, owner="owner")
    assert len(peer.calls) == 3


async def test_target_and_keys_are_explicit_and_only_valid_elements_dispatch():
    peer = Peer()
    gui = GUIMCP(peer)
    sid = "a" * 32
    seen = await gui.observe(
        GUIObserve(session_id=sid, app="Calculator", window_id=42), owner="owner"
    )
    common = {"session_id": sid, "observation_id": seen["observation_id"]}
    with pytest.raises(ValueError, match="Element"):
        await gui.act(GUIClick(**common, element_id="invented"), owner="owner")
    with pytest.raises(ValueError, match="key"):
        await gui.act(GUIKey(**common, keys=["unknown"]), owner="owner")
    assert len(peer.calls) == 1
    await gui.act(GUIType(**common, text="12+30"), owner="owner")
    assert all(name != "app" for name, _ in peer.calls)
    assert peer.calls[-1] == ("type", {"text": "12+30", "clear": False, "snapshot": "snapshot-1"})


async def test_coordinate_metadata_is_validated_and_unrelated_metadata_omitted():
    peer = Peer()
    peer.metadata = {
        "private": "omit-me",
        "coordinate_context": {
            "version": 1,
            "logical_space": "global_display_points",
            "origin": "top_left",
            "logical_bounds": {"x": 20, "y": 30, "width": 100, "height": 50},
            "delivered_image_size": {"width": 200, "height": 100},
            "reference_id": "snapshot-1",
            "private": "also-omit",
        },
    }
    gui = GUIMCP(peer)
    args = GUIObserve(session_id="a" * 32, app="Calculator", window_id=42)
    result = await gui.observe(args, owner="owner")
    assert result["coordinate_status"] == "validated"
    assert result["coordinate_context"]["logical_bounds"]["width"] == 100
    assert "omit" not in str(result)
    peer.metadata["coordinate_context"]["reference_id"] = "different"
    result = await gui.observe(args, owner="owner")
    assert result["action_ready"] is False
    assert result["reason"] == "coordinate_reference_mismatch"


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (["ESCAPE"], "Escape"),
        (["ESC"], "Escape"),
        (["CMD", "ENTER"], "cmd+Return"),
    ],
)
async def test_gui_key_normalizes_documented_case_and_aliases(keys, expected):
    peer = Peer()
    gui = GUIMCP(peer)
    sid = "a" * 32
    seen = await gui.observe(
        GUIObserve(session_id=sid, app="Calculator", window_id=42), owner="owner"
    )
    await gui.act(
        GUIKey(session_id=sid, observation_id=seen["observation_id"], keys=keys), owner="owner"
    )
    assert peer.calls[-1] == ("press", {"keys": [expected], "snapshot": "snapshot-1"})


async def test_snapshot_input_cannot_be_interleaved_by_another_typed_session():
    peer = Peer()
    peer.entries["b" * 32] = object()
    gui = GUIMCP(peer)
    first = await gui.observe(
        GUIObserve(session_id="a" * 32, app="Editor", window_id=42), owner="owner"
    )
    second = await gui.observe(
        GUIObserve(session_id="b" * 32, app="Browser", window_id=42), owner="owner"
    )
    focused, release = asyncio.Event(), asyncio.Event()
    original = peer.call

    async def delayed(session_id, name, arguments, *, owner):
        result = await original(session_id, name, arguments, owner=owner)
        if name == "type":
            focused.set()
            await release.wait()
        return result

    peer.call = delayed
    action = asyncio.create_task(
        gui.act(
            GUIType(session_id="a" * 32, observation_id=first["observation_id"], text="hello"),
            owner="owner",
        )
    )
    try:
        await asyncio.wait_for(focused.wait(), 1)
        count = len(peer.calls)
        with pytest.raises(ValueError, match="busy"):
            await gui.observe(
                GUIObserve(session_id="b" * 32, app="Browser", window_id=42), owner="owner"
            )
        assert len(peer.calls) == count
    finally:
        release.set()
        await action
    with pytest.raises(ValueError, match="missing"):
        await gui.act(
            GUIKey(session_id="b" * 32, observation_id=second["observation_id"], keys=["return"]),
            owner="owner",
        )
    assert peer.calls[-1][0] == "type"
    await gui.observe(GUIObserve(session_id="b" * 32, app="Browser", window_id=42), owner="owner")


async def test_cancelled_observation_releases_typed_gui_slot():
    peer = Peer()
    gui = GUIMCP(peer)
    started = asyncio.Event()
    original = peer.call

    async def waiting(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    peer.call = waiting
    observation = asyncio.create_task(
        gui.observe(GUIObserve(session_id="a" * 32, app="Editor", window_id=42), owner="owner")
    )
    await asyncio.wait_for(started.wait(), 1)
    observation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observation
    peer.call = original
    assert (
        await gui.observe(
            GUIObserve(session_id="a" * 32, app="Editor", window_id=42), owner="owner"
        )
    )["action_ready"]


async def test_engine_records_busy_without_replaying_input(tmp_path):
    from anywhere_computer.engine import Engine
    from anywhere_computer.models import Request

    engine = Engine(tmp_path)
    peer = Peer()
    engine.gui_mcp = GUIMCP(peer)
    focused, release = asyncio.Event(), asyncio.Event()
    original = peer.call

    async def delayed(session_id, name, arguments, *, owner):
        result = await original(session_id, name, arguments, owner=owner)
        if name == "type":
            focused.set()
            await release.wait()
        return result

    running = None
    try:
        observed = await engine.execute(
            Request(
                operation_id="1" * 32,
                tool="gui_observe",
                arguments={"session_id": "a" * 32, "app": "Editor", "window_id": 42},
            ),
            peer="owner",
        )
        peer.call = delayed
        arguments = {
            "session_id": "a" * 32,
            "observation_id": observed.data["observation_id"],
            "text": "hello",
        }
        running = asyncio.create_task(
            engine.execute(
                Request(operation_id="2" * 32, tool="gui_type", arguments=arguments), peer="owner"
            )
        )
        await asyncio.wait_for(focused.wait(), 1)
        rejected = Request(operation_id="3" * 32, tool="gui_type", arguments=arguments)
        busy = await engine.execute(rejected, peer="owner")
        assert busy.state == "failed" and "busy" in busy.error
        release.set()
        assert (await running).state == "completed"
        count = len(peer.calls)
        assert (await engine.execute(rejected, peer="owner")) == busy
        assert len(peer.calls) == count
        recovered = await engine.execute(
            Request(
                operation_id="4" * 32,
                tool="operations_get",
                arguments={"operation_id": rejected.operation_id},
            ),
            peer="owner",
        )
        assert recovered.data["state"] == "failed"
        assert [name for name, _ in peer.calls].count("type") == 1
    finally:
        release.set()
        if running is not None:
            await running
        await engine.close()


async def test_observe_captures_exact_window_without_foreground_focus():
    peer = Peer()
    gui = GUIMCP(peer)
    result = await gui.observe(
        GUIObserve(session_id="a" * 32, app="Editor", window_id=42), owner="owner"
    )
    assert peer.calls == [("see", {"app_target": "Editor", "window_id": 42})]
    assert result["action_ready"]


@pytest.mark.parametrize("application", ["Other", "", "Editor\nApplication: Other"])
async def test_observation_of_different_frontmost_app_cannot_authorize_input(application):
    peer = Peer()
    original = peer.call

    async def switched(session_id, name, arguments, *, owner):
        result = await original(session_id, name, arguments, owner=owner)
        if name == "see":
            result["content"][0]["text"] = (
                f"Snapshot ID: snapshot-1\nApplication: {application}\n  elem_1 - button"
            )
        return result

    peer.call = switched
    gui = GUIMCP(peer)
    result = await gui.observe(
        GUIObserve(session_id="a" * 32, app="Editor", window_id=42), owner="owner"
    )
    assert result["action_ready"] is False
    assert result["reason"] == "observed_application_mismatch"
    assert not gui.observations


async def test_exact_window_input_is_snapshot_bound_without_foreground_focus():
    peer = Peer()
    gui = GUIMCP(peer)
    sid = "a" * 32
    seen = await gui.observe(GUIObserve(session_id=sid, app="Editor", window_id=42), owner="owner")
    assert peer.calls == [("see", {"app_target": "Editor", "window_id": 42})]
    assert seen["window_id"] == 42
    common = {"session_id": sid, "observation_id": seen["observation_id"]}
    with pytest.raises(ValueError, match="Element"):
        await gui.act(GUIType(**common, text="wrong", element_id="missing"), owner="owner")
    with pytest.raises(ValueError, match="Return"):
        await gui.act(GUIType(**common, text="wrong", press_return=True), owner="owner")
    assert len(peer.calls) == 1
    result = await gui.act(
        GUIType(**common, text="日本語 🚀", clear=True, element_id="elem_1"), owner="owner"
    )
    assert peer.calls[-1] == (
        "type",
        {"snapshot": "snapshot-1", "text": "日本語 🚀", "clear": True, "on": "elem_1"},
    )
    assert result["focus_may_change_externally"] is None
    with pytest.raises(ValueError, match="missing"):
        await gui.act(GUIType(**common, text="replay"), owner="owner")


async def test_exact_window_keys_use_press_receipt_and_preserve_provider_error():
    peer = Peer()
    gui = GUIMCP(peer)
    seen = await gui.observe(
        GUIObserve(session_id="a" * 32, app="Editor", window_id=42), owner="owner"
    )
    original = peer.call

    async def refused(*args, **kwargs):
        result = await original(*args, **kwargs)
        return {
            **result,
            "isError": True,
            "_meta": {
                "state": "dispatched_unverified",
                "mutation_dispatched": True,
                "retry_safe": False,
            },
        }

    peer.call = refused
    result = await gui.act(
        GUIKey(session_id="a" * 32, observation_id=seen["observation_id"], keys=["cmd", "ENTER"]),
        owner="owner",
    )
    assert peer.calls[-1] == ("press", {"snapshot": "snapshot-1", "keys": ["cmd+Return"]})
    assert result["is_error"] is True
    assert result["provider_diagnostics"]["retry_safe"] is False
    assert not gui.observations
    assert all(name != "app" for name, _ in peer.calls)


async def test_unsupported_exact_window_provider_never_falls_back():
    peer = Peer()

    async def old_tools(*args, **kwargs):
        return {"tools": [{"name": "see", "inputSchema": {"properties": {}}}]}

    peer.tools = old_tools
    gui = GUIMCP(peer)
    with pytest.raises(ValueError, match="snapshot-bound see"):
        await gui.observe(
            GUIObserve(session_id="a" * 32, app="Editor", window_id=42), owner="owner"
        )
    assert not peer.calls and not gui.observations


@pytest.mark.parametrize("missing", ["click", "type", "press"])
async def test_observe_rejects_input_without_snapshot_contract(missing):
    peer = Peer()
    original = peer.tools

    async def incomplete(*args, **kwargs):
        page = await original(*args, **kwargs)
        if kwargs.get("name") == missing:
            for row in page["tools"]:
                if row["name"] == missing:
                    row["inputSchema"]["properties"].pop("snapshot")
        return page

    peer.tools = incomplete
    with pytest.raises(ValueError, match=f"snapshot-bound {missing}"):
        await GUIMCP(peer).observe(
            GUIObserve(session_id="a" * 32, app="Editor", window_id=42), owner="owner")
    assert not peer.calls


async def test_duplicate_provider_input_contract_never_authorizes_observation():
    peer = Peer()
    original = peer.tools

    async def duplicate(*args, **kwargs):
        page = await original(*args, **kwargs)
        if kwargs.get("name") == "type":
            row = next(row for row in page["tools"] if row["name"] == "type")
            page["tools"].append(row.copy())
        return page

    peer.tools = duplicate
    with pytest.raises(ValueError, match="Duplicate type contract"):
        await GUIMCP(peer).observe(
            GUIObserve(session_id="a" * 32, app="Editor", window_id=42), owner="owner")
    assert not peer.calls


@pytest.mark.parametrize(("meta", "reason"), [
    ({"snapshot_id": "different"}, "snapshot_reference_mismatch"),
    ({"target_receipt": {}}, "observed_window_unavailable"),
    ({"target_receipt": {"window_id": 43}}, "observed_window_mismatch"),
    ({"target_receipt": {"window_id": True}}, "observed_window_unavailable"),
])
async def test_observation_requires_matching_provider_snapshot_and_window(meta, reason):
    peer = Peer()
    peer.metadata = meta
    gui = GUIMCP(peer)
    result = await gui.observe(
        GUIObserve(session_id="a" * 32, app="Editor", window_id=42), owner="owner")
    assert result["action_ready"] is False
    assert result["reason"] == reason
    assert not gui.observations
    assert [name for name, _ in peer.calls] == ["see"]


async def test_exact_window_change_invalidates_previous_window_and_refusal_clears_it():
    peer = Peer()
    gui = GUIMCP(peer)
    sid = "a" * 32
    first = await gui.observe(GUIObserve(session_id=sid, app="Editor", window_id=42), owner="owner")
    await gui.observe(GUIObserve(session_id=sid, app="Editor", window_id=43), owner="owner")
    with pytest.raises(ValueError, match="missing"):
        await gui.act(
            GUIType(session_id=sid, observation_id=first["observation_id"], text="wrong window"),
            owner="owner",
        )
    original = peer.call

    async def refusal(*args, **kwargs):
        result = await original(*args, **kwargs)
        return {**result, "isError": True}

    peer.call = refusal
    result = await gui.observe(
        GUIObserve(session_id=sid, app="Editor", window_id=42), owner="owner"
    )
    assert result["is_error"] and sid not in gui.observations
    assert peer.calls == [
        ("see", {"app_target": "Editor", "window_id": target}) for target in (42, 43, 42)
    ]


async def test_missing_window_cannot_dispatch_foreground_input():
    import tempfile
    from pathlib import Path

    from anywhere_computer.engine import Engine
    from anywhere_computer.models import Request

    with tempfile.TemporaryDirectory() as directory:
        engine = Engine(Path(directory))
        peer = Peer()
        engine.gui_mcp = GUIMCP(peer)
        try:
            result = await engine.execute(
                Request(
                    operation_id="9" * 32,
                    tool="gui_observe",
                    arguments={"session_id": "a" * 32, "app": "Editor"},
                ),
                peer="owner",
            )
            assert result.state == "failed"
            assert "window_id" in result.error
            assert not peer.calls and not engine.gui_mcp.observations
        finally:
            await engine.close()
    with pytest.raises(ValidationError):
        GUIObserve(session_id="a" * 32, app="Editor")


async def test_provider_acknowledgement_does_not_claim_verified_input():
    gui = GUIMCP(Peer())
    seen = await gui.observe(
        GUIObserve(session_id="a" * 32, app="Editor", window_id=42), owner="owner"
    )
    result = await gui.act(
        GUIType(session_id="a" * 32, observation_id=seen["observation_id"], text="日本語 ✅"),
        owner="owner",
    )
    assert result["postcondition_verified"] is False
    assert "Observe the same window" in result["next_action"]


def test_live_text_acceptance_requires_unique_editable_body():
    import runpy
    from pathlib import Path

    match = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/verify_gui_http.py'))[
        'text_field'
    ]
    body = ('AXTextArea (1 found, 1 actionable):\n'
            '  elem_2 - "test" - value: "日本語 ✅" - [value settable]\n\n'
            'AXWindow (1 found, 0 actionable):\n'
            '  elem_0 - "test" - value: "日本語 ✅" - [not actionable]\n')
    assert match(body, '日本語 ✅') == 'elem_2'
    assert match(body, 'different') is None
    assert match(body.replace('AXTextArea', 'AXStaticText'), '日本語 ✅') is None
    assert match(body.replace('[value settable]', '[not actionable]'), '日本語 ✅') is None
    duplicate = body.replace('\n\nAXWindow',
        '\n  elem_3 - "test" - value: "日本語 ✅" - [value settable]\n\nAXWindow')
    assert match(duplicate, '日本語 ✅') is None
