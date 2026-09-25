"""Observed browser model IDs stay bound to the selected catalog preset."""
import json

import pytest

from anywhere_computer.subchat_browser.catalog import require_http_selection
from anywhere_computer.subchat_browser.request_content import generation_input
from anywhere_computer.subchat_state import SubchatHTTPSelection, SubchatSubmission


@pytest.mark.parametrize(('version_id', 'model'), [
    ('5.6', 'GPT-5.6 Sol'), ('latest', 'GPT-5.6 Sol'),
])
def test_observed_gpt56_instant_browser_model_alias(version_id, model):
    submission = SubchatSubmission(
        operation_id='a' * 32, prompt='test', model=model, effort='Instant',
        http_selection=SubchatHTTPSelection(
            version_id=version_id, preset_id=0, model_slug='gpt-5-6-instant',
            thinking_effort=None),
    )
    body = {'action': 'next', 'model': 'gpt-5-6', 'thinking_effort': None,
            'conversation_id': None, 'messages': [{
                'id': 'message-1', 'author': {'role': 'user'},
                'content': {'content_type': 'text', 'parts': ['test']},
                'metadata': {},
            }]}
    assert generation_input(json.dumps(body), submission)['model'] == 'gpt-5-6'
    for changed in ({'model': 'gpt-5-6-thinking'}, {'thinking_effort': 'standard'}):
        with pytest.raises(ValueError, match='Generation model or effort changed'):
            generation_input(json.dumps({**body, **changed}), submission)


def test_observed_latest_pro_browser_effort_alias():
    submission = SubchatSubmission(
        operation_id='b' * 32, prompt='test', model='GPT-6 Pro', effort='Pro',
        http_selection=SubchatHTTPSelection(
            version_id='latest', preset_id=3, model_slug='gpt-6-pro',
            thinking_effort=None),
    )
    body = {'action': 'next', 'model': 'gpt-6-pro', 'thinking_effort': 'standard',
            'conversation_id': None, 'messages': [{
                'id': 'message-1', 'author': {'role': 'user'},
                'content': {'content_type': 'text', 'parts': ['test']},
                'metadata': {},
            }]}
    assert generation_input(json.dumps(body), submission)['thinking_effort'] == 'standard'
    for changed in ({'model': 'gpt-5-6-thinking'}, {'thinking_effort': 'extended'}):
        with pytest.raises(ValueError, match='Generation model or effort changed'):
            generation_input(json.dumps({**body, **changed}), submission)
    wrong_label = submission.model_copy(update={'effort': 'Instant'})
    with pytest.raises(ValueError, match='Generation model or effort changed'):
        generation_input(json.dumps(body), wrong_label)


@pytest.mark.parametrize(('preset_id', 'model_slug', 'model_title', 'effort',
                          'wire_model', 'wire_effort'), [
    (0, 'gpt-5-6-instant', 'GPT-5.6 Sol', 'Instant', 'gpt-5-6', None),
    (3, 'gpt-6-pro', 'GPT-6 Pro', 'Pro', 'gpt-6-pro', 'standard'),
])
def test_latest_catalog_title_survives_browser_generation_validation(
        preset_id, model_slug, model_title, effort, wire_model, wire_effort):
    selected = SubchatHTTPSelection(
        version_id='latest', preset_id=preset_id, model_slug=model_slug,
        thinking_effort=None)
    catalog = {'state': 'http_catalog_observed', 'versions': [{
        'id': 'latest', 'label': '最新', 'choices': [{
            'available': True, 'model_title': model_title, 'title': effort,
            'http_selection': selected.model_dump(),
        }],
    }]}
    require_http_selection(catalog, selected, model=model_title, effort=effort)
    submission = SubchatSubmission(
        operation_id='c' * 32, prompt='test', model=model_title, effort=effort,
        http_selection=selected)
    body = {'action': 'next', 'model': wire_model,
            'thinking_effort': wire_effort, 'conversation_id': None,
            'messages': [{'id': 'message-1', 'author': {'role': 'user'},
                          'content': {'content_type': 'text', 'parts': ['test']},
                          'metadata': {}}]}
    assert generation_input(json.dumps(body), submission)['model'] == wire_model
