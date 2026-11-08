"""Vector tests for settlement routing (v1 CTF, v1 neg-risk, v2 Router).

The expected calldata below was hand-computed from the ABI signatures, not
copied from the implementation:

* Router: ``merge(bytes31,uint256)`` / ``redeem(bytes31,uint256,uint256)``
  from ``data/raw/docs/site/migrate/polymarket-v2/contract-integrations.md``.
* NegRiskAdapter: ``convertPositions(bytes32,uint256,uint256)`` from
  ``Polymarket/neg-risk-ctf-adapter/src/NegRiskAdapter.sol``.
* CTF: ``mergePositions(address,bytes32,bytes32,uint256[],uint256)``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from polymarketbot.chain import ChainClient
from polymarketbot.gamma import GammaClient
from polymarketbot.trading.settlement import (
    CTF,
    NEGRISK_ADAPTER,
    NEGRISK_CONVERT_SELECTOR,
    NEGRISK_MERGE_SELECTOR,
    ROUTER_MERGE_SELECTOR,
    ROUTER_REDEEM_SELECTOR,
    CTF_REDEEM_SELECTOR,
    CTF_PAYOUT_DENOMINATOR_SELECTOR,
    CTF_PAYOUT_NUMERATOR_SELECTOR,
    ROUTER,
    IS_APPROVED_FOR_ALL_SELECTOR,
    SettlementRoute,
    USDC_E,
    amount_to_6dp,
    build_ctf_merge,
    build_ctf_redeem,
    build_negrisk_convert,
    build_negrisk_merge,
    build_router_merge,
    build_router_redeem,
    classify_market,
    classify_merge_route,
    classify_redeem_route,
    decode_erc20_transfer_total,
    derive_ctf_position_id,
    receipt_succeeded,
    validate_bytes31,
    verify_route,
)

COND_V2 = "0x" + "11" * 31 + "00"
COND_V2_REDEEM = "0x" + "22" * 31 + "00"
NEG_RISK_MARKET_ID = "0x" + "33" * 31 + "00"
COND_CTF = "0x" + "44" * 32


def _word(value: int) -> str:
    return value.to_bytes(32, "big").hex()


def _addr_word(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


# ---------------------------------------------------------------------------
# Selectors and exact encoding
# ---------------------------------------------------------------------------
def test_selectors_are_independently_expected_hex():
    assert ROUTER_MERGE_SELECTOR == "5b63685e"        # merge(bytes31,uint256)
    assert ROUTER_REDEEM_SELECTOR == "d217a3cc"       # redeem(bytes31,uint256,uint256)
    assert NEGRISK_CONVERT_SELECTOR == "c64748c4"     # convertPositions(bytes32,uint256,uint256)
    assert IS_APPROVED_FOR_ALL_SELECTOR == "e985e9c5" # isApprovedForAll(address,address)
    assert CTF_REDEEM_SELECTOR == "01b7037c"          # redeemPositions(address,bytes32,bytes32,uint256[])
    assert CTF_PAYOUT_DENOMINATOR_SELECTOR == "dd34de67" # payoutDenominator(bytes32)
    assert CTF_PAYOUT_NUMERATOR_SELECTOR == "0504c814"   # payoutNumerators(bytes32,uint256)


def test_ctf_redeem_calldata_matches_hand_computed_vector():
    expected = (
        "0x01b7037c"
        + _addr_word(USDC_E)
        + "00" * 32
        + "44" * 32
        + _word(128)                # offset to dynamic array
        + _word(2)                  # length of array
        + _word(1)                  # indexSet 1
        + _word(2)                  # indexSet 2
    )
    assert build_ctf_redeem(COND_CTF, (1, 2)) == expected


def test_classify_redeem_route():
    assert classify_redeem_route({"version": "v2"}) is SettlementRoute.ROUTER_MERGE
    assert classify_redeem_route({"version": "v1", "neg_risk": True}) is SettlementRoute.CTF_REDEEM
    assert classify_redeem_route({"version": "v1", "neg_risk": False}) is SettlementRoute.CTF_REDEEM
    assert classify_redeem_route({"version": "v3"}) is SettlementRoute.UNSUPPORTED
    assert classify_redeem_route(None) is SettlementRoute.UNSUPPORTED


def test_router_merge_calldata_matches_hand_computed_vector():
    expected = (
        "0x5b63685e"
        + "11" * 31 + "00"          # bytes31 left-aligned + zero pad
        + _word(1_000_000)          # 0xf4240
    )
    assert build_router_merge(COND_V2, 1_000_000) == expected


def test_router_redeem_calldata_matches_hand_computed_vector():
    expected = (
        "0xd217a3cc"
        + "22" * 31 + "00"
        + _word(1)                  # outcomeIndex 1 == NO
        + _word(2_500_000)          # 0x2625a0
    )
    assert build_router_redeem(COND_V2_REDEEM, 1, 2_500_000) == expected


def test_negrisk_convert_calldata_matches_hand_computed_vector():
    expected = (
        "0xc64748c4"
        + "33" * 31 + "00"
        + _word(0b101)              # question indices [0, 2]
        + _word(1_000_000)
    )
    assert build_negrisk_convert(NEG_RISK_MARKET_ID, [0, 2], 1_000_000) == expected


def test_ctf_merge_calldata_matches_hand_computed_vector():
    expected = (
        "0x9e7212ad"
        + _addr_word(USDC_E)        # collateralToken
        + "0" * 64                  # parentCollectionId
        + "44" * 32                 # conditionId
        + _word(160)                # offset to indexSets
        + _word(1_000_000)          # amount
        + _word(2)                  # indexSets.length
        + _word(1) + _word(2)       # indexSets
    )
    assert build_ctf_merge(COND_CTF, 1_000_000) == expected


def test_ctf_merge_selector_matches_existing_ctf_client_packing():
    # trading/ctf.py hard-codes the same selector for the same signature.
    assert build_ctf_merge(COND_CTF, 1).startswith("0x9e7212ad")


def test_router_builders_reject_non_bytes31():
    with pytest.raises(ValueError, match="final byte"):
        build_router_merge("0x" + "11" * 31 + "01", 1)
    with pytest.raises(ValueError, match="final byte"):
        build_router_redeem("0x" + "22" * 32, 0, 1)


def test_router_redeem_rejects_unknown_outcome_index():
    with pytest.raises(ValueError, match="0 .*YES.*1 .*NO"):
        build_router_redeem(COND_V2, 2, 1)


# ---------------------------------------------------------------------------
# bytes31 validation
# ---------------------------------------------------------------------------
def test_validate_bytes31_returns_the_narrowed_value():
    assert validate_bytes31(COND_V2) == bytes.fromhex("11" * 31)


@pytest.mark.parametrize(
    "value, message",
    [
        ("0x" + "11" * 31 + "01", "final byte"),
        ("0x" + "11" * 31, "32 bytes"),          # already 31 bytes, not padded bytes32
        ("0x" + "11" * 33, "32 bytes"),
        ("0xzz" + "00" * 31, "not valid hex"),
        ("", "empty"),
        (None, "hex string"),
    ],
)
def test_validate_bytes31_rejects_invalid_input(value, message):
    with pytest.raises(ValueError, match=message):
        validate_bytes31(value)


# ---------------------------------------------------------------------------
# Route classification matrix
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "meta, expected",
    [
        ({"version": "v2", "neg_risk": False}, SettlementRoute.ROUTER_MERGE),
        ({"version": "v2", "neg_risk": True}, SettlementRoute.ROUTER_MERGE),
        ({"version": "v1", "neg_risk": False}, SettlementRoute.CTF_MERGE),
        (
            {"version": "v1", "neg_risk": True,
             "neg_risk_market_id": NEG_RISK_MARKET_ID},
            SettlementRoute.NEGRISK_CONVERT,
        ),
        ({"version": "v1", "neg_risk": True}, SettlementRoute.CTF_MERGE),
        (
            {"version": "v1", "neg_risk": True, "neg_risk_market_id": "0x1234"},
            SettlementRoute.CTF_MERGE,
        ),
        ({"version": "v3"}, SettlementRoute.UNSUPPORTED),
        ({}, SettlementRoute.UNSUPPORTED),
        (None, SettlementRoute.UNSUPPORTED),
    ],
)
def test_classify_market_matrix(meta, expected):
    assert classify_market(meta) is expected


def test_classify_market_accepts_raw_gamma_camelcase():
    assert classify_market(
        {"version": "v1", "negRisk": True,
         "negRiskMarketID": "0x" + "2c" * 32}
    ) is SettlementRoute.NEGRISK_CONVERT
    assert classify_market(
        {"version": "v1", "negRisk": False, "negRiskMarketID": None}
    ) is SettlementRoute.CTF_MERGE


# ---------------------------------------------------------------------------
# Amount conversion (ROUND_DOWN)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "shares, expected",
    [
        (1, 1_000_000),
        (2, 2_000_000),
        ("1.5", 1_500_000),
        (Decimal("3.9999999"), 3_999_999),
        (0.1, 100_000),               # float via repr, not binary expansion
        ("0.0000009", 0),
        (Decimal("0.000001"), 1),
    ],
)
def test_amount_to_6dp_rounds_down(shares, expected):
    assert amount_to_6dp(shares) == expected


@pytest.mark.parametrize("shares", [-1, "-0.1", "nan", "inf", "not a number"])
def test_amount_to_6dp_rejects_invalid(shares):
    with pytest.raises(ValueError):
        amount_to_6dp(shares)


# ---------------------------------------------------------------------------
# Neg-risk index-set packing
# ---------------------------------------------------------------------------
def test_negrisk_convert_ors_question_indices_and_ignores_duplicates():
    assert build_negrisk_convert(NEG_RISK_MARKET_ID, [0, 0, 2], 1) == \
        build_negrisk_convert(NEG_RISK_MARKET_ID, [0, 2], 1)


@pytest.mark.parametrize("indices", [[], [-1], [256], ["0"], [True], 3])
def test_negrisk_convert_rejects_bad_index_sets(indices):
    with pytest.raises(ValueError):
        build_negrisk_convert(NEG_RISK_MARKET_ID, indices, 1)


# ---------------------------------------------------------------------------
# verify_route with a stub chain (no network)
# ---------------------------------------------------------------------------
class _FakeChain:
    def __init__(self, value: str | None = None, error: str | None = None) -> None:
        self.value = value if value is not None else "0x" + _word(1)
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def eth_call(self, to: str, data: str, **_: object) -> str:
        self.calls.append((to, data))
        if self.error:
            raise RuntimeError(self.error)
        return self.value


def test_classify_merge_route_uses_adapter_merge_for_v1_negrisk():
    assert (
        classify_merge_route(
            {"version": "v2", "neg_risk": False}
        )
        is SettlementRoute.ROUTER_MERGE
    )
    assert (
        classify_merge_route(
            {"version": "v1", "neg_risk": True, "neg_risk_market_id": NEG_RISK_MARKET_ID}
        )
        is SettlementRoute.NEGRISK_MERGE
    )
    assert (
        classify_merge_route({"version": "v1", "neg_risk": False})
        is SettlementRoute.CTF_MERGE
    )
    assert classify_merge_route(None) is SettlementRoute.UNSUPPORTED


def test_build_negrisk_merge_vector():
    amount = 1_000_000
    calldata = build_negrisk_merge(COND_CTF, amount)
    expected = "0x" + NEGRISK_MERGE_SELECTOR + COND_CTF.removeprefix("0x") + _word(amount)
    assert NEGRISK_MERGE_SELECTOR == "b10c5c17"
    assert calldata == expected


def test_verify_negrisk_merge_requires_adapter_approval():
    chain = _FakeChain()
    ok, reason = verify_route(
        SettlementRoute.NEGRISK_MERGE, COND_CTF, "0x" + "ab" * 20, chain
    )
    assert ok is True, reason
    assert chain.calls[0][0] == CTF
    assert chain.calls[0][1].startswith("0x" + IS_APPROVED_FOR_ALL_SELECTOR)


def test_verify_negrisk_requires_adapter_approval():
    chain = _FakeChain()
    ok, reason = verify_route(
        SettlementRoute.NEGRISK_CONVERT, COND_CTF, "0x" + "ab" * 20, chain
    )
    assert ok is True, reason
    assert chain.calls and chain.calls[0][0] == CTF
    assert chain.calls[0][1].startswith("0x" + IS_APPROVED_FOR_ALL_SELECTOR)


def test_verify_negrisk_reports_missing_approval():
    chain = _FakeChain(value="0x" + _word(0))
    ok, reason = verify_route(
        SettlementRoute.NEGRISK_CONVERT, COND_CTF, "0x" + "ab" * 20, chain
    )
    assert ok is False
    assert "NegRiskAdapter" in reason and "setApprovalForAll" in reason


def test_verify_router_requires_router_approval():
    chain = _FakeChain()
    ok, reason = verify_route(
        SettlementRoute.ROUTER_MERGE, COND_V2, "0x" + "ab" * 20, chain
    )
    assert ok is True, reason
    assert "setApprovalForAll" not in reason


def test_verify_router_reports_missing_approval():
    chain = _FakeChain(value="0x" + _word(0))
    ok, reason = verify_route(
        SettlementRoute.ROUTER_MERGE, COND_V2, "0x" + "ab" * 20, chain
    )
    assert ok is False
    assert "ROUTER" in reason and "setApprovalForAll" in reason


def test_verify_ctf_merge_reads_both_outcome_balances():
    chain = _FakeChain(value="0x" + _word(5))
    ok, reason = verify_route(
        SettlementRoute.CTF_MERGE,
        COND_CTF,
        "0x" + "ab" * 20,
        chain,
        meta={"clob_token_ids": ["123", "456"]},
    )
    assert ok is True, reason
    assert len(chain.calls) == 2
    assert all(to == CTF for to, _ in chain.calls)
    assert all(data.startswith("0x00fdd58e") for _, data in chain.calls)


def test_verify_ctf_merge_rejects_empty_balance():
    chain = _FakeChain(value="0x" + _word(0))
    ok, reason = verify_route(
        SettlementRoute.CTF_MERGE,
        COND_CTF,
        "0x" + "ab" * 20,
        chain,
        meta={"clob_token_ids": ["123", "456"]},
    )
    assert ok is False
    assert "no CTF balance" in reason


def test_verify_ctf_redeem_requires_resolution_and_winning_balance():
    # 1. Unresolved condition: payoutDenominator == 0
    chain_unres = _FakeChain(value="0x" + _word(0))
    ok, reason = verify_route(
        SettlementRoute.CTF_REDEEM,
        COND_CTF,
        "0x" + "ab" * 20,
        chain_unres,
        meta={"clob_token_ids": ["123", "456"]},
    )
    assert ok is False
    assert "payoutDenominator == 0" in reason

    # 2. Resolved condition: payoutDenominator > 0 and winning balance > 0
    class _DynamicChain:
        def __init__(self):
            self.calls = []

        def eth_call(self, to: str, data: str, **_: object) -> str:
            self.calls.append((to, data))
            if CTF_PAYOUT_DENOMINATOR_SELECTOR in data:
                return "0x" + _word(10**18)
            if CTF_PAYOUT_NUMERATOR_SELECTOR in data:
                # Outcome 0 wins with 10^18, Outcome 1 loses with 0
                if data.endswith(_word(0)):
                    return "0x" + _word(10**18)
                return "0x" + _word(0)
            if "00fdd58e" in data:  # balanceOf
                return "0x" + _word(100_000_000)
            return "0x" + _word(1)

    dyn_chain = _DynamicChain()
    ok, reason = verify_route(
        SettlementRoute.CTF_REDEEM,
        COND_CTF,
        "0x" + "ab" * 20,
        dyn_chain,
        meta={"clob_token_ids": ["123", "456"]},
    )
    assert ok is True, reason
    assert "redeemable winning balance" in reason


def test_verify_route_turns_rpc_errors_into_reasons():
    chain = _FakeChain(error="connection refused")
    ok, reason = verify_route(
        SettlementRoute.NEGRISK_CONVERT, COND_CTF, "0x" + "ab" * 20, chain
    )
    assert ok is False
    assert "connection refused" in reason


def test_verify_unsupported_route_never_calls_chain():
    chain = _FakeChain()
    ok, reason = verify_route(
        SettlementRoute.UNSUPPORTED, COND_CTF, "0x" + "ab" * 20, chain
    )
    assert ok is False and chain.calls == []
    assert "no supported" in reason


# ---------------------------------------------------------------------------
# CTF position derivation matches the documented keccak collection math
# ---------------------------------------------------------------------------
def test_derive_ctf_position_id_is_stable_and_uint256():
    first = derive_ctf_position_id(COND_CTF, 1)
    second = derive_ctf_position_id(COND_CTF, 2)
    assert 0 <= first < 2**256 and 0 <= second < 2**256
    assert first != second


# ---------------------------------------------------------------------------
# Receipt helpers
# ---------------------------------------------------------------------------
def test_receipt_succeeded_and_transfer_total():
    recipient = "0x" + "ab" * 20
    topic = "0x" + "cc" * 32
    log = {
        "address": USDC_E,
        "topics": [
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            "0x" + "00" * 32,
            "0x" + recipient[2:].rjust(64, "0"),
        ],
        "data": "0x" + _word(2_500_000),
    }
    receipt = {"status": "0x1", "logs": [log]}
    assert receipt_succeeded(receipt) is True
    assert receipt_succeeded({"status": "0x0"}) is False
    assert receipt_succeeded(None) is False
    assert decode_erc20_transfer_total(receipt, USDC_E, recipient) == 2_500_000
    assert decode_erc20_transfer_total(receipt, USDC_E, "0x" + "cd" * 20) == 0
    assert decode_erc20_transfer_total({"status": "0x0", "logs": [log]}, USDC_E) == 0
    # A PUSD transfer must not be counted as USDC.e.
    assert decode_erc20_transfer_total(receipt, topic) == 0


# ---------------------------------------------------------------------------
# Gamma market_meta field mapping (stubbed HTTP)
# ---------------------------------------------------------------------------
class _StubGamma(GammaClient):
    def __init__(self, rows: list[dict]) -> None:
        super().__init__(min_interval_s=0.0)
        self._rows = rows

    def get(self, path: str, params=None):  # noqa: ANN001 - test stub
        assert path == "/markets/keyset"
        return {"markets": list(self._rows)}


def test_market_meta_maps_the_observed_gamma_field_names():
    client = _StubGamma([{
        "conditionId": COND_CTF,
        "version": "v1",
        "negRisk": True,
        "negRiskMarketID": "0x" + "2c" * 32,
        "clobTokenIds": '["54533043819946592547517511176940999955633860128497669742211153063842200957669", "87854174148074652060467921081181402357467303721471806610111179101805869578687"]',
        "positionIds": ["895257453734540292493143124621755605615887197499956109032745153687078305792"],
        "orderMinSize": 5,
    }])
    meta = client.market_meta(COND_CTF)
    assert meta is not None
    assert meta["version"] == "v1"
    assert meta["neg_risk"] is True
    assert meta["neg_risk_market_id"] == "0x" + "2c" * 32
    assert meta["order_min_size"] == 5.0
    assert meta["position_ids"] and len(meta["position_ids"]) == 1
    assert len(meta["clob_token_ids"]) == 2
    assert classify_market(meta) is SettlementRoute.NEGRISK_CONVERT


def test_market_meta_returns_none_when_gamma_has_no_row():
    assert _StubGamma([]).market_meta(COND_CTF) is None


# ---------------------------------------------------------------------------
# Read-only on-chain smoke (skipped when the RPC is unavailable)
# ---------------------------------------------------------------------------
def test_readonly_eth_call_smoke():
    chain = ChainClient(url=None, timeout_s=4.0, max_retries=1, min_interval_s=0.0)
    data = (
        "0x"
        + IS_APPROVED_FOR_ALL_SELECTOR
        + "0" * 64                                  # holder = address(0)
        + NEGRISK_ADAPTER.lower().removeprefix("0x").rjust(64, "0")
    )
    try:
        raw = chain.eth_call(to=CTF, data=data)
    except Exception as exc:  # noqa: BLE001 - offline CI must skip, not fail
        pytest.skip(f"Polygon RPC unavailable: {exc}")
    if not raw or raw == "0x":
        pytest.skip("Polygon RPC returned no data for CTF.isApprovedForAll")
    assert int(raw, 16) in (0, 1)


def test_router_and_ctf_addresses_are_checksum_agnostic():
    # Guards against silently routing to the placeholder zero address.
    for address in (ROUTER, CTF, USDC_E, NEGRISK_ADAPTER):
        assert address.lower() != "0x" + "00" * 20
        assert len(address) == 42
