"""An individual-file read grant never grants its parent or descendants."""

import os
import uuid

import httpx
import pytest
from test_client_tokens import MemoryVault
from test_http_service import authenticate, initialize, setup

from anywhere_computer.authorization import AuthorizationStore
from anywhere_computer.delegated_files import read
from anywhere_computer.delegation_admin import _canonical_files, manage_delegation
from anywhere_computer.http_service import http_service
from anywhere_computer.models import ReadFile
from anywhere_computer.owner_credentials import OwnerCredentials


def test_selected_file_read_does_not_include_sibling_or_descendant(tmp_path):
    selected = tmp_path / "selected.txt"
    sibling = tmp_path / "private.txt"
    selected.write_text("allowed", encoding="utf-8")
    sibling.write_text("private", encoding="utf-8")
    files = (str(selected),)
    assert read(ReadFile(path=str(selected)), (), files=files)["text"] == "allowed"
    with pytest.raises(ValueError, match="outside the permitted"):
        read(ReadFile(path=str(sibling)), (), files=files)
    selected.unlink()
    selected.mkdir()
    descendant = selected / "private.txt"
    descendant.write_text("private", encoding="utf-8")
    with pytest.raises((OSError, ValueError)):
        read(ReadFile(path=str(selected)), (), files=files)
    with pytest.raises(ValueError, match="outside the permitted"):
        read(ReadFile(path=str(descendant)), (), files=files)


def test_owner_selected_file_validation_rejects_directory_missing_and_relative(tmp_path):
    for value in (str(tmp_path), str(tmp_path / "missing.txt"), "relative.txt"):
        with pytest.raises(ValueError, match="existing absolute regular files"):
            _canonical_files((value,))


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink substitution")
def test_owner_selected_file_validation_rejects_leaf_and_ancestor_symlinks(tmp_path):
    selected = tmp_path / "selected.txt"
    selected.write_text("allowed", encoding="utf-8")
    alias = tmp_path / "alias.txt"
    alias.symlink_to(selected)
    parent_alias = tmp_path / "parent-alias"
    parent_alias.symlink_to(tmp_path, target_is_directory=True)
    for value in (str(alias), str(parent_alias / selected.name)):
        with pytest.raises(ValueError, match="symbolic links"):
            _canonical_files((value,))


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink substitution")
@pytest.mark.parametrize("replace_parent", [False, True])
def test_selected_file_rejects_symlink_substitution(tmp_path, replace_parent):
    parent = tmp_path / "parent"
    outside = tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    selected = parent / "selected.txt"
    selected.write_text("allowed", encoding="utf-8")
    secret = outside / "selected.txt"
    secret.write_text("private", encoding="utf-8")
    files = (str(selected),)
    assert read(ReadFile(path=str(selected)), (), files=files)["text"] == "allowed"
    if replace_parent:
        parent.rename(tmp_path / "original-parent")
        parent.symlink_to(outside, target_is_directory=True)
    else:
        selected.unlink()
        selected.symlink_to(secret)
    with pytest.raises(OSError):
        read(ReadFile(path=str(selected)), (), files=files)


async def test_owner_selected_file_child_http_read_reconnect_and_revoke(
    tmp_path, unused_tcp_port,
):
    tools = frozenset({"files_read", "files_write", "operations_get"})
    config = await setup(tmp_path, unused_tcp_port, scopes=tools)
    owner = OwnerCredentials(tmp_path, resource=config.resource, owner=config.owner,
                             vault=MemoryVault())
    owner.initialize("synthetic owner password")
    selected = tmp_path / "selected.txt"
    sibling = tmp_path / "private.txt"
    selected.write_text("allowed", encoding="utf-8")
    sibling.write_text("private", encoding="utf-8")
    async with http_service(tmp_path, credentials=owner):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{config.port}",
                                     trust_env=False) as http:
            parent_token = await authenticate(http)
        authority = AuthorizationStore(
            tmp_path / "http-server" / "authorization", resource=config.resource,
            known_tools=config.scopes,
        )
        try:
            parent = authority.verify(parent_token, resource=config.resource)
            assert parent is not None
        finally:
            authority.close()
        issued = manage_delegation(
            tmp_path, action="issue", password="synthetic owner password",
            parent_grant_id=parent.grant_id, tools=frozenset({"files_read"}),
            read_files=(str(selected),), credentials=owner,
        )
        listed = manage_delegation(tmp_path, action="list", password="synthetic owner password",
                                   credentials=owner)
        entry = next(child for child in listed["children"]
                     if child["child_id"] == issued["child_id"])
        assert entry["read_roots"] == [] and entry["read_files"] == [str(selected)]
        async def call(http, headers, tool, arguments):
            response = await http.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments, "_meta": {
                    "io.github.meteosimaji.anywhere-computer/operation_id": uuid.uuid4().hex,
                }},
            })
            return response

        for _ in range(2):
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{config.port}",
                                         trust_env=False) as http:
                headers = await initialize(http, issued["bearer"])
                allowed = await call(http, headers, "files_read", {"path": str(selected)})
                assert allowed.status_code == 200
                result = allowed.json()["result"]["structuredContent"]
                assert result["state"] == "completed" and result["data"]["text"] == "allowed"
                denied = await call(http, headers, "files_read", {"path": str(sibling)})
                refusal = denied.json()["result"]["structuredContent"]
                assert refusal["state"] == "failed"
                assert refusal["data"] == {"dispatched": False, "reason": "path_out_of_scope"}
                rejected = await call(http, headers, "files_write",
                                      {"path": str(selected), "text": "bad"})
                assert "error" in rejected.json()
                assert selected.read_text(encoding="utf-8") == "allowed"
        manage_delegation(tmp_path, action="revoke", password="synthetic owner password",
                          child_id=issued["child_id"], credentials=owner)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{config.port}",
                                     trust_env=False) as http:
            assert (await call(http, {"Authorization": "Bearer " + issued["bearer"]},
                               "files_read", {"path": str(selected)})).status_code == 401
