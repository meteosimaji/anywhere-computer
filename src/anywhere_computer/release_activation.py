"""Activate a prepared installation through the existing idle-only start transaction."""

import asyncio
import os
import subprocess
from pathlib import Path

from pydantic import JsonValue

from .connection import exchange
from .github_releases import ReleaseCandidate
from .locking import ProcessLock
from .release_preparation import prepare_release, probe_release_runtime
from .release_record import PreparedRelease, read_prepared_release, save_prepared_release
from .runtime_selection import RuntimeSelection, load_runtime_selection


def install_release(
    candidate: ReleaseCandidate, parent: Path, control: Path, *, verifier: Path | None = None,
) -> dict[str, JsonValue]:
    """Prepare a verified candidate and attempt the existing idle-only transaction."""
    with ProcessLock(control / 'release-install.lock'):
        record = read_prepared_release(control)
        if record is not None and record.candidate != candidate:
            if record.state != 'applied':
                raise RuntimeError('Resume the prepared release before selecting another candidate')
            record = None
        if record is None:
            if verifier is None:
                raise ValueError('A verifier is required to prepare a new release')
            app = prepare_release(candidate, parent, control, verifier=verifier)
            record = PreparedRelease(candidate=candidate, installation=str(app.absolute()))
            # Persist before activation, including before the first stop request.
            save_prepared_release(control, record)
        app = Path(record.installation)
        if app.is_symlink() or not app.is_dir():
            raise RuntimeError('Prepared installation is unavailable; retain its update record')
        result = activate_release(app, control, candidate.tag.removeprefix('v'))
        if result['state'] in {'current', 'applied'}:
            save_prepared_release(control, record.model_copy(update={'state': 'applied'}))
        return {**result, 'installation': str(app)}


def activate_release(app: Path, control: Path, expected_version: str) -> dict[str, JsonValue]:
    """Accept only the attested installation returned by prepare_release.

    Keep the installation on failure: start may have persisted a pending selection
    or started the candidate before its caller lost the response. Never restore an
    older ledger or remove a possibly selected runtime here.
    """
    with ProcessLock(control / 'release-activation.lock'):
        runtime_id = probe_release_runtime(app, expected_version)
        executable = app / ('runtime/python.exe' if os.name == 'nt' else 'runtime/bin/python3')
        pending = control / 'runtime-update.pending.json'
        recovering = pending.exists() or pending.is_symlink()
        if recovering:
            load_runtime_selection(control, recovery=RuntimeSelection(
                executable=str(executable.absolute()), runtime_id=runtime_id))
        try:
            before = asyncio.run(exchange(control, '__status', timeout=3))
        except (OSError, ValueError, TimeoutError):
            if not recovering:
                raise
            before = None
        if before is not None and before.state != 'completed':
            raise RuntimeError('Cannot inspect the running engine before release activation')
        if before is not None and before.data.get('runtime_id') == runtime_id and not recovering:
            return {'state': 'current', 'runtime_id': runtime_id}
        if before is not None and (before.data.get('update_blocked')
                or before.data.get('active_sessions') or before.data.get('active_operations')):
            return {'state': 'waiting', 'reason': 'active_work', 'runtime_id': runtime_id}
        # start rechecks busy state inside startup.lock and again at the engine's
        # stop boundary. The earlier status read is not permission to force-stop.
        try:
            result = subprocess.run(
                [str(executable.absolute()), '-B', '-I', '-m', 'anywhere_computer',
                 'start', '--state-dir', str(control.absolute())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=45, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                'Release activation response timed out; retain the prepared installation '
                'and inspect its pending selection before retrying'
            ) from error
        if result.returncode != 0:
            raise RuntimeError(
                'Release activation did not complete; retain the prepared installation '
                'and inspect engine status and pending selection'
            )
        after = asyncio.run(exchange(control, '__status', timeout=3))
        if (after.state != 'completed' or after.data.get('runtime_id') != runtime_id
                or after.data.get('version') != expected_version):
            raise RuntimeError('Release activation could not be confirmed by the running engine')
        return {'state': 'applied', 'runtime_id': runtime_id,
                'instance_id': after.data.get('instance_id'), 'version': expected_version}
