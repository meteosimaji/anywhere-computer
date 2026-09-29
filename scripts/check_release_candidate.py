"""Reject PR release versions that are already tagged on origin or cannot be checked."""

import os
import subprocess
from pathlib import Path

from assemble_release_assets import release_version

ROOT = Path(__file__).resolve().parents[1]


def check_release_candidate(root: Path) -> str:
    """Require matching clean source/bundle metadata and an absent remote release tag."""
    tag = 'v' + release_version(root)[0]
    try:
        result = subprocess.run(
            ['git', 'ls-remote', '--exit-code', '--tags', 'origin', f'refs/tags/{tag}'],
            cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=30, check=False,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError(
            'Cannot verify the release tag: origin query failed or timed out'
        ) from None
    if result.returncode == 2:
        return tag
    if result.returncode == 0:
        raise ValueError(
            f'Release tag {tag} already exists on origin; bump the version before merging'
        )
    raise RuntimeError(
        f'Cannot verify the release tag: git ls-remote exited with {result.returncode}'
    )


def main() -> None:
    try:
        tag = check_release_candidate(ROOT)
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
    print(f'Release candidate {tag} has no existing tag on origin')


if __name__ == '__main__':
    main()
