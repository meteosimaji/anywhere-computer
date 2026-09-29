"""A real bare origin distinguishes missing tags from published tags and remote errors."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def candidate(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        'check_release_candidate', scripts / 'check_release_candidate.py',
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(root, *arguments):
    return subprocess.run([
        'git', '-c', 'user.name=Release fixture', '-c', 'user.email=fixture@example.invalid',
        '-c', 'commit.gpgsign=false', '-c', 'tag.gpgsign=false',
        '-c', 'core.hooksPath=' + str(root / 'empty-hooks'), '-C', str(root), *arguments,
    ], check=True, capture_output=True, text=True)


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / 'source'
    remote = tmp_path / 'origin.git'
    git(tmp_path, 'init', '--bare', str(remote))
    git(tmp_path, 'init', str(root))
    git(root, 'remote', 'add', 'origin', str(remote))
    source = root / 'src/anywhere_computer/__init__.py'
    source.parent.mkdir(parents=True)
    source.write_text('__version__ = "1.2.3b4"\n')
    bundled = root / 'plugins/anywhere-computer/bundled'
    bundled.mkdir(parents=True)
    (bundled / 'release.json').write_text(json.dumps({
        'version': '1.2.3-beta.4', 'python_version': '1.2.3b4',
        'source_commit': 'a' * 40, 'source_dirty': False,
    }))
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'Disposable release fixture')
    return root, remote


def test_missing_tag_is_accepted_without_needing_a_local_tag_fetch(candidate, repository):
    root, _ = repository
    git(root, 'tag', 'v1.2.3b40')
    git(root, 'push', 'origin', 'refs/tags/v1.2.3b40')
    # Query the exact candidate, not a prefix or the presence of any remote tag.
    assert candidate.check_release_candidate(root) == 'v1.2.3b4'


@pytest.mark.parametrize('annotated', [False, True], ids=['lightweight', 'annotated'])
def test_existing_remote_tag_is_rejected_even_when_absent_locally(
    candidate, repository, annotated,
):
    root, _ = repository
    args = ['-a', '-m', 'Published fixture'] if annotated else []
    git(root, 'tag', *args, 'v1.2.3b4')
    git(root, 'push', 'origin', 'refs/tags/v1.2.3b4')
    git(root, 'tag', '-d', 'v1.2.3b4')
    with pytest.raises(ValueError, match='already exists on origin; bump the version'):
        candidate.check_release_candidate(root)


def test_unavailable_remote_is_not_treated_as_an_unpublished_version(candidate, repository):
    root, _ = repository
    git(root, 'remote', 'set-url', 'origin', str(root / 'private-origin-missing.git'))
    with pytest.raises(RuntimeError, match='git ls-remote exited with 128') as raised:
        candidate.check_release_candidate(root)
    assert 'private-origin-missing' not in str(raised.value)


@pytest.mark.parametrize('field,value', [
    ('python_version', '1.2.3b3'), ('version', '1.2.3-beta.3'), ('source_dirty', True),
])
def test_mismatched_source_or_dirty_bundle_fails_before_query(
    candidate, repository, monkeypatch, field, value,
):
    root, _ = repository
    path = root / 'plugins/anywhere-computer/bundled/release.json'
    release = json.loads(path.read_text())
    release[field] = value
    path.write_text(json.dumps(release))

    def unexpected_query(*args, **kwargs):
        raise AssertionError('Invalid metadata must fail before querying origin')

    monkeypatch.setattr(candidate.subprocess, 'run', unexpected_query)
    with pytest.raises(ValueError, match='clean build of the source version'):
        candidate.check_release_candidate(root)


@pytest.mark.parametrize('failure', [
    OSError('private-credential'), subprocess.TimeoutExpired('private-command', 30),
])
def test_query_start_or_timeout_failure_is_closed_and_sanitized(
    candidate, repository, monkeypatch, failure,
):
    root, _ = repository

    def fail_query(command, **options):
        assert command == [
            'git', 'ls-remote', '--exit-code', '--tags', 'origin', 'refs/tags/v1.2.3b4',
        ]
        assert options['timeout'] == 30 and options['env']['GIT_TERMINAL_PROMPT'] == '0'
        assert options['stdin'] == subprocess.DEVNULL
        raise failure

    monkeypatch.setattr(candidate.subprocess, 'run', fail_query)
    with pytest.raises(RuntimeError, match='origin query failed or timed out') as raised:
        candidate.check_release_candidate(root)
    assert 'private' not in str(raised.value)


def test_cli_reports_missing_tag_and_exits_on_published_tag(
    candidate, repository, monkeypatch, capsys,
):
    root, _ = repository
    monkeypatch.setattr(candidate, 'ROOT', root)
    candidate.main()
    assert 'v1.2.3b4 has no existing tag on origin' in capsys.readouterr().out
    git(root, 'tag', 'v1.2.3b4')
    git(root, 'push', 'origin', 'refs/tags/v1.2.3b4')
    with pytest.raises(SystemExit, match='already exists on origin'):
        candidate.main()
