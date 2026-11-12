"""Smart-wallet order auth tests: POLY_1271 and type-0 order signatures.

Everything here is offline; no network is touched.

Vectors were cross-checked against independent implementations while writing the
module:
  * POLY_1271 order signature: py-clob-client-v2's own expected signature
    (``tests/order_utils/test_exchange_order_builder_v2.py``).
  * Type-0 order signature: viem ``signTypedData`` with the same key/struct.
"""

from __future__ import annotations

import pytest

from polymarketbot.risk.fast_signer import (
    _SECP256K1_G,
    _SECP256K1_N,
    _SECP256K1_P,
    ORDER_V2_TYPE_STRING,
    FastEIP712SignerV2,
    OrderV2,
    _point_add,
    _point_mul,
    keccak256,
    private_key_to_address,
)
from polymarketbot.strategy.venue.clob import (
    DEFAULT_CTF_EXCHANGE_V2,
    ClobAuthCredentials,
    LiveClobClient,
)

SAMPLE_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204db7e716b78ac5c7807e8b88f211d733b"
EOA_ADDRESS = "0x4a6d2fa1266d0e22ae77f79f6e16f3e5f6294e93"
DEPOSIT_WALLET = "0x1111111111111111111111111111111111111111"
API_KEY = "11111111-2222-3333-4444-555555555555"

# py-clob-client-v2 fixed POLY_1271 vector (Amoy chain, dev private key).
SDK_DEV_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
SDK_CHAIN_ID = 80002
SDK_EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"
SDK_SALT = 479249096354
SDK_TIMESTAMP = 1710000000000
SDK_POLY_1271_SIGNATURE = (
    "0xa3a093c83b6c20c83355c16ce94c92e6e9fcbdeb840618cc74f6c57a42ad145b"
    "2b98db73d2c73cbf1f2b6af288566ae81960ddbc3a13921027358a8bff3be6ff1c"
    "a440cbd865bc0c6243d7a8df9a8bf48a8827b0a4abbb61c30e96d305423af148"
    "d23d42d3ad94e65d78258cecaf8dcbaddac0f73dc085040f2c12bb595dd83804"
    "4f726465722875696e743235362073616c742c61646472657373206d616b65722c"
    "61646472657373207369676e65722c75696e7432353620746f6b656e49642c75"
    "696e74323536206d616b6572416d6f756e742c75696e743235362074616b6572"
    "416d6f756e742c75696e743820736964652c75696e7438207369676e61747572"
    "65547970652c75696e743235362074696d657374616d702c6279746573333220"
    "6d657461646174612c62797465733332206275696c6465722900ba"
)

# viem signTypedData with SAMPLE_PRIVATE_KEY, CTF Exchange V2 on Polygon.
TYPE0_SIGNATURE = (
    "0x4a5b2a0ebb46c19908b64d36c50fa73d68f678962e8a3eec7d8452246d81b7e0"
    "4e9431a1917015893b80dae7b47d3ffa18bc2ffe68951b262e8f4ecf45f61c311c"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def recover_address(digest: bytes, signature: str) -> str:
    """Minimal secp256k1 public-key recovery for signature verification in tests."""
    raw = bytes.fromhex(signature.removeprefix("0x"))
    assert len(raw) == 65
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:64], "big")
    recid = (raw[64] - 27) & 1  # v may be 27/28 or eth_sign 31/32
    x = r
    y_squared = (pow(x, 3, _SECP256K1_P) + 7) % _SECP256K1_P
    y = pow(y_squared, (_SECP256K1_P + 1) // 4, _SECP256K1_P)
    if y % 2 != recid % 2:
        y = _SECP256K1_P - y
    point_r = (x, y)
    z = int.from_bytes(digest, "big")
    r_inv = pow(r, -1, _SECP256K1_N)
    s_r = _point_mul((s * r_inv) % _SECP256K1_N, point_r)
    z_g = _point_mul((-z * r_inv) % _SECP256K1_N, _SECP256K1_G)
    public = _point_add(s_r, z_g)
    assert public is not None
    return "0x" + keccak256(public[0].to_bytes(32, "big") + public[1].to_bytes(32, "big"))[-20:].hex()


# ---------------------------------------------------------------------------
# POLY_1271 orders
# ---------------------------------------------------------------------------
def sdk_signer() -> FastEIP712SignerV2:
    return FastEIP712SignerV2(
        private_key=SDK_DEV_KEY, verifying_contract=SDK_EXCHANGE, chain_id=SDK_CHAIN_ID
    )


def sdk_order() -> OrderV2:
    return OrderV2(
        salt=SDK_SALT,
        maker=DEPOSIT_WALLET,
        signer=DEPOSIT_WALLET,
        tokenId=1234,
        makerAmount=100000000,
        takerAmount=50000000,
        timestamp=SDK_TIMESTAMP,
        side=0,
        signatureType=3,
    )


def test_poly_1271_signature_matches_sdk_vector():
    signer = sdk_signer()
    order = sdk_order()

    signature = signer.sign_order(order)

    assert signature == SDK_POLY_1271_SIGNATURE
    expected_length = 2 + 130 + 64 + 64 + (len(ORDER_V2_TYPE_STRING) * 2) + 4
    assert len(signature) == expected_length


def test_poly_1271_envelope_components_match_sdk_vector():
    signer = sdk_signer()
    order = sdk_order()
    signature = signer.sign_order(order)

    body = signature[2:]
    inner = body[:130]
    app_domain = body[130:194]
    contents_hash = body[194:258]
    contents_type = bytes.fromhex(body[258:-4])
    type_length = int(body[-4:], 16)

    assert app_domain == signer.domain_separator.hex()
    assert app_domain == SDK_POLY_1271_SIGNATURE[2 + 130 : 2 + 194]
    assert contents_hash == signer.hash_order(order).hex()
    assert contents_hash == SDK_POLY_1271_SIGNATURE[2 + 194 : 2 + 258]
    assert contents_type == ORDER_V2_TYPE_STRING
    assert type_length == len(ORDER_V2_TYPE_STRING)

    digest = signer.poly_1271_digest(order)
    assert recover_address(digest, "0x" + inner) == private_key_to_address(
        bytes.fromhex(SDK_DEV_KEY[2:])
    )


def test_type_3_client_order_identity_and_payload():
    client = LiveClobClient(
        credentials=ClobAuthCredentials(
            api_key=API_KEY,
            api_secret="dGVzdF9zZWNyZXQ=",
            passphrase="pass",
            address=DEPOSIT_WALLET,
            private_key=SAMPLE_PRIVATE_KEY,
            signature_type=3,
        ),
        dry_run=True,
    )

    order, payload = client.build_signed_order(
        "42",
        "BUY",
        0.5,
        10.0,
        salt=123456789,
        timestamp=1710000000123,
        order_version=2,
    )

    assert client.signature_type == 3
    assert order.signatureType == 3
    assert order.maker == DEPOSIT_WALLET
    assert order.signer == DEPOSIT_WALLET  # SDK: signer == funder for POLY_1271
    assert payload["order"]["signatureType"] == 3
    assert payload["order"]["maker"] == DEPOSIT_WALLET
    assert payload["order"]["signer"] == DEPOSIT_WALLET
    assert payload["owner"] == API_KEY
    expected_signer = FastEIP712SignerV2(
        private_key=SAMPLE_PRIVATE_KEY,
        verifying_contract=DEFAULT_CTF_EXCHANGE_V2,
        chain_id=137,
    )
    assert payload["order"]["signature"] == expected_signer.sign_poly_1271_order(order)
    assert len(payload["order"]["signature"]) == (
        2 + 130 + 64 + 64 + (len(ORDER_V2_TYPE_STRING) * 2) + 4
    )
    # L2 auth identity is still the signer EOA, never the Deposit Wallet.
    headers = client._get_auth_headers("GET", "/data/orders")
    assert headers["POLY_ADDRESS"] == EOA_ADDRESS


def test_type_3_requires_v2_orders():
    client = LiveClobClient(
        credentials=ClobAuthCredentials(
            address=DEPOSIT_WALLET,
            private_key=SAMPLE_PRIVATE_KEY,
            signature_type=3,
        ),
        dry_run=True,
    )
    with pytest.raises(ValueError, match="version 2"):
        client.build_signed_order("42", "BUY", 0.5, 10.0, order_version=1)


def test_type_3_requires_deposit_wallet_address():
    client = LiveClobClient(
        credentials=ClobAuthCredentials(private_key=SAMPLE_PRIVATE_KEY, signature_type=3),
        dry_run=True,
    )
    with pytest.raises(ValueError, match="Deposit Wallet address"):
        client.build_signed_order("42", "BUY", 0.5, 10.0, order_version=2)


def test_signature_type_3_accepted_and_4_rejected():
    assert ClobAuthCredentials(signature_type=3).signature_type == 3
    with pytest.raises(ValueError, match="0, 1, 2, 3"):
        ClobAuthCredentials(signature_type=4)


def test_type_0_order_vector_unchanged():
    signer = FastEIP712SignerV2(
        private_key=SAMPLE_PRIVATE_KEY,
        verifying_contract=DEFAULT_CTF_EXCHANGE_V2,
        chain_id=137,
    )
    order = OrderV2(
        salt=123456789,
        maker=EOA_ADDRESS,
        signer=EOA_ADDRESS,
        tokenId=42,
        makerAmount=5000000,
        takerAmount=10000000,
        timestamp=1710000000123,
        side=0,
        signatureType=0,
    )
    assert signer.sign_order(order) == TYPE0_SIGNATURE

    client = LiveClobClient(
        credentials=ClobAuthCredentials(private_key=SAMPLE_PRIVATE_KEY),
        dry_run=True,
    )
    built, payload = client.build_signed_order(
        "42",
        "BUY",
        0.5,
        10.0,
        salt=123456789,
        timestamp=1710000000123,
        order_version=2,
    )
    assert built.signatureType == 0
    assert payload["order"]["signature"] == TYPE0_SIGNATURE
