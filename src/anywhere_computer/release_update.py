"""One stable-update cycle, shared by the CLI and future service supervision."""

import asyncio
import re
from pathlib import Path

from pydantic import JsonValue

from .connection import exchange
from .github_releases import stable_candidate
from .locking import ProcessLock
from .release_activation import install_release
from .release_preparation import release_platform
from .release_record import read_prepared_release


def release_version_key(value: str) -> tuple[int, int, int, int, int]:
    """Order the stable and prerelease forms produced by this project's packaging."""
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?', value)
    if match is None:
        raise ValueError('Running release version is unsupported; no automatic downgrade attempted')
    major, minor, patch, phase, number = match.groups()
    return (int(major), int(minor), int(patch),
            {'a': 0, 'b': 1, 'rc': 2, None: 3}[phase], int(number or 0))


def update_once(control: Path, *, verifier: Path) -> dict[str, JsonValue]:
    """Resume an interrupted candidate first; otherwise apply only a newer stable release."""
    with ProcessLock(control / 'release-check.lock'):
        record = read_prepared_release(control)
        parent = control / 'portable'
        if record is not None and record.state == 'prepared':
            return install_release(record.candidate, parent, control, verifier=verifier)
        status = asyncio.run(exchange(control, '__status', timeout=3))
        current = status.data.get('version')
        if status.state != 'completed' or not isinstance(current, str):
            raise RuntimeError('Cannot determine the running release before checking updates')
        current_key = release_version_key(current)
        if (status.data.get('update_blocked') or status.data.get('active_sessions')
                or status.data.get('active_operations')):
            return {'state': 'waiting', 'reason': 'active_work', 'version': current}
        candidate = stable_candidate(release_platform())
        if candidate is None:
            return {'state': 'no_stable_release', 'version': current}
        target = candidate.tag.removeprefix('v')
        if release_version_key(target) <= current_key:
            return {'state': 'current', 'version': current, 'latest_stable': target}
        parent.mkdir(mode=0o700, exist_ok=True)
        return install_release(candidate, parent, control, verifier=verifier)


def resume_pending_release(control: Path) -> None:
    """Recover only a previously prepared interrupted switch, without release discovery."""
    pending = control / 'runtime-update.pending.json'
    if not (pending.exists() or pending.is_symlink()):
        return
    with ProcessLock(control / 'release-check.lock', timeout=15):
        if not (pending.exists() or pending.is_symlink()):
            return  # The previous updater finished while this connector waited.
        record = read_prepared_release(control)
        if record is None:
            raise RuntimeError(
                'Runtime update is incomplete and has no prepared release; '
                'resume explicit start with its selected installation'
            )
        result = install_release(record.candidate, control / 'portable', control)
        if result['state'] not in {'current', 'applied'}:
            raise RuntimeError('Pending release recovery has not completed')
        if pending.exists() or pending.is_symlink():
            raise RuntimeError('Pending release marker remains after recovery')
