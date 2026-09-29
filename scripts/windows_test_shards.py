"""Partition every Windows test once; verify actual execution before the release gate."""

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SHARDS = 4
SERIAL_GROUPS = {
    'workspace': ['tests/test_workspace_ui.py::test_workspace_mutation_recovery_in_javascript'],
    'native': ['tests/test_startup_native.py'],
    'owner-pipe': ['tests/test_owner_json_pipe.py'],
    'terminal': ['tests/test_terminal_children.py', 'tests/test_terminal_pty.py',
                 'tests/test_terminal_conpty.py', 'tests/test_file_search_terminal_workflow.py'],
    'reconnect': ['tests/test_shared_agent_reconnect.py'],
}
COSTS = ROOT / 'scripts/ci/windows_test_costs.json'


def _matches(node: str, selector: str) -> bool:
    return node == selector or node.startswith(selector + '::')


def partition(nodes: list[str], costs: dict[str, Any]) -> dict[str, Any]:
    """Greedy file allocation; recorded durations affect balance, never coverage."""
    if not nodes or len(set(nodes)) != len(nodes):
        raise ValueError('Full collection must contain unique test IDs')
    groups: dict[str, dict[str, Any]] = {}
    serial_nodes: set[str] = set()
    for name, selectors in SERIAL_GROUPS.items():
        selected = [node for node in nodes if any(_matches(node, value) for value in selectors)]
        if not selected or serial_nodes.intersection(selected):
            raise ValueError('Serial selection is empty or overlaps another serial group')
        for selector in selectors:
            if not any(_matches(node, selector) for node in selected):
                raise ValueError('Serial selector no longer exists: ' + selector)
        serial_nodes.update(selected)
        groups['serial-' + name] = {'selectors': selectors, 'nodes': sorted(selected)}
    by_file: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        if node not in serial_nodes:
            by_file[node.split('::', 1)[0]].append(node)
    history = costs['files']
    per_test = [value['seconds'] / value['tests'] for value in history.values()
                if value['tests'] > 0 and value['seconds'] > 0]
    fallback = statistics.median(per_test)
    weights: dict[str, float] = {}
    for path, selected in by_file.items():
        previous = history.get(path)
        estimate = previous['seconds'] / previous['tests'] if previous else fallback
        weight = max(0.001, estimate * len(selected))
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError('Invalid historical test duration')
        weights[path] = weight
    loads = [0.0] * SHARDS
    shard_files: list[list[str]] = [[] for _ in range(SHARDS)]
    for path in sorted(by_file, key=lambda item: (-weights[item], item)):
        index = min(range(SHARDS), key=lambda value: (loads[value], len(shard_files[value]), value))
        shard_files[index].append(path)
        loads[index] += weights[path]
    for index, paths in enumerate(shard_files, start=1):
        if not paths:
            raise ValueError('Every Windows shard must select tests')
        groups[f'shard-{index}'] = {
            'selectors': sorted(paths),
            'nodes': sorted(node for path in paths for node in by_file[path]),
            'estimated_test_seconds': round(loads[index - 1], 6),
        }
    plan = {'format_version': 1, 'all_nodes': sorted(nodes), 'groups': groups}
    validate_plan(plan)
    return plan


def validate_plan(plan: dict[str, Any]) -> None:
    expected_groups = {'serial-' + name for name in SERIAL_GROUPS} | {
        f'shard-{index}' for index in range(1, SHARDS + 1)}
    if plan.get('format_version') != 1 or set(plan.get('groups', {})) != expected_groups:
        raise ValueError('Unexpected Windows shard plan format or groups')
    all_nodes = plan['all_nodes']
    selected = [node for group in plan['groups'].values() for node in group['nodes']]
    if (not all_nodes or len(set(all_nodes)) != len(all_nodes)
            or Counter(selected) != Counter(all_nodes)):
        raise ValueError('Windows test partition has missing, repeated or unexpected tests')
    seen_files: set[str] = set()
    for name, group in plan['groups'].items():
        if not group['nodes'] or not group['selectors']:
            raise ValueError('Empty Windows test group')
        if name.startswith('shard-'):
            paths = {node.split('::', 1)[0] for node in group['nodes']}
            if (paths != set(group['selectors']) or seen_files.intersection(paths)
                    or len(group['selectors']) != len(paths)):
                raise ValueError('Windows test file belongs to multiple or incorrect shards')
            seen_files.update(paths)
        elif group['selectors'] != SERIAL_GROUPS[name.removeprefix('serial-')]:
            raise ValueError('Serial timing-sensitive test selectors changed')


def plan_digest(plan: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


def _pytest(arguments: list[str], output: Path) -> int:
    import pytest

    class Recorder:
        def __init__(self) -> None:
            self.collected: list[str] = []
            self.finished: list[str] = []
            self.workers = 0
            self.distribution = 'no'

        def pytest_sessionstart(self, session: pytest.Session) -> None:
            self.workers = int(session.config.getoption('numprocesses', default=0) or 0)
            self.distribution = str(session.config.getoption('dist', default='no'))

        def pytest_collection_finish(self, session: pytest.Session) -> None:
            self.collected = [item.nodeid for item in session.items]

        def pytest_runtest_logfinish(self, nodeid: str) -> None:
            self.finished.append(nodeid)

    recorder = Recorder()
    started = time.monotonic()
    code = int(pytest.main(arguments, plugins=[recorder]))
    output.write_text(json.dumps({'exit_code': code, 'collected': recorder.collected,
                                 'finished': recorder.finished, 'workers': recorder.workers,
                                 'distribution': recorder.distribution,
                                 'seconds': time.monotonic() - started}) + '\n', encoding='utf-8')
    return code


def collect(root: Path, arguments: list[str]) -> list[str]:
    with tempfile.TemporaryDirectory(prefix='anywhere-pytest-collect-') as temporary:
        output = Path(temporary) / 'collection.json'
        result = subprocess.run([sys.executable, __file__, '_pytest', '--output', str(output),
                                 '--', '--collect-only', '-q', *arguments],
                                cwd=root, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not output.is_file():
            raise ValueError('Test collection failed:\n' + (result.stdout + result.stderr)[-20000:])
        return json.loads(output.read_text(encoding='utf-8'))['collected']


def selectors(plan: dict[str, Any], group_name: str) -> list[str]:
    result = list(plan['groups'][group_name]['selectors'])
    if group_name.startswith('shard-'):
        result.extend('--deselect=' + selector for values in SERIAL_GROUPS.values()
                      for selector in values if '::' in selector)
    return result


def run_group(root: Path, plan: dict[str, Any], name: str, output: Path) -> int:
    validate_plan(plan)
    names = ['serial-' + group for group in SERIAL_GROUPS] if name == 'serial' else [name]
    if any(group not in plan['groups'] for group in names):
        raise ValueError('Unknown Windows test group')
    output.mkdir(parents=True, exist_ok=True)
    for group in names:
        arguments = selectors(plan, group)
        actual = collect(root, arguments)
        if Counter(actual) != Counter(plan['groups'][group]['nodes']):
            raise ValueError('Current collection differs from the planned group: ' + group)
        report_path = output / f'windows-{group}-run.json'
        test_args = ['-ra', '--durations=20', *arguments,
                     '--junitxml=' + str(output / f'pytest-windows-{group}.xml')]
        if group.startswith('shard-'):
            test_args.extend(['-n', '2', '--dist', 'loadfile', '--max-worker-restart=0'])
        started = time.monotonic()
        result = subprocess.run([sys.executable, __file__, '_pytest', '--output', str(report_path),
                                 '--', *test_args], cwd=root, check=False)
        if not report_path.is_file():
            raise ValueError('Pytest did not write its execution receipt: ' + group)
        report = json.loads(report_path.read_text(encoding='utf-8'))
        report.update({'group': group, 'plan_sha256': plan_digest(plan),
                       'process_seconds': time.monotonic() - started,
                       'expected_nodes': plan['groups'][group]['nodes']})
        report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        if result.returncode != 0 or report['exit_code'] != 0:
            return result.returncode or 1
        if Counter(report['finished']) != Counter(plan['groups'][group]['nodes']):
            raise ValueError('Executed tests differ from the planned group: ' + group)
    return 0


def verify_results(plan: dict[str, Any], directory: Path) -> dict[str, Any]:
    validate_plan(plan)
    finished: list[str] = []
    seconds: dict[str, float] = {}
    for name, group in plan['groups'].items():
        report = json.loads((directory / f'windows-{name}-run.json').read_text(encoding='utf-8'))
        if (report['group'] != name or report['plan_sha256'] != plan_digest(plan)
                or report['exit_code'] != 0 or report['expected_nodes'] != group['nodes']
                or report['workers'] != (2 if name.startswith('shard-') else 0)
                or (name.startswith('shard-') and report['distribution'] != 'loadfile')
                or Counter(report['finished']) != Counter(group['nodes'])):
            raise ValueError('Windows execution receipt is incomplete or inconsistent: ' + name)
        finished.extend(report['finished'])
        seconds[name] = report['process_seconds']
    if Counter(finished) != Counter(plan['all_nodes']):
        raise ValueError('Windows execution has missing, repeated or unexpected tests')
    serial = sum(value for name, value in seconds.items() if name.startswith('serial-'))
    shards = [value for name, value in seconds.items() if name.startswith('shard-')]
    return {'tests': len(finished), 'serial_process_seconds': serial,
            'slowest_shard_process_seconds': max(shards),
            'all_shard_process_seconds': sum(shards),
            'test_process_critical_path_seconds': serial + max(shards),
            'test_process_total_seconds': serial + sum(shards),
            'runner_setup_queue_build_excluded': True,
            'planning_and_validation_excluded': True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    planning = subparsers.add_parser('plan')
    planning.add_argument('--output', required=True, type=Path)
    running = subparsers.add_parser('run')
    running.add_argument('--plan', required=True, type=Path)
    running.add_argument('--group', required=True)
    running.add_argument('--results', required=True, type=Path)
    verifying = subparsers.add_parser('verify')
    verifying.add_argument('--plan', required=True, type=Path)
    verifying.add_argument('--results', required=True, type=Path)
    internal = subparsers.add_parser('_pytest')
    internal.add_argument('--output', required=True, type=Path)
    internal.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == '_pytest':
        raise SystemExit(_pytest(args.arguments[1:], args.output))
    if args.command == 'plan':
        plan = partition(collect(ROOT, ['tests']), json.loads(COSTS.read_text(encoding='utf-8')))
        args.output.write_text(json.dumps(plan, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({name: {'tests': len(group['nodes']), 'files': len(group['selectors']),
                                'estimated_test_seconds': group.get('estimated_test_seconds')}
                          for name, group in plan['groups'].items()}, indent=2))
    else:
        plan = json.loads(args.plan.read_text(encoding='utf-8'))
        if args.command == 'run':
            raise SystemExit(run_group(ROOT, plan, args.group, args.results))
        summary = json.dumps(verify_results(plan, args.results), indent=2)
        (args.results / 'windows-test-summary.json').write_text(summary + '\n', encoding='utf-8')
        print(summary)


if __name__ == '__main__':
    main()
