import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from anywhere_computer.relay_enrollment import EnrollmentRejected, RelayEnrollment
from anywhere_computer.relay_registry import RelayAccount, RelayRegistry


@pytest.fixture
def registration(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    registry = RelayRegistry(tmp_path)
    service = RelayEnrollment(registry, issuer="https://auth.example",
                              audience="https://relay.example/enrollment", client="desktop",
                              public_keys={"fixture": public})
    now = int(time.time())
    claims = dict(iss="https://auth.example", sub="owner", azp="desktop", typ="Bearer",
                  aud="https://relay.example/enrollment", scope="device:enroll",
                  iat=now, exp=now + 60)
    try:
        yield key, registry, service, claims
    finally:
        registry.close()


def signed(key, claims, **headers):
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "fixture", **headers})


def test_signed_account_enrolls_and_replays_one_registration(registration):
    key, registry, service, claims = registration
    token = signed(key, claims)
    first = service.register(token, enrollment_id="a" * 32, name="PC")
    assert service.register(token, enrollment_id="a" * 32, name="PC") == first
    owner = RelayAccount(issuer=claims["iss"], subject=claims["sub"])
    assert registry.get(owner, first.device_id) == first
    assert "owner" not in first.model_dump_json()


@pytest.mark.parametrize("change", [
    {"iss": "https://other.example"}, {"aud": "https://other.example"},
    {"aud": ["https://relay.example/enrollment", "https://other.example"]},
    {"azp": "ai-client"}, {"scope": "files_write"},
    {"scope": "device:enroll files_write"}, {"typ": "ID"}, {"exp": 1},
    {"iat": 9999999999}, {"sub": None}, {"exp": True},
])
def test_invalid_grants_leave_registry_unchanged(registration, change):
    key, registry, service, claims = registration
    token = signed(key, {**claims, **change})
    with pytest.raises(EnrollmentRejected) as error:
        service.register(token, enrollment_id="a" * 32, name="PC")
    assert token not in str(error.value)
    assert registry.db.execute("SELECT COUNT(*) FROM relay_devices").fetchone()[0] == 0


@pytest.mark.parametrize("header", [
    {"kid": "unknown"}, {"jku": "https://other.example/keys"},
    {"crit": ["unknown-extension"]}, {"b64": False},
])
def test_token_cannot_choose_untrusted_signing_key(registration, header):
    key, registry, service, claims = registration
    token = signed(key, claims, **header)
    # Ensure the encoder retained the header being tested (it omits b64=true).
    encoded_header = json.loads(jwt.utils.base64url_decode(token.split(".")[0]))
    assert all(encoded_header.get(k) == v for k, v in header.items())
    with pytest.raises(EnrollmentRejected):
        service.register(token, enrollment_id="a" * 32, name="PC")
    assert registry.db.execute("SELECT COUNT(*) FROM relay_devices").fetchone()[0] == 0


def test_unsigned_or_other_key_cannot_register(registration):
    _, registry, service, claims = registration
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for token in (jwt.encode(claims, None, algorithm="none"), signed(other, claims)):
        with pytest.raises(EnrollmentRejected):
            service.register(token, enrollment_id="a" * 32, name="PC")
    assert registry.db.execute("SELECT COUNT(*) FROM relay_devices").fetchone()[0] == 0


@pytest.mark.parametrize("missing", ["iss", "sub", "aud", "azp", "typ", "exp", "iat", "scope"])
def test_required_claims_cannot_be_omitted(registration, missing):
    key, registry, service, claims = registration
    del claims[missing]
    with pytest.raises(EnrollmentRejected):
        service.register(signed(key, claims), enrollment_id="a" * 32, name="PC")
    assert registry.db.execute("SELECT COUNT(*) FROM relay_devices").fetchone()[0] == 0


def test_valid_other_account_gets_separate_device_and_revocation_survives_retry(registration):
    key, registry, service, claims = registration
    token = signed(key, claims)
    first = service.register(token, enrollment_id="a" * 32, name="PC")
    other = service.register(signed(key, {**claims, "sub": "other"}),
                             enrollment_id="a" * 32, name="PC")
    assert first.device_id != other.device_id
    owner = RelayAccount(issuer=claims["iss"], subject=claims["sub"])
    registry.revoke(owner, first.device_id)
    assert service.register(token, enrollment_id="a" * 32, name="PC").state == "revoked"
    assert registry.db.execute("SELECT COUNT(*) FROM relay_devices").fetchone()[0] == 2
