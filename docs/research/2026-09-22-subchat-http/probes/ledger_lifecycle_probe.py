"""新worker統合時の台帳初期化リスクを合成データで確認する。製品DBには触れない。"""
from pathlib import Path
import hashlib
import json
import tempfile
from anywhere_computer.state import Ledger
from anywhere_computer.models import Request
import anywhere_computer.state as state_module

result = {'scope': 'isolated synthetic core operation; no engine/Chat command dispatched',
          'state_source': state_module.__file__,
          'state_source_sha256': hashlib.sha256(Path(state_module.__file__).read_bytes()).hexdigest()}
with tempfile.TemporaryDirectory(prefix='ac-ledger-lifecycle-fixture-') as directory:
    first = Ledger(Path(directory))
    second = None
    try:
        operation = 'fd6ac8e48ef84f7e929a0928f8b3ec9f'
        first.claim(Request(operation_id=operation, tool='synthetic_only', arguments={}))
        result['first_before_second_open'] = first.get(operation).state
        second = Ledger(Path(directory))
        result['first_after_second_open'] = first.get(operation).state
        result['second_view'] = second.get(operation).state
        result['second_open_error_text'] = second.get(operation).error
        assert result['first_before_second_open'] == 'running'
        assert result['first_after_second_open'] == result['second_view'] == 'unknown'
    finally:
        if second is not None:
            second.close()
        first.close()
with Path(__file__).with_name('ledger-lifecycle-result.json').open('x', encoding='utf-8') as output:
    json.dump(result, output, ensure_ascii=False, indent=2)
    output.write('\n')
print(json.dumps(result, ensure_ascii=False, indent=2))
