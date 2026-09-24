"""Shape comparison must accept only names and never echo file contents."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    'compare_chat_shapes', Path(__file__).parents[1] / 'scripts/compare_chat_shapes.py')
assert SPEC and SPEC.loader
shapes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shapes)


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding='utf-8')
    return path


def shape(headers, body_keys):
    return {'headers': headers, 'body_keys': body_keys}


def test_identical_shapes_match_ignoring_order_and_header_case(tmp_path, capsys):
    a = write(tmp_path, 'a.json', shape(['Accept', 'Content-Type'], ['action', 'model']))
    b = write(tmp_path, 'b.json', shape(['content-type', 'accept'], ['model', 'action']))
    assert shapes.main([str(a), str(b)]) == 0
    assert capsys.readouterr().out == 'shape matches\n'


def test_differences_report_added_and_missing_names_and_exit_nonzero(tmp_path, capsys):
    a = write(tmp_path, 'a.json', shape(['accept', 'oai-language'], ['action', 'model']))
    b = write(tmp_path, 'b.json', shape(['accept', 'x-extra'], ['action', 'parent_message_id']))
    assert shapes.main([str(a), str(b)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        'headers missing: oai-language', 'headers added: x-extra',
        'body_keys missing: model', 'body_keys added: parent_message_id',
        'shape differs']


def test_long_real_header_names_are_accepted(tmp_path):
    name = 'openai-sentinel-chat-requirements-prepare-token'
    assert shapes.load_shape(write(tmp_path, 'a.json', shape([name], [])))['headers'] == {name}


@pytest.mark.parametrize('data', [
    {'headers': {'authorization': 'x'}, 'body_keys': []},
    {'headers': [], 'body_keys': {'model': 'gpt'}},
    {'headers': [{'authorization': 'x'}], 'body_keys': []},
    {'headers': [], 'body_keys': [], 'extra': []},
    {'headers': []},
    [],
    shape(['authorization: Bearer abc'], []),
    shape(['x-token=abc'], []),
    shape([], ['model ']),
    shape([], [1]),
    shape(['accept', 'Accept'], []),
    shape(['eyJhbGciOiJIUzI1NiJ9'], []),
    shape(['sk-proj-abc'], []),
    shape(['a1b2c3d4e5f60718293a4b5c6d7e8f90'], []),
    shape(['x-12345678'], []),
    shape([], ['abcdefghijklmnopqrstuvwxyzabc']),
    shape(['a' * 65], []),
    shape([''], []),
    shape(['x', *(f'y{i}' for i in range(201))], []),
])
def test_values_mappings_and_credential_like_names_are_refused(tmp_path, data):
    with pytest.raises(shapes.ShapeError):
        shapes.load_shape(write(tmp_path, 'a.json', data))


def test_invalid_json_oversize_and_missing_files_are_refused(tmp_path):
    for path in (write(tmp_path, 'bad.json', '{"headers": ['),
                 write(tmp_path, 'big.json', ' ' * (shapes.MAX_BYTES + 1)),
                 tmp_path / 'missing.json',
                 write(tmp_path, 'deep.json', '[' * 4000),
                 write(tmp_path, 'duplicate.json',
                       '{"headers":[],"headers":["accept"],"body_keys":[]}')):
        with pytest.raises(shapes.ShapeError):
            shapes.load_shape(path)


def test_non_regular_input_is_refused_without_reading(tmp_path):
    fifo = tmp_path / 'input.pipe'
    os.mkfifo(fifo)
    with pytest.raises(shapes.ShapeError, match='regular'):
        shapes.load_shape(fifo)


def test_refusal_exits_2_without_printing_input_contents(tmp_path, capsys):
    secret = 'Bearer super-secret-credential-value'
    good = write(tmp_path, 'good.json', shape(['accept'], []))
    for bad in (write(tmp_path, 'mapping.json', shape({'authorization': secret}, [])),
                write(tmp_path, 'name.json', shape([secret], [])),
                write(tmp_path, 'garbage.json', 'not json ' + secret)):
        for arguments in ([good, bad], [bad, good]):
            assert shapes.main([str(path) for path in arguments]) == 2
            captured = capsys.readouterr()
            assert 'secret' not in captured.out + captured.err
            assert captured.out == ''
