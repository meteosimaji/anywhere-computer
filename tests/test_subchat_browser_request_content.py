"""Observed browser model IDs stay bound to the selected catalog preset."""
import json

import pytest

from anywhere_computer.subchat_browser.request_content import generation_input
from anywhere_computer.subchat_state import SubchatHTTPSelection, SubchatSubmission


def test_observed_gpt56_instant_browser_model_alias():
    submission = SubchatSubmission(
        operation_id='a' * 32, prompt='test', model='GPT-5.6 Sol', effort='Instant',
        http_selection=SubchatHTTPSelection(
            version_id='5.6', preset_id=0, model_slug='gpt-5-6-instant',
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
