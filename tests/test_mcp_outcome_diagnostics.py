from anywhere_computer.mcp_results import normalize_tool_result


def test_provider_outcome_survives_without_overriding_error():
    result = normalize_tool_result({'content': [], 'isError': True, '_meta': {
        'state': 'partial', 'dispatch_state': 'dispatched',
        'evidence': 'primary_change_verified_cleanup_failed',
        'escalation': 'recover_side_effect', 'retry_safe': False,
        'mutation_dispatched': True, 'requires_fresh_observation': True,
    }})
    assert result['is_error'] is True
    assert result['provider_diagnostics']['state'] == 'partial'
    assert result['provider_diagnostics']['retry_safe'] is False
    assert result['provider_diagnostics']['mutation_dispatched'] is True
    assert result['error_diagnostic']['error_code'] == 'provider_rejected'
    assert result['error_diagnostic']['execution_state'] == 'rejected'
    assert result['error_diagnostic']['next_action']


def test_private_unknown_and_wrong_typed_metadata_are_not_forwarded():
    result = normalize_tool_result({'content': [], '_meta': {
        'state': 'secret-token', 'evidence': {'value': 'secret'},
        'retry_safe': 'true', 'mutation_dispatched': 1,
        'access_token': 'secret', 'target_receipt': {'private': 'secret'},
        'escalation': 'observe_before_retry',
    }})
    assert result['provider_diagnostics'] == {'escalation': 'observe_before_retry'}
    assert 'secret' not in str(result)


def test_absent_metadata_does_not_imply_no_dispatch():
    result = normalize_tool_result({'content': [], 'isError': True})
    assert 'provider_diagnostics' not in result
    assert result['error_diagnostic']['error_code'] == 'provider_rejected'
    assert 'dispatched' not in result['error_diagnostic']
