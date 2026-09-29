"""Real pytest collection/execution proves file shards and serial tests form one suite."""

import copy
import importlib.util
import json
import re
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sharding():
    spec = importlib.util.spec_from_file_location(
        'windows_test_shards', ROOT / 'scripts/windows_test_shards.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def suite(tmp_path, monkeypatch, sharding):
    tests = tmp_path / 'tests'
    tests.mkdir()
    (tmp_path / 'pytest.ini').write_text('[pytest]\ntestpaths = tests\n')
    (tests / 'test_serial.py').write_text('def test_timing():\n    assert True\n')
    (tests / 'test_shared.py').write_text(
        'def test_sensitive():\n    assert True\n\ndef test_remaining():\n    assert True\n')
    for index in range(4):
        (tests / f'test_part_{index}.py').write_text(
            'import pytest\n@pytest.mark.parametrize("value", [1, 2])\n'
            'def test_value(value):\n    assert value in (1, 2)\n'
            '@pytest.mark.skip(reason="fixture skip remains collected")\n'
            'def test_skip():\n    raise AssertionError("must stay skipped")\n')
    monkeypatch.setattr(sharding, 'SERIAL_GROUPS', {
        'timing': ['tests/test_serial.py'],
        'special': ['tests/test_shared.py::test_sensitive'],
    })
    costs = {'files': {'tests/test_part_0.py': {'tests': 3, 'seconds': 30}}}
    nodes = sharding.collect(tmp_path, ['tests'])
    plan = sharding.partition(nodes, costs)
    return tmp_path, plan, costs


def test_actual_two_worker_runs_cover_each_test_once_including_skips(sharding, suite):
    root, plan, _ = suite
    output = root / 'results'
    assert sharding.run_group(root, plan, 'serial', output) == 0
    for index in range(1, sharding.SHARDS + 1):
        assert sharding.run_group(root, plan, f'shard-{index}', output) == 0
    totals = sharding.verify_results(plan, output)
    assert totals['tests'] == 15
    assert totals['test_process_total_seconds'] >= totals['test_process_critical_path_seconds']
    assert totals['runner_setup_queue_build_excluded'] is True
    finished = []
    for path in output.glob('windows-*-run.json'):
        receipt = json.loads(path.read_text())
        assert receipt['workers'] == (2 if 'shard-' in receipt['group'] else 0)
        finished.extend(receipt['finished'])
    assert Counter(finished) == Counter(plan['all_nodes'])
    assert sum('test_skip' in node for node in finished) == 4
    report_path = output / 'windows-shard-1-run.json'
    report = json.loads(report_path.read_text())
    report['finished'].append(report['finished'][0])
    report_path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match='incomplete or inconsistent'):
        sharding.verify_results(plan, output)


def test_new_test_file_is_automatically_assigned_once(sharding, suite):
    root, old, costs = suite
    (root / 'tests/test_brand_new.py').write_text('def test_new():\n    assert True\n')
    plan = sharding.partition(sharding.collect(root, ['tests']), costs)
    assert len(plan['all_nodes']) == len(old['all_nodes']) + 1
    assert sum('tests/test_brand_new.py::test_new' in group['nodes']
               for group in plan['groups'].values()) == 1
    assert sharding.partition(list(reversed(plan['all_nodes'])), costs) == plan


@pytest.mark.parametrize('corruption', ['missing', 'duplicate', 'extra', 'file_overlap'])
def test_partition_proof_rejects_missing_or_repeated_selection(sharding, suite, corruption):
    _, original, _ = suite
    plan = copy.deepcopy(original)
    first = plan['groups']['shard-1']
    if corruption == 'missing':
        first['nodes'].pop()
    elif corruption == 'duplicate':
        first['nodes'].append(first['nodes'][0])
    elif corruption == 'extra':
        first['nodes'].append('tests/test_ghost.py::test_missing')
    else:
        plan['groups']['shard-2']['selectors'].append(first['selectors'][0])
    with pytest.raises(ValueError, match='partition|multiple or incorrect'):
        sharding.validate_plan(plan)


def test_changed_collection_stops_before_any_test_executes(sharding, suite):
    root, plan, _ = suite
    selected = plan['groups']['shard-1']['selectors'][0]
    path = root / selected
    path.write_text(path.read_text() + '\ndef test_added_after_plan():\n    assert True\n')
    with pytest.raises(ValueError, match='Current collection differs'):
        sharding.run_group(root, plan, 'shard-1', root / 'results')
    assert not list((root / 'results').glob('*-run.json'))


def test_missing_serial_selector_cannot_silently_move_to_parallel(sharding, suite):
    _, plan, costs = suite
    with pytest.raises(ValueError, match='Serial selection is empty'):
        sharding.partition([node for node in plan['all_nodes'] if 'test_timing' not in node], costs)


def test_failed_or_missing_execution_receipt_rejects_gate(sharding, suite):
    root, plan, _ = suite
    results = root / 'reports'
    results.mkdir()
    for name, group in plan['groups'].items():
        (results / f'windows-{name}-run.json').write_text(json.dumps({
            'group': name, 'plan_sha256': sharding.plan_digest(plan), 'exit_code': 0,
            'finished': group['nodes'], 'expected_nodes': group['nodes'], 'process_seconds': 1,
            'workers': 2 if name.startswith('shard-') else 0, 'distribution': 'loadfile',
        }))
    sharding.verify_results(plan, results)
    missing = results / 'windows-shard-4-run.json'
    report = json.loads(missing.read_text())
    report['exit_code'] = 1
    missing.write_text(json.dumps(report))
    with pytest.raises(ValueError, match='incomplete or inconsistent'):
        sharding.verify_results(plan, results)
    missing.unlink()
    with pytest.raises(FileNotFoundError):
        sharding.verify_results(plan, results)


def test_historical_costs_are_observations_not_an_allowlist():
    costs = json.loads((ROOT / 'scripts/ci/windows_test_costs.json').read_text())
    assert costs['source_run'].endswith('/36583627634')
    assert costs['remaining_step_seconds'] == 1092
    assert sum(row['tests'] for row in costs['files'].values()) == 2593
    assert round(sum(row['seconds'] for row in costs['files'].values()), 3) == 2166.536


def job(source, name):
    match = re.search(r'^  ' + re.escape(name) + r':\n(.*?)(?=^  [\w-]+:\n|\Z)',
                      source, re.MULTILINE | re.DOTALL)
    assert match is not None, name
    return match.group(1)


def test_workflow_preserves_required_context_and_dependency_failure_gate():
    source = (ROOT / '.github/workflows/quality.yml').read_text()
    common = job(source, 'shared-runtime')
    assert 'os: [ubuntu-latest, macos-latest]' in common
    assert '\n    needs:' not in common  # Linux/macOS do not wait for Windows.
    shards = job(source, 'windows-test-shards')
    assert 'needs: windows-preflight' in shards and 'fail-fast: false' in shards
    assert 'shard: [1, 2, 3, 4]' in shards
    assert 'scripts/windows_test_shards.py run' in shards
    assert '--group shard-${{ matrix.shard }}' in shards
    gate = job(source, 'windows-shared-runtime')
    assert 'name: shared-runtime (windows-latest)' in gate
    assert 'needs: [windows-preflight, windows-test-shards]' in gate
    assert 'if: always()' in gate
    assert "$env:PREFLIGHT_RESULT -ne 'success' -or $env:SHARDS_RESULT -ne 'success'" in gate
    assert 'scripts/windows_test_shards.py verify' in gate
    assert gate.index('scripts/windows_test_shards.py verify') < gate.index('uv build')
    assert 'name: portable-windows-latest' in gate
    assert 'needs: [shared-runtime, windows-shared-runtime]' in job(source, 'attest-portable')
    publication = job(source, 'publish-release')
    assert re.search(r'needs: \[[^\]]*windows-shared-runtime', publication)
    assert 'Release tag points at another commit; bump the version before merging' in publication
    preflight = job(source, 'windows-preflight')
    assert 'scripts/windows_test_shards.py plan' in preflight
    assert '--group serial' in preflight
    assert 'scripts/measure_test_costs.py' in preflight
