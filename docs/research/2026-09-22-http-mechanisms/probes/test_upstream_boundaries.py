"""Offline characterization of pinned upstream functions, not full-app acceptance.

Only selected reviewed AST definitions execute. Module initialization, credential
loading, browser code and challenge modules are not imported. All network connect
attempts are forbidden. The HTTP method is exercised with an in-memory fake only.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import dataclasses
import hashlib
import json
import socket
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SOURCES = {
    'transport': ('suphotP--chatgpt-api/chatgpt_api/providers/chatgpt/transport.py',
                  '0a28a7b1815f2af4e049a37590c405bfe9d3e048ad619cea967d3d0f8b7e9037'),
    'compat': ('suphotP--chatgpt-api/chatgpt_api/api/openai_compat.py',
               '48d1fbf477043292302b74763577260861a78c6437c0def4d21863d2d2f7c0ae'),
    'history': ('robotlearning123--gpt2agent/gpt2agent/tools/conversations.py',
                '5ddc955690f21d82a84d597c080028491b53900b2423e0e1069ce74d206f4a0f'),
}
EXTRACTED = []
OBSERVATIONS = []
NETWORK_ATTEMPTS = []


def selected(key, functions=(), methods=None, namespace=None):
    relative, expected = SOURCES[key]
    path = ROOT / relative
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == expected, 'Source revision changed'
    tree = ast.parse(raw)
    nodes = [ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)]
    found = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions:
            nodes.append(node)
            found.add(node.name)
            EXTRACTED.append({'source': relative, 'sha256': expected, 'symbol': node.name,
                              'start_line': node.lineno, 'end_line': node.end_lineno})
        if isinstance(node, ast.ClassDef) and methods and node.name in methods:
            wanted = methods[node.name]
            chosen = [x for x in node.body if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and x.name in wanted]
            assert {x.name for x in chosen} == set(wanted)
            cls = copy.deepcopy(node)
            cls.body = chosen
            cls.decorator_list = []
            cls.bases = []
            nodes.append(cls)
            for item in chosen:
                EXTRACTED.append({'source': relative, 'sha256': expected,
                                  'symbol': node.name + '.' + item.name,
                                  'start_line': item.lineno, 'end_line': item.end_lineno})
    assert found == set(functions)
    env = {'json': json, 'asyncio': asyncio, 'threading': threading}
    env.update(namespace or {})
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 str(path), 'exec'), env)
    return env


def observe(case, undesirable=False, **data):
    OBSERVATIONS.append({'case': case, 'undesirable_for_anywhere': undesirable, **data})


def line_reader(lines):
    calls = []
    def post(*args, **kwargs):
        calls.append({'stream': kwargs['stream'], 'payload': kwargs['data'].decode()})
        return types.SimpleNamespace(status_code=200, iter_lines=lambda: iter(lines))
    fake = types.ModuleType('curl_cffi')
    fake.requests = types.SimpleNamespace(post=post)
    env = selected('transport', methods={'ChatGPTWebTransport': ['_iter_post_conversation']},
                   namespace={'_conversation_headers': lambda headers: dict(headers)})
    obj = env['ChatGPTWebTransport']()
    obj.impersonate = 'synthetic-not-a-browser-profile'
    obj.timeout = 1
    with patch.dict(sys.modules, {'curl_cffi': fake}):
        output = list(obj._iter_post_conversation('https://fixture.invalid/not-sent', {}, {}))
    assert len(calls) == 1
    return output


class ParserCharacterization(unittest.TestCase):
    def test_spaced_data_baseline(self):
        output = line_reader([b'data: {"value":1}', b''])
        self.assertEqual(output, [{'value': 1}])
        observe('spaced_data_baseline', output=output)

    def test_valid_data_without_space_is_dropped(self):
        output = line_reader([b'data:{"value":1}', b''])
        self.assertEqual(output, [])
        observe('valid_no_space_dropped', True, output=output)

    def test_valid_multiline_json_event_is_dropped(self):
        output = line_reader([b'data: {"value":', b'data: 1}', b''])
        self.assertEqual(output, [])
        observe('valid_multiline_dropped', True, output=output)

    def test_unterminated_sse_frame_is_emitted(self):
        output = line_reader([b'data: {"value":1}'])
        self.assertEqual(output, [{'value': 1}])
        observe('unterminated_frame_emitted', True, output=output)

    def test_replace_is_not_an_append_delta(self):
        env = selected('transport', functions=['_event_to_delta', '_extract_text'],
                       namespace={'ChatDelta': lambda **kwargs: types.SimpleNamespace(**kwargs)})
        events = [{'p': '/message/content/parts/0', 'o': 'replace', 'v': text}
                  for text in ['Hel', 'Hello']]
        values = [env['_event_to_delta'](event).text for event in events]
        self.assertEqual(values, ['Hel', 'Hello'])
        observe('replace_not_reduced', True, emitted_text=values,
                concatenated=''.join(values), correct_snapshot='Hello')

    def test_unhandled_operation_can_still_emit_text(self):
        env = selected('transport', functions=['_event_to_delta', '_extract_text'],
                       namespace={'ChatDelta': lambda **kwargs: types.SimpleNamespace(**kwargs)})
        delta = env['_event_to_delta']({'p': '/message/content/parts/0',
                                       'o': 'unknown_fixture_operation', 'v': 'fixture'})
        self.assertEqual(delta.text, 'fixture')
        observe('operation_not_checked_by_projection', True, emitted_text=delta.text)

    def test_conflicting_conversation_ids_choose_first(self):
        env = selected('transport', functions=['_conversation_id_from_events'])
        value = env['_conversation_id_from_events']([{'conversation_id': 'chat-a'},
                                                    {'conversation_id': 'chat-b'}])
        self.assertEqual(value, 'chat-a')
        observe('first_id_without_conflict_check', True, chosen=value)

    def test_captured_payload_overrides_new_request(self):
        env = selected('transport', methods={'ChatGPTWebTransport': ['build_chat_payload']})
        old = {'model': 'old-fixture-model', 'messages': [{'content': 'old-fixture-prompt'}]}
        request = types.SimpleNamespace(metadata={'captured_request_json': old},
                                        model='new-fixture-model', messages=['new-fixture-prompt'])
        output = env['ChatGPTWebTransport']().build_chat_payload(request)
        self.assertEqual(output, old)
        observe('explicit_capture_passthrough', True, selected_request_ignored=True,
                note='Explicit override feature, not proof that ordinary non-capture calls ignore input')


class FakeMCP:
    def __init__(self):
        self.tools = {}
    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function
        return register


def node(identity, parent, text, created):
    return {'parent': parent, 'message': {'id': identity,
            'author': {'role': 'assistant'}, 'channel': 'final', 'end_turn': True,
            'status': 'finished_successfully', 'create_time': created,
            'metadata': {'request_id': 'fixture-request'},
            'content': {'content_type': 'text', 'parts': [text]}}}


async def history_tool(payload):
    async def fake_get(*args, **kwargs):
        return payload
    env = selected('history', functions=['register'], namespace={
        'async_get': fake_get, 'validate_path_id': lambda value, **kwargs: value,
        'redact': lambda value: value})
    mcp = FakeMCP()
    env['register'](mcp, object())
    return await mcp.tools['get_conversation']('fixture-conversation')


class AsyncCharacterization(unittest.IsolatedAsyncioTestCase):
    async def test_history_text_is_truncated_without_flag(self):
        output = await history_tool({'id': 'fixture', 'current_node': 'answer',
                    'mapping': {'answer': node('answer', None, 'x' * 5000, 1)}})
        message = output['messages'][0]
        self.assertEqual(len(message['text']), 2000)
        self.assertFalse(any('trunc' in key for key in (*output, *message)))
        self.assertNotIn('channel', message)
        self.assertNotIn('end_turn', message)
        self.assertNotIn('metadata', message)
        observe('history_truncation', True, input_chars=5000, returned_chars=2000,
                truncation_flag=False, completion_correlation_metadata_retained=False)

    async def test_active_branch_excludes_sibling(self):
        payload = {'id': 'fixture', 'current_node': 'a', 'mapping': {
            'root': node('root', None, 'root', 1),
            'a': node('a', 'root', 'selected', 2), 'b': node('b', 'root', 'sibling', 3)}}
        output = await history_tool(payload)
        self.assertEqual([item['id'] for item in output['messages']], ['root', 'a'])
        observe('active_branch_baseline', message_ids=['root', 'a'])

    async def test_missing_current_node_blends_branches(self):
        payload = {'id': 'fixture', 'mapping': {
            'root': node('root', None, 'root', 1),
            'a': node('a', 'root', 'branch a', 2), 'b': node('b', 'root', 'branch b', 3)}}
        output = await history_tool(payload)
        self.assertEqual([item['id'] for item in output['messages']], ['root', 'a', 'b'])
        observe('missing_branch_fallback', True, message_ids=['root', 'a', 'b'])

    async def test_cancelled_wait_loses_semaphore_permit(self):
        env = selected('compat', methods={'AccountRouter': ['acquire']},
                       namespace={'AccountLease': lambda **kwargs: types.SimpleNamespace(**kwargs)})
        entered, acquired = threading.Event(), threading.Event()
        class ObservableSemaphore(threading.BoundedSemaphore):
            def acquire(self, *args, **kwargs):
                entered.set()
                value = super().acquire(*args, **kwargs)
                if value:
                    acquired.set()
                return value
        sem = ObservableSemaphore(1)
        threading.BoundedSemaphore.acquire(sem)
        router = env['AccountRouter']()
        router._lock = threading.Lock()
        router._limiters = {'fixture': sem}
        task = asyncio.create_task(router.acquire('fixture'))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            sem.release()
            self.assertTrue(await asyncio.to_thread(acquired.wait, 2))
            available = threading.BoundedSemaphore.acquire(sem, blocking=False)
            self.assertFalse(available)
            observe('cancelled_wait_ghost_acquire', True, caller_cancelled=True,
                    background_acquired=True, permit_available=False)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            # Release the stranded synthetic permit, never leave the test worker blocked.
            if acquired.is_set():
                sem.release()


def main():
    def forbidden(*args, **kwargs):
        NETWORK_ATTEMPTS.append('blocked')
        raise AssertionError('No network connections in upstream characterization')
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    with patch.object(socket.socket, 'connect', forbidden), patch.object(socket, 'create_connection', forbidden):
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    unique = {json.dumps(item, sort_keys=True): item for item in EXTRACTED}
    summary = {'scope': 'selected AST functions, synthetic data and fake HTTP; not whole projects',
               'tests_run': result.testsRun, 'failures': len(result.failures),
               'errors': len(result.errors), 'network_connect_attempts': len(NETWORK_ATTEMPTS),
               'live_generation_posts': 0, 'credential_access': False,
               'observations': OBSERVATIONS, 'source_definitions': list(unique.values())}
    with (ROOT / 'upstream-characterization.json').open('x', encoding='utf-8') as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'tests_run': result.testsRun, 'success': result.wasSuccessful(),
                      'observations': OBSERVATIONS}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.wasSuccessful() and not NETWORK_ATTEMPTS else 1)


if __name__ == '__main__':
    main()
