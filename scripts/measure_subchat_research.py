"""Read-only, bounded measurements for one Chat versus independent Subchats.

Creates a local plan and sanitized ledger snapshots; never opens a browser,
changes the operation ledger, sends a message, or starts a model turn.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from anywhere_computer.subchat_state import (
    SubchatHTTPSelection,
    SubchatSubmission,
    provider_receipt_state,
)

MAX_FILE = 2 * 1024 * 1024
MAX_OPERATIONS = 256
Identifier = Annotated[str, Field(pattern=r'^[0-9a-f]{32}$')]
Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Timestamp = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Mode = Literal['single_chat', 'parallel_subchat']


class Measurement(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Selection(Measurement):
    model: str = Field(min_length=1, max_length=256)
    effort: str = Field(min_length=1, max_length=256)
    http_selection: SubchatHTTPSelection | None = None


class Rubric(Measurement):
    # 0 = absent/wrong, 1 = partly met, 2 = fully checked against the task.
    accuracy: int = Field(ge=0, le=2)
    source_support: int = Field(ge=0, le=2)
    completeness: int = Field(ge=0, le=2)
    traceability: int = Field(ge=0, le=2)
    reviewed_output_sha256: Digest


class Usage(Measurement):
    provider: Literal['codex', 'chatgpt']
    unit: Literal['input_tokens', 'output_tokens', 'total_tokens', 'credits', 'percent']
    scope: Literal['whole_workflow', 'workers_only', 'account_window']
    meter_sha256: Digest
    evidence_sha256: Digest
    before: float = Field(ge=0, allow_inf_nan=False)
    after: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode='after')
    def valid_counter(self) -> Usage:
        if self.after < self.before:
            raise ValueError('Counter reset cannot be attributed to this run')
        if self.unit == 'percent' and (self.scope != 'account_window' or self.after > 100):
            raise ValueError('Percent counters only describe account windows')
        return self


class Run(Measurement):
    run_id: Identifier
    mode: Mode
    planned_intents: list[Identifier] = Field(min_length=1, max_length=MAX_OPERATIONS)
    tool_calls: int | None = Field(default=None, ge=0)
    rubric: Rubric | None = None
    usage: list[Usage] = Field(default_factory=list, max_length=16)

    @model_validator(mode='after')
    def unique_intents(self) -> Run:
        if len(set(self.planned_intents)) != len(self.planned_intents):
            raise ValueError('Duplicate planned intent')
        if self.mode == 'single_chat' and len(self.planned_intents) != 1:
            raise ValueError('Single Chat must have one planned worker')
        keys = [(u.provider, u.unit, u.scope, u.meter_sha256) for u in self.usage]
        if len(keys) != len(set(keys)):
            raise ValueError('Duplicate usage counter')
        return self


class Plan(Measurement):
    schema_version: Literal[1] = 1
    task_sha256: Digest
    selection: Selection
    runs: list[Run] = Field(min_length=2, max_length=2)

    @model_validator(mode='after')
    def paired_runs(self) -> Plan:
        if {r.mode for r in self.runs} != {'single_chat', 'parallel_subchat'}:
            raise ValueError('One baseline and one parallel run are required')
        if len({r.run_id for r in self.runs}) != 2:
            raise ValueError('Run identities must differ')
        intents = [i for r in self.runs for i in r.planned_intents]
        if len(intents) != len(set(intents)):
            raise ValueError('Run intent identities must differ')
        return self


class Operation(Measurement):
    operation_id: Identifier
    intent_key: Identifier | None
    state: Literal['queued', 'prepared', 'sending', 'submitted', 'completed', 'cancelled',
                   'interrupted', 'preflight_failed']
    provider_receipt: Literal['not_sent', 'unconfirmed', 'confirmed']
    new_chat: bool
    conversation_digest: Digest | None
    message_digest: Digest | None
    created_at: Timestamp | None
    generation_request_at: Timestamp | None
    history_final_at: Timestamp | None
    input_reference_matches: bool
    requested_selection_matches: bool


class Snapshot(Measurement):
    schema_version: Literal[1] = 1
    run_id: Identifier
    specification_sha256: Digest
    phase: Literal['start', 'checkpoint', 'final']
    observed_at: Timestamp
    operations: list[Operation] = Field(max_length=MAX_OPERATIONS)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_file(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError('Measurement file must be regular')
        raw = source.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        raise ValueError('Measurement file exceeds the limit')
    return raw


def specification(plan: Plan, run: Run) -> str:
    data = {'task_sha256': plan.task_sha256, 'selection': plan.selection.model_dump(),
            'run_id': run.run_id, 'mode': run.mode, 'planned_intents': run.planned_intents}
    return digest(json.dumps(data, sort_keys=True, separators=(',', ':')).encode())


def save(path: Path, document: Measurement) -> None:
    # Refuse overwriting another run's evidence; snapshots use distinct files.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        output.write(document.model_dump_json(indent=2) + '\n')


def capture(database: Path, owner: str | None, plan: Plan, run: Run,
            phase: Literal['start', 'checkpoint', 'final']) -> Snapshot:
    operations: list[Operation] = []
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True,
                                timeout=5)) as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')  # One coherent view across the side tables.
        deadline = time.monotonic() + 5
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if 'subchat_submissions' not in tables:
            raise ValueError('Subchat ledger is unavailable')
        rows = connection.execute(
            'SELECT operation_id, CASE WHEN length(body)<=? THEN body END '
            'FROM subchat_submissions WHERE owner IS ? AND '
            "CASE WHEN json_valid(body) THEN json_extract(body,'$.work_context.task_id') END=? "
            'ORDER BY rowid LIMIT ?', (MAX_FILE, owner, run.run_id, MAX_OPERATIONS + 1))
        total_size = 0
        for operation_id, body in rows:
            total_size += len(body or '')
            if len(operations) >= MAX_OPERATIONS or total_size > 8 * MAX_FILE:
                raise ValueError('Run exceeds the measurement bound')
            if body is None:
                raise ValueError('Submission exceeds the measurement bound')
            saved = SubchatSubmission.model_validate_json(body)
            if saved.operation_id != operation_id or saved.work_context is None:
                raise ValueError('Saved identity mismatch')
            intent = connection.execute(
                'SELECT intent_key FROM subchat_send_intents WHERE owner IS ? AND operation_id=?',
                (owner, operation_id)).fetchone() if 'subchat_send_intents' in tables else None
            created = connection.execute(
                'SELECT created_at FROM subchat_created_at WHERE owner IS ? AND operation_id=?',
                (owner, operation_id)).fetchone() if 'subchat_created_at' in tables else None
            checkpoints = dict(connection.execute(
                'SELECT stage, MIN(timestamp) FROM subchat_http_events WHERE operation_id=? '
                "AND stage IN ('generation_request','history_final') GROUP BY stage",
                (operation_id,))) if 'subchat_http_events' in tables else {}
            receipt = provider_receipt_state(saved)
            selection = Selection(model=saved.model, effort=saved.effort,
                                  http_selection=saved.http_selection)
            operations.append(Operation(
                operation_id=operation_id, intent_key=intent[0] if intent else None,
                state=saved.state, provider_receipt=receipt,
                new_chat=(saved.requested_conversation_id is None
                          and saved.after_operation_id is None),
                conversation_digest=(digest(saved.conversation_id.encode())
                                     if receipt == 'confirmed' and saved.conversation_id else None),
                message_digest=(digest((saved.conversation_id + '\0'
                                        + saved.user_message_id).encode())
                                if receipt == 'confirmed' and saved.conversation_id
                                and saved.user_message_id else None),
                created_at=created[0] if created else None,
                generation_request_at=checkpoints.get('generation_request'),
                history_final_at=checkpoints.get('history_final'),
                input_reference_matches=any(i.sha256 == plan.task_sha256
                                            for i in saved.work_context.inputs),
                requested_selection_matches=selection == plan.selection))
    return Snapshot(run_id=run.run_id, specification_sha256=specification(plan, run),
                    phase=phase, observed_at=time.time(), operations=operations)


def overlap(intervals: list[tuple[float, float]]) -> float:
    events = sorted([(a, 1) for a, _ in intervals] + [(b, -1) for _, b in intervals])
    active, previous, total = 0, 0.0, 0.0
    for timestamp, change in events:
        if active >= 2:
            total += timestamp - previous
        active += change
        previous = timestamp
    return total


def summarize(run: Run, snapshots: list[Snapshot]) -> dict[str, object]:
    samples = sorted((s for s in snapshots if s.run_id == run.run_id), key=lambda s: s.observed_at)
    if (len(samples) < 2 or samples[0].phase != 'start' or samples[-1].phase != 'final'
            or any(s.phase != 'checkpoint' for s in samples[1:-1])
            or samples[0].operations or samples[-1].observed_at <= samples[0].observed_at):
        raise ValueError('Run needs an empty start and a later final snapshot')
    operations = samples[-1].operations
    identities = {o.operation_id for o in operations}
    if len(identities) != len(operations) or any(
            o.operation_id not in identities for s in samples for o in s.operations):
        raise ValueError('Operation identities disappeared or are duplicated')
    receipts = Counter(o.provider_receipt for o in operations)
    new_chats = {o.conversation_digest for o in operations if o.new_chat and o.conversation_digest}
    bound_messages = [o.message_digest for o in operations if o.message_digest]
    intervals = [(o.generation_request_at, o.history_final_at) for o in operations]
    timing_complete = bool(intervals) and all(
        a is not None and b is not None
        and samples[0].observed_at <= a <= b <= samples[-1].observed_at for a, b in intervals)
    measured_intervals = [(a, b) for a, b in intervals
                          if a is not None and b is not None and a <= b]
    quality = None if run.rubric is None else run.rubric.model_dump(
        exclude={'reviewed_output_sha256'})
    return {
        'mode': run.mode, 'requested_child_count': len(run.planned_intents),
        'recorded_operation_count': len(operations), 'confirmed_new_chat_count': len(new_chats),
        'actual_child_count_lower_bound': len(new_chats),
        'child_count_complete': not receipts['unconfirmed'],
        'unexpected_intent_operations': sum(o.intent_key not in run.planned_intents
                                           for o in operations),
        'missing_planned_intents': len(set(run.planned_intents)
                                      - {o.intent_key for o in operations}),
        'repeated_receipt_bindings': len(bound_messages) - len(set(bound_messages)),
        'provider_receipts': {k: receipts[k] for k in ('not_sent', 'unconfirmed', 'confirmed')},
        'completed_operations': sum(o.state == 'completed' for o in operations),
        'all_recorded_inputs_match': bool(operations) and all(o.input_reference_matches
                                                            for o in operations),
        'all_requested_selections_match': bool(operations) and all(o.requested_selection_matches
                                                                  for o in operations),
        'workflow_wall_seconds': samples[-1].observed_at - samples[0].observed_at,
        'timing_basis': 'observer_start_to_final_including_coordination_and_synthesis',
        'http_dispatch_to_final_overlap_seconds': overlap(measured_intervals)
                                                  if timing_complete else None,
        'overlap_basis': 'retained_http_checkpoints' if timing_complete else 'unavailable',
        'tool_calls': run.tool_calls, 'quality_rubric': quality,
        'quality_basis': 'reviewer_supplied' if quality else 'unavailable',
        'usage': [{'provider': u.provider, 'unit': u.unit, 'scope': u.scope,
                   'measured_delta': u.after - u.before} for u in run.usage],
        'usage_state': 'recorded_counters' if run.usage else 'unavailable',
    }


def report(plan: Plan, snapshots: list[Snapshot]) -> dict[str, object]:
    specs = {r.run_id: specification(plan, r) for r in plan.runs}
    if len(snapshots) > 128 or any(
            specs.get(s.run_id) != s.specification_sha256 for s in snapshots):
        raise ValueError('Unexpected or excessive snapshots')
    runs = {r.mode: r for r in plan.runs}
    summaries = {mode: summarize(run, snapshots) for mode, run in runs.items()}
    baseline = {(u.provider, u.unit, u.scope, u.meter_sha256): u
                for u in runs['single_chat'].usage if u.scope == 'whole_workflow'}
    deltas = []
    for candidate in runs['parallel_subchat'].usage:
        key = (candidate.provider, candidate.unit, candidate.scope, candidate.meter_sha256)
        if key in baseline:
            before = baseline[key]
            deltas.append({'provider': candidate.provider, 'unit': candidate.unit,
                           'parallel_minus_single': (candidate.after - candidate.before)
                                                    - (before.after - before.before)})
    return {'schema_version': 1, 'task_sha256': plan.task_sha256,
            'runs': summaries, 'usage_comparison': deltas or None,
            'usage_comparison_state': 'measured_counters' if deltas else 'unavailable',
            'savings_conclusion': 'not_established',
            'limitations': ['input_references_are_caller_supplied_not_proof_of_child_access',
                            'http_history_final_is_observation_time_not_model_finish_time',
                            'account_window_usage_cannot_be_attributed_to_a_run',
                            'one_pair_does_not_establish_repeatable_savings']}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('plan')
    init.add_argument('--task-file', type=Path, required=True)
    init.add_argument('--selection', type=Path, required=True)
    init.add_argument('--parallel-children', type=int, default=2, choices=range(1, 257))
    init.add_argument('--output', type=Path, required=True)
    snap = sub.add_parser('capture')
    snap.add_argument('--plan', type=Path, required=True)
    snap.add_argument('--mode', choices=['single_chat', 'parallel_subchat'], required=True)
    snap.add_argument('--phase', choices=['start', 'checkpoint', 'final'], required=True)
    snap.add_argument('--database', type=Path, required=True)
    owner = snap.add_mutually_exclusive_group(required=True)
    owner.add_argument('--owner')
    owner.add_argument('--unowned', action='store_true')
    snap.add_argument('--output', type=Path, required=True)
    summary = sub.add_parser('report')
    summary.add_argument('--plan', type=Path, required=True)
    summary.add_argument('--snapshots', type=Path, nargs='+', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'plan':
            selection = Selection.model_validate_json(read_file(args.selection))
            runs = [Run(run_id=uuid4().hex, mode='single_chat', planned_intents=[uuid4().hex]),
                    Run(run_id=uuid4().hex, mode='parallel_subchat',
                        planned_intents=[uuid4().hex for _ in range(args.parallel_children)])]
            save(args.output, Plan(task_sha256=digest(read_file(args.task_file)),
                                   selection=selection, runs=runs))
        elif args.command == 'capture':
            plan = Plan.model_validate_json(read_file(args.plan))
            run = next(r for r in plan.runs if r.mode == args.mode)
            save(args.output, capture(args.database, args.owner, plan, run, args.phase))
        else:
            if len(args.snapshots) > 128:
                raise ValueError('Too many snapshots')
            plan = Plan.model_validate_json(read_file(args.plan))
            snapshots = [Snapshot.model_validate_json(read_file(p)) for p in args.snapshots]
            print(json.dumps(report(plan, snapshots), indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, sqlite3.Error):
        # Validation errors may contain prompts, paths or account IDs. Keep them local.
        print('Measurement refused: invalid input, incomplete evidence, or unreadable ledger.',
              file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
