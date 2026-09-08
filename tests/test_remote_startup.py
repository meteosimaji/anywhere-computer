import json
import signal
import subprocess
import sys
import time

import pytest
from test_client_tokens import MemoryVault
from test_http_service import RESOURCE, setup

from anywhere_computer import remote_service
from anywhere_computer.client_tokens import ClientCredentialError, CredentialStoreUnavailable
from anywhere_computer.cloudflare_tunnel import TunnelCredential
from anywhere_computer.credentials import SERVICE
from anywhere_computer.locking import ProcessLock
from anywhere_computer.owner_credentials import OwnerCredentials


class UnreadyVault(MemoryVault):
    def __init__(self):
        super().__init__()
        self.fail_account = None
        self.failures = 0
        self.failed_reads = 0

    def get_password(self, service, account):
        if account == self.fail_account and self.failures:
            self.failures -= 1
            self.failed_reads += 1
            raise RuntimeError("synthetic secret exception must remain hidden")
        return super().get_password(service, account)


@pytest.fixture
async def startup_profile(tmp_path, unused_tcp_port, monkeypatch):
    await setup(tmp_path, unused_tcp_port)
    vault = UnreadyVault()
    owner = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=vault)
    owner.initialize("synthetic owner password")
    tunnel = TunnelCredential(tmp_path, vault=vault)
    tunnel.install("synthetic_tunnel_token_12345")
    monkeypatch.setattr(remote_service, "OwnerCredentials", lambda *args, **kwargs: owner)
    monkeypatch.setattr(remote_service, "TunnelCredential", lambda *args, **kwargs: tunnel)
    monkeypatch.setattr(remote_service, "cloudflared_executable", lambda: "fixture")
    return tmp_path, vault, owner, tunnel


@pytest.mark.parametrize("store", ["owner", "tunnel"])
def test_startup_waits_before_spawning_and_preserves_credentials(
    startup_profile, monkeypatch, capsys, store,
):
    directory, vault, owner, tunnel = startup_profile
    before, writes = dict(vault.data), vault.writes
    vault.fail_account = owner.account if store == "owner" else tunnel.account
    vault.failures = 2
    delays, launches = [], []

    def wait(delay):
        delays.append(delay)
        observation = json.loads((directory / "remote-watch-status.json").read_text())
        assert observation["event"] == "credential_store_unavailable"
        assert observation["startup_attempt"] == len(delays)
        assert observation["restart_attempts"] == 0
        assert not launches
        for name in ("remote-watch.lock", "http-watch.lock", "http-server.lock",
                     "cloudflare-tunnel.lock"):
            with pytest.raises(TimeoutError), ProcessLock(directory / name):
                pass

    monkeypatch.setattr(remote_service.time, "sleep", wait)
    monkeypatch.setattr(remote_service, "supervise",
                        lambda command, **kwargs: launches.append(command) or 0)
    assert remote_service.watch_remote(directory) == 0
    assert delays == [1, 2] and len(launches) == 1 and vault.failed_reads == 2
    assert vault.data == before and vault.writes == writes
    saved = (directory / "remote-watch-status.json").read_text()
    assert json.loads(saved)["event"] == "credentials_ready"
    assert json.loads(saved)["startup_attempt"] == 3
    assert "synthetic" not in saved
    output = capsys.readouterr().out
    assert "synthetic" not in output
    assert [json.loads(line)["retry_attempt"] for line in output.splitlines()] == [1, 2]


@pytest.mark.parametrize("corrupt", [False, True])
@pytest.mark.parametrize("store", ["owner", "tunnel"])
def test_missing_or_corrupt_credentials_are_not_retried(
    startup_profile, monkeypatch, corrupt, store,
):
    directory, vault, owner, tunnel = startup_profile
    account = owner.account if store == "owner" else tunnel.account
    if corrupt:
        vault.data[(SERVICE, account)] = "invalid record"
    else:
        del vault.data[(SERVICE, account)]
    before = dict(vault.data)
    monkeypatch.setattr(remote_service.time, "sleep", lambda _: pytest.fail("Must not retry"))
    monkeypatch.setattr(remote_service, "supervise", lambda *a, **k: pytest.fail("Must not start"))
    with pytest.raises((ClientCredentialError, ValueError)) as error:
        remote_service.watch_remote(directory)
    assert not isinstance(error.value, CredentialStoreUnavailable)
    assert vault.data == before
    saved = (directory / "remote-watch-status.json").read_text()
    assert json.loads(saved)["event"] == "credential_rejected"
    assert json.loads(saved)["startup_attempt"] == 1
    assert "invalid record" not in saved and "synthetic" not in saved


def test_startup_retry_budget_and_interrupt_release_locks(startup_profile, monkeypatch):
    directory, vault, owner, _ = startup_profile
    vault.fail_account, vault.failures = owner.account, 20
    delays = []
    previous = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(remote_service.time, "sleep", delays.append)
    monkeypatch.setattr(remote_service, "supervise", lambda *a, **k: pytest.fail("Must not start"))
    with pytest.raises(CredentialStoreUnavailable):
        remote_service.watch_remote(directory)
    assert delays == [1, 2, 4, 8, 16] and vault.failed_reads == 6
    observation = json.loads((directory / "remote-watch-status.json").read_text())
    assert observation["event"] == "credential_store_unavailable"
    assert observation["startup_attempt"] == 6
    assert signal.getsignal(signal.SIGTERM) == previous

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(remote_service.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        remote_service.watch_remote(directory)
    assert signal.getsignal(signal.SIGTERM) == previous
    for name in ("remote-watch.lock", "http-watch.lock", "http-server.lock",
                 "cloudflare-tunnel.lock"):
        with ProcessLock(directory / name):
            pass
    observation = json.loads((directory / "remote-watch-status.json").read_text())
    assert observation["event"] == "interrupted"


def test_competing_watcher_does_not_overwrite_history(startup_profile):
    directory, _, _, _ = startup_profile
    path = directory / "remote-watch-status.json"
    previous = b'{"owned-by-running-watcher":true}'
    path.write_bytes(previous)
    with ProcessLock(directory / "remote-watch.lock"):
        with pytest.raises(TimeoutError):
            remote_service.watch_remote(directory)
    assert path.read_bytes() == previous


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGTERM/SIGKILL integration")
@pytest.mark.parametrize("hard_kill", [False, True])
def test_real_waiting_process_releases_locks_on_termination(tmp_path, hard_kill):
    script = '''
import sys
from pathlib import Path
from types import SimpleNamespace
from anywhere_computer import remote_service as service
from anywhere_computer.client_tokens import CredentialStoreUnavailable
def unavailable():
    raise CredentialStoreUnavailable("synthetic unavailable store")
service.load_http_config = lambda _: SimpleNamespace(resource="https://example.com/mcp", owner="o")
service.OwnerCredentials = lambda *a, **k: SimpleNamespace(ensure_initialized=unavailable)
service.TunnelCredential = lambda d: SimpleNamespace(
    lock=d / "cloudflare-tunnel.lock", read=lambda: "")
service.cloudflared_executable = lambda: "fixture"
try:
    service.watch_remote(Path(sys.argv[1]))
except KeyboardInterrupt:
    raise SystemExit(130)
'''
    log_path = tmp_path / "waiting.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)],
                                   stdout=log, stderr=log, start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            while b"credential_store_unavailable" not in log_path.read_bytes():
                assert process.poll() is None, log_path.read_text()
                assert time.monotonic() < deadline, "Watcher did not enter credential wait"
                time.sleep(0.02)
            for name in ("remote-watch.lock", "http-watch.lock", "http-server.lock",
                         "cloudflare-tunnel.lock"):
                with pytest.raises(TimeoutError), ProcessLock(tmp_path / name):
                    pass
            process.send_signal(signal.SIGKILL if hard_kill else signal.SIGTERM)
            assert process.wait(timeout=10) == (-signal.SIGKILL if hard_kill else 130)
            from anywhere_computer.watch_status import read_watch_observation

            history = read_watch_observation(tmp_path / "remote-watch-status.json")
            assert history["current_process_state"] == "unverified"
            assert history["last_observation"]["event"] == (
                "credential_store_unavailable" if hard_kill else "interrupted"
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    for name in ("remote-watch.lock", "http-watch.lock", "http-server.lock",
                 "cloudflare-tunnel.lock"):
        with ProcessLock(tmp_path / name):
            pass
