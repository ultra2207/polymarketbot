"""Offline tests for real CTF settlement: RLP, EIP-1559 signing, broadcasting.

No network is touched here. The EIP-155 vector is the canonical example from
the specification, which independently pins the RLP encoder, keccak256 and the
in-tree secp256k1 signer against a known-good transaction.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from polymarketbot.risk.fast_signer import (
    _SECP256K1_G,
    _SECP256K1_N,
    _SECP256K1_P,
    _point_add,
    _point_mul,
    keccak256,
    private_key_to_address,
)
from polymarketbot.strategy.onchain.core import ChainClient
from polymarketbot.strategy.onchain.ctf import (
    CTF_POSITION_BALANCE_SELECTOR,
    POLYGON_CHAIN_ID,
    PolygonCtfClient,
    eip1559_signing_hash,
    rlp_encode,
    sign_eip1559_transaction,
)

# Deterministic test key (already used by the signer tests); never funds-bearing.
TEST_KEY = bytes.fromhex("4c0883a69102937d6231471b5dbb6204db7e716b78ac5c7807e8b88f211d733b")
TEST_KEY_HEX = "0x4c0883a69102937d6231471b5dbb6204db7e716b78ac5c7807e8b88f211d733b"
TEST_ADDRESS = "0x4a6d2fa1266d0e22ae77f79f6e16f3e5f6294e93"

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CONDITION_ID = "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"


# ---------------------------------------------------------------------------
# RLP
# ---------------------------------------------------------------------------


def test_rlp_known_vectors():
    assert rlp_encode(["cat", "dog"]).hex() == "c88363617483646f67"
    assert rlp_encode(b"dog").hex() == "83646f67"
    assert rlp_encode(b"").hex() == "80"
    assert rlp_encode(0).hex() == "80"
    assert rlp_encode([]).hex() == "c0"
    assert rlp_encode(15).hex() == "0f"
    assert rlp_encode(127).hex() == "7f"
    assert rlp_encode(128).hex() == "8180"
    assert rlp_encode(1024).hex() == "820400"
    # 56-byte string switches to the long form: 0xb8 <len> <payload>
    assert rlp_encode(b"a" * 56).hex() == "b838" + "61" * 56
    # Nested list: ["cat", ["dog"]]
    assert rlp_encode(["cat", ["dog"]]).hex() == "c983636174c483646f67"


def test_eip155_canonical_transaction_vector():
    """The EIP-155 spec example: pins RLP, keccak256 and ECDSA end to end."""
    to = bytes.fromhex("35" * 20)
    signing_payload = rlp_encode([9, 20 * 10**9, 21_000, to, 10**18, b"", 1, 0, 0])
    assert signing_payload.hex() == (
        "ec098504a817c800825208943535353535353535353535353535353535353535"
        "880de0b6b3a764000080018080"
    )
    digest = keccak256(signing_payload)
    assert digest.hex() == "daf5a779ae972f972197303d7b574746c7ef83eadac0f2791ad23db92e4c8e53"

    from polymarketbot.risk.fast_signer import ecdsa_sign_digest

    r, s, v = ecdsa_sign_digest(digest, bytes.fromhex("46" * 32))
    assert r == 18515461264373351373200002665853028612451056578545711640558177340181847433846
    assert s == 46948507304638947509940763649030358759909902576025900602547168820602576006531
    assert v == 27
    raw = rlp_encode([9, 20 * 10**9, 21_000, to, 10**18, b"", 37, r, s])
    assert raw.hex() == (
        "f86c098504a817c800825208943535353535353535353535353535353535353535"
        "880de0b6b3a76400008025a028ef61340bd939bc2195fe537567866003e1a15d3c"
        "71ff63e1590620aa636276a067cbe9d8997f761aecb703304b3800ccf555c9f3dc"
        "64214b297fb1966a3b6d83"
    )


# ---------------------------------------------------------------------------
# EIP-1559 signing
# ---------------------------------------------------------------------------

TX_FIELDS = {
    "chain_id": POLYGON_CHAIN_ID,
    "nonce": 7,
    "max_priority_fee_per_gas": 30 * 10**9,
    "max_fee_per_gas": 120 * 10**9,
    "gas_limit": 97_500,
    "to": CTF_ADDRESS,
    "value": 0,
    "data": "0x9e7212ad",
    "access_list": [],
}


def _recover_address(digest: bytes, y_parity: int, r: int, s: int) -> str:
    """Recover the signer from (digest, yParity, r, s) using the same curve code."""
    y_squared = (pow(r, 3, _SECP256K1_P) + 7) % _SECP256K1_P
    y = pow(y_squared, (_SECP256K1_P + 1) // 4, _SECP256K1_P)
    if y % 2 != y_parity:
        y = _SECP256K1_P - y
    z = int.from_bytes(digest, "big") % _SECP256K1_N
    point = _point_mul(
        pow(r, -1, _SECP256K1_N),
        _point_add(
            _point_mul(s, (r, y)),
            _point_mul((-z) % _SECP256K1_N, _SECP256K1_G),
        ),
    )
    assert point is not None
    public_key = point[0].to_bytes(32, "big") + point[1].to_bytes(32, "big")
    return "0x" + keccak256(public_key)[-20:].hex()


def test_eip1559_signing_hash_is_fixed_and_deterministic():
    digest = eip1559_signing_hash(**TX_FIELDS)
    assert digest.hex() == "9ceb1854ba950d5dc989b63f7e11ead6cd363399cd199c8878cff0ce4f485ac6"
    assert eip1559_signing_hash(**TX_FIELDS) == digest
    # The hash is keccak256(0x02 || rlp(fields)) for the 137/type-2 layout.
    assert digest == keccak256(b"\x02" + rlp_encode([137, 7, 30 * 10**9, 120 * 10**9,
                                                     97_500, bytes.fromhex(CTF_ADDRESS[2:]),
                                                     0, bytes.fromhex("9e7212ad"), []]))


def test_eip1559_signature_recovers_signer_and_hashes_to_tx_hash():
    assert private_key_to_address(TEST_KEY) == TEST_ADDRESS
    signed = sign_eip1559_transaction(TEST_KEY, **TX_FIELDS)
    assert signed.chain_id == 137
    assert signed.signing_hash == eip1559_signing_hash(**TX_FIELDS)
    assert signed.y_parity in (0, 1)
    assert 0 < signed.r < _SECP256K1_N
    assert 0 < signed.s <= _SECP256K1_N // 2  # EIP-2 low-s
    assert _recover_address(signed.signing_hash, signed.y_parity, signed.r, signed.s) == TEST_ADDRESS

    raw = bytes.fromhex(signed.raw_transaction.removeprefix("0x"))
    assert raw[0] == 0x02
    assert "0x" + keccak256(raw).hex() == signed.tx_hash
    # Deterministic signatures (RFC 6979), so re-signing yields identical bytes.
    assert sign_eip1559_transaction(TEST_KEY, **TX_FIELDS).raw_transaction == signed.raw_transaction


# ---------------------------------------------------------------------------
# Live-mode refusal
# ---------------------------------------------------------------------------


def test_live_merge_with_invalid_key_refuses_without_touching_rpc():
    chain = MagicMock()
    ctf = PolygonCtfClient(
        private_key="0xdeadbeef", dry_run=False, chain_client=chain
    )
    assert ctf.dry_run is False
    assert ctf.is_settlement_enabled is False

    res = ctf.merge_positions(CONDITION_ID, 10.0, pol_usd_price=0.1)

    assert res.success is False
    assert res.error
    assert res.usdc_unlocked == 0.0
    assert res.tx_hash == ""
    assert res.complete_sets_merged == 0.0
    chain.get_transaction_count.assert_not_called()
    chain.estimate_gas.assert_not_called()
    chain.send_raw_transaction.assert_not_called()


def test_live_mode_without_key_refuses_instead_of_simulating(monkeypatch):
    monkeypatch.delenv("POLY_PRIVATE_KEY", raising=False)
    chain = MagicMock()
    ctf = PolygonCtfClient(dry_run=False, chain_client=chain)
    # Safe internal mode is retained, but a live request must not fake success.
    assert ctf.dry_run is True
    assert ctf.is_settlement_enabled is False

    merge_res = ctf.merge_positions(CONDITION_ID, 10.0)
    redeem_res = ctf.redeem_positions(CONDITION_ID)

    assert merge_res.success is False
    assert "POLY_PRIVATE_KEY" in (merge_res.error or "")
    assert merge_res.usdc_unlocked == 0.0
    assert merge_res.tx_hash == ""
    assert redeem_res.success is False
    assert redeem_res.error
    chain.send_raw_transaction.assert_not_called()


def test_live_merge_proxy_wallet_is_refused_not_simulated():
    chain = MagicMock()
    ctf = PolygonCtfClient(
        private_key=TEST_KEY_HEX,
        dry_run=False,
        chain_client=chain,
        signature_type=1,
    )
    assert ctf.is_settlement_enabled is False

    merge_res = ctf.merge_positions(CONDITION_ID, 10.0, pol_usd_price=0.1)
    redeem_res = ctf.redeem_positions(CONDITION_ID, pol_usd_price=0.1)

    assert merge_res.success is False
    assert "signature_type=1" in (merge_res.error or "")
    assert merge_res.usdc_unlocked == 0.0
    assert merge_res.tx_hash == ""
    assert redeem_res.success is False
    assert redeem_res.error
    chain.send_raw_transaction.assert_not_called()


def test_settlement_enabled_only_for_live_eoa_with_valid_key():
    assert PolygonCtfClient(private_key=TEST_KEY_HEX, dry_run=False,
                            chain_client=MagicMock()).is_settlement_enabled is True
    assert PolygonCtfClient(private_key=TEST_KEY_HEX, dry_run=True,
                            chain_client=MagicMock()).is_settlement_enabled is False
    assert PolygonCtfClient(private_key=TEST_KEY_HEX, dry_run=False,
                            chain_client=MagicMock(), signature_type=2).is_settlement_enabled is False


# ---------------------------------------------------------------------------
# Dry-run behaviour is preserved
# ---------------------------------------------------------------------------


def test_dry_run_merge_and_redeem_unchanged(monkeypatch):
    monkeypatch.delenv("POLY_PRIVATE_KEY", raising=False)
    ctf = PolygonCtfClient(dry_run=True)
    monkeypatch.setattr(ctf, "get_dynamic_gas_price_gwei", lambda: 40.0)
    monkeypatch.setattr("polymarketbot.strategy.onchain.ctf.gas.get_dynamic_pol_usd_price", lambda: 0.092)
    merge_res = ctf.merge_positions(CONDITION_ID, 20.0)
    redeem_res = ctf.redeem_positions(CONDITION_ID)

    assert merge_res.success is True
    assert merge_res.is_dry_run is True
    assert merge_res.complete_sets_merged == 20.0
    assert merge_res.usdc_unlocked == 20.0
    assert merge_res.tx_hash.startswith("0xmock_tx_")
    assert merge_res.gas_fee_usd > 0.0
    assert merge_res.gas_units_used == 78_000

    assert redeem_res.success is True
    assert redeem_res.is_dry_run is True
    assert redeem_res.tx_hash.startswith("0xmock_redeem_tx_")


# ---------------------------------------------------------------------------
# Balance preconditions
# ---------------------------------------------------------------------------


def _enabled_ctf(chain: MagicMock) -> PolygonCtfClient:
    # Every live signing path reads the latest block for baseFeePerGas and the
    # EIP-1559 projection; a fake chain must answer that call realistically.
    chain._rpc.return_value = {
        "baseFeePerGas": hex(30 * 10**9),
        "gasUsed": hex(15_000_000),
        "gasLimit": hex(30_000_000),
    }
    return PolygonCtfClient(
        private_key=TEST_KEY_HEX, dry_run=False, chain_client=chain
    )


def test_live_merge_aborts_when_position_balance_is_insufficient():
    chain = MagicMock()
    chain.eth_call.return_value = hex(5 * 1_000_000)  # 5 shares held
    ctf = _enabled_ctf(chain)

    res = ctf.merge_positions(CONDITION_ID, 10.0, pol_usd_price=0.1)

    assert res.success is False
    assert "insufficient CTF balance" in (res.error or "")
    assert res.usdc_unlocked == 0.0
    assert res.tx_hash == ""
    chain.send_raw_transaction.assert_not_called()
    # Both binary outcome tokens are checked, via balanceOf(address,uint256).
    assert chain.eth_call.call_count == 2
    data = chain.eth_call.call_args.kwargs["data"]
    assert data.startswith("0x" + CTF_POSITION_BALANCE_SELECTOR)
    assert chain.eth_call.call_args.kwargs["to"] == CTF_ADDRESS.lower()


def test_live_merge_aborts_when_balance_cannot_be_read():
    chain = MagicMock()
    chain.eth_call.return_value = "0x"
    ctf = _enabled_ctf(chain)

    res = ctf.merge_positions(CONDITION_ID, 10.0, pol_usd_price=0.1)

    assert res.success is False
    assert "balance verification failed" in (res.error or "").lower()
    assert res.usdc_unlocked == 0.0
    chain.send_raw_transaction.assert_not_called()


def test_derive_position_token_id_matches_ctf_collection_math():
    ctf = PolygonCtfClient(dry_run=True, chain_client=MagicMock())
    parent = b"\x00" * 32
    condition = bytes.fromhex(CONDITION_ID[2:])
    expected = []
    for index_set in (1, 2):
        collection = keccak256(parent + condition + index_set.to_bytes(32, "big"))
        expected.append(
            "0x" + keccak256(bytes.fromhex(ctf.collateral_address[2:]) + collection).hex()
        )
    assert ctf.derive_position_token_id(CONDITION_ID, 1) == expected[0]
    assert ctf.derive_position_token_id(CONDITION_ID, 2) == expected[1]


def test_live_merge_uses_supplied_token_ids_for_balance_check():
    chain = MagicMock()
    chain.eth_call.return_value = hex(50 * 1_000_000)
    # A real Polygon block publishes baseFeePerGas; the signed fee cap is now
    # derived from it (no hard-coded Gwei clamps), so the fixture must carry it.
    chain._rpc.return_value = {
        "baseFeePerGas": hex(30 * 10**9),
        "gasUsed": hex(15_000_000),
        "gasLimit": hex(30_000_000),
    }
    ctf = _enabled_ctf(chain)
    tokens = ("0x" + "11" * 32, "0x" + "22" * 32)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ctf, "get_dynamic_gas_price_gwei", lambda: 40.0)
        mp.setattr("polymarketbot.strategy.onchain.ctf.gas.get_dynamic_pol_usd_price", lambda: 0.1)
        chain.get_transaction_count.return_value = 3
        chain.estimate_gas.return_value = 80_000
        chain.send_raw_transaction.side_effect = (
            lambda raw: "0x" + keccak256(bytes.fromhex(raw[2:])).hex()
        )
        chain.get_transaction_receipt.return_value = {
            "status": "0x1",
            "gasUsed": hex(60_000),
            "effectiveGasPrice": hex(100 * 10**9),
        }
        res = ctf.merge_positions(CONDITION_ID, 20.0, token_ids=tokens)

    assert res.success is True
    checked = [call.kwargs["data"][-64:] for call in chain.eth_call.call_args_list]
    assert checked == [tokens[0][2:], tokens[1][2:]]
    assert res.usdc_unlocked == 20.0


# ---------------------------------------------------------------------------
# Live broadcast happy path / receipt handling
# ---------------------------------------------------------------------------


def test_live_merge_broadcasts_signed_tx_and_uses_receipt_gas():
    chain = MagicMock()
    chain.eth_call.return_value = hex(50 * 1_000_000)  # 50 shares of each outcome
    chain.get_transaction_count.return_value = 9
    chain.estimate_gas.return_value = 80_000
    chain.send_raw_transaction.side_effect = (
        lambda raw: "0x" + keccak256(bytes.fromhex(raw[2:])).hex()
    )
    chain.get_transaction_receipt.return_value = {
        "status": "0x1",
        "gasUsed": hex(70_000),
        "effectiveGasPrice": hex(100 * 10**9),
    }
    ctf = _enabled_ctf(chain)
    captured: dict = {}
    real_sign = sign_eip1559_transaction

    def spy_sign(key, **kwargs):
        captured.update(kwargs)
        return real_sign(key, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ctf, "get_dynamic_gas_price_gwei", lambda: 40.0)
        mp.setattr("polymarketbot.strategy.onchain.ctf.gas.get_dynamic_pol_usd_price", lambda: 0.10)
        mp.setattr("polymarketbot.strategy.onchain.ctf.rlp.sign_eip1559_transaction", spy_sign)
        res = ctf.merge_positions(CONDITION_ID, 25.0, pol_usd_price=0.10)

    assert res.success is True
    assert res.is_dry_run is False
    assert res.complete_sets_merged == 25.0
    assert res.usdc_unlocked == 25.0
    assert res.gas_units_used == 70_000
    # 70,000 gas * 100 Gwei = 0.007 POL; at $0.10/POL -> $0.0007.
    assert res.gas_fee_usd == pytest.approx(0.0007)

    raw = chain.send_raw_transaction.call_args.args[0]
    assert raw.startswith("0x02")  # type-2 envelope
    assert res.tx_hash == "0x" + keccak256(bytes.fromhex(raw[2:])).hex()
    assert chain.get_transaction_count.call_args.args == (TEST_ADDRESS, "pending")
    # eth_estimateGas result carries a 25% buffer into the signed gas limit.
    assert captured["gas_limit"] == 100_000
    assert captured["chain_id"] == POLYGON_CHAIN_ID
    assert captured["to"] == CTF_ADDRESS.lower()
    assert chain.estimate_gas.call_args.args[0]["to"] == CTF_ADDRESS.lower()


def test_live_merge_revert_returns_failure_without_crediting_usdc():
    chain = MagicMock()
    chain.eth_call.return_value = hex(50 * 1_000_000)
    chain.get_transaction_count.return_value = 4
    chain.estimate_gas.return_value = 80_000
    chain.send_raw_transaction.return_value = "0x" + "ab" * 32
    chain.get_transaction_receipt.return_value = {
        "status": "0x0",
        "gasUsed": hex(50_000),
        "effectiveGasPrice": hex(100 * 10**9),
    }
    ctf = _enabled_ctf(chain)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ctf, "get_dynamic_gas_price_gwei", lambda: 40.0)
        mp.setattr("polymarketbot.strategy.onchain.ctf.gas.get_dynamic_pol_usd_price", lambda: 0.10)
        res = ctf.merge_positions(CONDITION_ID, 25.0)

    assert res.success is False
    assert res.status == "failed"
    assert "reverted" in (res.error or "")
    assert res.usdc_unlocked == 0.0
    # The stored hash is the identity of the settlement, not the node's echo:
    # a revert is looked up by the hash of the bytes this process signed.
    sent_raw = chain.send_raw_transaction.call_args.args[0]
    assert res.tx_hash == "0x" + keccak256(bytes.fromhex(sent_raw[2:])).hex()
    assert chain.send_raw_transaction.call_count == 1  # a revert is never re-sent
    # Gas was really spent even though the merge did not happen.
    assert res.gas_fee_usd == pytest.approx(0.0005)
    assert res.gas_units_used == 50_000
    row = ctf.settlement_store.get(res.tx_hash)
    assert row["status"] == "REVERTED"
    assert row["raw_tx"] == sent_raw


def test_live_merge_receipt_timeout_is_pending_and_rebroadcasts_identical_bytes():
    chain = MagicMock()
    chain.eth_call.return_value = hex(50 * 1_000_000)
    chain.get_transaction_count.return_value = 4
    chain.estimate_gas.return_value = 80_000
    chain.send_raw_transaction.return_value = "0x" + "cd" * 32
    chain.get_transaction_receipt.return_value = None
    ctf = _enabled_ctf(chain)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ctf, "get_dynamic_gas_price_gwei", lambda: 40.0)
        mp.setattr(ctf, "receipt_timeout_s", 0.0)
        mp.setattr(ctf, "receipt_poll_interval_s", 0.0)
        mp.setattr(ctf, "receipt_rebroadcast_wait_s", 0.0)
        res = ctf.merge_positions(CONDITION_ID, 25.0)

    assert res.success is False
    assert res.status == "pending"  # not a failure: the tx may still be mined
    assert "not mined" in (res.error or "")
    assert res.usdc_unlocked == 0.0
    sent = [call.args[0] for call in chain.send_raw_transaction.call_args_list]
    assert len(sent) == 2
    assert sent[0] == sent[1]  # the retry is the same signed bytes, never a new tx
    assert res.tx_hash == "0x" + keccak256(bytes.fromhex(sent[0][2:])).hex()
    assert ctf.settlement_store.get(res.tx_hash)["status"] == "PENDING"


# ---------------------------------------------------------------------------
# ChainClient RPC surface
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, result):
        self.status_code = 200
        self._result = result

    def json(self):
        return {"jsonrpc": "2.0", "id": 1, "result": self._result}


class _FakeSession:
    def __init__(self, results):
        self.headers = {}
        self.results = list(results)
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append(json)
        return _FakeResponse(self.results.pop(0))


def _chain_with(results):
    session = _FakeSession(results)
    client = ChainClient(url="http://stub.invalid", session=session, min_interval_s=0.0)
    return client, session


def test_chain_client_settlement_rpc_methods():
    receipt = {"status": "0x1", "gasUsed": "0x5208"}
    chain, session = _chain_with(["0x1a", "0x5208", "0x" + "ef" * 32, receipt, None])

    assert chain.get_transaction_count("0xAbC") == 26
    assert session.calls[0] == {
        "jsonrpc": "2.0", "id": 1,
        "method": "eth_getTransactionCount",
        "params": ["0xAbC", "pending"],
    }

    tx = {"from": TEST_ADDRESS, "to": CTF_ADDRESS, "data": "0x00", "value": "0x0"}
    assert chain.estimate_gas(tx) == 21_000
    assert session.calls[1]["method"] == "eth_estimateGas"
    assert session.calls[1]["params"] == [tx]

    assert chain.send_raw_transaction("02f8") == "0x" + "ef" * 32
    assert session.calls[2]["method"] == "eth_sendRawTransaction"
    assert session.calls[2]["params"] == ["0x02f8"]

    assert chain.get_transaction_receipt("0x" + "ef" * 32) == receipt
    assert chain.get_transaction_receipt("0x" + "ef" * 32) is None
    assert session.calls[3]["method"] == "eth_getTransactionReceipt"
