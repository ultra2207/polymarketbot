"""UMA/adapter log decoding: ABI topics, payloads, and the address mismatch.

The event signatures are asserted against the topic0 values observed on
Polygon mainnet (see the live probe in docs/data-collection.md), so a typo in
the ABI table fails a test instead of silently decoding nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Sequence

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from polymarketbot.data.config import load_config
from polymarketbot.data.schema import POLYGON_LOGS
from polymarketbot.data.streams.polygon import PolygonSettings
from polymarketbot.data.uma import (
    CONTRACT_ADAPTER_V1,
    CONTRACT_ADAPTER_V2,
    CONTRACT_OO_V2,
    UMA_ADAPTER_V1,
    UMA_ADAPTER_V2,
    UMA_OO_V2,
    decode_log,
    event_topic0,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.replay_uma import (  # noqa: E402
    configured_addresses,
    main as replay_main,
    run_probe,
)

CONFIG = REPO_ROOT / "configs" / "data_collection.yaml"
CTF = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"


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
            "DisputePrice(address,address,bytes32,uint256,bytes,int256)",
            "0xdb5d525cf38fcf3b5f15304610b0482cea1a86f2c3aac48adf04617cf8a84490",
        ),
        (
            "Settle(address,address,address,bytes32,uint256,bytes,int256,int256)",
            "0x44b71f4dd337078450f2f5aa7bf770c1b0005de6ccd88581311987934eaa5711",
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
        "DisputePrice(address,address,bytes32,uint256,bytes,int256)",
        indexed=[("requester", ADAPTER), ("disputer", disputer)],
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
            "Settle(address,address,address,bytes32,uint256,bytes,int256,int256)",
            indexed=[("requester", ADAPTER), ("proposer", PROPOSER), ("disputer", disputer)],
            data=[
                ("identifier", IDENTIFIER),
                ("timestamp", 1),
                ("ancillaryData", ANCILLARY),
                ("proposedPrice", 10**18),
                ("settlementPrice", 0),
            ],
        )
        return decode_log(row)

    undisputed = settle("0x0000000000000000000000000000000000000000")
    disputed = settle("0x00000000000000000000000000000000000000aa")
    assert undisputed is not None and disputed is not None
    assert undisputed["disputed"] is False
    assert undisputed["settlement_price"] == "0"
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


# --------------------------------------------------------------------------- #
# Replay script
# --------------------------------------------------------------------------- #
def _polygon_row(log: dict[str, Any]) -> dict[str, Any]:
    row = {name: None for name in POLYGON_LOGS.names}
    row.update(log)
    row.setdefault("recv_mono_ns", 1)
    return row


def test_replay_writes_uma_rows_and_skips_scanned_parts(tmp_path: Path) -> None:
    tape_root = tmp_path / "tape"
    part = tape_root / "polygon_logs" / "date=2026-11-04" / "hour=00" / "part-1.parquet"
    part.parent.mkdir(parents=True)
    uma_log = build_log(
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
    ctf_log = {
        "address": "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
        "topic0": "0x" + "11" * 32,
        "data": "0x",
        "block_number": 1,
        "tx_index": 0,
        "log_index": 0,
        "removed": False,
        "recv_ts_ns": 1,
    }
    pq.write_table(
        pa.Table.from_pylist([_polygon_row(ctf_log), _polygon_row(uma_log)], schema=POLYGON_LOGS),
        part,
    )

    output = tmp_path / "uma_events.parquet"
    journal = tmp_path / "uma_events.parts.json"
    argv = ["--tape-root", str(tape_root), "--output", str(output), "--journal", str(journal)]
    assert replay_main(argv) == 0

    table = pq.read_table(output)
    assert table.num_rows == 1
    row = table.to_pylist()[0]
    assert row["event"] == "ProposePrice"
    assert row["proposer"] == PROPOSER
    assert row["source_part"].endswith("part-1.parquet")

    # Second run: the part is in the journal, so nothing is decoded or appended.
    assert replay_main(argv) == 0
    assert pq.read_table(output).num_rows == 1


def test_replay_decodes_v1_adapter_log_from_tape(tmp_path: Path) -> None:
    """The v1 adapter is a separate config key, so it must survive the filter."""
    tape_root = tmp_path / "tape"
    part = tape_root / "polygon_logs" / "date=2026-11-04" / "hour=01" / "part-1.parquet"
    part.parent.mkdir(parents=True)
    v1_log = build_log(
        UMA_ADAPTER_V1,
        "QuestionInitialized(bytes32,bytes,uint256,address,uint256,uint256,bool)",
        indexed=[("questionID", QUESTION_ID)],
        data=[
            ("ancillaryData", ANCILLARY),
            ("resolutionTime", 1_793_778_520),
            ("rewardToken", CURRENCY),
            ("reward", 10 * 10**6),
            ("proposalBond", 5 * 10**18),
            ("earlyResolutionEnabled", False),
        ],
    )
    pq.write_table(pa.Table.from_pylist([_polygon_row(v1_log)], schema=POLYGON_LOGS), part)

    output = tmp_path / "uma_events.parquet"
    journal = tmp_path / "uma_events.parts.json"
    argv = [
        "--tape-root",
        str(tape_root),
        "--output",
        str(output),
        "--journal",
        str(journal),
        "--config",
        str(CONFIG),
    ]
    assert replay_main(argv) == 0

    row = pq.read_table(output).to_pylist()[0]
    assert row["event"] == "QuestionInitialized"
    assert row["contract"] == "uma_ctf_adapter_v1"
    assert row["question_id"] == QUESTION_ID
    assert row["early_resolution"] is False


# --------------------------------------------------------------------------- #
# Config-driven address filter and the read-only probe
# --------------------------------------------------------------------------- #
def test_config_declares_the_four_subscribed_addresses() -> None:
    section = load_config(CONFIG)["streams"]["polygon"]
    settings = PolygonSettings.from_section(section, fallbacks=[])
    assert len(settings.addresses) == 4
    lowered = [address.lower() for address in settings.addresses]
    assert len(set(lowered)) == 4
    assert set(lowered) == {CTF, UMA_OO_V2, UMA_ADAPTER_V1, UMA_ADAPTER_V2}
    # The replay filters on exactly the same set the stream subscribes to.
    assert {address.lower() for address in configured_addresses(CONFIG)} == set(lowered)


class _Recorder:
    """Minimal loguru stand-in: the probe only needs info/error/warning."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def info(self, message: str, *args: Any) -> None:
        self.lines.append(("info", message.format(*args)))

    def warning(self, message: str, *args: Any) -> None:
        self.lines.append(("warning", message.format(*args)))

    def error(self, message: str, *args: Any) -> None:
        self.lines.append(("error", message.format(*args)))


class _StubRpc:
    def __init__(self, head: int, *, fail: Sequence[str] = ()) -> None:
        self.head = head
        self.fail = set(fail)
        self.calls: list[tuple[str, int, int]] = []
        self.closed = False

    def block_number(self) -> int:
        return self.head

    def get_logs(self, addresses: Sequence[str], low: int, high: int) -> list[dict[str, Any]]:
        (address,) = addresses
        self.calls.append((address, low, high))
        if address.lower() in self.fail:
            raise RuntimeError(f"{address}: HTTP 429 rate limited")
        if address.lower() != UMA_OO_V2:
            return []
        return [
            {
                "address": address,
                "blockNumber": hex(high),
                "topics": ["0x" + "ab" * 32],
                "data": "0x",
            }
        ]

    def close(self) -> None:
        self.closed = True


def test_probe_reports_every_address_and_continues_after_a_failure() -> None:
    rpc = _StubRpc(1_000, fail=[UMA_ADAPTER_V1])
    log = _Recorder()
    addresses = configured_addresses(CONFIG)

    assert run_probe(addresses, 200, log=log, client=rpc) == 1

    # One call per address over the same window, not one combined call.
    assert [call[0] for call in rpc.calls] == list(addresses)
    assert all(call[1:] == (801, 1_000) for call in rpc.calls)
    assert rpc.closed

    messages = [line for _kind, line in log.lines]
    for address in addresses:
        assert any(address in line and line.startswith("probe ") for line in messages)
    assert any("0x" + "ab" * 32 in line for line in messages)
    assert any("HTTP 429" in line for _kind, line in log.lines if _kind == "error")


def test_probe_exits_clean_when_every_address_answers() -> None:
    rpc = _StubRpc(500)
    log = _Recorder()
    assert run_probe([CTF, UMA_OO_V2], 10, log=log, client=rpc) == 0
    assert [call[1:] for call in rpc.calls] == [(491, 500), (491, 500)]
