"""Synthetic-log tests for the UMA oracle stream (no network).

The ABI fixtures are encoded in-process from the same event signatures the
decoder uses, so a broken signature or an offset mistake fails here rather than
in the tape.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from polymarketbot.data.config import load_config
from polymarketbot.data.context import HealthRegistry
from polymarketbot.data.streams import uma
from polymarketbot.data.uma import event_topic0

OO = "0xee3afe347d5c74317041e2618c49534daf887c24"
ADAPTER_V2 = "0x6a9d222616c90fca5754cd1333cfd9b7fb6a4f74"
UNIVERSE_CONDITION = "0x" + "11" * 32

REQUESTER = "0x" + "22" * 20
PROPOSER = "0x" + "33" * 20
DISPUTER = "0x" + "44" * 20
CURRENCY = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
IDENTIFIER = b"YES_OR_NO_QUERY".ljust(32, b"\x00")
QUESTION = "Will the synthetic test market resolve YES?"
ANCILLARY = (
    f"q: title: {QUESTION}, description: Synthetic fixture, no network., "
    "res_data: p1: 0, p2: 1, p3: 0.5."
)
ANCILLARY_BYTES = ANCILLARY.encode()


# --------------------------------------------------------------------------- #
# ABI fixture builders
# --------------------------------------------------------------------------- #
def _word(value: int, *, signed: bool = False) -> bytes:
    return int(value).to_bytes(32, "big", signed=signed)


def _addr_word(address: str) -> bytes:
    return bytes(12) + bytes.fromhex(address[2:])


def _topic_address(address: str) -> str:
    return "0x" + _addr_word(address).hex()


def _encode(types: list[str], values: list[object]) -> str:
    """Solidity ABI head/tail encoding for the handful of types used here."""
    heads: list[bytes] = []
    tail = b""
    head_size = 32 * len(types)
    for type_name, value in zip(types, values):
        if type_name == "address":
            heads.append(_addr_word(str(value)))
        elif type_name == "bytes32":
            raw = bytes(value)  # type: ignore[arg-type]
            heads.append(raw.ljust(32, b"\x00"))
        elif type_name == "uint256":
            heads.append(_word(int(value)))  # type: ignore[arg-type]
        elif type_name == "int256":
            heads.append(_word(int(value), signed=True))  # type: ignore[arg-type]
        elif type_name == "bool":
            heads.append(_word(1 if value else 0))
        elif type_name == "bytes":
            raw = bytes(value)  # type: ignore[arg-type]
            heads.append(_word(head_size + len(tail)))
            tail += _word(len(raw)) + raw.ljust((len(raw) + 31) // 32 * 32, b"\x00")
        elif type_name == "uint256[]":
            items = [int(item) for item in value]  # type: ignore[union-attr]
            heads.append(_word(head_size + len(tail)))
            tail += _word(len(items)) + b"".join(_word(item) for item in items)
        else:  # pragma: no cover - fixture typo
            raise AssertionError(f"unsupported fixture type {type_name}")
    return "0x" + b"".join(heads + ([tail] if tail else [])).hex()


def _log(
    *,
    address: str,
    signature: str,
    topics: list[str],
    data: str,
    block: int,
    log_index: int,
    tx_hash: str,
) -> dict:
    return {
        "address": address,
        "topics": [event_topic0(signature), *topics],
        "data": data,
        "blockNumber": hex(block),
        "blockHash": "0x" + "cd" * 32,
        "transactionHash": tx_hash,
        "transactionIndex": "0x1",
        "logIndex": hex(log_index),
        "removed": False,
    }


def request_price(block: int = 100, log_index: int = 0, tx: str = "0x" + "a1" * 32) -> dict:
    return _log(
        address=OO,
        signature="RequestPrice(address,bytes32,uint256,bytes,address,uint256,uint256)",
        topics=[_topic_address(REQUESTER)],
        data=_encode(
            ["bytes32", "uint256", "bytes", "address", "uint256", "uint256"],
            [IDENTIFIER, 1_760_000_000, ANCILLARY_BYTES, CURRENCY, 5 * 10**18, 2 * 10**18],
        ),
        block=block,
        log_index=log_index,
        tx_hash=tx,
    )


def propose_price(block: int = 101, log_index: int = 0, tx: str = "0x" + "a2" * 32) -> dict:
    return _log(
        address=OO,
        signature="ProposePrice(address,address,bytes32,uint256,bytes,int256,uint256,address)",
        topics=[_topic_address(REQUESTER), _topic_address(PROPOSER)],
        data=_encode(
            ["bytes32", "uint256", "bytes", "int256", "uint256", "address"],
            [
                IDENTIFIER,
                1_760_000_000,
                ANCILLARY_BYTES,
                10**18,
                1_760_003_600,
                CURRENCY,
            ],
        ),
        block=block,
        log_index=log_index,
        tx_hash=tx,
    )


def dispute_price(block: int = 102, log_index: int = 0, tx: str = "0x" + "a3" * 32) -> dict:
    return _log(
        address=OO,
        signature="DisputePrice(address,address,bytes32,uint256,bytes,int256)",
        topics=[_topic_address(REQUESTER), _topic_address(DISPUTER)],
        data=_encode(
            ["bytes32", "uint256", "bytes", "int256"],
            [IDENTIFIER, 1_760_000_000, ANCILLARY_BYTES, 10**18],
        ),
        block=block,
        log_index=log_index,
        tx_hash=tx,
    )


def adapter_resolved(block: int = 103, log_index: int = 0, tx: str = "0x" + "a4" * 32) -> dict:
    question_id = "0x" + "77" * 32
    settled_price = "0x" + _word(10**18, signed=True).hex()
    return _log(
        address=ADAPTER_V2,
        signature="QuestionResolved(bytes32,int256,uint256[])",
        topics=[question_id, settled_price],
        data=_encode(["uint256[]"], [[0, 1]]),
        block=block,
        log_index=log_index,
        tx_hash=tx,
    )


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeRpc:
    def __init__(self, head: int, logs: list[dict], base_ts: int = 1_760_000_000) -> None:
        self.head = head
        self.logs = logs
        self.base_ts = base_ts
        self.calls: list[tuple[tuple[str, ...], int, int]] = []
        self.block_calls: list[int] = []

    def block_number(self) -> int:
        return self.head

    def get_logs(self, addresses, low: int, high: int) -> list[dict]:
        self.calls.append((tuple(addresses), low, high))
        return [
            log
            for log in self.logs
            if low <= int(log["blockNumber"], 16) <= high
        ]

    def get_block(self, number: int) -> dict:
        self.block_calls.append(number)
        return {"number": hex(number), "timestamp": hex(self.base_ts + number)}

    def close(self) -> None:
        pass


class _Log:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(message)

    def warning(self, message: str) -> None:
        self.messages.append(message)

    def error(self, message: str) -> None:
        self.messages.append(message)

    def debug(self, message: str) -> None:
        self.messages.append(message)

    def exception(self, message: str) -> None:
        self.messages.append(message)


@dataclass(frozen=True)
class _Market:
    condition_id: str
    question: str
    slug: str


class _Universe:
    def __init__(self, markets: list[_Market]) -> None:
        self.markets = {market.condition_id: market for market in markets}


class FakeCtx:
    def __init__(self, root: Path, universe: _Universe | None = None) -> None:
        self.config = {"root": str(root), "streams": {"uma": {}}}
        self.health = HealthRegistry()
        self.stop = asyncio.Event()
        self.universe = universe
        self.rows: list[tuple[str, dict]] = []
        self.tables: dict[str, object] = {}
        self.logger = _Log()

    def section(self, name: str) -> dict:
        return dict(self.config.get("streams", {}).get(name, {}))

    def emit(self, table: str, row: dict) -> None:
        self.rows.append((table, row))

    def register_table(self, table: str, schema: object) -> None:
        self.tables[table] = schema

    def log(self, _name: str) -> _Log:
        return self.logger


def _metrics(ctx: FakeCtx) -> dict[str, float]:
    return {row["metric"]: row["value"] for row in ctx.health.snapshot()}


def _poll(
    ctx: FakeCtx,
    rpc: FakeRpc,
    state: uma.UmaState,
    *,
    lookback: int = 6,
) -> uma.PollStats:
    settings = uma.UmaSettings.from_section(
        {
            "addresses": [OO, ADAPTER_V2],
            "initial_lookback_blocks": lookback,
            "max_windows_per_poll": 10,
            "log_block_span": 10,
            "window_pause_seconds": 0,
            "retry_total": 1,
        },
        ctx.config["root"],
    )
    return asyncio.run(uma.poll_once(ctx, settings, state, rpc))


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_poll_decodes_lifecycle_and_dedupes(tmp_path: Path) -> None:
    universe = _Universe(
        [_Market(UNIVERSE_CONDITION, QUESTION, "will-the-synthetic-test-market-resolve-yes")]
    )
    ctx = FakeCtx(tmp_path, universe)
    propose = propose_price()
    rpc = FakeRpc(
        head=105,
        logs=[
            request_price(),
            propose,
            propose,  # provider overlap / replayed response
            dispute_price(),
        ],
    )
    state = uma.UmaState()

    stats = _poll(ctx, rpc, state)

    assert stats.events == 3
    assert stats.duplicates == 1
    assert stats.windows == 1
    assert rpc.calls == [((OO, ADAPTER_V2), 100, 105)]
    assert state.last_block == 105

    by_event = {row["event"]: row for _table, row in ctx.rows}
    assert set(by_event) == {"RequestPrice", "ProposePrice", "DisputePrice"}

    request = by_event["RequestPrice"]
    assert request["requester"] == REQUESTER
    assert request["proposer"] is None and request["disputer"] is None
    assert request["identifier_text"] == "YES_OR_NO_QUERY"
    assert request["request_timestamp"] == 1_760_000_000
    assert request["ancillary_text"] == ANCILLARY
    assert request["ancillary_data"] == "0x" + ANCILLARY_BYTES.hex()
    assert request["reward"] == "5000000000000000000"
    assert request["final_fee"] == "2000000000000000000"
    # Unknown fields are null, not zero.
    assert request["proposed_price"] is None
    assert request["settlement_price"] is None
    assert request["payouts"] is None
    assert request["disputed"] is None
    assert request["ts"] == (1_760_000_000 + 100) * 1000
    assert request["block_number"] == 100
    assert request["tx_hash"] == "0x" + "a1" * 32
    assert request["log_index"] == 0

    proposal = by_event["ProposePrice"]
    assert proposal["proposer"] == PROPOSER
    assert proposal["proposed_price"] == str(10**18)
    assert proposal["expiry"] == 1_760_003_600
    assert proposal["disputed"] is False
    assert proposal["disputer"] is None

    dispute = by_event["DisputePrice"]
    assert dispute["disputer"] == DISPUTER
    assert dispute["disputed"] is True
    assert dispute["proposer"] is None
    assert dispute["settlement_price"] is None

    # condition_id/slug/question resolved from the universe manifest by exact
    # question text; the question itself came from the ancillary data.
    for row in (request, proposal, dispute):
        assert row["condition_id"] == UNIVERSE_CONDITION
        assert row["market_slug"] == "will-the-synthetic-test-market-resolve-yes"
        assert row["question"] == QUESTION

    metrics = _metrics(ctx)
    assert metrics["polls"] == 1
    assert metrics["events"] == 3
    assert metrics["duplicates"] == 1
    assert metrics["last_block"] == 105


def test_rerun_does_not_rewrite(tmp_path: Path) -> None:
    ctx = FakeCtx(tmp_path)
    rpc = FakeRpc(head=105, logs=[request_price(), propose_price(), dispute_price()])
    state = uma.UmaState()

    first = _poll(ctx, rpc, state)
    assert first.events == 3
    state_file = tmp_path / "uma_state.json"
    assert state_file.exists()

    # A fresh process loads the persisted anchor; the same head emits nothing.
    loaded = uma.load_state(state_file, ctx.logger)
    assert loaded.last_block == 105
    emitted = len(ctx.rows)
    calls = len(rpc.calls)

    second = _poll(ctx, rpc, loaded)

    assert second.events == 0
    assert second.windows == 0
    assert len(ctx.rows) == emitted
    assert len(rpc.calls) == calls
    assert uma.load_state(state_file, ctx.logger).last_block == 105


def test_unmapped_question_keeps_ids_null(tmp_path: Path) -> None:
    universe = _Universe([_Market(UNIVERSE_CONDITION, "Some other question?", "other")])
    ctx = FakeCtx(tmp_path, universe)
    rpc = FakeRpc(head=100, logs=[request_price()])

    stats = _poll(ctx, rpc, uma.UmaState())

    assert stats.events == 1
    row = ctx.rows[0][1]
    assert row["condition_id"] is None
    assert row["market_slug"] is None
    # The chain supplied the question; it is not thrown away when unmapped.
    assert row["question"] == QUESTION


def test_adapter_event_decodes(tmp_path: Path) -> None:
    ctx = FakeCtx(tmp_path)
    rpc = FakeRpc(head=103, logs=[adapter_resolved()])

    stats = _poll(ctx, rpc, uma.UmaState(), lookback=4)

    assert stats.events == 1
    row = ctx.rows[0][1]
    assert row["contract"] == "uma_ctf_adapter_v2"
    assert row["event"] == "QuestionResolved"
    assert row["settlement_price"] == str(10**18)
    assert row["payouts"] == "[0, 1]"
    assert row["identifier"] is None
    assert row["ancillary_text"] is None


def test_stream_writes_uma_events_table_and_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeCtx(tmp_path)
    rpc = FakeRpc(head=105, logs=[request_price()])
    _poll(ctx, rpc, uma.UmaState())

    # The emitter only ever writes the registered table; `run` registers the
    # schema before its first poll (checked here with a pre-set stop event).
    assert [table for table, _row in ctx.rows] == ["uma_events"]

    ctx.stop.set()
    monkeypatch.setattr(uma, "open_rpc", lambda: rpc)
    asyncio.run(uma.run(ctx))

    assert "uma_events" in ctx.tables
    assert ctx.tables["uma_events"] is uma.UMA_EVENTS

    settings = uma.UmaSettings.from_section(
        {"addresses": [OO], "poll_seconds": 60}, tmp_path
    )
    assert settings.poll_seconds == 60
    assert settings.addresses == (OO,)
    assert settings.state_path == tmp_path / "uma_state.json"


def test_config_enables_uma_with_verified_addresses() -> None:
    config = load_config("configs/data_collection.yaml")
    assert "uma" in config["streams"]["enabled"]
    section = config["streams"]["uma"]
    assert section["poll_seconds"] == 60
    assert set(section["addresses"]) == {
        "0xee3afe347d5c74317041e2618c49534daf887c24",
        "0xCB1822859cEF82Cd2Eb4E6276C7916e692995130",
        "0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74",
    }


def test_corrupt_state_starts_cold(tmp_path: Path) -> None:
    path = tmp_path / "uma_state.json"
    path.write_bytes(b"not json")

    log = _Log()
    state = uma.load_state(path, log)

    assert state.last_block is None
    assert state.seen == set()
    assert any("unreadable" in message for message in log.messages)
