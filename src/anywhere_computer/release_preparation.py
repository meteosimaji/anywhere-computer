"""Prepare and probe an attested release without starting or selecting an engine."""

import json
import os
import re
import shutil
import subprocess
import sysconfig
import tempfile
from pathlib import Path

from .github_releases import REPOSITORY, ReleaseCandidate, download_candidate
from .portable_archive import inspect_portable, require_portable_compatibility, stage_portable
from .runtime_identity import ENGINE_API_VERSION


def probe_release_runtime(app: Path, expected_version: str) -> str:
    """Import the attested installation before any running engine is stopped."""
    executable = app / ('runtime/python.exe' if os.name == 'nt' else 'runtime/bin/python3')
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError('Release Python executable is missing or not executable')
    script = (
        'import json; from anywhere_computer import __version__; '
        'from anywhere_computer.runtime_identity import ENGINE_API_VERSION, runtime_identity; '
        'import anywhere_computer.cli; '
        'print(json.dumps([__version__, ENGINE_API_VERSION, runtime_identity()]))'
    )
    result = subprocess.run(
        [str(executable.absolute()), '-B', '-I', '-c', script], cwd=app,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=30, check=False,
    )
    if result.returncode != 0:
        raise ValueError('Release Python startup check failed')
    try:
        report = json.loads(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise ValueError('Release Python startup returned invalid identity') from error
    if (not isinstance(report, list) or len(report) != 3
            or report[0] != expected_version or type(report[1]) is not int
            or report[1] != ENGINE_API_VERSION or not isinstance(report[2], str)
            or not re.fullmatch(r'[a-f0-9]{64}', report[2])):
        raise ValueError('Release Python startup identity is incompatible')
    return report[2]


def release_platform(platform: str | None = None) -> str:
    value = platform if platform is not None else sysconfig.get_platform()
    if re.fullmatch(r'macosx-\d+(?:\.\d+)*-(arm64|x86_64)', value):
        return 'macos-' + value.rsplit('-', 1)[1]
    supported = {'linux-x86_64': 'linux-x86_64', 'win-amd64': 'windows-x86_64'}
    if value in supported:
        return supported[value]
    raise ValueError('No supported portable release for this runtime platform')


def verify_release_provenance(
    archive: Path, candidate: ReleaseCandidate, *, verifier: Path,
) -> None:
    """Use the official verifier with exact repository, workflow, source and ref policy.

    The verifier is an installed prerequisite, never code from the candidate.
    No success-shaped fallback is permitted when it is missing or fails.
    """
    if not verifier.is_absolute() or not verifier.is_file() or not os.access(verifier, os.X_OK):
        raise ValueError('An installed GitHub attestation verifier is required')
    command = [str(verifier), 'attestation', 'verify', str(archive.absolute()),
               '--repo', REPOSITORY, '--signer-workflow',
               f'{REPOSITORY}/.github/workflows/quality.yml',
               '--source-digest', candidate.commit, '--source-ref', 'refs/heads/main',
               '--deny-self-hosted-runners']
    # Verification output may contain signed temporary URLs. Do not relay it into logs.
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=120, check=False)
    if result.returncode != 0:
        raise ValueError('Release attestation verification failed')


def prepare_release(
    candidate: ReleaseCandidate, parent: Path, control: Path, *, verifier: Path,
) -> Path:
    """Return an unselected installation only after byte, identity and compatibility checks."""
    platform = release_platform()
    expected_name = f'anywhere-computer-{candidate.tag.removeprefix("v")}-{platform}.zip'
    if candidate.asset_name != expected_name:
        raise ValueError('Release asset does not match the current runtime platform')
    with tempfile.TemporaryDirectory(prefix='.release-prepare-', dir=parent) as temporary:
        archive = Path(temporary) / 'release.zip'
        download_candidate(candidate, archive)
        verify_release_provenance(archive, candidate, verifier=verifier)
        inspection = inspect_portable(archive, expected_sha256=candidate.sha256)
        if inspection.python_version != candidate.tag.removeprefix('v'):
            raise ValueError('Release archive version differs from its stable tag')
        if release_platform(inspection.platform) != platform:
            raise ValueError('Release archive platform differs from this runtime')
        require_portable_compatibility(inspection, control)
        app = stage_portable(archive, parent, expected_sha256=candidate.sha256)
        try:
            probe_release_runtime(app, inspection.python_version)
        except BaseException:
            # stage_portable creates this unique directory; no existing runtime is removed.
            shutil.rmtree(app.parent)
            raise
        return app
