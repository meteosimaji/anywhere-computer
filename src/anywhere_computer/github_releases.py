"""Read public stable-release candidates; this does not authorize artifact execution."""

import hashlib
import http.client
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from pydantic import JsonValue

REPOSITORY = 'meteosimaji/anywhere-computer'
API_ROOT = f'/repos/{REPOSITORY}/'
METADATA_LIMIT = 2 * 1024 * 1024


def github_metadata(path: str) -> dict[str, JsonValue] | None:
    if not path.startswith(API_ROOT) or any(c in path for c in '\r\n#'):
        raise ValueError('Unexpected GitHub API path')
    connection = http.client.HTTPSConnection('api.github.com', timeout=20)
    try:
        connection.request('GET', path, headers={
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2026-03-10',
            'User-Agent': 'Anywhere-Computer-update-check',
        })
        response = connection.getresponse()
        if response.status == 404:
            return None
        if response.status != 200:
            raise ConnectionError(f'GitHub metadata request failed: HTTP {response.status}')
        raw = response.read(METADATA_LIMIT + 1)
        if len(raw) > METADATA_LIMIT:
            raise ValueError('GitHub metadata exceeds size limit')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('GitHub metadata must be an object')
        return result
    finally:
        connection.close()


@dataclass(frozen=True)
class ReleaseCandidate:
    tag: str
    commit: str
    asset_id: int
    asset_name: str
    size: int
    sha256: str
    quality_run_id: int


def stable_candidate(
    platform: str, *, get: Callable[[str], dict[str, JsonValue] | None] = github_metadata,
) -> ReleaseCandidate | None:
    """Require stable tag, uploaded asset and successful same-commit quality workflow.

    This is discovery evidence only. Downloaded bytes, embedded source provenance,
    compatibility and publisher attestation still require verification before use.
    """
    if platform not in {'macos-arm64', 'macos-x86_64', 'linux-x86_64', 'windows-x86_64'}:
        raise ValueError('Unsupported release platform')
    release = get(API_ROOT + 'releases/latest')
    if release is None:
        return None
    tag = release.get('tag_name')
    if (release.get('draft') is not False or release.get('prerelease') is not False
            or not isinstance(tag, str) or not re.fullmatch(r'v\d+\.\d+\.\d+', tag)):
        raise ValueError('Latest release is not a stable semantic version')
    ref = get(API_ROOT + 'git/ref/tags/' + quote(tag, safe=''))
    obj = ref.get('object') if ref else None
    for _ in range(4):
        if not isinstance(obj, dict):
            raise ValueError('Release tag object is unavailable')
        sha = obj.get('sha')
        if not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{40}', sha):
            raise ValueError('Invalid release commit')
        if obj.get('type') == 'commit':
            break
        if obj.get('type') != 'tag':
            raise ValueError('Release tag does not reference a commit')
        annotated = get(API_ROOT + 'git/tags/' + sha)
        obj = annotated.get('object') if annotated else None
    else:
        raise ValueError('Release tag nesting exceeds limit')
    runs = get(API_ROOT + f'actions/workflows/quality.yml/runs?head_sha={sha}&per_page=100')
    entries = runs.get('workflow_runs') if runs else None
    if not isinstance(entries, list):
        raise ValueError('Release quality checks are unavailable')
    run_id = next((identity for entry in entries if isinstance(entry, dict)
                   and type(identity := entry.get('id')) is int and identity > 0
                   and entry.get('head_sha') == sha and entry.get('event') == 'push'
                   and entry.get('path') == '.github/workflows/quality.yml'
                   and entry.get('status') == 'completed'
                   and entry.get('conclusion') == 'success'), None)
    if not isinstance(run_id, int):
        raise ValueError('No successful quality run for the release commit')
    name = f'anywhere-computer-{tag[1:]}-{platform}.zip'
    assets = release.get('assets')
    matches = [entry for entry in assets if isinstance(entry, dict) and entry.get('name') == name
               ] if isinstance(assets, list) else []
    if len(matches) != 1:
        raise ValueError('Release must contain exactly one matching platform asset')
    asset = matches[0]
    identity, size, digest = asset.get('id'), asset.get('size'), asset.get('digest')
    if (type(identity) is not int or identity <= 0 or type(size) is not int
            or not 0 < size <= 1024 * 1024 * 1024 or asset.get('state') != 'uploaded'
            or not isinstance(digest, str) or not re.fullmatch('sha256:[a-f0-9]{64}', digest)):
        raise ValueError('Invalid release artifact metadata')
    return ReleaseCandidate(tag, sha, identity, name, size, digest[7:], run_id)


def download_candidate(candidate: ReleaseCandidate, destination: Path) -> Path:
    """Download fixed-repository asset bytes; publish only an exact size/hash match."""
    if (type(candidate.asset_id) is not int or candidate.asset_id <= 0
            or type(candidate.size) is not int or not 0 < candidate.size <= 1024**3
            or not re.fullmatch('[a-f0-9]{64}', candidate.sha256)):
        raise ValueError('Invalid download candidate')
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Download destination already exists')
    descriptor, name = tempfile.mkstemp(prefix='.release-download-', dir=destination.parent)
    staged = Path(name)
    url = f'https://api.github.com{API_ROOT}releases/assets/{candidate.asset_id}'
    try:
        with os.fdopen(descriptor, 'wb') as output:
            for _ in range(4):
                parsed = urlsplit(url)
                if (parsed.scheme != 'https' or parsed.hostname not in {
                        'api.github.com', 'release-assets.githubusercontent.com',
                        'objects.githubusercontent.com',
                    } or parsed.port not in {None, 443} or parsed.username is not None
                        or parsed.password is not None or parsed.fragment
                        or any(ord(char) < 32 for char in url)):
                    raise ValueError('Unexpected release download redirect')
                connection = http.client.HTTPSConnection(parsed.hostname, timeout=30)
                try:
                    path = parsed.path + ('?' + parsed.query if parsed.query else '')
                    connection.request('GET', path, headers={
                        'Accept': 'application/octet-stream',
                        'User-Agent': 'Anywhere-Computer-update-download',
                        'X-GitHub-Api-Version': '2026-03-10',
                    })
                    response = connection.getresponse()
                    if response.status == 302:
                        location = response.getheader('Location')
                        if not location or len(location) > 16384:
                            raise ValueError('Invalid release download redirect')
                        url = location
                        continue
                    if response.status != 200:
                        raise ConnectionError(f'Release download failed: HTTP {response.status}')
                    digest, received = hashlib.sha256(), 0
                    while block := response.read(min(1024 * 1024, candidate.size - received + 1)):
                        received += len(block)
                        if received > candidate.size:
                            raise ValueError('Release download exceeds declared size')
                        digest.update(block)
                        output.write(block)
                    if received != candidate.size or digest.hexdigest() != candidate.sha256:
                        raise ValueError('Release download size or checksum mismatch')
                    output.flush()
                    os.fsync(output.fileno())
                    break
                finally:
                    connection.close()
            else:
                raise ValueError('Release download redirect limit exceeded')
        os.link(staged, destination)  # Atomic publication without replacing an existing file.
        return destination
    finally:
        staged.unlink(missing_ok=True)
