import copy

import pytest

from anywhere_computer.github_releases import API_ROOT, stable_candidate


def metadata():
    sha = 'a' * 40
    return {
        API_ROOT + 'releases/latest': {
            'tag_name': 'v1.2.3', 'draft': False, 'prerelease': False,
            'assets': [{'name': 'anywhere-computer-1.2.3-macos-arm64.zip', 'id': 12,
                        'size': 100, 'state': 'uploaded', 'digest': 'sha256:' + 'b' * 64}],
        },
        API_ROOT + 'git/ref/tags/v1.2.3': {'object': {'type': 'commit', 'sha': sha}},
        API_ROOT + f'actions/workflows/quality.yml/runs?head_sha={sha}&per_page=100': {
            'workflow_runs': [{'id': 10, 'head_sha': sha, 'event': 'push',
                               'path': '.github/workflows/quality.yml',
                               'status': 'completed', 'conclusion': 'success'}],
        },
    }


def test_release_discovery_requires_matching_commit_and_asset():
    replies = metadata()
    found = stable_candidate('macos-arm64', get=replies.get)
    assert found.commit == 'a' * 40 and found.sha256 == 'b' * 64
    assert found.asset_id == 12 and found.quality_run_id == 10
    assert stable_candidate('macos-arm64', get=lambda _: None) is None


@pytest.mark.parametrize('problem', ['draft', 'prerelease', 'tag', 'failed', 'other-commit',
                                    'other-workflow', 'missing-digest', 'duplicate', 'bool-id'])
def test_unverified_release_is_not_a_candidate(problem):
    replies = metadata()
    release = replies[API_ROOT + 'releases/latest']
    run = next(value['workflow_runs'][0] for value in replies.values() if 'workflow_runs' in value)
    if problem in {'draft', 'prerelease'}:
        release[problem] = True
    elif problem == 'tag':
        release['tag_name'] = 'v1.2.3-alpha.1'
    elif problem == 'failed':
        run['conclusion'] = 'failure'
    elif problem == 'other-commit':
        run['head_sha'] = 'c' * 40
    elif problem == 'other-workflow':
        run['path'] = '.github/workflows/unrelated.yml'
    elif problem == 'missing-digest':
        release['assets'][0]['digest'] = None
    elif problem == 'duplicate':
        release['assets'].append(copy.deepcopy(release['assets'][0]))
    else:
        release['assets'][0]['id'] = True
    with pytest.raises(ValueError):
        stable_candidate('macos-arm64', get=replies.get)


@pytest.mark.parametrize('mode', ['direct', 'redirect', 'truncated', 'extra', 'hash',
                                 'wrong-host', 'loop', 'existing'])
def test_download_verifies_bytes_before_publication(tmp_path, monkeypatch, mode):
    import hashlib
    import io

    import anywhere_computer.github_releases as releases

    payload = b'release fixture'
    candidate = releases.ReleaseCandidate('v1.2.3', 'a' * 40, 12, 'fixture.zip', len(payload),
                                          hashlib.sha256(payload).hexdigest(), 10)
    opened, requests = [], []
    class Response:
        def __init__(self, status, body=b'', location=None):
            self.status, self.body, self.location = status, io.BytesIO(body), location
        def read(self, size):
            return self.body.read(size)
        def getheader(self, name):
            return self.location
    class Connection:
        def __init__(self, host, timeout):
            opened.append(self)
            self.host, self.closed = host, False
        def request(self, method, path, headers):
            requests.append((self.host, path, headers))
        def getresponse(self):
            if mode in {'wrong-host', 'loop'} or (mode == 'redirect' and len(opened) == 1):
                host = ('127.0.0.1' if mode == 'wrong-host'
                        else 'release-assets.githubusercontent.com')
                return Response(302, location=f'https://{host}/asset?fixture=1')
            body = payload[:-1] if mode == 'truncated' else payload
            if mode == 'extra':
                body += b'x'
            if mode == 'hash':
                body = b'x' * len(payload)
            return Response(200, body)
        def close(self):
            self.closed = True
    monkeypatch.setattr(releases.http.client, 'HTTPSConnection', Connection)
    destination = tmp_path / 'release.zip'
    if mode == 'existing':
        destination.write_bytes(b'preserved')
        with pytest.raises(FileExistsError):
            releases.download_candidate(candidate, destination)
        assert destination.read_bytes() == b'preserved' and not requests
    elif mode in {'direct', 'redirect'}:
        assert releases.download_candidate(candidate, destination) == destination
        assert destination.read_bytes() == payload
    else:
        with pytest.raises(ValueError):
            releases.download_candidate(candidate, destination)
        assert not destination.exists()
    assert all(connection.closed for connection in opened)
    assert not list(tmp_path.glob('.release-download-*'))
    assert all('Authorization' not in headers for _, _, headers in requests)
