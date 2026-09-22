"""送信要求のローカル検査範囲を調べる。認証取得・外部通信・ブラウザー起動なし。
成功はAnywhereの検査器を通るという意味であり、ChatGPTの受理ではない。
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import socket
from pathlib import Path
from typing import Any

from anywhere_computer.subchat_browser.request_content import generation_input
from anywhere_computer.subchat_state import SubchatHTTPSelection, SubchatSubmission

ROOT = Path(__file__).parent
PROMPT = '合成テスト。外部送信しない。'
BASE = {
    'action': 'next',
    'messages': [{'id': 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',
                  'author': {'role': 'user'},
                  'content': {'content_type': 'text', 'parts': [PROMPT]},
                  'metadata': {}}],
}
SELECTION = SubchatHTTPSelection(version_id='fixture-version', preset_id=7,
                                model_slug='fixture-model', thinking_effort='fixture-effort')
NEW = SubchatSubmission(operation_id='a' * 32, prompt=PROMPT, model='fixture UI model',
                        effort='fixture UI effort')
CONSTRAINED = NEW.model_copy(update={'http_selection': SELECTION})
FOLLOWUP = CONSTRAINED.model_copy(update={
    'requested_conversation_id': '11111111-2222-4333-8444-555555555555',
    'conversation_id': '11111111-2222-4333-8444-555555555555',
})


def selected_body() -> dict[str, Any]:
    return {**copy.deepcopy(BASE), 'model': SELECTION.model_slug,
            'thinking_effort': SELECTION.thinking_effort}


def main() -> None:
    calls = 0
    original_connect = socket.socket.connect
    def forbid_connect(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError('この検証ではネットワーク接続を許可しない')
    socket.socket.connect = forbid_connect
    cases: list[dict[str, Any]] = []
    def check(name: str, body: dict[str, Any], submission: SubchatSubmission,
              expected: bool, interpretation: str) -> None:
        try:
            output = generation_input(json.dumps(body, ensure_ascii=False), submission)
            accepted, error = True, None
            assert output == body
        except ValueError as exc:
            accepted, error = False, type(exc).__name__
        cases.append({'case': name, 'guard_accepts': accepted, 'expected': expected,
                      'error_type': error, 'interpretation': interpretation})
        assert accepted is expected, name
    try:
        check('minimal_body_no_model_no_prepare', copy.deepcopy(BASE), NEW, True,
              '旧レコード互換の本文照合を通過するだけ。独立した生成要求としての充足性は未検証。')
        check('selected_new_without_conversation_or_parent', selected_body(), CONSTRAINED, True,
              'ローカル検査は新規送信のconversation_idとparent_message_id省略を許可する。')
        body = selected_body(); body['conversation_id'] = None
        check('new_explicit_null_conversation', body, CONSTRAINED, True,
              '新規での省略とnullはこの検査器では同じ。サーバーでの同値性は未証明。')
        body = selected_body(); body['conversation_id'] = FOLLOWUP.conversation_id
        check('new_invented_conversation', body, CONSTRAINED, False,
              '別の既知会話への混入はローカルで拒否。')
        body = selected_body(); del body['model']
        check('selected_model_missing', body, CONSTRAINED, False,
              'http_selectionを指定した場合はmodel一致が必要。')
        body = selected_body(); body['model'] = 'another-fixture'
        check('selected_model_wrong', body, CONSTRAINED, False,
              '指定モデルの置換を拒否。')
        body = selected_body(); del body['thinking_effort']
        check('selected_nonnull_effort_missing', body, CONSTRAINED, False,
              '非null effortの欠落を拒否。')
        no_effort = CONSTRAINED.model_copy(update={
            'http_selection': SELECTION.model_copy(update={'thinking_effort': None})})
        check('selected_null_effort_missing', body, no_effort, True,
              '選択値がnullなら、本文側省略も.get()でnullとして一致。')
        body = selected_body(); del body['messages'][0]['metadata']
        check('message_metadata_missing', body, CONSTRAINED, False,
              'このローカル検査ではmetadataの辞書が必要。')
        body = selected_body(); body['messages'][0]['author']['role'] = 'assistant'
        check('message_author_wrong', body, CONSTRAINED, False,
              '送信元ロールがuserでない本文を拒否。')
        body = selected_body(); body['messages'][0]['content']['parts'] = ['異なる合成本文']
        check('message_prompt_wrong', body, CONSTRAINED, False,
              '保存された入力と異なる本文を拒否。')
        body = selected_body(); body['conversation_id'] = FOLLOWUP.conversation_id
        check('followup_parent_missing', body, FOLLOWUP, True,
              'parent_message_idの有無はこの検査器の対象外。追送に不要と結論してはいけない。')
        body['parent_message_id'] = 'unrelated-fixture-parent'
        check('followup_parent_unverified', body, FOLLOWUP, True,
              '親IDの正しさはこの検査器では立証されない。')
        body = selected_body(); body['client_prepare_state'] = {'fixture_only': True}
        check('arbitrary_prepare_object_preserved', body, CONSTRAINED, True,
              '未知の準備状態を通過・保持する。準備の正当性や有効性を検証したことにならない。')
        body = selected_body(); body['system_hints'] = ['plugin:fixture']
        check('preexisting_resources_refused', body, CONSTRAINED, False,
              '無関係な既存リソース混入を拒否。明示リソース追加は別段階。')
        body = selected_body(); body['messages'].append(copy.deepcopy(body['messages'][0]))
        check('multiple_messages_refused', body, CONSTRAINED, False,
              'この独自controllerは入力一件の形だけを許可。サーバーの一般仕様ではない。')
        source = Path(inspect.getfile(generation_input))
        result = {'scope': 'real product validator, synthetic bodies only; not provider acceptance',
                  'source': str(source), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                  'network_connect_attempts': calls, 'generation_posts': 0,
                  'cases': cases, 'assertions_passed': len(cases),
                  'http_headers_checked_by_this_function': False,
                  'provider_minimal_request_proven': False}
        assert calls == 0
        with (ROOT / 'contract-boundary-result.json').open('x', encoding='utf-8') as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
            output.write('\n')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        socket.socket.connect = original_connect


if __name__ == '__main__':
    main()
