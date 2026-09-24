import asyncio
import subprocess
from pathlib import Path

import pytest

from anywhere_computer.regex_worker import regex_line_numbers
from anywhere_computer.runtime_launch import python_module_command


@pytest.mark.parametrize("placement", ["cwd", "pythonpath"])
async def test_worker_and_cli_ignore_workspace_python(tmp_path, monkeypatch, placement):
    untrusted = tmp_path / "untrusted"
    untrusted.mkdir()
    package = untrusted / "anywhere_computer"
    package.mkdir()
    marker = tmp_path / "unexpected-code-execution"
    planted = f"from pathlib import Path\nPath({str(marker)!r}).write_text('fixture-only')\n"
    (package / "__init__.py").write_text(planted)
    (package / "regex_worker.py").write_text("print('{\"lines\": [999]}')\n")
    (package / "cli.py").write_text(planted)
    (untrusted / "sitecustomize.py").write_text(planted)
    safe = tmp_path / "safe"
    safe.mkdir()
    monkeypatch.chdir(untrusted if placement == "cwd" else safe)
    monkeypatch.setenv("PYTHONPATH", str(untrusted))
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "not-a-python-home"))
    assert await regex_line_numbers(
        "first\nneedle", "needle", ignore_case=False, whole_word=False, limit=1, timeout=5,
    ) == [2]
    for module in ("anywhere_computer", "anywhere_computer.cli"):
        result = subprocess.run(
            python_module_command(module, "--help"), capture_output=True, timeout=10,
        )
        assert result.returncode == 0
        assert b"usage: anywhere" in result.stdout
    assert not marker.exists()


def test_internal_launch_isolation_keeps_selected_venv():
    executable = str(Path("/trusted/venv/bin/python"))
    command = python_module_command("anywhere_computer.cli", "--help", executable=executable)
    assert command == [executable, "-B", "-I", "-X", "utf8", "-m",
                       "anywhere_computer.cli", "--help"]


async def test_regex_real_worker_handles_syntax_and_word_boundaries():
    options = dict(ignore_case=True, whole_word=True, limit=100, timeout=5)
    assert await regex_line_numbers("cat\nconcatenate\nCAT!\ndog", "c[ae]t", **options) == [1, 3]
    assert await regex_line_numbers("first\nsecond", "^second$", **options) == [2]
    with pytest.raises(ValueError, match="Invalid regular"):
        await regex_line_numbers("content", "[", **options)


async def test_regex_pathological_pattern_is_terminated():
    with pytest.raises(TimeoutError):
        await regex_line_numbers("a" * 10000 + "!", "(a+)+$", ignore_case=False,
                                 whole_word=False, limit=1, timeout=0.2)


async def test_regex_cancellation_reaps_child(monkeypatch):
    original = asyncio.create_subprocess_exec
    children = []
    ready = asyncio.Event()

    async def create(*args, **kwargs):
        process = await original(*args, **kwargs)
        children.append(process)
        ready.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(regex_line_numbers(
        "a" * 10000 + "!", "(a+)+$", ignore_case=False, whole_word=False, limit=1, timeout=30,
    ))
    await asyncio.wait_for(ready.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(children) == 1 and children[0].returncode is not None


def test_internal_child_explicitly_enables_utf8(monkeypatch):
    monkeypatch.setenv("PYTHONUTF8", "0")
    command = python_module_command("anywhere_computer.cli")
    flags = command[:command.index("-m")]
    result = subprocess.run(
        [*flags, "-c", "import sys; print(sys.flags.utf8_mode); print('日本語 🚀')"],
        capture_output=True, timeout=10, check=True,
    )
    assert result.stdout.decode("utf-8").splitlines() == ["1", "日本語 🚀"]
