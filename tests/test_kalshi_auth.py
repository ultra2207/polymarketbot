"""Signing tests with ephemeral keys generated at test time (no network, no repo secrets)."""

from __future__ import annotations

import base64

import pytest
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

from polymarketbot.kalshi_auth import (
    HEADER_KEY,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    KalshiAuthError,
    KalshiSigner,
    error_code,
    signed_get,
    signed_request,
    signing_path,
)

STAMP = 1_700_000_000_000
KEY_ID = "00000000-0000-4000-8000-000000000000"
PATH = "/trade-api/v2/portfolio/balance"


def _write_pem(tmp_path, private_key, name: str = "kalshi.pem") -> str:
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / name
    path.write_bytes(pem)
    return str(path)


def _ed25519_signer(tmp_path) -> tuple[KalshiSigner, Ed25519PrivateKey]:
    key = Ed25519PrivateKey.generate()
    signer = KalshiSigner.from_path(_write_pem(tmp_path, key), key_id=KEY_ID)
    return signer, key


def _rsa_signer(tmp_path) -> tuple[KalshiSigner, object]:
    key = generate_private_key(public_exponent=65537, key_size=2048)
    signer = KalshiSigner.from_path(_write_pem(tmp_path, key, "rsa.pem"), key_id=KEY_ID)
    return signer, key


def test_ed25519_signature_verifies_over_pre_sign_text(tmp_path) -> None:
    signer, key = _ed25519_signer(tmp_path)
    assert signer.kind == "ed25519"

    headers = signer.sign_headers("GET", PATH, timestamp_ms=STAMP)

    signature = base64.b64decode(headers[HEADER_SIGNATURE])
    key.public_key().verify(signature, f"{STAMP}GET{PATH}".encode("utf-8"))


def test_rsa_signature_verifies_with_pss_sha256(tmp_path) -> None:
    signer, key = _rsa_signer(tmp_path)
    assert signer.kind == "rsa"

    headers = signer.sign_headers("post", "/trade-api/v2/portfolio/orders", timestamp_ms=STAMP)

    signature = base64.b64decode(headers[HEADER_SIGNATURE])
    key.public_key().verify(
        signature,
        f"{STAMP}POST/trade-api/v2/portfolio/orders".encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )


def test_headers_are_complete_and_carry_the_key_id(tmp_path) -> None:
    signer, _ = _ed25519_signer(tmp_path)

    headers = signer.sign_headers("GET", PATH, timestamp_ms=STAMP)

    assert set(headers) == {HEADER_KEY, HEADER_SIGNATURE, HEADER_TIMESTAMP}
    assert headers[HEADER_KEY] == KEY_ID
    assert headers[HEADER_TIMESTAMP] == str(STAMP)
    assert headers[HEADER_SIGNATURE] == base64.b64encode(
        base64.b64decode(headers[HEADER_SIGNATURE])
    ).decode("ascii")


def test_query_string_is_stripped_from_the_signature(tmp_path) -> None:
    signer, _ = _ed25519_signer(tmp_path)

    plain = signer.sign_headers("GET", "/trade-api/v2/portfolio/orders", timestamp_ms=STAMP)
    query = signer.sign_headers(
        "GET", "/trade-api/v2/portfolio/orders?limit=5&cursor=abc", timestamp_ms=STAMP
    )
    full_url = signer.sign_headers(
        "GET",
        "https://demo-api.kalshi.co/trade-api/v2/portfolio/orders?limit=5",
        timestamp_ms=STAMP,
    )

    assert query[HEADER_SIGNATURE] == plain[HEADER_SIGNATURE]
    assert full_url[HEADER_SIGNATURE] == plain[HEADER_SIGNATURE]


def test_signing_path_rejects_relative_targets() -> None:
    assert signing_path("https://api.elections.kalshi.com/trade-api/ws/v2?x=1") == "/trade-api/ws/v2"
    # The WebSocket handshake signs the URL path: /trade-api/ws/v2, not /trade-api/v2/ws/v2.
    assert signing_path("wss://demo-api.kalshi.co/trade-api/ws/v2") == "/trade-api/ws/v2"
    with pytest.raises(KalshiAuthError):
        signing_path("trade-api/v2/portfolio/balance")


def test_timestamp_comes_from_the_injected_clock(tmp_path) -> None:
    key = Ed25519PrivateKey.generate()
    signer = KalshiSigner(KEY_ID, key, clock=lambda: 424242)

    assert signer.sign_headers("GET", PATH)[HEADER_TIMESTAMP] == "424242"


def test_unsupported_key_type_is_rejected(tmp_path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    path = _write_pem(tmp_path, key, "ec.pem")

    with pytest.raises(KalshiAuthError):
        KalshiSigner.from_path(path, key_id=KEY_ID)


def test_missing_key_file_does_not_leak_contents(tmp_path) -> None:
    missing = tmp_path / "nope.pem"

    with pytest.raises(KalshiAuthError) as excinfo:
        KalshiSigner.from_path(str(missing), key_id=KEY_ID)

    assert str(missing) in str(excinfo.value)
    assert "BEGIN" not in str(excinfo.value)


def test_from_env_reads_key_path_and_key_id(tmp_path, monkeypatch) -> None:
    signer, _ = _ed25519_signer(tmp_path)
    pem = _write_pem(tmp_path, Ed25519PrivateKey.generate(), "env.pem")
    env = {"KALSHI_PRIVATE_KEY_PATH": pem, "KALSHI_KEY_ID": "env-key-id"}

    from_env = KalshiSigner.from_env(env=env)

    assert from_env.kind == signer.kind
    assert from_env.sign_headers("GET", PATH)[HEADER_KEY] == "env-key-id"

    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", pem)
    monkeypatch.setenv("KALSHI_KEY_ID", "env-key-id")
    assert KalshiSigner.from_env().sign_headers("GET", PATH)[HEADER_KEY] == "env-key-id"


def test_from_env_requires_a_key_path(monkeypatch) -> None:
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)

    with pytest.raises(KalshiAuthError):
        KalshiSigner.from_env(env={"KALSHI_KEY_ID": KEY_ID})


class _FakeSession:
    """Records requests and replays a scripted list of status codes."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        self.calls.append({"method": method, "url": url, "headers": headers})
        response = requests.Response()
        response.status_code = self.statuses.pop(0)
        response._content = b'{"error":{"code":"authentication_error"}}'
        response._content_consumed = True
        response.headers["Content-Type"] = "application/json"
        return response


def test_signed_get_retries_once_on_401_with_a_fresh_timestamp(tmp_path) -> None:
    ticks = iter([1000, 2000, 3000])
    key = Ed25519PrivateKey.generate()
    signer = KalshiSigner(KEY_ID, key, clock=lambda: next(ticks))
    session = _FakeSession([401, 200])

    response = signed_get(
        session, "https://demo-api.kalshi.co" + PATH + "?limit=1", signer=signer
    )

    assert response.status_code == 200
    assert len(session.calls) == 2
    assert session.calls[0]["headers"][HEADER_TIMESTAMP] == "1000"
    assert session.calls[1]["headers"][HEADER_TIMESTAMP] == "2000"
    # The query string is sent but never signed.
    assert session.calls[0]["url"].endswith("?limit=1")
    signature = base64.b64decode(session.calls[1]["headers"][HEADER_SIGNATURE])
    key.public_key().verify(signature, f"2000GET{PATH}".encode("utf-8"))


def test_signed_request_does_not_retry_other_errors(tmp_path) -> None:
    key = Ed25519PrivateKey.generate()
    signer = KalshiSigner(KEY_ID, key, clock=lambda: STAMP)
    for status in (400, 403, 429, 500):
        session = _FakeSession([status, 200])

        response = signed_request(
            session, "GET", "https://demo-api.kalshi.co" + PATH, signer=signer
        )

        assert response.status_code == status
        assert len(session.calls) == 1


def test_error_code_reads_the_documented_envelope() -> None:
    response = requests.Response()
    response.status_code = 401
    response._content = b'{"error":{"code":"authentication_error","message":"x"}}'

    assert error_code(response) == "authentication_error"

    response._content = b'{"code":"not_found"}'
    assert error_code(response) == "not_found"

    response._content = b"<html>401</html>"
    assert error_code(response) is None
