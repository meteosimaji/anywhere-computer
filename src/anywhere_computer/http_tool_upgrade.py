"""Explicit, offline addition of HTTP tools without replacing credentials.

Only grants that already cover the complete old tool set are expanded for ordinary
tools. Subchat and delegation tools require fresh consent. Restricted, revoked and
expired grants stay unchanged. A crash between the database commit and config
publication fails closed; repeat the same command to finish publication.
"""

import json
import os
import tempfile
import time
from pathlib import Path

from .authorization import LOCAL_ONLY_TOOLS, AuthorizationStore
from .device_router import ROUTER_TOOLS
from .engine import Engine
from .http_service import _check_enrollment, load_http_config
from .locking import ProcessLock
from .subchat_gateway import SUBCHAT_GATEWAY_TOOLS, SubchatGatewayConfig


def _requires_new_consent(tools: frozenset[str]) -> bool:
    return any(tool.startswith(("subchat_", "mcp_", "codex_plugin_", "devices_"))
               for tool in tools)


async def add_http_tools(directory: Path, tools: frozenset[str], *,
                         subchat: SubchatGatewayConfig | None = None) -> dict[str, object]:
    if not tools or tools & LOCAL_ONLY_TOOLS:
        raise ValueError("Specify remote tools to add")
    with ProcessLock(directory / "http-server.lock"):
        config = load_http_config(directory)
        if subchat is not None and config.subchat is not None and subchat != config.subchat:
            raise ValueError("Existing Subchat selection cannot be replaced by tool upgrade")
        selection = config.subchat or subchat
        adding_subchat = bool(tools & SUBCHAT_GATEWAY_TOOLS)
        if adding_subchat and selection is None:
            raise ValueError("Subchat tools require an explicit gateway selection")
        if subchat is not None and not (adding_subchat or config.scopes & SUBCHAT_GATEWAY_TOOLS):
            raise ValueError("Subchat selection requires a Subchat tool scope")
        expanded = config.model_copy(update={"scopes": config.scopes | tools,
                                             "subchat": selection})
        # Validate the expanded model, including the scope-count bound.
        expanded = type(config).model_validate_json(expanded.model_dump_json())
        new_consent_required = _requires_new_consent(expanded.scopes - config.scopes)
        database = directory / "http-server/authorization/authorization.sqlite3"
        if database.is_symlink() or not database.is_file():
            raise ValueError("HTTP authorization database is missing")
        with tempfile.TemporaryDirectory(prefix="anywhere-tool-catalog-") as temporary:
            # The standard temporary root has a platform ACL. Create state
            # privately beneath it, as setup_scopes does.
            engine = Engine(Path(temporary) / "catalog-state")
            try:
                known = (frozenset(engine.tools) | ROUTER_TOOLS
                         | (SUBCHAT_GATEWAY_TOOLS if selection else frozenset())) - LOCAL_ONLY_TOOLS
            finally:
                await engine.close()
        if expanded.scopes - known:
            raise ValueError("Requested tools are not supported by this version")
        store = AuthorizationStore(database.parent, resource=config.resource, known_tools=known)
        try:
            # Accept only the old enrollment or the exact intended new enrollment
            # left by an interrupted publication. Never repair unrelated drift.
            row = store.db.execute(
                "SELECT tools FROM authorized_devices WHERE id=?",
                (config.device,),
            ).fetchone()
            if row is None:
                raise ValueError("HTTP device enrollment is missing")
            stored = frozenset(json.loads(row[0]))
            if stored not in {config.scopes, expanded.scopes}:
                raise ValueError("HTTP enrollment differs from the requested upgrade")
            _check_enrollment(store, config.model_copy(update={"scopes": stored}))
            if not store.device_enabled(owner=config.owner, device=config.device):
                raise ValueError("Disabled HTTP devices cannot be upgraded")
            changed = 0
            with store.db:
                store.db.execute("BEGIN IMMEDIATE")
                candidates = store.db.execute(
                    "SELECT id,tools FROM grants WHERE owner=? AND device=? AND client=? "
                    "AND revoked=0 AND (expires=0 OR expires>?)",
                    (config.owner, config.device, config.client, time.time()),
                ).fetchall()
                encoded = json.dumps(sorted(expanded.scopes))
                for grant_id, raw in candidates:
                    if (not new_consent_required
                            and frozenset(json.loads(raw)) == config.scopes
                            and config.scopes != expanded.scopes):
                        store.db.execute(
                            "UPDATE grants SET tools=? WHERE id=?", (encoded, grant_id)
                        )
                        changed += 1
                store.db.execute(
                    "UPDATE authorized_devices SET tools=? WHERE id=?",
                    (encoded, config.device),
                )
            destination = directory / "http-server/config.json"
            fd, temporary_name = tempfile.mkstemp(prefix=".config-tools-", dir=destination.parent)
            staged = Path(temporary_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    output.write(expanded.model_dump_json(indent=2) + "\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(staged, destination)
            finally:
                staged.unlink(missing_ok=True)
            return {
                "added_tools": sorted(expanded.scopes - config.scopes),
                "expanded_full_access_grants": changed,
                "new_consent_required": new_consent_required,
                "credentials_replaced": False,
                "restart_required": True,
            }
        finally:
            store.close()
