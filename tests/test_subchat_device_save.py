import asyncio
import base64
import hashlib
import os
from dataclasses import replace

import httpx
import pytest

from anywhere_computer.authorization import AuthorizationStore, GrantIdentity, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.engine import Engine
from anywhere_computer.http_mcp import HTTPMCP
from anywhere_computer.models import Reply, Request
from anywhere_computer.subchat_device_adapters import RoutedSaveTarget, SubchatSaveSource
from anywhere_computer.subchat_device_save import DeviceSave, SaveJournal, SaveRunner
from anywhere_computer.subchat_gateway import LazySubchatGateway, SubchatGatewayConfig


@pytest.mark.parametrize("path", [r"C:\Users\owner\result.bin",
                                  r"\\server\share\result.bin"])
def test_remote_save_accepts_canonical_windows_destination(path):
    args = DeviceSave(source_operation_id="a" * 32,
                      sandbox_link="sandbox:/result.bin", device_id="b" * 32,
                      destination_path=path)
    assert args.destination_path == path


@pytest.mark.parametrize("path", [r"C:result.bin", r"C:\dir\..\result.bin",
                                  "C:/dir/result.bin", r"C:\dir\\result.bin"])
def test_remote_save_rejects_ambiguous_windows_destination(path):
    with pytest.raises(ValueError, match="canonical absolute path"):
        DeviceSave(source_operation_id="a" * 32,
                   sandbox_link="sandbox:/result.bin", device_id="b" * 32,
                   destination_path=path)


@pytest.mark.skipif(os.name == "nt", reason="Windows paths are local on Windows")
def test_local_save_rejects_foreign_windows_destination():
    with pytest.raises(ValueError, match="canonical absolute path"):
        DeviceSave(source_operation_id="a" * 32,
                   sandbox_link="sandbox:/result.bin", device_id="local",
                   destination_path=r"C:\Users\owner\result.bin")


def test_remote_save_reconciles_target_canonical_path_from_original_request(tmp_path):
    grant = GrantIdentity(grant_id="grant", owner="owner", device="device",
                          client="client", resource="https://example.test/mcp",
                          tools=frozenset({"subchat_save_file"}))
    args = DeviceSave(source_operation_id="a" * 32,
                      sandbox_link="sandbox:/result.bin", device_id="b" * 32,
                      destination_path="/alias/result.bin")
    digest = hashlib.sha256(b"x").hexdigest()
    observed = {"requested_path": args.destination_path,
                "path": "/canonical/result.bin", "total_bytes": 1,
                "sha256": digest, "state": "receiving", "received_bytes": 0}
    journal = SaveJournal(tmp_path / "saves.sqlite3")
    try:
        journal.claim("c" * 32, args, grant=grant, account_id="account",
                      route_digest="d" * 64)
        journal.verified_source("c" * 32, total=1, sha256=digest)
        journal.dispatched("c" * 32, stage="uploading", target_operation_id="e" * 32)
        record = journal.claim("c" * 32, args, grant=grant, account_id="account",
                               route_digest="d" * 64)
        assert SaveRunner._target_state(observed, args, record) == ("receiving", 0)
        with pytest.raises(ValueError, match="destination"):
            SaveRunner._target_state({**observed, "requested_path": "/other/result.bin"},
                                     args, record)
        with pytest.raises(ValueError, match="Target status"):
            journal.reconciled("c" * 32, target_operation_id="e" * 32,
                               observed={**observed, "requested_path": "/other/result.bin"})
        journal.reconciled("c" * 32, target_operation_id="e" * 32, observed=observed)
    finally:
        journal.close()


def test_save_journal_binds_principal_account_route_and_source_across_restart(tmp_path):
    grant = GrantIdentity(grant_id="first", owner="owner", device="device",
                          client="client", resource="https://example.test/mcp",
                          tools=frozenset({"subchat_save_file"}))
    renewed = replace(grant, grant_id="second")
    args = DeviceSave(source_operation_id="a" * 32, sandbox_link="sandbox:/answer.csv",
                      device_id="b" * 32, destination_path="/tmp/answer.csv")
    database = tmp_path / "saves.sqlite3"
    journal = SaveJournal(database)
    first = journal.claim("c" * 32, args, grant=grant, account_id="account",
                          route_digest="d" * 64)
    assert first["stage"] == "claimed"
    assert first["transfer_id"] != "c" * 32
    journal.verified_source("c" * 32, total=3, sha256=hashlib.sha256(b"abc").hexdigest())
    journal.dispatched("c" * 32, stage="uploading", target_operation_id="e" * 32)
    journal.close()

    journal = SaveJournal(database)
    try:
        resumed = journal.claim("c" * 32, args, grant=renewed, account_id="account",
                                route_digest="d" * 64)
        assert resumed["target_operation_id"] == "e" * 32
        with pytest.raises(ValueError, match="unresolved"):
            journal.dispatched("c" * 32, stage="uploading", target_operation_id="f" * 32)
        for changed in (
            {"account_id": "other"}, {"route_digest": "f" * 64},
            {"grant": replace(grant, owner="other")},
            {"args": args.model_copy(update={"destination_path": "/tmp/other.csv"})},
        ):
            with pytest.raises(ValueError, match="bound"):
                journal.claim("c" * 32, changed.get("args", args),
                              grant=changed.get("grant", renewed),
                              account_id=changed.get("account_id", "account"),
                              route_digest=changed.get("route_digest", "d" * 64))
        observed = {"path": args.destination_path, "total_bytes": 3,
                    "sha256": hashlib.sha256(b"abc").hexdigest(),
                    "state": "receiving", "received_bytes": 0}
        journal.reconciled("c" * 32, target_operation_id="e" * 32,
                           observed=observed)
        journal.dispatched("c" * 32, stage="commit_unknown",
                           target_operation_id="1" * 32)
        with pytest.raises(ValueError, match="publication is unverified"):
            journal.reconciled("c" * 32, target_operation_id="1" * 32,
                               observed=observed)
        with pytest.raises(ValueError, match="unresolved"):
            journal.dispatched("c" * 32, stage="commit_unknown",
                               target_operation_id="2" * 32)
        journal.reconciled("c" * 32, target_operation_id="1" * 32,
                           observed={**observed, "state": "complete",
                                     "received_bytes": 3,
                                     "publication_verified": True})
        assert journal.claim("c" * 32, args, grant=renewed, account_id="account",
                             route_digest="d" * 64)["stage"] == "completed"
        with pytest.raises(ValueError, match="another save ID"):
            journal.claim("2" * 32, args, grant=renewed, account_id="account",
                          route_digest="d" * 64)
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_runner_reconciles_lost_chunk_and_commit_without_replay(tmp_path):
    grant = GrantIdentity(grant_id="grant", owner="owner", device="device",
                          client="client", resource="https://example.test/mcp",
                          tools=frozenset({"subchat_save_file"}))
    args = DeviceSave(source_operation_id="a" * 32, sandbox_link="sandbox:/answer.csv",
                      device_id="b" * 32, destination_path="/tmp/answer.csv")
    content = b"answer content"
    calls = []
    route = "d" * 64

    async def authorize():
        return grant, route

    async def download(actual, offset, limit):
        assert actual == args and limit == 262_144
        calls.append(("download", offset))
        return content[offset:offset + limit], len(content), True

    class Target:
        def __init__(self):
            self.data = None

        async def status(self, device_id, transfer_id):
            assert device_id == args.device_id
            return self.data.copy() if self.data is not None else None

        async def operation(self, device_id, operation_id):
            calls.append(("inspect_operation", operation_id))
            return None

        async def begin(self, device_id, operation_id, transfer_id, path, total, sha256):
            calls.append(("begin", operation_id))
            self.data = {"path": path, "total_bytes": total, "sha256": sha256,
                         "state": "receiving", "received_bytes": 0}

        async def chunk(self, device_id, operation_id, transfer_id, offset, chunk):
            calls.append(("chunk", operation_id))
            assert offset == 0 and chunk == content
            self.data["received_bytes"] = len(chunk)
            raise ConnectionError("response lost after target stored chunk")

        async def commit(self, device_id, operation_id, transfer_id):
            calls.append(("commit", operation_id))
            self.data["state"] = "complete"
            self.data["publication_verified"] = True
            raise ConnectionError("response lost after target published")

    target = Target()
    database = tmp_path / "saves.sqlite3"
    journal = SaveJournal(database)
    runner = SaveRunner(journal, authorize=authorize, download=download,
                        target=target, account_id="account",
                        spool_directory=tmp_path / "spool")
    assert (await runner.advance("c" * 32, args))["state"] == "running"
    assert (await runner.advance("c" * 32, args))["state"] == "unknown"
    assert [item[0] for item in calls] == ["download", "begin", "chunk"]
    journal.close()
    journal = SaveJournal(database)
    runner = SaveRunner(journal, authorize=authorize, download=download,
                        target=target, account_id="account",
                        spool_directory=tmp_path / "spool")
    assert (await runner.advance("c" * 32, args))["state"] == "unknown"
    assert [item[0] for item in calls] == ["download", "begin", "chunk", "commit"]
    assert (await runner.advance("c" * 32, args))["state"] == "completed"
    assert [item[0] for item in calls] == ["download", "begin", "chunk", "commit"]
    journal.close()


@pytest.mark.asyncio
async def test_runner_rejects_route_change_before_target_write(tmp_path):
    grant = GrantIdentity(grant_id="grant", owner="owner", device="device",
                          client="client", resource="https://example.test/mcp",
                          tools=frozenset({"subchat_save_file"}))
    args = DeviceSave(source_operation_id="a" * 32, sandbox_link="sandbox:/answer.csv",
                      device_id="b" * 32, destination_path="/tmp/answer.csv")
    calls = 0

    async def authorize():
        nonlocal calls
        calls += 1
        return grant, ("d" if calls == 1 else "e") * 64

    async def download(actual, offset, limit):
        return b"a", 1, True

    class Target:
        async def status(self, *_):
            raise AssertionError("Target must not be contacted")

    journal = SaveJournal(tmp_path / "saves.sqlite3")
    try:
        runner = SaveRunner(journal, authorize=authorize, download=download,
                            target=Target(), account_id="account",
                            spool_directory=tmp_path / "spool")
        with pytest.raises(PermissionError, match="route changed"):
            await runner.advance("c" * 32, args)
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_runner_pauses_existing_save_when_device_route_changes(tmp_path):
    grant = GrantIdentity(grant_id="grant", owner="owner", device="device",
                          client="client", resource="https://example.test/mcp",
                          tools=frozenset({"subchat_save_file"}))
    args = DeviceSave(source_operation_id="a" * 32, sandbox_link="sandbox:/answer.csv",
                      device_id="b" * 32, destination_path="/tmp/answer.csv")
    journal = SaveJournal(tmp_path / "saves.sqlite3")
    journal.claim("c" * 32, args, grant=grant, account_id="account",
                  route_digest="d" * 64)

    async def authorize():
        return grant, "e" * 64

    async def unreachable_download(*_):
        raise AssertionError("Provider must not be contacted")

    runner = SaveRunner(journal, authorize=authorize, download=unreachable_download,
                        target=None, account_id="account",
                        spool_directory=tmp_path / "spool")
    try:
        assert await runner.advance("c" * 32, args) == {
            "state": "paused", "reason": "selected_device_route_changed"}
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_runner_publishes_via_real_local_router_and_uploads(tmp_path):
    scopes = frozenset({"subchat_save_file", "upload_begin", "upload_chunk",
                        "upload_status", "upload_commit", "operations_get"})
    grant = GrantIdentity(grant_id="grant", owner="owner", device="device",
                          client="client", resource="https://example.test/mcp",
                          tools=scopes)
    engine = Engine(tmp_path / "engine")

    async def catalog():
        return engine.catalog(scopes)

    target = RoutedSaveTarget(tmp_path / "devices", catalog,
                              engine.execute, lambda: grant, "local")
    content = b"verified final answer file"
    destination = tmp_path / "answer.bin"
    args = DeviceSave(source_operation_id="a" * 32,
                      sandbox_link="sandbox:/answer.bin", device_id="local",
                      destination_path=str(destination))

    async def authorize():
        return grant, await target.authorize()

    async def download(actual, offset, limit):
        assert actual == args
        return content[offset:offset + limit], len(content), True

    journal = SaveJournal(tmp_path / "saves.sqlite3")
    try:
        runner = SaveRunner(journal, authorize=authorize, download=download,
                            target=target, account_id="account",
                            spool_directory=tmp_path / "spool")
        states = []
        for _ in range(5):
            result = await runner.advance("c" * 32, args)
            states.append(result["state"])
            if result["state"] == "completed":
                break
        assert states == ["running", "running", "unknown", "completed"]
        assert destination.read_bytes() == content
        assert "content_base64" not in result
    finally:
        journal.close()
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["grant", "route"])
async def test_upload_rechecks_authorization_after_target_catalog(tmp_path, monkeypatch,
                                                                   changed):
    scopes = frozenset({"subchat_save_file", "upload_begin", "upload_chunk",
                        "upload_status", "upload_commit", "operations_get"})
    grant = GrantIdentity(grant_id="grant", owner="owner", device="device",
                          client="client", resource="https://example.test/mcp",
                          tools=scopes)
    engine = Engine(tmp_path / "engine")
    entered, release = asyncio.Event(), asyncio.Event()
    dispatched = []

    async def catalog():
        entered.set()
        await release.wait()
        return engine.catalog(scopes)

    async def execute(request):
        dispatched.append(request)
        return await engine.execute(request)

    target = RoutedSaveTarget(tmp_path / "devices", catalog, execute,
                              lambda: grant, "local")
    route_identity = "first-route"
    monkeypatch.setattr(target, "route_digest", lambda: route_identity)
    try:
        request = asyncio.create_task(target.begin(
            "local", "b" * 32, "c" * 32, str(tmp_path / "answer.bin"), 1,
            hashlib.sha256(b"x").hexdigest()))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if changed == "grant":
                grant = replace(grant, tools=frozenset())
            else:
                route_identity = "changed-route"
        finally:
            release.set()
        with pytest.raises(PermissionError, match=(
                "Direct save is not granted" if changed == "grant" else
                "Selected device route changed before upload dispatch")):
            await request
        assert dispatched == []
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_local_save_reconciles_canonical_target_path_without_replaying_begin(tmp_path):
    scopes = frozenset({"subchat_save_file", "upload_begin", "upload_chunk",
                        "upload_status", "upload_commit", "operations_get"})
    grant = GrantIdentity(grant_id="grant", owner="owner", device="device",
                          client="client", resource="https://example.test/mcp",
                          tools=scopes)
    actual_parent = tmp_path / "actual"
    actual_parent.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual_parent, target_is_directory=True)
    destination = alias / "answer.bin"
    engine = Engine(tmp_path / "engine")

    async def catalog():
        return engine.catalog(scopes)

    target = RoutedSaveTarget(tmp_path / "devices", catalog,
                              engine.execute, lambda: grant, "local")
    args = DeviceSave(source_operation_id="a" * 32,
                      sandbox_link="sandbox:/answer.bin", device_id="local",
                      destination_path=str(destination))

    async def authorize():
        return grant, await target.authorize()

    async def download(_actual, offset, limit):
        content = b"verified answer"
        return content[offset:offset + limit], len(content), True

    journal = SaveJournal(tmp_path / "saves.sqlite3")
    try:
        runner = SaveRunner(journal, authorize=authorize, download=download,
                            target=target, account_id="account",
                            spool_directory=tmp_path / "spool")
        states = [(await runner.advance("c" * 32, args))["state"] for _ in range(4)]
        assert states == ["running", "running", "unknown", "completed"]
        assert (actual_parent / "answer.bin").read_bytes() == b"verified answer"
        steps = journal.connection.execute(
            "SELECT stage,COUNT(*) FROM subchat_device_save_steps "
            "GROUP BY stage ORDER BY stage").fetchall()
        assert steps == [("commit_unknown", 1), ("uploading", 2)]
    finally:
        journal.close()
        await engine.close()


@pytest.mark.asyncio
async def test_provider_adapter_keeps_base64_inside_service():
    grant = GrantIdentity(grant_id="grant", owner="owner", device="device",
                          client="client", resource="https://example.test/mcp",
                          tools=frozenset({"subchat_save_file"}))
    args = DeviceSave(source_operation_id="a" * 32,
                      sandbox_link="sandbox:/answer.bin", device_id="local",
                      destination_path="/tmp/answer.bin")

    class Gateway:
        account_id = "account"

        def owner_for_request(self, request, **kwargs):
            assert request.arguments["operation_id"] == args.source_operation_id
            return kwargs["stable_owner"]

        async def execute(self, owner, request, granted):
            assert granted == frozenset({"subchat_download_file"})
            assert request.arguments["max_bytes"] == 262_144
            assert request.arguments["offset"] == 0
            return Reply(operation_id=request.operation_id, state="completed", data={
                "content_base64": base64.b64encode(b"abc").decode(),
                "file_size_bytes": 3, "offset": 0, "eof": True,
            })

    source = SubchatSaveSource(Gateway(), lambda: grant, lambda *_: True)
    assert await source.download(args, 0, 262_144) == (b"abc", 3, True)


@pytest.mark.asyncio
async def test_real_router_lost_chunk_commit_and_revocation(tmp_path):
    scopes = frozenset({"subchat_save_file", "upload_begin", "upload_chunk",
                        "upload_status", "upload_commit", "operations_get"})
    grant = GrantIdentity(grant_id="grant", owner="owner", device="device",
                          client="client", resource="https://example.test/mcp",
                          tools=scopes)
    engine = Engine(tmp_path / "engine")

    async def catalog():
        return engine.catalog(grant.tools)

    class Target(RoutedSaveTarget):
        chunk_calls = 0
        commit_calls = 0

        async def chunk(self, *args):
            self.chunk_calls += 1
            await super().chunk(*args)
            raise ConnectionError("lost chunk response")

        async def commit(self, *args):
            self.commit_calls += 1
            await super().commit(*args)
            raise ConnectionError("lost commit response")

    target = Target(tmp_path / "devices", catalog, engine.execute,
                    lambda: grant, "local")
    data = b"verified bytes"
    path = tmp_path / "result.bin"
    args = DeviceSave(source_operation_id="a" * 32,
                      sandbox_link="sandbox:/result.bin", device_id="local",
                      destination_path=str(path))

    async def authorize():
        return grant, await target.authorize()

    async def download(actual, offset, limit):
        return data[offset:offset + limit], len(data), True

    journal = SaveJournal(tmp_path / "saves.sqlite3")
    try:
        runner = SaveRunner(journal, authorize=authorize, download=download,
                            target=target, account_id="account",
                            spool_directory=tmp_path / "spool")
        assert (await runner.advance("c" * 32, args))["state"] == "running"
        grant = replace(grant, tools=frozenset())
        with pytest.raises(PermissionError):
            await runner.advance("c" * 32, args)
        assert target.chunk_calls == 0 and not path.exists()
        grant = replace(grant, tools=scopes)
        assert (await runner.advance("c" * 32, args))["state"] == "unknown"
        assert target.chunk_calls == 1
        assert (await runner.advance("c" * 32, args))["state"] == "unknown"
        assert target.commit_calls == 1
        result = await runner.advance("c" * 32, args)
        assert result["state"] == "completed"
        assert target.chunk_calls == 1 and target.commit_calls == 1
        assert path.read_bytes() == data
    finally:
        journal.close()
        await engine.close()


@pytest.mark.asyncio
async def test_https_save_scope_catalog_and_same_id_delivery(tmp_path, monkeypatch):
    scopes = frozenset({"subchat_save_file", "upload_begin", "upload_chunk",
                        "upload_status", "upload_commit", "operations_get"})
    grant = GrantIdentity(grant_id="grant", owner="owner", device="device",
                          client="client", resource="https://example.test/mcp",
                          tools=scopes)

    class Store:
        database = tmp_path / "http-server" / "authorization" / "authorization.sqlite3"

        def current_grant(self, identity):
            return grant if identity == grant.grant_id else None

        def same_principal_grant(self, active, candidate):
            return active.grant_id == candidate

    config = SubchatGatewayConfig(
        profile=str(tmp_path / "Default"), ledger=str(tmp_path / "subchat-ledger"),
        account_id="account", consent="ordinary-chat-browser-control-approved")
    gateway = LazySubchatGateway(config, owner="owner")
    content = b"verified bytes"

    async def download(owner, request, granted):
        assert request.tool == "subchat_download_file"
        offset = request.arguments["offset"]
        return Reply(operation_id=request.operation_id, state="completed", data={
            "content_base64": base64.b64encode(content[offset:]).decode(),
            "file_size_bytes": len(content), "offset": offset, "eof": True,
        })

    monkeypatch.setattr(gateway, "execute", download)
    original_chunk = RoutedSaveTarget.chunk
    chunk_calls = 0

    async def lost_chunk_reply(self, *args):
        nonlocal chunk_calls
        chunk_calls += 1
        await original_chunk(self, *args)
        raise ConnectionError("chunk response lost after target write")

    monkeypatch.setattr(RoutedSaveTarget, "chunk", lost_chunk_reply)
    engine = Engine(tmp_path / "engine")
    backend = AuthorizedDeviceMCP(Store(), engine, owner="owner", device="device",
                                  client="client", allowed_tools=scopes,
                                  device_directory=tmp_path / "devices",
                                  subchat_gateway=gateway)
    destination = tmp_path / "answer.bin"
    request = Request(operation_id="c" * 32, tool="subchat_save_file",
                      arguments={"source_operation_id": "a" * 32,
                                 "sandbox_link": "sandbox:/answer.bin",
                                 "device_id": "local",
                                 "destination_path": str(destination)})
    try:
        session = backend.session("grant")
        catalog = await session.catalog()
        assert "subchat_save_file" in {item["name"] for item in catalog}
        assert "request_id" in next(item for item in catalog if item["name"] ==
                                    "subchat_save_file")["inputSchema"]["required"]
        states = []
        for _ in range(2):
            result = await session.execute(request)
            states.append(result.state)
        assert states == ["running", "unknown"]
        grant = replace(grant, grant_id="renewed")
        renewed_session = backend.session("renewed")
        for _ in range(3):
            result = await renewed_session.execute(request)
            states.append(result.state)
            if result.state == "completed":
                break
        assert states == ["running", "unknown", "unknown", "completed"]
        assert chunk_calls == 1
        assert destination.read_bytes() == content
        assert "content_base64" not in result.data
        renewed = await renewed_session.execute(request)
        assert renewed.state == "completed"
        assert destination.read_bytes() == content
        grant = replace(grant, tools=scopes - {"subchat_save_file"})
        assert "subchat_save_file" not in {
            item["name"] for item in await backend.session("renewed").catalog()}
    finally:
        await backend.close()
        await gateway.close()
        await engine.close()


@pytest.mark.asyncio
async def test_real_oauth_http_save_requires_own_scope_and_publishes_verified_bytes(
        tmp_path, monkeypatch):
    content = b"authenticated HTTP save"
    scopes = frozenset({"subchat_save_file", "upload_begin", "upload_chunk",
                        "upload_status", "upload_commit", "operations_get"})
    engine = Engine(tmp_path / "engine")
    resource = "https://computer.example/mcp"
    authority = AuthorizationStore(tmp_path / "authority", resource=resource,
                                   known_tools=frozenset(engine.tools) | scopes)
    authority.register_client("client", frozenset({"https://client.example/callback"}))
    authority.enroll_device("owner", "device", scopes)
    gateway = LazySubchatGateway(SubchatGatewayConfig(
        profile=str(tmp_path / "Default"), ledger=str(tmp_path / "subchat-ledger"),
        account_id="account", consent="ordinary-chat-browser-control-approved"),
        owner="owner")

    async def download(owner, request, granted):
        assert request.tool == "subchat_download_file"
        assert granted == frozenset({"subchat_download_file"})
        offset = request.arguments["offset"]
        return Reply(operation_id=request.operation_id, state="completed", data={
            "content_base64": base64.b64encode(content[offset:]).decode(),
            "file_size_bytes": len(content), "offset": offset, "eof": True,
        })

    monkeypatch.setattr(gateway, "execute", download)

    def token_for(tools):
        verifier = "v" * 43
        code = authority.approve(
            owner="owner", device="device", client="client",
            redirect="https://client.example/callback", resource=resource,
            tools=frozenset(tools), challenge=pkce_s256(verifier))
        return authority.exchange_code(
            code=code, verifier=verifier, client="client",
            redirect="https://client.example/callback", resource=resource).value

    limited_token = token_for(scopes - {"subchat_save_file"})
    save_token = token_for(scopes)
    backend = AuthorizedDeviceMCP(
        authority, engine, owner="owner", device="device", client="client",
        device_directory=tmp_path / "devices", subchat_gateway=gateway)
    server = HTTPMCP(backend.authenticate, backend.session)
    destination = tmp_path / "http-result.bin"
    operation_id = "d" * 32
    args = {"request_id": operation_id, "source_operation_id": "a" * 32,
            "sandbox_link": "sandbox:/answer.bin", "device_id": "local",
            "destination_path": str(destination)}
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "save-test", "version": "1"}}}
    try:
        port = await server.start()
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Accept": "application/json, text/event-stream"},
        ) as http:
            async def connect(token):
                http.headers["Authorization"] = f"Bearer {token}"
                started = await http.post("/mcp", json=initialize)
                assert started.status_code == 200
                http.headers["MCP-Session-Id"] = started.headers["mcp-session-id"]
                initialized = await http.post("/mcp", json={
                    "jsonrpc": "2.0", "method": "notifications/initialized"})
                assert initialized.status_code in {200, 202}

            async def call():
                response = await http.post("/mcp", json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "subchat_save_file", "arguments": args}})
                assert response.status_code == 200
                return response.json()

            await connect(limited_token)
            refused = await call()
            assert refused["error"]["code"] == -32602
            assert not destination.exists()
            http.headers.pop("MCP-Session-Id")
            await connect(save_token)
            async with asyncio.timeout(10):
                while True:
                    result = (await call())["result"]["structuredContent"]
                    if result["state"] == "completed":
                        break
                    assert result["state"] in {"running", "unknown"}, result
                    await asyncio.sleep(0.02)
            assert result["operation_id"] == operation_id
            assert result["data"]["sha256"] == hashlib.sha256(content).hexdigest()
            assert destination.read_bytes() == content
            assert "content_base64" not in str(result)
            assert (await call())["result"]["structuredContent"] == result
    finally:
        await server.close()
        await backend.close()
        await gateway.close()
        authority.close()
        await engine.close()
