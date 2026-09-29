"""Research comparisons use durable receipts and never manufacture usage savings."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from anywhere_computer.state import Ledger
from anywhere_computer.subchat_state import (
    SubchatInputReference,
    SubchatSubmissions,
    SubchatWorkContext,
)

spec = importlib.util.spec_from_file_location(
    'research_measurement', Path(__file__).resolve().parents[1]
    / 'scripts/measure_subchat_research.py')
assert spec is not None and spec.loader is not None
measurement = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = measurement
spec.loader.exec_module(measurement)


def make_plan():
    return measurement.Plan(task_sha256='f' * 64,
        selection=measurement.Selection(model='fixture', effort='fixture'),
        runs=[measurement.Run(run_id='1' * 32, mode='single_chat', planned_intents=['a' * 32]),
              measurement.Run(run_id='2' * 32, mode='parallel_subchat',
                              planned_intents=['b' * 32, 'c' * 32, 'd' * 32])])


def add_submission(store, plan, run, operation, intent, *,
                   owner='private-owner', state='completed'):
    store.prepare(operation, 'private prompt and secret account notes', 'fixture', 'fixture',
                  owner=owner, intent_key=intent,
                  work_context=SubchatWorkContext(task_id=run.run_id, device_id='private-device',
                      workspace='/private/workspace', inputs=(SubchatInputReference(
                          reference='/private/input', sha256=plan.task_sha256),)))
    if state == 'prepared':
        return
    store.begin_send(operation, owner=owner)
    store.record_http_event(operation, 'generation_request', owner=owner)
    if state == 'sending':
        return
    store.submitted(operation, 'private-conversation-' + operation, 'private-user-' + operation,
                    owner=owner)
    store.complete(operation, 'private-answer-' + operation, 'private answer content', owner=owner)
    store.record_http_event(operation, 'history_final', owner=owner)


def test_capture_selects_tagged_owner_and_read_only_receipts(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        plan = make_plan()
        run = plan.runs[1]
        add_submission(store, plan, run, 'a' * 32, run.planned_intents[0])
        add_submission(store, plan, run, 'b' * 32, run.planned_intents[1], state='sending')
        add_submission(store, plan, run, 'c' * 32, run.planned_intents[2], owner='another-owner')
        add_submission(store, plan, plan.runs[0], 'd' * 32, plan.runs[0].planned_intents[0])
        before = '\n'.join(ledger.connection.iterdump())
        snapshot = measurement.capture(tmp_path / 'operations.sqlite3', 'private-owner',
                                       plan, run, 'final')
        assert '\n'.join(ledger.connection.iterdump()) == before
        assert len(snapshot.operations) == 2
        assert [o.provider_receipt for o in snapshot.operations] == ['confirmed', 'unconfirmed']
        assert snapshot.operations[0].history_final_at is not None
        assert snapshot.operations[1].history_final_at is None
        assert all(o.input_reference_matches and o.requested_selection_matches
                   for o in snapshot.operations)
        serialized = snapshot.model_dump_json()
        for forbidden in ('private-', 'private ', '/private/', 'Bearer', 'conversation_url'):
            assert forbidden not in serialized
    finally:
        ledger.close()


def test_report_catches_three_requested_but_eight_created_without_count_parser(
        tmp_path, monkeypatch):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        plan = make_plan()
        samples = []
        for index, run in enumerate(plan.runs):
            with monkeypatch.context() as clock:
                clock.setattr(measurement.time, 'time', lambda index=index: 100.0 + index * 100)
                samples.append(measurement.capture(tmp_path / 'operations.sqlite3',
                               'private-owner', plan, run, 'start'))
            count = 1 if index == 0 else 8
            for number in range(count):
                identity = f'{index * 10 + number + 10:032x}'
                intent = (run.planned_intents[number]
                          if number < len(run.planned_intents) else identity)
                with monkeypatch.context() as clock:
                    clock.setattr(measurement.time, 'time',
                                  lambda index=index, number=number: 110.0 + index * 100 + number)
                    add_submission(store, plan, run, identity, intent)
            with monkeypatch.context() as clock:
                clock.setattr(measurement.time, 'time', lambda index=index: 150.0 + index * 100)
                samples.append(measurement.capture(tmp_path / 'operations.sqlite3',
                               'private-owner', plan, run, 'final'))
        result = measurement.report(plan, samples)
        parallel = result['runs']['parallel_subchat']
        assert parallel['requested_child_count'] == 3
        assert parallel['actual_child_count_lower_bound'] == 8
        assert parallel['unexpected_intent_operations'] == 5
        assert parallel['workflow_wall_seconds'] == 50
        assert parallel['tool_calls'] is None and parallel['quality_rubric'] is None
        assert result['usage_comparison'] is None
        assert result['usage_comparison_state'] == 'unavailable'
        assert result['savings_conclusion'] == 'not_established'
        assert 'private' not in json.dumps(result)
    finally:
        ledger.close()


def samples_for(plan):
    return [measurement.Snapshot(run_id=run.run_id,
                specification_sha256=measurement.specification(plan, run),
                phase=phase, observed_at=timestamp, operations=[])
            for run in plan.runs for phase, timestamp in [('start', 100.0), ('final', 150.0)]]


def test_usage_compares_only_matching_whole_workflow_counters():
    plan = make_plan()
    def counter(scope, value, meter='a'):
        return measurement.Usage(provider='codex', unit='total_tokens', scope=scope,
            meter_sha256=meter * 64, evidence_sha256='b' * 64, before=0.0, after=value)
    plan.runs[0].usage = [counter('whole_workflow', 100.0), counter('account_window', 900.0)]
    plan.runs[1].usage = [counter('whole_workflow', 120.0), counter('account_window', 901.0)]
    result = measurement.report(plan, samples_for(plan))
    assert result['usage_comparison'] == [
        {'provider': 'codex', 'unit': 'total_tokens', 'parallel_minus_single': 20.0}]
    plan.runs[1].usage = [counter('whole_workflow', 120.0, 'c')]
    assert measurement.report(plan, samples_for(plan))['usage_comparison'] is None
    with pytest.raises(ValueError):
        counter('whole_workflow', -1.0)
    with pytest.raises(ValueError):
        measurement.Usage(provider='codex', unit='percent', scope='whole_workflow',
            meter_sha256='a' * 64, evidence_sha256='b' * 64, before=0.0, after=1.0)


def test_overlap_counts_time_with_two_or_more_operations_once():
    assert measurement.overlap([(10, 30), (20, 40), (25, 35)]) == 15
    assert measurement.overlap([(10, 20), (20, 30)]) == 0


def test_changed_plan_or_missing_start_refuses_comparison():
    plan = make_plan()
    snapshots = samples_for(plan)
    with pytest.raises(ValueError):
        measurement.report(plan, snapshots[1:])
    plan.runs[1].planned_intents.append('e' * 32)
    with pytest.raises(ValueError):
        measurement.report(plan, snapshots)


def test_cli_refuses_oversized_or_private_invalid_data_without_echo(tmp_path, capsys):
    plan_file = tmp_path / 'plan.json'
    plan_file.write_text('{"private-account": "secret-prompt-content"}')
    status = measurement.main(['report', '--plan', str(plan_file), '--snapshots', str(plan_file)])
    assert status == 2
    output = capsys.readouterr()
    assert 'private-account' not in output.err and 'secret-prompt-content' not in output.err
    plan_file.write_bytes(b'x' * (measurement.MAX_FILE + 1))
    with pytest.raises(ValueError):
        measurement.read_file(plan_file)


def test_snapshot_write_never_overwrites_saved_evidence(tmp_path):
    plan = make_plan()
    path = tmp_path / 'plan.json'
    measurement.save(path, plan)
    saved = path.read_bytes()
    with pytest.raises(FileExistsError):
        measurement.save(path, plan)
    assert path.read_bytes() == saved
