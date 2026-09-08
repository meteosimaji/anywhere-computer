import asyncio
import hashlib
import shutil
import ssl
import struct
import subprocess
import uuid

import pytest

from anywhere_computer.engine import Engine
from anywhere_computer.models import Empty, Reply, Request
from anywhere_computer.remote_transport import (
    FRAME_LIMIT,
    RemoteListener,
    check_tls,
    remote_exchange,
    write_frame,
)


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    executable = shutil.which("openssl")
    if executable is None:
        pytest.fail("TLS integration tests require the openssl test certificate tool")
    directory = tmp_path_factory.mktemp("disposable-tls")

    def openssl(*args):
        subprocess.run([executable, *args], cwd=directory, check=True, capture_output=True)

    openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-subj",
        "/CN=Disposable test authority",
        "-keyout",
        "ca.key",
        "-out",
        "ca.pem",
    )
    (directory / "extensions.cnf").write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth,clientAuth\n"
        "subjectAltName=DNS:localhost\n"
    )
    for name in ("server", "client", "stranger"):
        openssl(
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            f"/CN={name}",
            "-keyout",
            f"{name}.key",
            "-out",
            f"{name}.csr",
        )
        openssl(
            "x509",
            "-req",
            "-in",
            f"{name}.csr",
            "-CA",
            "ca.pem",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-days",
            "1",
            "-extfile",
            "extensions.cnf",
            "-out",
            f"{name}.pem",
        )

    def context(name, client):
        result = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT if client else ssl.PROTOCOL_TLS_SERVER)
        result.minimum_version = ssl.TLSVersion.TLSv1_2
        result.verify_mode = ssl.CERT_REQUIRED
        result.load_verify_locations(directory / "ca.pem")
        result.load_cert_chain(directory / f"{name}.pem", directory / f"{name}.key")
        return result

    def fingerprint(name):
        der = ssl.PEM_cert_to_DER_cert((directory / f"{name}.pem").read_text())
        return hashlib.sha256(der).hexdigest()

    yield context, fingerprint
    for path in directory.iterdir():
        path.unlink()
    directory.rmdir()


@pytest.fixture
async def remote_agent(certificates, tmp_path):
    context, fingerprint = certificates
    engine = Engine(tmp_path / "state")
    seen = []

    async def dispatch(identity, payload):
        seen.append(identity)
        request = Request.model_validate_json(payload)
        return (await engine.execute(request)).model_dump_json().encode()

    listener = RemoteListener(context("server", False), {fingerprint("client"): "owner"}, dispatch)
    port = await listener.start("127.0.0.1", 0)

    async def call(request, client="client", expected=None, server_name="localhost"):
        response = await remote_exchange(
            "127.0.0.1",
            port,
            request.model_dump_json().encode(),
            context=context(client, True),
            server_name=server_name,
            expected_fingerprint=expected or fingerprint("server"),
            timeout=5,
        )
        return Reply.model_validate_json(response)

    yield engine, listener, port, call, seen
    await listener.close()
    await engine.close()


def request(tool, **arguments):
    return Request(operation_id=uuid.uuid4().hex, tool=tool, arguments=arguments)


async def test_tls_write_duplicate_and_retrieve(remote_agent, tmp_path):
    engine, listener, port, call, seen = remote_agent
    path = tmp_path / "日本語.txt"
    write = request("files_write", path=str(path), text="一度だけ\r\n")
    first = await call(write)
    second = await call(write)
    assert first.state == "completed" and second == first
    assert path.read_bytes() == "一度だけ\r\n".encode()
    recovered = await call(request("operations_get", operation_id=write.operation_id))
    assert recovered.data["state"] == "completed"
    assert seen == ["owner"] * 3


async def test_tls_rejects_unenrolled_revoked_and_wrong_server(remote_agent, certificates):
    engine, listener, port, call, seen = remote_agent
    context, fingerprint = certificates
    status = request("computer_status")
    with pytest.raises((OSError, asyncio.IncompleteReadError)):
        await call(status, client="stranger")
    with pytest.raises(PermissionError):
        await call(status, expected="0" * 64)
    with pytest.raises(ssl.SSLCertVerificationError):
        await call(status, server_name="wrong.invalid")
    listener.revoke(fingerprint("client"))
    with pytest.raises((OSError, asyncio.IncompleteReadError)):
        await call(status)
    assert seen == []


async def test_disconnect_keeps_effect_and_operation_identity(remote_agent, certificates):
    engine, listener, port, call, seen = remote_agent
    context, fingerprint = certificates
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    async def delayed(_):
        entered.set()
        await release.wait()
        effects.append("once")
        return {"effects": len(effects)}

    engine.register("test_delayed", "Test effect", Empty, delayed)
    operation = request("test_delayed")
    tls = context("client", True)
    check_tls(tls, client=True)
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", port, ssl=tls, server_hostname="localhost"
    )
    await write_frame(writer, operation.model_dump_json().encode())
    await asyncio.wait_for(entered.wait(), 5)
    writer.transport.abort()
    release.set()
    restored = await call(operation)
    assert restored.state == "completed"
    assert restored.data == {"effects": 1}
    assert effects == ["once"]


async def test_oversized_frame_never_dispatches(remote_agent, certificates):
    engine, listener, port, call, seen = remote_agent
    context, fingerprint = certificates
    tls = context("client", True)
    check_tls(tls, client=True)
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", port, ssl=tls, server_hostname="localhost"
    )
    writer.write(struct.pack("!I", FRAME_LIMIT + 1))
    await writer.drain()
    assert await asyncio.wait_for(reader.read(1), 5) == b""
    writer.close()
    await writer.wait_closed()
    assert seen == []


def test_unverified_tls_context_is_rejected():
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with pytest.raises(ValueError, match="verification"):
        check_tls(server, client=False)
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.check_hostname = False
    with pytest.raises(ValueError, match="hostname"):
        check_tls(client, client=True)
