"""Smart-wallet auth tests: POLY_1271 orders, Relayer batches, Safe exec, proxy probe.

Everything here is offline. HTTP goes through an injected fake session; the only
"network" object is a fake chain for the Safe nonce read.

Vectors were cross-checked against independent implementations while writing the
modules:
  * POLY_1271 order signature: py-clob-client-v2's own expected signature
    (``tests/order_utils/test_exchange_order_builder_v2.py``).
  * Type-0 order signature: viem ``signTypedData`` with the same key/struct.
  * SafeTx hash, Safe domain separator, ``execTransaction`` calldata: viem
    ``hashTypedData`` / ``encodeFunctionData``.
  * Deposit Wallet ``Batch`` digest: viem ``hashTypedData``.
  * Proxy ``proxy(ProxyCall[])`` calldata and relay digest: viem
    ``encodeFunctionData`` / ``keccak256``.
"""

from __future__ import annotations

import json

import pytest

from polymarketbot.risk.fast_signer import (
    ORDER_V2_TYPE_STRING,
    FastEIP712SignerV2,
    OrderV2,
    _SECP256K1_G,
    _SECP256K1_N,
    _SECP256K1_P,
    _point_add,
    _point_mul,
    keccak256,
    private_key_to_address,
)
from polymarketbot.trading import proxy_wallet, relayer, safe
from polymarketbot.trading.client import (
    DEFAULT_CTF_EXCHANGE_V2,
    ClobAuthCredentials,
    LiveClobClient,
)

SAMPLE_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204db7e716b78ac5c7807e8b88f211d733b"
EOA_ADDRESS = "0x4a6d2fa1266d0e22ae77f79f6e16f3e5f6294e93"
DEPOSIT_WALLET = "0x1111111111111111111111111111111111111111"
API_KEY = "11111111-2222-3333-4444-555555555555"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
TARGET = "0x12121212006e4CD160D18e3f00711DA5c3372600"
RELAY = "0x1111111111111111111111111111111111111111"

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

# viem hashTypedData / encodeFunctionData vectors.
SAFE_TX_HASH_VECTOR = "0xc9ec358bea6a8a00de0ab9c3408ab5ed665d7a47f44d3a3af88d21995de5b978"
SAFE_DOMAIN_SEPARATOR_VECTOR = "0x9d7cc2704796172af167f1918bcfd2325d7ff2a7caff961d3e1d33ebf98ba1d1"
SAFE_EXEC_CALLDATA_VECTOR = (
    "0x6a7612020000000000000000000000004bfb41d5b3570defd03c39a9a4d8de6bd8b8982e0000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000014000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000000000000001800000000000000000000000"
    "000000000000000000000000000000000000000002123400000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000041ababababababababababab"
    "abababababababababababababababababababababcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"
    "cdcdcdcdcd1c00000000000000000000000000000000000000000000000000000000000000"
)
SAFE_DUMMY_SIGNATURE = "0x" + "ab" * 32 + "cd" * 32 + "1c"

BATCH_DIGEST_VECTOR = "0x87b9d2fc1e9af10d4b53f785d4c9ce1cd8b70c746b3690a0ed707de92eacea4e"
PROXY_CALLDATA_VECTOR = (
    "0x34ee97910000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "00000000000000000000000012121212006e4cd160d18e3f00711da5c3372600"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000080"
    "0000000000000000000000000000000000000000000000000000000000000004"
    "deadbeef00000000000000000000000000000000000000000000000000000000"
)
PROXY_RELAY_DIGEST_VECTOR = (
    "0x748bd34fb312dc91d7abaf0d7af674f27821f04fc8298f37d73a5200d6bdd771"
)
PROXY_RELAY_DATA = "0x34ee9791" + "00" * 31 + "20"

# Constants printed in safe-fndn/safe-smart-account contracts/Safe.sol.
SAFE_DOMAIN_TYPEHASH_SOURCE = (
    "0x47e79534a245952e8b16893a336b85a3d9ea9fa8c573f3d803afb92a79469218"
)
SAFE_TX_TYPEHASH_SOURCE = (
    "0xbb8310d486368db6bd6f849402fdd73ad53d316b5a4b2644ad6efe0f941286d8"
)


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ("" if payload is None else json.dumps(payload))

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise AssertionError(f"unexpected {method} {url}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeChain:
    def __init__(self, result="0x0000000000000000000000000000000000000000000000000000000000000007"):
        self.result = result
        self.calls: list[tuple] = []

    def eth_call(self, to, data, *, block="latest"):
        self.calls.append((to, data, block))
        return self.result


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


def eip191(digest: bytes) -> bytes:
    return keccak256(b"\x19Ethereum Signed Message:\n32" + digest)


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


# ---------------------------------------------------------------------------
# Relayer
# ---------------------------------------------------------------------------
def relayer_client(fake: FakeSession) -> relayer.RelayerClient:
    return relayer.RelayerClient(
        credentials=relayer.RelayerCredentials(
            api_key="dummy-key", api_key_address=EOA_ADDRESS
        ),
        session=fake,
    )


def test_relayer_call_payload_shape():
    call = relayer.RelayerCall(target=TARGET, data="0xdeadbeef")
    assert call.to_payload() == {"target": TARGET, "value": "0", "data": "0xdeadbeef"}


def test_relayer_batch_typed_data_and_digest():
    calls = [relayer.RelayerCall(target=TARGET, data="0xdeadbeef")]
    typed_data = relayer.build_batch_typed_data(
        deposit_wallet=DEPOSIT_WALLET, nonce=3, deadline=1700000000, calls=calls
    )

    assert typed_data == {
        "domain": {
            "name": "DepositWallet",
            "version": "1",
            "chainId": 137,
            "verifyingContract": DEPOSIT_WALLET,
        },
        "types": {
            "Call": [
                {"name": "target", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "data", "type": "bytes"},
            ],
            "Batch": [
                {"name": "wallet", "type": "address"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
                {"name": "calls", "type": "Call[]"},
            ],
        },
        "primaryType": "Batch",
        "message": {
            "wallet": DEPOSIT_WALLET,
            "nonce": 3,
            "deadline": 1700000000,
            "calls": [{"target": TARGET, "value": 0, "data": "0xdeadbeef"}],
        },
    }
    assert "0x" + relayer.hash_batch_typed_data(typed_data).hex() == BATCH_DIGEST_VECTOR

    signature = relayer.sign_batch_typed_data(SAMPLE_PRIVATE_KEY, typed_data)
    assert recover_address(relayer.hash_batch_typed_data(typed_data), signature) == EOA_ADDRESS


def test_relayer_wallet_submit_body_is_exact():
    calls = [relayer.RelayerCall(target=TARGET, data="0xdeadbeef")]
    body = relayer.build_wallet_submit_body(
        signer=EOA_ADDRESS,
        deposit_wallet=DEPOSIT_WALLET,
        nonce=3,
        deadline=1700000000,
        signature="0x" + "11" * 65,
        calls=calls,
        metadata="test batch",
    )

    assert body == {
        "type": "WALLET",
        "from": EOA_ADDRESS,
        "to": "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07",
        "nonce": "3",
        "signature": "0x" + "11" * 65,
        "metadata": "test batch",
        "depositWalletParams": {
            "depositWallet": DEPOSIT_WALLET,
            "deadline": "1700000000",
            "calls": [{"target": TARGET, "value": "0", "data": "0xdeadbeef"}],
        },
    }


def test_relayer_submit_posts_exact_request():
    fake = FakeSession(
        FakeResponse({"transactionID": "tx-1", "state": "STATE_NEW"})
    )
    client = relayer_client(fake)

    submission = client.submit_wallet_calls(
        [relayer.RelayerCall(target=TARGET, data="0xdeadbeef")],
        signer=EOA_ADDRESS,
        deposit_wallet=DEPOSIT_WALLET,
        nonce=3,
        deadline=1700000000,
        signature="0x" + "11" * 65,
        metadata="test batch",
    )

    assert submission.transaction_id == "tx-1"
    assert submission.state == "STATE_NEW"
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == relayer.RELAYER_HOST + "/submit"
    assert call["headers"]["RELAYER_API_KEY"] == "dummy-key"
    assert call["headers"]["RELAYER_API_KEY_ADDRESS"] == EOA_ADDRESS
    assert call["json"] == relayer.build_wallet_submit_body(
        signer=EOA_ADDRESS,
        deposit_wallet=DEPOSIT_WALLET,
        nonce=3,
        deadline=1700000000,
        signature="0x" + "11" * 65,
        calls=[relayer.RelayerCall(target=TARGET, data="0xdeadbeef")],
        metadata="test batch",
    )


def test_relayer_nonce_fetch():
    fake = FakeSession(FakeResponse({"address": EOA_ADDRESS, "nonce": "7"}))
    client = relayer_client(fake)

    assert client.get_wallet_nonce(EOA_ADDRESS) == 7
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == relayer.RELAYER_HOST + "/v1/account/transactions/params"
    assert call["params"] == {"address": EOA_ADDRESS, "type": "WALLET"}


def test_relayer_nonce_rejects_garbage():
    fake = FakeSession(FakeResponse({"address": EOA_ADDRESS, "nonce": "soon"}))
    with pytest.raises(relayer.RelayerError, match="not an integer"):
        relayer_client(fake).get_wallet_nonce(EOA_ADDRESS)


def test_relayer_poll_state_machine_confirms():
    fake = FakeSession(
        FakeResponse({"transaction_id": "tx-1", "state": "STATE_NEW"}),
        FakeResponse({"transaction_id": "tx-1", "state": "STATE_PENDING"}),
        FakeResponse(
            {
                "transaction_id": "tx-1",
                "transaction_hash": "0xfeed",
                "state": "STATE_CONFIRMED",
                "error_msg": None,
            }
        ),
    )
    client = relayer_client(fake)

    transaction = client.poll_transaction("tx-1", interval=0, timeout=5)

    assert transaction.confirmed
    assert transaction.transaction_hash == "0xfeed"
    assert [call["url"] for call in fake.calls] == [
        relayer.RELAYER_HOST + "/v1/account/transactions/tx-1"
    ] * 3


def test_relayer_poll_raises_on_terminal_failure():
    fake = FakeSession(
        FakeResponse(
            {
                "transaction_id": "tx-1",
                "state": "STATE_FAILED",
                "error_msg": "reverted",
            }
        )
    )
    with pytest.raises(relayer.RelayerError, match="reverted"):
        relayer_client(fake).poll_transaction("tx-1", interval=0, timeout=5)


def test_relayer_poll_times_out():
    fake = FakeSession(FakeResponse({"transaction_id": "tx-1", "state": "STATE_NEW"}))
    with pytest.raises(relayer.RelayerError, match="still STATE_NEW"):
        relayer_client(fake).poll_transaction("tx-1", interval=0, timeout=0)


def test_relayer_legacy_list_shape_and_legacy_path():
    fake = FakeSession(
        FakeResponse(
            [
                {
                    "transactionID": "tx-legacy",
                    "transactionHash": "0xbeef",
                    "state": "STATE_CONFIRMED",
                }
            ]
        )
    )
    transaction = relayer_client(fake).poll_transaction(
        "tx-legacy", interval=0, path=relayer.LEGACY_TRANSACTION_PATH
    )
    assert transaction.transaction_id == "tx-legacy"
    assert transaction.transaction_hash == "0xbeef"
    assert fake.calls[0]["url"] == relayer.RELAYER_HOST + "/transaction?id=tx-legacy"


def test_relayer_missing_credentials_fails_without_network():
    fake = FakeSession()
    client = relayer.RelayerClient(credentials=relayer.RelayerCredentials(), session=fake)
    with pytest.raises(relayer.RelayerError, match="not configured"):
        client.get_wallet_nonce(EOA_ADDRESS)
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Safe
# ---------------------------------------------------------------------------
def safe_tx() -> safe.SafeExecTransaction:
    return safe.build_exec_transaction(
        safe=DEPOSIT_WALLET,
        to="0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
        data="0x1234",
        nonce=7,
    )


def test_safe_domain_and_tx_hash_vectors():
    assert "0x" + safe.SAFE_DOMAIN_TYPEHASH.hex() == SAFE_DOMAIN_TYPEHASH_SOURCE
    assert "0x" + safe.SAFE_TX_TYPEHASH.hex() == SAFE_TX_TYPEHASH_SOURCE
    assert "0x" + safe.safe_domain_separator(DEPOSIT_WALLET).hex() == SAFE_DOMAIN_SEPARATOR_VECTOR
    assert "0x" + safe_tx().safe_tx_hash.hex() == SAFE_TX_HASH_VECTOR


def test_safe_exec_calldata_vector():
    assert safe_tx().exec_calldata(SAFE_DUMMY_SIGNATURE) == SAFE_EXEC_CALLDATA_VECTOR
    assert safe.EXEC_TRANSACTION_SELECTOR == SAFE_EXEC_CALLDATA_VECTOR[:10]


def test_safe_owner_signature_packing_and_recovery():
    tx = safe_tx()
    signature = tx.signature(SAMPLE_PRIVATE_KEY)

    raw = bytes.fromhex(signature[2:])
    assert len(raw) == 65
    assert raw[64] in (31, 32)  # eth_sign mode: 27/28 + 4

    assert recover_address(eip191(tx.safe_tx_hash), signature) == EOA_ADDRESS

    bare = tx.signature(SAMPLE_PRIVATE_KEY, eth_sign=False)
    assert bytes.fromhex(bare[2:])[64] in (27, 28)
    assert recover_address(tx.safe_tx_hash, bare) == EOA_ADDRESS


def test_pack_owner_signature_normalises_v():
    body = "ab" * 32 + "cd" * 32
    assert safe.pack_owner_signature("0x" + body + "00") == "0x" + body + "1b"
    assert safe.pack_owner_signature("0x" + body + "01") == "0x" + body + "1c"
    assert safe.pack_owner_signature("0x" + body + "00", eth_sign=True) == "0x" + body + "1f"
    assert safe.pack_owner_signature("0x" + body + "01", eth_sign=True) == "0x" + body + "20"
    assert safe.pack_owner_signature("0x" + body + "1c", eth_sign=True) == "0x" + body + "20"
    assert safe.pack_owner_signature("0x" + body + "1c") == "0x" + body + "1c"
    with pytest.raises(ValueError, match="v must be"):
        safe.pack_owner_signature("0x" + body + "07")
    with pytest.raises(ValueError, match="65 bytes"):
        safe.pack_owner_signature("0x" + body)


def test_safe_relayer_envelope_shape():
    envelope = safe_tx().relayer_envelope(
        SAFE_DUMMY_SIGNATURE, owner=EOA_ADDRESS, metadata="test"
    )
    assert envelope == {
        "type": "SAFE",
        "from": EOA_ADDRESS,
        "to": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
        "proxyWallet": DEPOSIT_WALLET,
        "value": "0",
        "data": "0x1234",
        "nonce": "7",
        "signature": SAFE_DUMMY_SIGNATURE,
        "metadata": "test",
        "signatureParams": {
            "operation": "0",
            "safeTxnGas": "0",
            "baseGas": "0",
            "gasPrice": "0",
            "gasToken": ZERO_ADDRESS,
            "refundReceiver": ZERO_ADDRESS,
        },
    }


def test_safe_nonce_read_uses_eth_call():
    chain = FakeChain()
    assert safe.nonce(DEPOSIT_WALLET, chain=chain) == 7
    assert chain.calls == [(DEPOSIT_WALLET, safe.NONCE_SELECTOR, "latest")]


def test_safe_nonce_read_rejects_empty_result():
    with pytest.raises(RuntimeError, match="no data"):
        safe.nonce(DEPOSIT_WALLET, chain=FakeChain(result="0x"))


# ---------------------------------------------------------------------------
# Legacy proxy wallet
# ---------------------------------------------------------------------------
def test_proxy_probe_capabilities():
    supported = proxy_wallet.probe_proxy_wallet(1)
    assert supported.supported
    assert supported.proxy_function == "proxy((uint8,address,uint256,bytes)[])"
    assert supported.proxy_selector == proxy_wallet.PROXY_CALLS_SELECTOR
    assert supported.relay_type == "PROXY"

    unsupported = proxy_wallet.probe_proxy_wallet(3)
    assert not unsupported.supported
    assert "type 3" in unsupported.reason
    assert "unsupported" in unsupported.describe()


def test_proxy_calldata_vector():
    calls = [proxy_wallet.ProxyCall(to=TARGET, data="0xdeadbeef")]
    assert proxy_wallet.encode_proxy_calls(calls) == PROXY_CALLDATA_VECTOR


def test_proxy_relay_digest_and_signature():
    digest = proxy_wallet.build_relay_digest(
        from_address=EOA_ADDRESS,
        to=proxy_wallet.PROXY_WALLET_FACTORY,
        data=PROXY_RELAY_DATA,
        relay=RELAY,
        relayer_fee=0,
        gas_price=0,
        gas_limit=210000,
        nonce=3,
    )
    assert "0x" + digest.hex() == PROXY_RELAY_DIGEST_VECTOR

    signature = proxy_wallet.sign_relay_request(SAMPLE_PRIVATE_KEY, digest)
    assert recover_address(eip191(digest), signature) == EOA_ADDRESS


def test_proxy_call_type_validation():
    with pytest.raises(ValueError, match="type_code"):
        proxy_wallet.ProxyCall(to=TARGET, type_code=0)
