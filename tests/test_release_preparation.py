import hashlib
import json
import shutil
import subprocess
import sys
import sysconfig
import zipfile

import pytest

from anywhere_computer.github_releases import ReleaseCandidate
from anywhere_computer.release_preparation import (
    prepare_release,
    probe_release_runtime,
    release_platform,
    verify_release_provenance,
)


def test_verifier_pins_source_identity_and_rejects_failure(tmp_path, monkeypatch):
    candidate = ReleaseCandidate('v1.2.3', 'a' * 40, 1, 'fixture.zip', 1, 'b' * 64, 2)
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 1)
    monkeypatch.setattr('anywhere_computer.release_preparation.subprocess.run', run)
    from pathlib import Path
    with pytest.raises(ValueError, match='verification failed'):
        verify_release_provenance(tmp_path / 'file', candidate, verifier=Path(sys.executable))
    command, options = calls[0]
    assert command[command.index('--source-digest') + 1] == candidate.commit
    assert command[command.index('--source-ref') + 1] == 'refs/heads/main'
    assert command[command.index('--signer-workflow') + 1] == (
        'meteosimaji/anywhere-computer/.github/workflows/quality.yml')
    assert '--deny-self-hosted-runners' in command
    assert options['stderr'] == subprocess.DEVNULL and 'shell' not in options


@pytest.mark.parametrize(('valid_attestation', 'platform_mismatch', 'runtime_failure'), [
    (False, False, False), (True, False, False), (True, True, False), (True, False, True),
])
def test_preparation_requires_attestation_and_never_selects_runtime(
    tmp_path, monkeypatch, valid_attestation, platform_mismatch, runtime_failure,
):
    from anywhere_computer import release_preparation as preparation

    body = b'not executable; fixture only'
    archive = tmp_path / 'fixture.zip'
    manifest = {'platform': sysconfig.get_platform(), 'release': {
        'python_version': '1.2.3', 'source_commit': 'a' * 40, 'source_dirty': False,
        'engine_api': {'minimum': 1, 'maximum': 1},
        'ledger_schema': {'minimum': 0, 'maximum': 1, 'writes': 1}},
        'files': {'runtime/file': hashlib.sha256(body).hexdigest()}}
    if platform_mismatch:
        manifest['platform'] = ('win-amd64' if release_platform() != 'windows-x86_64'
                                else 'linux-x86_64')
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('Anywhere Computer/runtime/file', body)
        bundle.writestr('Anywhere Computer/manifest.json', json.dumps(manifest))
    candidate = ReleaseCandidate('v1.2.3', 'a' * 40, 1,
                                 f'anywhere-computer-1.2.3-{release_platform()}.zip',
                                 archive.stat().st_size,
                                 hashlib.sha256(archive.read_bytes()).hexdigest(), 2)
    parent, control = tmp_path / 'installed', tmp_path / 'control'
    parent.mkdir()
    control.mkdir()
    def download(selected, destination):
        assert selected == candidate
        shutil.copyfile(archive, destination)
    def verify(path, selected, **kwargs):
        assert path.read_bytes() == archive.read_bytes() and selected == candidate
        if not valid_attestation:
            raise ValueError('fixture attestation rejected')
    monkeypatch.setattr(preparation, 'download_candidate', download)
    monkeypatch.setattr(preparation, 'verify_release_provenance', verify)
    def probe(app, version):
        assert valid_attestation and not platform_mismatch
        assert version == '1.2.3' and (app / 'runtime/file').read_bytes() == body
        if runtime_failure:
            raise ValueError('fixture startup failed')
        return 'c' * 64
    monkeypatch.setattr(preparation, 'probe_release_runtime', probe)
    if valid_attestation and not platform_mismatch and not runtime_failure:
        app = prepare_release(candidate, parent, control, verifier=tmp_path / 'unused')
        assert (app / 'runtime/file').read_bytes() == body
        assert not list(parent.glob('.release-prepare-*'))
    else:
        with pytest.raises(ValueError, match='platform' if platform_mismatch
                           else 'startup failed' if runtime_failure else 'attestation rejected'):
            prepare_release(candidate, parent, control, verifier=tmp_path / 'unused')
        assert not list(parent.iterdir())
    assert not list(control.iterdir())


@pytest.mark.parametrize('report', [
    ['1.2.3', 1, 'a' * 64], ['wrong', 1, 'a' * 64],
    ['1.2.3', True, 'a' * 64], ['1.2.3', 2, 'a' * 64],
    ['1.2.3', 1, 'invalid'], {},
])
def test_runtime_probe_checks_identity_without_starting_engine(tmp_path, monkeypatch, report):
    import os
    executable = tmp_path / ('runtime/python.exe' if os.name == 'nt' else 'runtime/bin/python3')
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'fixture')
    executable.chmod(0o700)
    def run(command, **options):
        assert command[:3] == [str(executable), '-I', '-c']
        assert 'ensure_agent' not in command[3] and 'serve(' not in command[3]
        assert options['timeout'] == 30 and options['cwd'] == tmp_path
        return subprocess.CompletedProcess(command, 0, json.dumps(report).encode())
    monkeypatch.setattr('anywhere_computer.release_preparation.subprocess.run', run)
    if report == ['1.2.3', 1, 'a' * 64] and type(report[1]) is int:
        assert probe_release_runtime(tmp_path, '1.2.3') == 'a' * 64
    else:
        with pytest.raises(ValueError, match='incompatible'):
            probe_release_runtime(tmp_path, '1.2.3')


@pytest.mark.parametrize(('runtime', 'expected'), [
    ('macosx-11.0-arm64', 'macos-arm64'), ('macosx-10.13-x86_64', 'macos-x86_64'),
    ('linux-x86_64', 'linux-x86_64'), ('win-amd64', 'windows-x86_64'),
])
def test_release_platform_mapping(runtime, expected):
    assert release_platform(runtime) == expected


@pytest.mark.parametrize('runtime', ['win-arm64', 'linux-aarch64', 'macosx-11.0-universal2', ''])
def test_unsupported_platform_has_no_silent_fallback(runtime):
    with pytest.raises(ValueError):
        release_platform(runtime)
