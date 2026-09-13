import pytest

from anywhere_computer.device_polling import DevicePolling, PollOutcome


def test_polling_respects_initial_wait_and_persistent_slowdown() -> None:
    now = 0.0
    poll = DevicePolling(100, clock=lambda: now)
    assert not poll.begin_request()
    now = 5
    assert poll.begin_request()
    assert not poll.begin_request()  # One request in flight.
    assert poll.finish_request("slow_down").retry_after == 10
    now = 14
    assert not poll.begin_request()
    now = 15
    assert poll.begin_request()
    assert poll.finish_request("authorization_pending").retry_after == 10
    now = 25
    assert poll.begin_request()
    assert poll.finish_request("slow_down").retry_after == 15


@pytest.mark.parametrize("in_flight", [False, True])
def test_cancel_does_not_revive_after_late_response(in_flight: bool) -> None:
    now = 0.0
    poll = DevicePolling(100, clock=lambda: now)
    now = 5
    if in_flight:
        assert poll.begin_request()
    assert poll.cancel().phase == "cancelled"
    assert poll.finish_request("authorized").phase == "cancelled"
    now = 200
    assert not poll.begin_request()


@pytest.mark.parametrize("outcome,phase", [
    ("authorized", "authorized"), ("access_denied", "denied"),
    ("expired_token", "expired"), ("invalid_response", "failed"),
    ("unknown_result", "uncertain"),
])
def test_terminal_results_never_redeem_code_again(outcome: PollOutcome, phase: str) -> None:
    now = 0.0
    poll = DevicePolling(100, clock=lambda: now)
    now = 5
    assert poll.begin_request()
    assert poll.finish_request(outcome).phase == phase
    now = 50
    assert not poll.begin_request()
    assert poll.finish_request("authorization_pending").phase == phase


def test_expiry_wakes_before_next_poll_and_does_not_send() -> None:
    now = 0.0
    poll = DevicePolling(8, clock=lambda: now)
    now = 5
    assert poll.begin_request()
    assert poll.finish_request("slow_down").retry_after == 3
    now = 8
    assert poll.progress().phase == "expired"
    assert not poll.begin_request()


def test_connection_timeouts_back_off_but_unknown_exchange_stops() -> None:
    now = 0.0
    poll = DevicePolling(100, clock=lambda: now)
    now = 5
    assert poll.begin_request()
    assert poll.finish_request("connection_timeout").retry_after == 10
    now = 15
    assert poll.begin_request()
    assert poll.finish_request("connection_timeout").retry_after == 20
    now = 35
    assert poll.begin_request()
    assert poll.finish_request("unknown_result").phase == "uncertain"
    assert not poll.begin_request()


@pytest.mark.parametrize("lifetime,interval", [(0, 5), (-1, 5), (10, 0), (True, 5)])
def test_invalid_timing_rejected(lifetime: int, interval: int) -> None:
    with pytest.raises(ValueError):
        DevicePolling(lifetime, interval)
