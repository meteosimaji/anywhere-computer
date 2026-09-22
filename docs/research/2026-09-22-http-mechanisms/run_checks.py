"""Run research checks in a NEW directory; never contact a provider.

Optional upstream sources use the manifest's owner--repo layout. Only the three
hash-checked files used by the characterization are copied. No auth module runs.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sources', type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    here = Path(__file__).resolve().parent
    for name in ('sse_reference.py', 'test_sse_reference.py'):
        shutil.copyfile(here / 'probes' / name, output / name)
    scripts = ['test_sse_reference.py']
    if args.sources is not None:
        sources = args.sources.resolve()
        required = (
            'suphotP--chatgpt-api/chatgpt_api/providers/chatgpt/transport.py',
            'suphotP--chatgpt-api/chatgpt_api/api/openai_compat.py',
            'robotlearning123--gpt2agent/gpt2agent/tools/conversations.py',
        )
        for relative in required:
            source = sources / relative
            if not source.is_file() or source.is_symlink():
                raise ValueError('Missing regular pinned source file: ' + relative)
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        shutil.copyfile(here / 'probes' / 'test_upstream_boundaries.py',
                        output / 'test_upstream_boundaries.py')
        scripts.append('test_upstream_boundaries.py')
    results = []
    for script in scripts:
        with (output / (script + '.log')).open('x', encoding='utf-8') as log:
            process = subprocess.run([sys.executable, str(output / script)], cwd=output,
                stdout=log, stderr=subprocess.STDOUT, timeout=45,
                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        results.append({'script': script, 'exit_code': process.returncode})
    summary = {'results': results, 'scope': 'offline/localhost research, not live Chat acceptance'}
    (output / 'runner-result.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
    return int(any(item['exit_code'] != 0 for item in results))


if __name__ == '__main__':
    raise SystemExit(main())
