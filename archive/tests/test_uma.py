"""UMA/adapter log decoding: ABI topics, payloads, and the address mismatch.

The event signatures are asserted against the topic0 values observed on
Polygon mainnet (see ``CHAIN_TOPIC0`` below), so a typo in the ABI table fails
a test instead of silently decoding nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from polymarketbot.data.uma import (
    CONTRACT_ADAPTER_V1,
    CONTRACT_ADAPTER_V2,
    CONTRACT_OO_V2,
    SPEC_BY_CONTRACT_TOPIC,
    UMA_ADAPTER_V1,
    UMA_ADAPTER_V2,
    UMA_OO_V2,
    ancillary_title,
    decode_log,
    event_topic0,
    hex_int,
    parse_log,
)


# --------------------------------------------------------------------------- #
# Minimal ABI encoder (no eth_abi dependency)
# --------------------------------------------------------------------------- #
def _word(value: int) -> bytes:
    return (value % (1 << 256)).to_bytes(32, "big")


def _addr(value: str) -> bytes:
    return bytes.fromhex(value[2:].rjust(40, "0"))


def _encode_static(type_name: str, value: Any) -> bytes:
    if type_name == "address":
        return b"\x00" * 12 + _addr(value)
    if type_name == "bytes32":
        return bytes.fromhex(value[2:])
    if type_name == "uint256":
        return _word(int(value))
    if type_name == "int256":
        return _word(int(value))
    if type_name == "bool":
        return _word(1 if value else 0)
    raise ValueError(type_name)


def _encode_dynamic(type_name: str, value: Any) -> bytes:
    if type_name == "bytes":
        raw = bytes(value)
        padded = raw + b"\x00" * ((32 - len(raw) % 32) % 32)
        return _word(len(raw)) + padded
    if type_name == "uint256[]":
        items = list(value)
        return _word(len(items)) + b"".join(_word(int(item)) for item in items)
    raise ValueError(type_name)


def _infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (bytes, bytearray)):
        return "bytes"
    if isinstance(value, (list, tuple)):
        return "uint256[]"
    if isinstance(value, int):
        return "int256" if value < 0 else "uint256"
    if isinstance(value, str) and value.startswith("0x"):
        if len(value) == 42:
            return "address"
        if len(value) == 66:
            return "bytes32"
    raise TypeError(f"cannot infer an ABI type for {value!r}")


def encode_data(values: Sequence[Any]) -> str:
    types = [_infer_type(value) for value in values]
    head: list[bytes] = []
    tail = b""
    offset = 32 * len(types)
    for type_name, value in zip(types, values):
        if type_name in {"bytes", "uint256[]"}:
            encoded = _encode_dynamic(type_name, value)
            head.append(_word(offset))
            tail += encoded
            offset += len(encoded)
        else:
            head.append(_encode_static(type_name, value))
    return "0x" + b"".join(head).hex() + tail.hex()


def build_log(
    address: str,
    signature: str,
    indexed: Sequence[tuple[str, Any]] = (),
    data: Sequence[tuple[str, Any]] = (),
) -> dict[str, Any]:
    topics = [event_topic0(signature)]
    topics.extend("0x" + _encode_static(_infer_type(v), v).hex() for _t, v in indexed)
    topics.extend([None] * (4 - len(topics)))
    return {
        "address": address,
        "topic0": topics[0],
        "topic1": topics[1],
        "topic2": topics[2],
        "topic3": topics[3],
        "data": encode_data([v for _t, v in data]),
        "block_number": 96_710_576,
        "block_hash": "0xblock",
        "tx_hash": "0xtx",
        "tx_index": 7,
        "log_index": 31,
        "recv_ts_ns": 1_791_000_000_000_000_000,
        "removed": False,
    }


IDENTIFIER = "0x5945535f4f525f4e4f5f51554552590000000000000000000000000000000000"
CURRENCY = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
PROPOSER = "0xd9aa1a620f692eedee28f267c9d448cf86f3b5d3"
ADAPTER = "0x2f5e3684cb1f318ec51b00edba38d79ac2c0aa9d"
ANCILLARY = b"q: title: Will the Democratic Party win the WI-01 House seat?"


# --------------------------------------------------------------------------- #
# Topic0 table
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "signature,expected",
    [
        # OOv2 (matches the live ProposePrice log fetched at block 96,710,576).
        (
            "RequestPrice(address,bytes32,uint256,bytes,address,uint256,uint256)",
            "0xf1679315ff325c257a944e0ca1bfe7b26616039e9511f9610d4ba3eca851027b",
        ),
        (
            "ProposePrice(address,address,bytes32,uint256,bytes,int256,uint256,address)",
            "0x6e51dd00371aabffa82cd401592f76ed51e98a9ea4b58751c70463a2c78b5ca1",
        ),
        (
            "DisputePrice(address,address,address,bytes32,uint256,bytes,int256)",
            "0x5165909c3d1c01c5d1e121ac6f6d01dda1ba24bc9e1f975b5a375339c15be7f3",
        ),
        (
            "Settle(address,address,address,bytes32,uint256,bytes,int256,uint256)",
            "0x3f384afb4bd9f0aef0298c80399950011420eb33b0e1a750b20966270247b9a0",
        ),
        # Adapter v1 (0xCB18..., PolygonScan: UmaConditionalTokensBinaryAdapter).
        (
            "QuestionInitialized(bytes32,bytes,uint256,address,uint256,uint256,bool)",
            "0x53af6c2e40505bb55a47d917203a12c8f063b6c505c11094d98ac8f772e8a031",
        ),
        (
            "ResolutionDataRequested(address,uint256,bytes32,bytes32,bytes,address,uint256,uint256,bool)",
            "0x6010c5373137e7c7c584b6bf87be56bf4a685890351251ac0a8d74e599455929",
        ),
        # Adapter v2 (0x6A9D...).
        (
            "QuestionInitialized(bytes32,uint256,address,bytes,address,uint256,uint256)",
            "0xeee0897acd6893adcaf2ba5158191b3601098ab6bece35c5d57874340b64c5b7",
        ),
        (
            "QuestionResolved(bytes32,int256,uint256[])",
            "0x566c3fbdd12dd86bb341787f6d531f79fd7ad4ce7e3ae2d15ac0ca1b601af9df",
        ),
    ],
)
def test_topic0_matches_mainnet(signature: str, expected: str) -> None:
    assert event_topic0(signature) == expected


# --------------------------------------------------------------------------- #
# Optimistic Oracle events
# --------------------------------------------------------------------------- #
def test_request_price_maps_request_fields() -> None:
    row = build_log(
        UMA_OO_V2,
        "RequestPrice(address,bytes32,uint256,bytes,address,uint256,uint256)",
        indexed=[("requester", ADAPTER)],
        data=[
            ("identifier", IDENTIFIER),
            ("timestamp", 1_765_910_303),
            ("ancillaryData", ANCILLARY),
            ("currency", CURRENCY),
            ("reward", 0),
            ("finalFee", 5 * 10**18),
        ],
    )
    event = decode_log(row, source_part="polygon_logs/date=2026-11-04/hour=00/part.parquet")
    assert event is not None
    assert event["event"] == "RequestPrice"
    assert event["contract"] == CONTRACT_OO_V2
    assert event["requester"] == ADAPTER
    assert event["identifier"] == IDENTIFIER
    assert event["identifier_text"] == "YES_OR_NO_QUERY"
    assert event["request_timestamp"] == 1_765_910_303
    assert event["ancillary_text"] == ANCILLARY.decode()
    assert event["final_fee"] == str(5 * 10**18)
    assert event["expiry"] is None
    assert event["disputed"] is None
    assert event["question_id"] is not None
    assert event["source_part"].endswith("part.parquet")


def test_propose_price_real_shape() -> None:
    """Values taken from the on-chain ProposePrice at block 96,710,576."""
    row = build_log(
        UMA_OO_V2,
        "ProposePrice(address,address,bytes32,uint256,bytes,int256,uint256,address)",
        indexed=[("requester", ADAPTER), ("proposer", PROPOSER)],
        data=[
            ("identifier", IDENTIFIER),
            ("timestamp", 1_765_910_303),
            ("ancillaryData", ANCILLARY),
            ("proposedPrice", 10**18),
            ("expirationTimestamp", 1_793_778_520),
            ("currency", CURRENCY),
        ],
    )
    event = decode_log(row)
    assert event is not None
    assert event["event"] == "ProposePrice"
    assert event["requester"] == ADAPTER
    assert event["proposer"] == PROPOSER
    assert event["proposed_price"] == str(10**18)
    assert event["expiry"] == 1_793_778_520
    assert event["currency"] == CURRENCY
    assert event["disputed"] is False


def test_propose_price_negative_int256() -> None:
    row = build_log(
        UMA_OO_V2,
        "ProposePrice(address,address,bytes32,uint256,bytes,int256,uint256,address)",
        indexed=[("requester", ADAPTER), ("proposer", PROPOSER)],
        data=[
            ("identifier", IDENTIFIER),
            ("timestamp", 1),
            ("ancillaryData", b"q: title: x"),
            ("proposedPrice", -(5 * 10**17)),
            ("expirationTimestamp", 2),
            ("currency", CURRENCY),
        ],
    )
    event = decode_log(row)
    assert event is not None
    assert event["proposed_price"] == str(-(5 * 10**17))


def test_dispute_price_sets_disputed() -> None:
    disputer = "0x00000000000000000000000000000000000000aa"
    row = build_log(
        UMA_OO_V2,
        "DisputePrice(address,address,address,bytes32,uint256,bytes,int256)",
        indexed=[("requester", ADAPTER), ("proposer", PROPOSER), ("disputer", disputer)],
        data=[
            ("identifier", IDENTIFIER),
            ("timestamp", 1),
            ("ancillaryData", ANCILLARY),
            ("proposedPrice", 10**18),
        ],
    )
    event = decode_log(row)
    assert event is not None
    assert event["disputer"] == disputer
    assert event["disputed"] is True
    assert event["proposed_price"] == str(10**18)


def test_settle_disputed_flag_follows_disputer() -> None:
    def settle(disputer: str) -> dict[str, Any] | None:
        row = build_log(
            UMA_OO_V2,
            "Settle(address,address,address,bytes32,uint256,bytes,int256,uint256)",
            indexed=[("requester", ADAPTER), ("proposer", PROPOSER), ("disputer", disputer)],
            data=[
                ("identifier", IDENTIFIER),
                ("timestamp", 1),
                ("ancillaryData", ANCILLARY),
                ("proposedPrice", 10**18),
                ("expirationTime", 0),
            ],
        )
        return decode_log(row)

    undisputed = settle("0x0000000000000000000000000000000000000000")
    disputed = settle("0x00000000000000000000000000000000000000aa")
    assert undisputed is not None and disputed is not None
    assert undisputed["disputed"] is False
    assert undisputed["expiry"] == 0
    assert disputed["disputed"] is True


# --------------------------------------------------------------------------- #
# Adapter events
# --------------------------------------------------------------------------- #
QUESTION_ID = "0x" + "ab" * 32


def test_adapter_v1_question_initialized() -> None:
    row = build_log(
        UMA_ADAPTER_V1,
        "QuestionInitialized(bytes32,bytes,uint256,address,uint256,uint256,bool)",
        indexed=[("questionID", QUESTION_ID)],
        data=[
            ("ancillaryData", ANCILLARY),
            ("resolutionTime", 1_793_778_520),
            ("rewardToken", CURRENCY),
            ("reward", 10 * 10**6),
            ("proposalBond", 5 * 10**18),
            ("earlyResolutionEnabled", True),
        ],
    )
    event = decode_log(row)
    assert event is not None
    assert event["event"] == "QuestionInitialized"
    assert event["contract"] == CONTRACT_ADAPTER_V1
    # The derived table's contract tag is what joins to the config key.
    assert event["contract"] == "uma_ctf_adapter_v1"
    assert event["address"] == UMA_ADAPTER_V1
    assert event["question_id"] == QUESTION_ID
    assert event["resolution_time"] == 1_793_778_520
    assert event["reward"] == str(10 * 10**6)
    assert event["proposal_bond"] == str(5 * 10**18)
    assert event["early_resolution"] is True


def test_adapter_v1_resolution_data_requested() -> None:
    row = build_log(
        UMA_ADAPTER_V1,
        "ResolutionDataRequested(address,uint256,bytes32,bytes32,bytes,address,uint256,uint256,bool)",
        indexed=[
            ("requestor", ADAPTER),
            ("requestTimestamp", 1_765_910_303),
            ("questionID", QUESTION_ID),
        ],
        data=[
            ("identifier", IDENTIFIER),
            ("ancillaryData", ANCILLARY),
            ("rewardToken", CURRENCY),
            ("reward", 0),
            ("proposalBond", 0),
            ("earlyResolution", False),
        ],
    )
    event = decode_log(row)
    assert event is not None
    assert event["requester"] == ADAPTER
    assert event["request_timestamp"] == 1_765_910_303
    assert event["question_id"] == QUESTION_ID
    assert event["identifier_text"] == "YES_OR_NO_QUERY"


def test_adapter_v2_question_initialized_and_resolved() -> None:
    initialized = build_log(
        UMA_ADAPTER_V2,
        "QuestionInitialized(bytes32,uint256,address,bytes,address,uint256,uint256)",
        indexed=[
            ("questionID", QUESTION_ID),
            ("requestTimestamp", 1_765_910_303),
            ("creator", ADAPTER),
        ],
        data=[
            ("ancillaryData", ANCILLARY),
            ("rewardToken", CURRENCY),
            ("reward", 0),
            ("proposalBond", 5 * 10**18),
        ],
    )
    resolved = build_log(
        UMA_ADAPTER_V2,
        "QuestionResolved(bytes32,int256,uint256[])",
        indexed=[("questionID", QUESTION_ID), ("settledPrice", 10**18)],
        data=[("payouts", [0, 1])],
    )
    first = decode_log(initialized)
    second = decode_log(resolved)
    assert first is not None and second is not None
    assert first["question_id"] == QUESTION_ID
    assert first["requester"] == ADAPTER
    assert first["request_timestamp"] == 1_765_910_303
    assert second["settlement_price"] == str(10**18)
    assert second["payouts"] == "[0, 1]"


def test_shared_topic_is_disambiguated_by_address() -> None:
    """QuestionReset has the same signature in v1 and v2."""
    signature = "QuestionReset(bytes32)"
    v1 = decode_log(build_log(UMA_ADAPTER_V1, signature, indexed=[("questionID", QUESTION_ID)]))
    v2 = decode_log(build_log(UMA_ADAPTER_V2, signature, indexed=[("questionID", QUESTION_ID)]))
    assert v1 is not None and v2 is not None
    assert v1["contract"] == CONTRACT_ADAPTER_V1
    assert v2["contract"] == CONTRACT_ADAPTER_V2


# --------------------------------------------------------------------------- #
# Rejection paths
# --------------------------------------------------------------------------- #
def test_unknown_address_returns_none() -> None:
    row = build_log(
        "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
        "QuestionReset(bytes32)",
        indexed=[("questionID", QUESTION_ID)],
    )
    assert decode_log(row) is None


def test_unknown_topic_at_known_address_returns_none() -> None:
    row = build_log(
        UMA_OO_V2,
        "Transfer(address,address,uint256)",
        indexed=[("from", ADAPTER), ("to", PROPOSER)],
        data=[("value", 1)],
    )
    assert decode_log(row) is None


def test_truncated_payload_returns_none_not_partial_row() -> None:
    row = build_log(
        UMA_OO_V2,
        "ProposePrice(address,address,bytes32,uint256,bytes,int256,uint256,address)",
        indexed=[("requester", ADAPTER), ("proposer", PROPOSER)],
        data=[
            ("identifier", IDENTIFIER),
            ("timestamp", 1),
            ("ancillaryData", ANCILLARY),
            ("proposedPrice", 10**18),
            ("expirationTimestamp", 2),
            ("currency", CURRENCY),
        ],
    )
    row["data"] = row["data"][:128]
    assert decode_log(row) is None


# Topic0s observed on chain for UMA OptimisticOracleV2 (0xee3afe..7c24) in
# ``data/tape/polygon_logs`` over the whole taped window (2026-11-10):
# 0x6e51dd00.. -> 283 ProposePrice, 0xf1679315.. -> 38 RequestPrice,
# 0x3f384afb.. -> 272 Settle.  The dispute topic below occurs zero times, which
# is why the 2026-11-10 window's dispute posterior is unexercised rather than
# zero: no dispute was emitted for this population in 34 h.
CHAIN_TOPIC0 = {
    "ProposePrice": "0x6e51dd00371aabffa82cd401592f76ed51e98a9ea4b58751c70463a2c78b5ca1",
    "RequestPrice": "0xf1679315ff325c257a944e0ca1bfe7b26616039e9511f9610d4ba3eca851027b",
    "Settle": "0x3f384afb4bd9f0aef0298c80399950011420eb33b0e1a750b20966270247b9a0",
    "DisputePrice": "0x5165909c3d1c01c5d1e121ac6f6d01dda1ba24bc9e1f975b5a375339c15be7f3",
}


@pytest.mark.parametrize(("event", "topic0"), sorted(CHAIN_TOPIC0.items()))
def test_the_collector_looks_up_the_topic0s_the_chain_emits(event: str, topic0: str) -> None:
    """A spec whose signature drifted from the ABI is a silent hole.

    ``DisputePrice`` used to hash ``0xdb5d525c..`` (two indexed addresses) and
    ``Settle`` ``0x44b71f4d..`` (``settlementPrice`` instead of
    ``expirationTime``): neither topic occurs on chain, so every dispute and all
    272 settlements in the taped window were dropped as "not a UMA event".
    """
    spec = SPEC_BY_CONTRACT_TOPIC.get((CONTRACT_OO_V2, topic0))
    assert spec is not None, f"{event} {topic0} is not decodable"
    assert spec.name == event


def test_dispute_price_decodes_all_three_indexed_parties() -> None:
    """``disputer`` is topic 3, not topic 2: a shifted read names the proposer."""
    spec = SPEC_BY_CONTRACT_TOPIC[(CONTRACT_OO_V2, CHAIN_TOPIC0["DisputePrice"])]
    assert [name for name, _type in spec.indexed] == ["requester", "proposer", "disputer"]


# --------------------------------------------------------------------------- #
# Raw log parsing: the poller feeds node JSON straight in
# --------------------------------------------------------------------------- #
def _log_object() -> dict:
    return {
        "address": "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
        "topics": [
            "0x63bf4d16b2a61e2b1d4b0c8d1e4c5e0a0b8b3c6f2ad2e1e3f7d1c0b9a8e7d6c5",
            "0x0000000000000000000000000000000000000000000000000000000000000001",
            "0x0000000000000000000000000000000000000000000000000000000000000002",
        ],
        "data": "0x0000000000000000000000000000000000000000000000000de0b6b3a7640000",
        "blockNumber": "0x5c378e5",
        "transactionHash": "0x" + "ab" * 32,
        "transactionIndex": "0x2",
        "blockHash": "0x" + "cd" * 32,
        "logIndex": "0x7",
        "removed": False,
    }


def test_hex_int_parses_rpc_quantities() -> None:
    assert hex_int("0x5c378e5") == 96_696_549
    assert hex_int("0x0") == 0
    assert hex_int("42") == 42
    assert hex_int(12) == 12
    assert hex_int(None) is None
    assert hex_int("0x") is None
    assert hex_int("not hex") is None
    assert hex_int(True) is None
    assert hex_int("", default=-1) == -1


def test_parse_log_maps_every_field() -> None:
    row = parse_log(_log_object(), recv_ts_ns=1_700_000_000_000_000_000, recv_mono_ns=99)
    assert row is not None
    assert row["recv_ts_ns"] == 1_700_000_000_000_000_000
    assert row["recv_mono_ns"] == 99
    assert row["block_number"] == 96_696_549
    assert row["tx_index"] == 2
    assert row["log_index"] == 7
    assert row["address"] == "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
    assert row["topic0"].startswith("0x63bf4d16")
    assert row["topic1"].endswith("01") and row["topic2"].endswith("02")
    assert row["topic3"] is None
    assert row["data"].startswith("0x0000000000000000")
    assert row["block_hash"] == "0x" + "cd" * 32
    assert row["tx_hash"] == "0x" + "ab" * 32
    assert row["removed"] is False


def test_parse_log_keeps_removed_flag() -> None:
    raw = _log_object()
    raw["removed"] = True
    row = parse_log(raw)
    assert row is not None
    assert row["removed"] is True


def test_parse_log_rejects_unplaceable_log() -> None:
    raw = _log_object()
    raw.pop("blockNumber")
    assert parse_log(raw) is None
    assert parse_log("not an object") is None


def test_parse_log_tolerates_missing_topics_and_indices() -> None:
    row = parse_log({"blockNumber": "0x10"})
    assert row is not None
    assert row["block_number"] == 16
    assert row["topic0"] is None and row["topic3"] is None
    assert row["tx_index"] is None and row["log_index"] is None
    assert row["address"] is None
    assert row["removed"] is False


def test_parse_log_feeds_decode_log() -> None:
    """The poller's path: node JSON -> row -> decoded UMA event."""
    row = parse_log(_log_object())
    assert row is not None
    # CTF address with an unknown topic is not a UMA event, but the row parses.
    assert decode_log(row) is None


def test_ancillary_title_extracts_the_question() -> None:
    text = "q: title: Will BTC close above 100k on Friday, Dec 5?, description: resolves per Coinbase"
    assert ancillary_title(text) == "Will BTC close above 100k on Friday, Dec 5?"
    assert ancillary_title("q: title: , description: empty") is None
    assert ancillary_title("resolution rules without a title") is None
    assert ancillary_title(None) is None

