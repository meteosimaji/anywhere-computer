"""Exercise the experimental native helper without opening any capture device."""

import json
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin", reason="macOS ScreenCaptureKit probe"
)


@pytest.fixture(scope="module")
def binaries(tmp_path_factory):
    if int(platform.mac_ver()[0].split(".")[0]) < 15:
        pytest.skip("probe requires macOS 15+")
    swift = shutil.which("swiftc")
    if not swift:
        pytest.skip("Swift compiler unavailable")
    directory = tmp_path_factory.mktemp("audio-probe-bin")
    probe = directory / "probe"
    harness = directory / "harness"
    source = ROOT / "scripts/probe_audio_capture.swift"
    for target, extra in [
        (probe, []),
        (harness, ["-D", "AUDIO_PROBE_TESTS", ROOT / "tests/fixtures/audio/probe_harness.swift"]),
    ]:
        subprocess.run(
            [swift, "-parse-as-library", source, *extra, "-o", target],
            check=True, capture_output=True, text=True, timeout=120,
        )
    return probe, harness


@pytest.mark.parametrize("source,extra", [("microphone", []), ("both", [])])
def test_no_implicit_microphone(binaries, tmp_path, source, extra):
    destination = tmp_path / "recording"
    result = subprocess.run(
        [binaries[0], source, "1", destination, *extra],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["error_code"] == "invalidArguments"
    assert not destination.exists()


def test_caps_each_track_and_reports_actual_samples(binaries, tmp_path):
    result = subprocess.run(
        [binaries[1], tmp_path], check=True, capture_output=True, text=True, timeout=15,
    )
    assert "synthetic duration and measurements passed" in result.stdout
