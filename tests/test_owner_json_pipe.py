import asyncio
import base64
import json
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anywhere_computer.owner_json_pipe import (  # noqa: E402
    MAX_PAYLOAD_BYTES,
    OwnerJsonPipeServer,
    OwnerPipeIdentityError,
    OwnerPipeProtocolError,
    OwnerPipeTimeout,
    request_owner_json_pipe_async,
)


@unittest.skipUnless(sys.platform == "win32", "Windows native named-pipe test")
class OwnerJsonPipeNativeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.calls: list[str] = []
        self.slow_started = asyncio.Event()
        self.slow_release = asyncio.Event()
        self.slow_completed = asyncio.Event()
        self.slow_cancelled = False

        async def handler(payload: bytes) -> bytes:
            request = json.loads(payload)
            action = request["action"]
            self.calls.append(action)
            if action == "slow":
                self.slow_started.set()
                try:
                    await self.slow_release.wait()
                    self.slow_completed.set()
                except asyncio.CancelledError:
                    self.slow_cancelled = True
                    raise
            return json.dumps({"ok": True, "action": action}, separators=(",", ":")).encode()

        self.server = OwnerJsonPipeServer(
            handler, max_payload_bytes=1024, request_timeout=2, max_connections=8
        )
        self.endpoint = self.server.start(asyncio.get_running_loop())

    async def asyncTearDown(self) -> None:
        self.slow_release.set()
        await self.server.aclose()
        self.assertFalse(self.server.worker_alive)

    async def request(self, action: str, timeout: float = 1) -> dict[str, object]:
        raw = json.dumps({"action": action}, separators=(",", ":")).encode()
        reply = await request_owner_json_pipe_async(self.endpoint, raw, timeout=timeout)
        parsed = json.loads(reply)
        self.assertIsInstance(parsed, dict)
        return parsed

    async def test_client_identity_does_not_require_process_query_access(self) -> None:
        from unittest.mock import patch

        from anywhere_computer import owner_json_pipe as transport

        original = transport._WINDOWS.process_sid

        def restricted_process_sid(pid=None):
            if threading.current_thread().name == "anywhere-owner-json-client":
                raise OSError(5, "Synthetic cross-logon process query denial")
            return original(pid)

        with patch.object(transport._WINDOWS, "process_sid", restricted_process_sid):
            self.assertTrue((await self.request("identity"))["ok"])
        self.assertEqual(self.calls, ["identity"])

    async def test_different_client_sid_rejected_before_dispatch(self) -> None:
        from unittest.mock import patch

        from anywhere_computer import owner_json_pipe as transport

        with patch.object(transport._WINDOWS, "client_sid", return_value="S-1-5-21-999"):
            with self.assertRaises((EOFError, OSError)):
                await self.request("must-not-dispatch")
        self.assertEqual(self.calls, [])

    async def test_multiple_requests_reconnect_and_parallel_status(self) -> None:
        self.assertEqual((await self.request("first"))["action"], "first")
        self.assertEqual((await self.request("second"))["action"], "second")

        slow = asyncio.create_task(self.request("slow", timeout=2))
        await asyncio.wait_for(self.slow_started.wait(), timeout=1)
        quick = await self.request("status", timeout=1)
        self.assertEqual(quick, {"ok": True, "action": "status"})
        self.assertFalse(slow.done())
        self.slow_release.set()
        self.assertEqual((await slow)["action"], "slow")
        self.assertEqual(self.calls, ["first", "second", "slow", "status"])

    async def test_wrong_server_identity_rejected_before_dispatch(self) -> None:
        calls_before = len(self.calls)
        wrong = replace(
            self.endpoint, server_creation_time=self.endpoint.server_creation_time + 60
        )
        with self.assertRaises(OwnerPipeIdentityError):
            await request_owner_json_pipe_async(
                wrong, b'{"action":"must-not-dispatch"}', timeout=1
            )
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.calls), calls_before)
        self.assertEqual((await self.request("after-identity-failure"))["ok"], True)

    async def test_strict_json_and_size_limits_reject_before_dispatch(self) -> None:
        calls_before = len(self.calls)
        with self.assertRaises(OwnerPipeProtocolError):
            await request_owner_json_pipe_async(self.endpoint, b"not-json", timeout=1)
        with self.assertRaises(OwnerPipeProtocolError):
            await request_owner_json_pipe_async(
                self.endpoint, json.dumps({"data": "x" * 2048}).encode(), timeout=1
            )
        self.assertEqual(len(self.calls), calls_before)

    async def test_client_timeout_does_not_cancel_started_dispatch(self) -> None:
        task = asyncio.create_task(self.request("slow", timeout=0.05))
        await asyncio.wait_for(self.slow_started.wait(), timeout=1)
        with self.assertRaises((OwnerPipeTimeout, EOFError, OSError)):
            await task
        self.assertFalse(self.slow_cancelled)
        self.slow_release.set()
        await asyncio.wait_for(self.slow_completed.wait(), timeout=1)
        self.assertFalse(self.slow_cancelled)
        self.assertEqual((await self.request("after-timeout"))["ok"], True)

    async def test_first_pipe_instance_collision_fails_closed(self) -> None:
        async def unused(payload: bytes) -> bytes:
            return payload

        collision = OwnerJsonPipeServer(
            unused,
            max_payload_bytes=1024,
            request_timeout=1,
            _pipe_name=self.endpoint.pipe_name,
        )
        with self.assertRaises(OSError):
            collision.start(asyncio.get_running_loop())

    async def test_separate_native_process_with_same_sid_roundtrips(self) -> None:
        endpoint = base64.b64encode(json.dumps(self.endpoint.to_dict()).encode()).decode()
        source = str(Path(__file__).resolve().parents[1] / "src")
        code = (
            "import base64,json,sys;"
            "sys.path.insert(0,sys.argv[1]);"
            "from anywhere_computer.owner_json_pipe import "
            "OwnerPipeEndpoint,request_owner_json_pipe;"
            "endpoint=OwnerPipeEndpoint.from_dict(json.loads(base64.b64decode(sys.argv[2])));"
            "reply=request_owner_json_pipe(endpoint,b'{\"action\":\"child\"}',timeout=2);"
            "print(reply.decode())"
        )

        def run_child() -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                [sys.executable, "-I", "-X", "utf8", "-c", code, source, endpoint],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=5,
                check=False,
            )

        child = await asyncio.to_thread(run_child)
        self.assertEqual(child.returncode, 0, child.stderr.decode(errors="replace"))
        self.assertEqual(json.loads(child.stdout), {"ok": True, "action": "child"})

    async def test_delayed_reply_reader_receives_complete_frame(self) -> None:
        from anywhere_computer.owner_json_pipe import (  # noqa: PLC0415
            _WINDOWS,
            _encode_frame,
            _read_frame,
            _verify_hello,
            _verify_server_identity,
        )

        def delayed_reader() -> bytes:
            handle = _WINDOWS.open_client(self.endpoint.pipe_name, 1)
            thread_handle = _WINDOWS.kernel.OpenThread(
                0x0001, False, threading.get_native_id()
            )
            self.assertTrue(thread_handle)
            try:
                _verify_server_identity(handle, self.endpoint)
                hello = _read_frame(handle, self.endpoint.max_payload_bytes, 1)
                _verify_hello(hello, self.endpoint)
                request = _encode_frame(b'{"action":"delayed-reader"}', 1024)
                _WINDOWS.write_all(handle, request, 1, int(thread_handle))
                time.sleep(0.15)
                return _read_frame(handle, self.endpoint.max_payload_bytes, 1)
            finally:
                _WINDOWS.close(handle)
                _WINDOWS.close(int(thread_handle))

        reply = await asyncio.to_thread(delayed_reader)
        self.assertEqual(json.loads(reply), {"ok": True, "action": "delayed-reader"})

    async def test_nonreading_client_is_bounded_and_server_recovers(self) -> None:
        from anywhere_computer.owner_json_pipe import (  # noqa: PLC0415
            _WINDOWS,
            _encode_frame,
            _read_frame,
            _verify_hello,
            _verify_server_identity,
        )

        async def handler(payload: bytes) -> bytes:
            action = json.loads(payload)["action"]
            return json.dumps({"ok": True, "action": action}).encode()

        server = OwnerJsonPipeServer(handler, max_payload_bytes=1024, request_timeout=0.1)
        endpoint = server.start(asyncio.get_running_loop())

        def abandon_reply() -> None:
            handle = _WINDOWS.open_client(endpoint.pipe_name, 1)
            thread_handle = _WINDOWS.kernel.OpenThread(
                0x0001, False, threading.get_native_id()
            )
            self.assertTrue(thread_handle)
            try:
                _verify_server_identity(handle, endpoint)
                hello = _read_frame(handle, endpoint.max_payload_bytes, 1)
                _verify_hello(hello, endpoint)
                request = _encode_frame(b'{"action":"not-read"}', 1024)
                _WINDOWS.write_all(handle, request, 1, int(thread_handle))
                time.sleep(0.25)
            finally:
                _WINDOWS.close(handle)
                _WINDOWS.close(int(thread_handle))

        try:
            await asyncio.to_thread(abandon_reply)
            deadline = time.monotonic() + 1
            while server.active_connection_count and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            self.assertEqual(server.active_connection_count, 0)
            raw = await request_owner_json_pipe_async(
                endpoint, b'{"action":"recovered"}', timeout=1
            )
            self.assertEqual(json.loads(raw), {"ok": True, "action": "recovered"})
        finally:
            await server.aclose()

    async def test_eight_megabyte_request_and_reply_are_bounded(self) -> None:
        async def echo(payload: bytes) -> bytes:
            return payload

        server = OwnerJsonPipeServer(
            echo,
            max_payload_bytes=MAX_PAYLOAD_BYTES,
            request_timeout=15,
            max_connections=2,
        )
        endpoint = server.start(asyncio.get_running_loop())
        payload = b'{"data":"' + (b"x" * (MAX_PAYLOAD_BYTES - 11)) + b'"}'
        self.assertEqual(len(payload), MAX_PAYLOAD_BYTES)
        started = time.monotonic()
        try:
            reply = await request_owner_json_pipe_async(endpoint, payload, timeout=15)
        finally:
            await server.aclose()
        elapsed = time.monotonic() - started
        self.assertEqual(reply, payload)
        self.assertLess(elapsed, 15)
        print(f"eight_megabyte_roundtrip_seconds={elapsed:.3f}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
