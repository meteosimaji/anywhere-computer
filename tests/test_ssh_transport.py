import subprocess

import pytest

from anywhere_computer.ssh_transport import run_ssh_mcp, ssh_command


@pytest.mark.parametrize(
    "host", ["-oProxyCommand=bad", "host;echo x", "user@host", "$(bad)", "", "x y"]
)
def test_ssh_rejects_option_and_shell_injection(host):
    with pytest.raises(ValueError):
        ssh_command(host)


def test_ssh_requires_verified_noninteractive_connection(monkeypatch):
    monkeypatch.setattr("anywhere_computer.ssh_transport.shutil.which", lambda _: "/system/ssh")
    command = ssh_command("windows-lab")
    assert command[-3:] == ["windows-lab", "anywhere", "mcp"]
    for required in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ClearAllForwardings=yes"):
        assert required in command
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 255)

    monkeypatch.setattr("anywhere_computer.ssh_transport.subprocess.run", run)
    assert run_ssh_mcp("windows-lab") == 255
    assert len(calls) == 1
    assert calls[0][1] == {"check": False}
