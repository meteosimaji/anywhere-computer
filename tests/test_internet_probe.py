import os
import runpy
import uuid
from pathlib import Path

from anywhere_computer.downloads import DOWNLOAD_TOOLS
from anywhere_computer.models import Request


async def test_internet_probe_only_exposes_disposable_file(tmp_path):
    helper = runpy.run_path(str(Path(__file__).parents[1] / "scripts/verify_internet.py"))
    engine, target = helper["restricted_engine"](tmp_path)
    outside = tmp_path / "not-exposed.txt"
    outside.write_text("private fixture", encoding="utf-8")

    async def call(tool, **arguments):
        return await engine.execute(
            Request(operation_id=uuid.uuid4().hex, tool=tool, arguments=arguments)
        )

    try:
        assert set(engine.tools) == {"files_read", "files_write", "operations_get"} | DOWNLOAD_TOOLS
        for tool, args in [
            ("files_read", {"path": str(outside)}),
            ("files_write", {"path": str(outside), "text": "bad"}),
            ("files_write", {"path": str(target), "text": "x" * 1025}),
            ("download_begin", {"path": str(outside), "transfer_id": uuid.uuid4().hex}),
        ]:
            result = await call(tool, **args)
            assert result.state == "failed" and "Probe only permits" in result.error
        assert outside.read_text(encoding="utf-8") == "private fixture"
        assert (
            await call("terminal_start", command="echo forbidden", cwd=str(tmp_path))
        ).state == "failed"
        assert (await call("files_write", path=str(target), text="probe")).state == "completed"
        assert (await call("files_read", path=str(target))).data["text"] == "probe"
        binary = target.with_name("download.bin")
        prepared = await call("download_begin", path=str(binary), transfer_id=uuid.uuid4().hex)
        assert prepared.state == "completed" and prepared.data["total_bytes"] == 17 * 1024**2
        if os.name != "nt":
            target.unlink()
            target.symlink_to(outside)
            assert (await call("files_read", path=str(target))).state == "failed"
            assert (await call("files_write", path=str(target), text="bad")).state == "failed"
            binary.unlink()
            binary.symlink_to(outside)
            assert (
                await call("download_begin", path=str(binary), transfer_id=uuid.uuid4().hex)
            ).state == "failed"
    finally:
        await engine.close()
