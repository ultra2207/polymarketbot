"""CTF Exchange V2 order tests (CLOB order version 2) — offline, deterministic.

References verified while implementing:
- https://github.com/Polymarket/ctf-exchange-v2 (Structs.sol ORDER_TYPEHASH,
  Hashing.sol domain name "Polymarket CTF Exchange" / version "2")
- https://docs.polymarket.com/resources/contracts.md (Polygon addresses)
- GET https://clob.polymarket.com/version -> {"version": 2} (2026-11-03)
"""

from __future__ import annotations

import json
import time

import pytest

from polymarketbot.risk.fast_signer import (
    ORDER_V2_TYPEHASH,
    ORDER_V2_TYPE_STRING,
    _SECP256K1_G,
    _SECP256K1_N,
    _SECP256K1_P,
    DEFAULT_CTF_EXCHANGE_V2,
    FastEIP712SignerV2,
    OrderV2,
    V2_CTF_EXCHANGE,
    V2_NEG_RISK_CTF_EXCHANGE,
    compute_domain_separator,
    compute_domain_separator_v2,
    keccak256,
    _point_add,
    _point_mul,
)
from polymarketbot.strategy.venue.clob import (
    DEFAULT_CTF_EXCHANGE,
    ClobAPIError,
    ClobAuthCredentials,
    LiveClobClient,
)

SAMPLE_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204db7e716b78ac5c7807e8b88f211d733b"
EOA_ADDRESS = "0x4a6d2fa1266d0e22ae77f79f6e16f3e5f6294e93"
PROXY_ADDRESS = "0x" + "11" * 20
API_KEY = "11111111-2222-3333-4444-555555555555"
EXPECTED_V2_TYPEHASH = "0xbb86318a2138f5fa8ae32fbe8e659f8fcf13cc6ae4014a707893055433818589"


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text="", json_error=False):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error
        self.text = text or ("" if payload is None else json.dumps(payload))

    def json(self):
        if self._json_error:
            raise ValueError("Expecting value")
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[dict] = []
        # DataAPIClient sets a default User-Agent on the session it is given.
        self.headers: dict[str, str] = {}

    def _next(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise AssertionError(f"unexpected {method} {url}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kwargs):
        return self._next("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._next("DELETE", url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._next(method, url, **kwargs)


def make_client(fake: FakeSession, **kwargs) -> LiveClobClient:
    client = LiveClobClient(
        credentials=ClobAuthCredentials(
            api_key=API_KEY,
            api_secret="dGVzdF9zZWNyZXQ=",
            passphrase="pass",
            address=PROXY_ADDRESS,
            private_key=SAMPLE_PRIVATE_KEY,
            signature_type=1,
        ),
        dry_run=False,
        **kwargs,
    )
    assert client.dry_run is False
    client._get_session = lambda: fake  # type: ignore[method-assign]
    return client


def make_dry_client(**kwargs) -> LiveClobClient:
    return LiveClobClient(
        credentials=ClobAuthCredentials(private_key=SAMPLE_PRIVATE_KEY),
        dry_run=True,
        **kwargs,
    )


def post_body(call: dict) -> dict:
    return json.loads(call["data"])


# ---------------------------------------------------------------------------
# Signer primitives
# ---------------------------------------------------------------------------
def test_v2_typehash_matches_contract_constant():
    assert "0x" + ORDER_V2_TYPEHASH.hex() == EXPECTED_V2_TYPEHASH
    assert ORDER_V2_TYPEHASH == keccak256(ORDER_V2_TYPE_STRING)
    assert V2_CTF_EXCHANGE == DEFAULT_CTF_EXCHANGE_V2 == "0xE111180000d2663C0091e4f400237545B87B996B"
    assert V2_NEG_RISK_CTF_EXCHANGE == "0xe2222d279d744050d28e00520010520000310F59"


def test_domain_separator_v2_reproducible_and_distinct_from_v1():
    first = compute_domain_separator_v2()
    second = compute_domain_separator_v2(chain_id=137, verifying_contract=V2_CTF_EXCHANGE)
    assert first == second
    assert len(first) == 32
    assert first == compute_domain_separator(
        name="Polymarket CTF Exchange",
        version="2",
        chain_id=137,
        verifying_contract=V2_CTF_EXCHANGE,
    )
    assert first != compute_domain_separator()  # V1 default domain (version "1")
    assert first != compute_domain_separator_v2(verifying_contract=V2_NEG_RISK_CTF_EXCHANGE)


def test_v2_payload_field_types_and_side_strings():
    signer = FastEIP712SignerV2(private_key=SAMPLE_PRIVATE_KEY)
    order = OrderV2(
        salt=123456789,
        maker=PROXY_ADDRESS,
        signer=EOA_ADDRESS,
        tokenId=42,
        makerAmount=4_100_000,
        takerAmount=10_000_000,
        timestamp=1_700_000_000_123,
        side=0,
        signatureType=1,
    )
    payload = signer.build_signed_order_payload(
        order, owner=API_KEY, expiration=0, order_type="GTC", post_only=True
    )
    body = payload["order"]

    assert body["side"] == "BUY"
    assert isinstance(body["salt"], int)
    assert isinstance(body["signatureType"], int)
    for field in (
        "tokenId",
        "makerAmount",
        "takerAmount",
        "expiration",
        "timestamp",
        "metadata",
        "builder",
        "signature",
    ):
        assert isinstance(body[field], str), field
    assert body["timestamp"] == "1700000000123"
    assert body["metadata"] == "0x" + "00" * 32
    assert body["builder"] == "0x" + "00" * 32
    assert body["signature"].startswith("0x") and len(body["signature"]) == 132
    assert "taker" not in body and "nonce" not in body and "feeRateBps" not in body

    assert payload["owner"] == API_KEY
    assert payload["orderType"] == "GTC"
    assert payload["deferExec"] is False
    assert payload["postOnly"] is True

    sell = OrderV2(
        salt=1,
        maker=PROXY_ADDRESS,
        signer=EOA_ADDRESS,
        tokenId=42,
        makerAmount=10_000_000,
        takerAmount=4_100_000,
        timestamp=1_700_000_000_123,
        side=1,
    )
    assert sell.side == 1
    assert signer.build_signed_order_payload(sell)["order"]["side"] == "SELL"


def test_v2_timestamp_is_unix_milliseconds():
    client = make_dry_client(order_version=2)
    before_ms = int(time.time() * 1000)
    order, payload = client.build_signed_order("42", "BUY", 0.5, 10.0)
    after_ms = int(time.time() * 1000)

    assert isinstance(order, OrderV2)
    assert before_ms <= order.timestamp <= after_ms + 5
    assert order.timestamp > 10**12  # milliseconds, not seconds
    assert payload["order"]["timestamp"] == str(order.timestamp)

    fixed, fixed_payload = client.build_signed_order(
        "42", "BUY", 0.5, 10.0, timestamp=1_700_000_000_123
    )
    assert fixed.timestamp == 1_700_000_000_123
    assert fixed_payload["order"]["timestamp"] == "1700000000123"


# ---------------------------------------------------------------------------
# Signature recovery
# ---------------------------------------------------------------------------
def recover_address(digest: bytes, signature: str) -> str:
    """Recover the EOA that produced a 65-byte (r, s, v=27/28) signature."""
    raw = bytes.fromhex(signature.removeprefix("0x"))
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:64], "big")
    v = raw[64]
    assert v in (27, 28), v
    recid = v - 27

    x = r
    alpha = (pow(x, 3, _SECP256K1_P) + 7) % _SECP256K1_P
    y = pow(alpha, (_SECP256K1_P + 1) // 4, _SECP256K1_P)
    assert (y * y) % _SECP256K1_P == alpha
    if y % 2 != recid:
        y = _SECP256K1_P - y

    z = int.from_bytes(digest, "big") % _SECP256K1_N
    s_r = _point_mul(s % _SECP256K1_N, (x, y))
    z_g = _point_mul(z, _SECP256K1_G)
    neg_z_g = (z_g[0], (-z_g[1]) % _SECP256K1_P)
    q = _point_mul(pow(r, -1, _SECP256K1_N), _point_add(s_r, neg_z_g))
    pub = q[0].to_bytes(32, "big") + q[1].to_bytes(32, "big")
    return "0x" + keccak256(pub)[12:].hex()


def test_v2_signature_recovers_to_signer_eoa():
    client = make_dry_client(order_version=2)
    order, payload = client.build_signed_order(
        "42", "BUY", 0.5, 10.0, salt=99, timestamp=1_700_000_000_123
    )
    signer = client._signers[f"v2:{DEFAULT_CTF_EXCHANGE_V2.lower()}"]
    assert isinstance(signer, FastEIP712SignerV2)

    digest = signer.hash_typed_data(order)
    assert recover_address(digest, payload["order"]["signature"]).lower() == EOA_ADDRESS.lower()
    assert order.maker.lower() == EOA_ADDRESS.lower()  # no proxy in this fixture
    assert order.signatureType == 0


def test_v1_and_v2_digests_differ_for_same_order_inputs():
    client = make_dry_client()
    v2_order, v2_payload = client.build_signed_order(
        "42", "BUY", 0.5, 10.0, salt=5, timestamp=1_700_000_000_123, order_version=2
    )
    v1_order, v1_payload = client.build_signed_order(
        "42", "BUY", 0.5, 10.0, salt=5, order_version=1
    )
    assert v2_payload["order"]["signature"] != v1_payload["signature"]
    assert "taker" in v1_payload["order"]


# ---------------------------------------------------------------------------
# Version routing and retry
# ---------------------------------------------------------------------------
def test_place_order_routes_v1_when_server_reports_one():
    fake = FakeSession(
        FakeResponse({"version": 1}),
        FakeResponse({"success": True, "orderID": "0xv1"}),
    )
    client = make_client(fake)
    result = client.place_order("42", "BUY", 0.5, 10.0, tick_size=0.01, neg_risk=False)

    assert result.success is True
    assert client.order_version == 1
    assert fake.calls[0]["url"].endswith("/version")
    body = post_body(fake.calls[1])
    assert body["order"]["taker"] == "0x" + "00" * 20
    assert body["order"]["nonce"] == "0"
    assert body["order"]["feeRateBps"] == "0"
    assert "signature" in body and isinstance(body["signature"], str)
    assert "timestamp" not in body["order"]
    assert f"v1:{DEFAULT_CTF_EXCHANGE.lower()}" in client._signers


def test_place_order_routes_v2_when_server_reports_two():
    fake = FakeSession(
        FakeResponse({"version": 2}),
        FakeResponse({"success": True, "orderID": "0xv2"}),
    )
    client = make_client(fake)
    result = client.place_order("42", "BUY", 0.5, 10.0, tick_size=0.01, neg_risk=False)

    assert result.success is True
    body = post_body(fake.calls[1])
    assert "taker" not in body["order"] and "nonce" not in body["order"]
    assert body["order"]["timestamp"].isdigit()
    assert body["order"]["metadata"] == "0x" + "00" * 32
    assert body["order"]["signature"].startswith("0x")
    assert f"v2:{DEFAULT_CTF_EXCHANGE_V2.lower()}" in client._signers


def test_order_version_mismatch_refetches_and_retries_once():
    fake = FakeSession(
        FakeResponse({"error": "order_version_mismatch"}, status_code=400),
        FakeResponse({"version": 2}),
        FakeResponse({"success": True, "orderID": "0xretry"}),
    )
    client = make_client(fake, order_version=1)
    result = client.place_order("42", "BUY", 0.5, 10.0, tick_size=0.01, neg_risk=False)

    assert result.success is True
    assert [c["method"] for c in fake.calls] == ["POST", "GET", "POST"]
    assert fake.calls[1]["url"].endswith("/version")
    retried = post_body(fake.calls[2])
    assert "timestamp" in retried["order"]  # rebuilt as V2
    assert client.order_version == 2


def test_order_version_mismatch_retries_only_once():
    fake = FakeSession(
        FakeResponse({"error": "order_version_mismatch"}, status_code=400),
        FakeResponse({"version": 2}),
        FakeResponse({"error": "order_version_mismatch"}, status_code=400),
    )
    client = make_client(fake, order_version=1)
    result = client.place_order("42", "BUY", 0.5, 10.0, tick_size=0.01, neg_risk=False)

    assert result.success is False
    assert result.status == "REJECTED"
    assert len(fake.calls) == 3
    assert all(c["method"] in ("POST", "GET") for c in fake.calls)


def test_v3_version_raises_and_place_order_refuses_to_sign():
    fake = FakeSession(
        FakeResponse({"version": 3}),
        FakeResponse({"version": 3}),
    )
    client = make_client(fake)
    with pytest.raises(ClobAPIError, match="unsupported CLOB order version 3"):
        client.order_version

    result = client.place_order("42", "BUY", 0.5, 10.0, tick_size=0.01, neg_risk=False)
    assert result.success is False
    assert "unsupported CLOB order version 3" in (result.error_message or "")
    assert result.signature == ""
    assert all(c["url"].endswith("/version") for c in fake.calls)


# ---------------------------------------------------------------------------
# Dry-run version smoke + market parameter helpers
# ---------------------------------------------------------------------------
def test_dry_run_version_smoke_with_mocked_http():
    fake = FakeSession(FakeResponse({"version": 2}))
    client = make_dry_client()  # no version injected
    client._get_session = lambda: fake  # type: ignore[method-assign]

    assert client.order_version == 2
    assert len(fake.calls) == 1
    assert fake.calls[0]["method"] == "GET"
    assert fake.calls[0]["url"].endswith("/version")

    result = client.place_order("42", "BUY", 0.5, 10.0)
    assert result.success is True and result.is_dry_run is True
    assert client._mock_orders[result.order_id]["order_version"] == 2
    # no second /version call and no POST while dry-run
    assert len(fake.calls) == 1


def test_tick_size_and_neg_risk_helpers_cache():
    fake = FakeSession(
        FakeResponse({"minimum_tick_size": 0.001}),
        FakeResponse({"neg_risk": True}),
    )
    client = make_client(fake, order_version=2)

    assert client.get_tick_size("42") == pytest.approx(0.001)
    assert client.get_neg_risk("42") is True
    assert client.get_tick_size("42") == pytest.approx(0.001)  # cached
    assert client.get_neg_risk("42") is True  # cached
    assert len(fake.calls) == 2
    assert fake.calls[0]["url"].endswith("/tick-size")
    assert fake.calls[0]["params"] == {"token_id": "42"}
    assert fake.calls[1]["url"].endswith("/neg-risk")


def test_unsupported_tick_size_raises():
    fake = FakeSession(FakeResponse({"minimum_tick_size": 0.02}))
    client = make_client(fake, order_version=2)
    with pytest.raises(ClobAPIError, match="unsupported tick"):
        client.get_tick_size("42")


def test_place_order_fetches_tick_and_neg_risk_when_omitted():
    fake = FakeSession(
        FakeResponse({"minimum_tick_size": 0.001}),
        FakeResponse({"neg_risk": True}),
        FakeResponse({"success": True, "orderID": "0xneg"}),
    )
    client = make_client(fake, order_version=2)
    result = client.place_order("42", "BUY", 0.4135, 10.0)

    assert result.success is True
    assert result.price == pytest.approx(0.414)
    body = post_body(fake.calls[2])
    # BUY: makerAmount = size(10.00) * price(0.414) = 4.14
    assert int(body["order"]["makerAmount"]) == 4_140_000
    assert int(body["order"]["takerAmount"]) == 10_000_000
    assert f"v2:{V2_NEG_RISK_CTF_EXCHANGE.lower()}" in client._signers
