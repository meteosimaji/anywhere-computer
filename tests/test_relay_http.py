import httpx
import pytest
from test_relay_enrollment import registration as registration_fixture
from test_relay_enrollment import signed

from anywhere_computer.relay_http import enrollment_http

registration = registration_fixture


async def test_verified_account_lookup_is_read_only_and_token_bound(registration):
    key, registry, service, claims = registration
    adapter = enrollment_http(service)
    port = await adapter.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                     trust_env=False) as client:
            for subject in ("owner", "another-account"):
                token = signed(key, {**claims, "sub": subject})
                headers = {"Authorization": "Bearer " + token}
                response = await client.post("/enrollment/account", headers=headers, json={})
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-store"
                assert response.json() == {"issuer": claims["iss"], "subject": subject}
                assert token not in response.text
                override = await client.post("/enrollment/account", headers=headers,
                                             json={"subject": "victim"})
                assert override.status_code == 400
            for invalid in ("malformed", signed(key, {**claims, "scope": "files_write"}),
                            signed(key, {**claims, "aud": "https://other.example"}),
                            signed(key, {**claims, "exp": 1})):
                response = await client.post("/enrollment/account", json={},
                                             headers={"Authorization": "Bearer " + invalid})
                assert response.status_code == 401 and not response.content
            assert registry.db.execute("SELECT COUNT(*) FROM relay_devices").fetchone()[0] == 0
            assert not adapter.sessions
    finally:
        await adapter.close()


async def test_actual_http_registration_retry_and_mcp_separation(registration):
    key, registry, service, claims = registration
    adapter = enrollment_http(service)
    port = await adapter.start()
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
        ) as client:
            headers = {"Authorization": "Bearer " + signed(key, claims)}
            request = {"enrollment_id": "a" * 32, "name": "試験 PC 🚀"}
            first = await client.post("/enrollment/devices", headers=headers, json=request)
            assert first.status_code == 200
            assert first.headers["cache-control"] == "no-store"
            assert first.json()["state"] == "registered"
            again = await client.post("/enrollment/devices", headers=headers, json=request)
            assert again.json() == first.json()
            denied = await client.post("/mcp", headers=headers, json={"method": "initialize"})
            assert denied.status_code == 401 and not adapter.sessions
            conflict = await client.post("/enrollment/devices", headers=headers,
                                         json={**request, "name": "Other"})
            assert conflict.status_code == 409
            assert registry.db.execute("SELECT COUNT(*) FROM relay_devices").fetchone()[0] == 1
    finally:
        await adapter.close()


@pytest.mark.parametrize("case,status", [
    ("missing_token", 401), ("wrong_audience", 401), ("origin", 403),
    ("account_override", 400), ("query", 400), ("oversize", 413), ("get", 405),
])
async def test_http_rejections_do_not_register(registration, case, status):
    key, registry, service, claims = registration
    adapter = enrollment_http(service)
    port = await adapter.start()
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
        ) as client:
            if case == "wrong_audience":
                claims["aud"] = "https://other.example"
            headers = {"Authorization": "Bearer " + signed(key, claims)}
            request = {"enrollment_id": "a" * 32, "name": "PC"}
            if case == "missing_token":
                headers.clear()
            if case == "origin":
                headers["Origin"] = "https://other.example"
            if case == "account_override":
                request["subject"] = "victim"
            if case == "oversize":
                request["name"] = "x" * 4097
            path = "/enrollment/devices" + ("?token=forbidden" if case == "query" else "")
            response = (await client.get(path, headers=headers) if case == "get" else
                        await client.post(path, headers=headers, json=request))
            assert response.status_code == status
            assert not response.content
            assert registry.db.execute("SELECT COUNT(*) FROM relay_devices").fetchone()[0] == 0
    finally:
        await adapter.close()
