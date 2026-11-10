"""Decode UMA Optimistic Oracle and Polymarket adapter logs.

Why this exists
---------------
`polygon_logs` stores raw event logs for the CTF, the UMA Optimistic Oracle
addresses, and the Polymarket resolution adapters. Gamma tells you that a market
resolved; it does not tell you when a price was proposed, by whom, what was
proposed, whether it was disputed, or when the proposal expires. Those facts
live only in the logs, and they are what settlement-latency and dispute studies
need.

What is decoded
---------------
Three contracts are recognised, each with the event set from its verified ABI:

* ``0xee3afe347d5c74317041e2618c49534daf887c24`` - UMA Optimistic Oracle V2.
  ``RequestPrice``, ``ProposePrice``, ``DisputePrice``, ``Settle``.
* ``0xCB1822859cEF82Cd2Eb4E6276C7916e692995130`` - Polymarket's *old* binary
  adapter (PolygonScan contract name ``UmaConditionalTokensBinaryAdapter``,
  config key ``uma_v1_adapter``). It is a CTF adapter, not the Optimistic
  Oracle; the OOv2 address is the separate ``uma_oo`` key above.
* ``0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74`` - ``UmaCtfAdapter`` v2.0.0.

The Optimistic Oracle V2 address was read back from the v2 adapter itself:
``eth_call optimisticOracle()`` returned ``0xee3a...87c24`` on 2026-11-04, and
that is the address UMA publishes for OOv2 on Polygon.

Both adapter versions emit ``QuestionReset``/``QuestionPaused``/``QuestionUnpaused``
with identical signatures, so decoding is keyed by ``(address role, topic0)``,
never by topic0 alone. An unknown address or topic yields no row: a quiet
contract and an ABI mismatch must not both look like "no events".

Prices are int256 on chain. They are written as decimal *strings* because an
int256 does not fit int64 and silently clipping a price is worse than making the
caller parse a string. ``1e18`` is ``1.0`` for YES/NO questions.

``question_id`` is the adapter's ``questionID`` when the log carries it. For
Optimistic Oracle logs it is ``keccak256(ancillaryData)``: the v2 adapter
computes ``questionID = keccak256(data)`` before requesting the price (see
``initialize``/``priceDisputed`` in Polymarket/uma-ctf-adapter), so the raw OO
log joins to the adapter log without an off-chain lookup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import pyarrow as pa
from Crypto.Hash import keccak

# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #
# Verified 2026-11-04 via eth_call on polygon.drpc.org:
#   adapter 0x6A9D...F74.optimisticOracle() -> 0xee3a...87c24
# and PolygonScan verified source: UmaCtfAdapter uses IOptimisticOracleV2.
UMA_OO_V2 = "0xee3afe347d5c74317041e2618c49534daf887c24"

# Polymarket's old binary adapter (retired collector config key
# `uma_v1_adapter`). PolygonScan calls this contract
# `UmaConditionalTokensBinaryAdapter` (name tag "UMA CTF Adapter Old").
# It is an adapter, not the oracle; markets opened before the v2 cutover settle
# through it, so both adapters must be decoded.
UMA_ADAPTER_V1 = "0xcb1822859cef82cd2eb4e6276c7916e692995130"

# `UmaCtfAdapter` v2.0.0 (retired collector config key `uma_adapter`).
UMA_ADAPTER_V2 = "0x6a9d222616c90fca5754cd1333cfd9b7fb6a4f74"

CONTRACT_OO_V2 = "optimistic_oracle_v2"
CONTRACT_ADAPTER_V1 = "uma_ctf_adapter_v1"
CONTRACT_ADAPTER_V2 = "uma_ctf_adapter_v2"

CONTRACT_BY_ADDRESS: dict[str, str] = {
    UMA_OO_V2: CONTRACT_OO_V2,
    UMA_ADAPTER_V1: CONTRACT_ADAPTER_V1,
    UMA_ADAPTER_V2: CONTRACT_ADAPTER_V2,
}
UMA_ADDRESSES: frozenset[str] = frozenset(CONTRACT_BY_ADDRESS)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


# --------------------------------------------------------------------------- #
# ABI primitives
# --------------------------------------------------------------------------- #
def keccak256_hex(data: bytes) -> str:
    digest = keccak.new(digest_bits=256)
    digest.update(data)
    return "0x" + digest.hexdigest()


def event_topic0(signature: str) -> str:
    """`keccak256` of an event signature, e.g. ``Settle(address,...)``."""
    return keccak256_hex(signature.encode("ascii"))


def _strip_hex(value: str) -> str:
    text = str(value or "").strip().lower()
    return text[2:] if text.startswith("0x") else text


def _to_bytes(value: str) -> bytes:
    text = _strip_hex(value)
    if len(text) % 2:
        raise ValueError("odd-length hex payload")
    return bytes.fromhex(text)


def _word(raw: bytes, index: int) -> bytes:
    start = index * 32
    word = raw[start : start + 32]
    if len(word) != 32:
        raise ValueError(f"ABI payload too short for word {index}")
    return word


def _as_address(word: bytes) -> str:
    return "0x" + word[-20:].hex()


def _as_bytes32(word: bytes) -> str:
    return "0x" + word.hex()


def _as_uint(word: bytes) -> int:
    return int.from_bytes(word, "big", signed=False)


def _as_int(word: bytes) -> int:
    return int.from_bytes(word, "big", signed=True)


def _as_bool(word: bytes) -> bool:
    return int.from_bytes(word, "big") != 0


_STATIC_DECODERS = {
    "address": _as_address,
    "bytes32": _as_bytes32,
    "uint256": _as_uint,
    "int256": _as_int,
    "bool": _as_bool,
}
_DYNAMIC_TYPES = frozenset({"bytes", "uint256[]"})


def _decode_static(type_name: str, word: bytes) -> Any:
    try:
        return _STATIC_DECODERS[type_name](word)
    except KeyError as exc:  # pragma: no cover - spec table is fixed
        raise ValueError(f"unsupported ABI type {type_name!r}") from exc


def _decode_dynamic(type_name: str, raw: bytes, offset: int) -> Any:
    if offset + 32 > len(raw):
        raise ValueError("ABI offset points past the payload")
    if type_name == "bytes":
        length = int.from_bytes(raw[offset : offset + 32], "big")
        start = offset + 32
        end = start + length
        if end > len(raw):
            raise ValueError("ABI bytes length runs past the payload")
        return raw[start:end]
    if type_name == "uint256[]":
        count = int.from_bytes(raw[offset : offset + 32], "big")
        start = offset + 32
        end = start + count * 32
        if end > len(raw):
            raise ValueError("ABI array length runs past the payload")
        return [int.from_bytes(raw[start + i * 32 : start + (i + 1) * 32], "big") for i in range(count)]
    raise ValueError(f"unsupported dynamic ABI type {type_name!r}")  # pragma: no cover


def decode_values(types: Sequence[str], data_hex: str) -> list[Any]:
    """Decode the non-indexed tail of an ABI-encoded event into Python values."""
    raw = _to_bytes(data_hex)
    if len(raw) % 32:
        raise ValueError("ABI payload is not a multiple of 32 bytes")
    values: list[Any] = []
    for index, type_name in enumerate(types):
        head = _word(raw, index)
        if type_name in _DYNAMIC_TYPES:
            values.append(_decode_dynamic(type_name, raw, int.from_bytes(head, "big")))
        else:
            values.append(_decode_static(type_name, head))
    return values


def decode_indexed(types: Sequence[str], topics: Sequence[str | None]) -> list[Any]:
    """Decode the indexed topics of an event (all supported indexed types are static)."""
    if len(topics) < len(types):
        raise ValueError(f"event needs {len(types)} indexed topics, got {len(topics)}")
    values: list[Any] = []
    for index, type_name in enumerate(types):
        values.append(_decode_static(type_name, _to_bytes(topics[index] or "")))
    return values


# --------------------------------------------------------------------------- #
# Event table
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EventSpec:
    """One event from a verified ABI.

    `indexed` and `data` are ordered ``(field, solidity-type)`` pairs. Field
    names are the ABI names; `decode_log` maps them onto canonical columns.
    """

    name: str
    signature: str
    contract: str
    indexed: tuple[tuple[str, str], ...] = ()
    data: tuple[tuple[str, str], ...] = ()

    @property
    def topic0(self) -> str:
        return event_topic0(self.signature)


def _spec(
    name: str,
    contract: str,
    indexed: Sequence[tuple[str, str]],
    data: Sequence[tuple[str, str]],
) -> EventSpec:
    signature = f"{name}({','.join(t for _f, t in (*indexed, *data))})"
    return EventSpec(name, signature, contract, tuple(indexed), tuple(data))


_OO_SPECS = (
    _spec(
        "RequestPrice",
        CONTRACT_OO_V2,
        [("requester", "address")],
        [
            ("identifier", "bytes32"),
            ("timestamp", "uint256"),
            ("ancillaryData", "bytes"),
            ("currency", "address"),
            ("reward", "uint256"),
            ("finalFee", "uint256"),
        ],
    ),
    _spec(
        "ProposePrice",
        CONTRACT_OO_V2,
        [("requester", "address"), ("proposer", "address")],
        [
            ("identifier", "bytes32"),
            ("timestamp", "uint256"),
            ("ancillaryData", "bytes"),
            ("proposedPrice", "int256"),
            ("expirationTimestamp", "uint256"),
            ("currency", "address"),
        ],
    ),
    _spec(
        "DisputePrice",
        CONTRACT_OO_V2,
        # Three indexed addresses. The deployed OOv2 declares ``proposer``
        # indexed as well, so its topic0 is
        # ``0x5165909c3d1c01c5d1e121ac6f6d01dda1ba24bc9e1f975b5a375339c15be7f3``.
        # The old two-address list hashed to ``0xdb5d525c..4490``, a topic that
        # occurs zero times on chain (measured over the whole
        # ``data/tape/polygon_logs`` window, 2026-11-10), i.e. every dispute was
        # silently undecodable and a disputed proposal kept pricing as if
        # undisputed.
        [("requester", "address"), ("proposer", "address"), ("disputer", "address")],
        [
            ("identifier", "bytes32"),
            ("timestamp", "uint256"),
            ("ancillaryData", "bytes"),
            ("proposedPrice", "int256"),
        ],
    ),
    _spec(
        "Settle",
        CONTRACT_OO_V2,
        [("requester", "address"), ("proposer", "address"), ("disputer", "address")],
        # ``settlementPrice`` is not part of the deployed ABI: OOv2 emits
        # ``Settle(...,int256 proposedPrice,uint256 expirationTime)``, topic0
        # ``0x3f384afb4bd9f0aef0298c80399950011420eb33b0e1a750b20966270247b9a0``
        # (272 such logs on chain in the same window, 0 of the old
        # ``..,int256,int256)`` topic ``0x44b71f4d..5711``). ``disputed`` is
        # read from the indexed ``disputer``, so nothing downstream depends on
        # the trailing words.
        [
            ("identifier", "bytes32"),
            ("timestamp", "uint256"),
            ("ancillaryData", "bytes"),
            ("proposedPrice", "int256"),
            ("expirationTimestamp", "uint256"),
        ],
    ),
)

# UmaConditionalTokensBinaryAdapter (v1). QuestionUpdated shares the
# QuestionInitialized signature, so both entries exist with different names.
_ADAPTER_V1_SPECS = (
    _spec(
        "QuestionInitialized",
        CONTRACT_ADAPTER_V1,
        [("questionID", "bytes32")],
        [
            ("ancillaryData", "bytes"),
            ("resolutionTime", "uint256"),
            ("rewardToken", "address"),
            ("reward", "uint256"),
            ("proposalBond", "uint256"),
            ("earlyResolutionEnabled", "bool"),
        ],
    ),
    _spec(
        "QuestionUpdated",
        CONTRACT_ADAPTER_V1,
        [("questionID", "bytes32")],
        [
            ("ancillaryData", "bytes"),
            ("resolutionTime", "uint256"),
            ("rewardToken", "address"),
            ("reward", "uint256"),
            ("proposalBond", "uint256"),
            ("earlyResolutionEnabled", "bool"),
        ],
    ),
    _spec("QuestionReset", CONTRACT_ADAPTER_V1, [("questionID", "bytes32")], []),
    _spec(
        "QuestionResolved",
        CONTRACT_ADAPTER_V1,
        [("questionID", "bytes32"), ("emergencyReport", "bool")],
        [],
    ),
    _spec(
        "QuestionSettled",
        CONTRACT_ADAPTER_V1,
        [("questionID", "bytes32"), ("settledPrice", "int256"), ("earlyResolution", "bool")],
        [],
    ),
    _spec("QuestionPaused", CONTRACT_ADAPTER_V1, [("questionID", "bytes32")], []),
    _spec("QuestionUnpaused", CONTRACT_ADAPTER_V1, [("questionID", "bytes32")], []),
    _spec(
        "QuestionFlaggedForAdminResolution",
        CONTRACT_ADAPTER_V1,
        [("questionID", "bytes32")],
        [],
    ),
    _spec(
        "ResolutionDataRequested",
        CONTRACT_ADAPTER_V1,
        [
            ("requestor", "address"),
            ("requestTimestamp", "uint256"),
            ("questionID", "bytes32"),
        ],
        [
            ("identifier", "bytes32"),
            ("ancillaryData", "bytes"),
            ("rewardToken", "address"),
            ("reward", "uint256"),
            ("proposalBond", "uint256"),
            ("earlyResolution", "bool"),
        ],
    ),
)

# UmaCtfAdapter v2.0.0. Admin events (NewAdmin/RemovedAdmin) are not
# resolution data and are deliberately absent.
_ADAPTER_V2_SPECS = (
    _spec(
        "QuestionInitialized",
        CONTRACT_ADAPTER_V2,
        [("questionID", "bytes32"), ("requestTimestamp", "uint256"), ("creator", "address")],
        [
            ("ancillaryData", "bytes"),
            ("rewardToken", "address"),
            ("reward", "uint256"),
            ("proposalBond", "uint256"),
        ],
    ),
    _spec(
        "QuestionResolved",
        CONTRACT_ADAPTER_V2,
        [("questionID", "bytes32"), ("settledPrice", "int256")],
        [("payouts", "uint256[]")],
    ),
    _spec(
        "QuestionEmergencyResolved",
        CONTRACT_ADAPTER_V2,
        [("questionID", "bytes32")],
        [("payouts", "uint256[]")],
    ),
    _spec("QuestionReset", CONTRACT_ADAPTER_V2, [("questionID", "bytes32")], []),
    _spec("QuestionPaused", CONTRACT_ADAPTER_V2, [("questionID", "bytes32")], []),
    _spec("QuestionUnpaused", CONTRACT_ADAPTER_V2, [("questionID", "bytes32")], []),
    _spec("QuestionFlagged", CONTRACT_ADAPTER_V2, [("questionID", "bytes32")], []),
    _spec(
        "AncillaryDataUpdated",
        CONTRACT_ADAPTER_V2,
        [("questionID", "bytes32"), ("owner", "address")],
        [("update", "bytes")],
    ),
)

EVENT_SPECS: tuple[EventSpec, ...] = _OO_SPECS + _ADAPTER_V1_SPECS + _ADAPTER_V2_SPECS

# (contract, topic0) -> spec. Keying by contract is required: QuestionReset,
# QuestionPaused and QuestionUnpaused have byte-identical signatures in v1 and
# v2, so a topic-only index would pick an arbitrary ABI.
SPEC_BY_CONTRACT_TOPIC: dict[tuple[str, str], EventSpec] = {
    (spec.contract, spec.topic0): spec for spec in EVENT_SPECS
}

# Every topic0 this module can decode, in stable order, for a replay report.
TOPIC0S: tuple[str, ...] = tuple(dict.fromkeys(spec.topic0 for spec in EVENT_SPECS))


# --------------------------------------------------------------------------- #
# Canonical row
# --------------------------------------------------------------------------- #
#: Columns written to `data/derived/uma_events.parquet`, in schema order.
UMA_EVENT_COLUMNS: tuple[str, ...] = (
    "source_part",
    "contract",
    "event",
    "address",
    "block_number",
    "block_hash",
    "tx_hash",
    "tx_index",
    "log_index",
    "recv_ts_ns",
    "removed",
    "question_id",
    "requester",
    "proposer",
    "disputer",
    "identifier",
    "identifier_text",
    "request_timestamp",
    "resolution_time",
    "expiry",
    "ancillary_data",
    "ancillary_text",
    "proposed_price",
    "settlement_price",
    "disputed",
    "payouts",
    "currency",
    "reward",
    "final_fee",
    "proposal_bond",
    "early_resolution",
    "emergency_resolution",
)

#: Parquet schema for `data/derived/uma_events.parquet`.
UMA_EVENTS_SCHEMA = pa.schema(
    [
        ("source_part", pa.string()),
        ("contract", pa.string()),
        ("event", pa.string()),
        ("address", pa.string()),
        ("block_number", pa.int64()),
        ("block_hash", pa.string()),
        ("tx_hash", pa.string()),
        ("tx_index", pa.int32()),
        ("log_index", pa.int32()),
        ("recv_ts_ns", pa.int64()),
        ("removed", pa.bool_()),
        ("question_id", pa.string()),
        ("requester", pa.string()),
        ("proposer", pa.string()),
        ("disputer", pa.string()),
        ("identifier", pa.string()),
        ("identifier_text", pa.string()),
        ("request_timestamp", pa.int64()),
        ("resolution_time", pa.int64()),
        ("expiry", pa.int64()),
        ("ancillary_data", pa.string()),
        ("ancillary_text", pa.string()),
        ("proposed_price", pa.string()),
        ("settlement_price", pa.string()),
        ("disputed", pa.bool_()),
        ("payouts", pa.string()),
        ("currency", pa.string()),
        ("reward", pa.string()),
        ("final_fee", pa.string()),
        ("proposal_bond", pa.string()),
        ("early_resolution", pa.bool_()),
        ("emergency_resolution", pa.bool_()),
    ]
)

if tuple(UMA_EVENTS_SCHEMA.names) != UMA_EVENT_COLUMNS:  # pragma: no cover
    raise RuntimeError("uma schema and UMA_EVENT_COLUMNS drifted")


def empty_row() -> dict[str, Any]:
    """A row with every canonical column present, so all Parquet parts align."""
    return {name: None for name in UMA_EVENT_COLUMNS}


def _text(value: bytes | None) -> str | None:
    if value is None:
        return None
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        # UMA ancillary data is UTF-8 by convention, not by constraint. Keep the
        # hex column authoritative and never invent replacement characters.
        return None


def _price(value: Any) -> str | None:
    return None if value is None else str(int(value))


def _bytes32_text(value: str | None) -> str | None:
    """Printable ASCII of a right-padded bytes32 identifier, else ``None``.

    ``YES_OR_NO_QUERY`` is the only identifier Polymarket uses, and reading it
    as hex in every downstream query is unnecessary friction. The hex column
    stays authoritative.
    """
    if not value:
        return None
    raw = _to_bytes(value).rstrip(b"\x00")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not text or not text.isprintable():
        return None
    return text


def _payouts(value: Any) -> str | None:
    if value is None:
        return None
    return "[" + ", ".join(str(int(item)) for item in value) + "]"


# --------------------------------------------------------------------------- #
# Raw logs
# --------------------------------------------------------------------------- #
# Polymarket's UMA ancillary data starts ``q: title: <question>, description:``.
# The title itself may contain commas, so the terminator is the literal
# ``description:`` key, not the first comma.
_TITLE_RE = re.compile(r"^\s*q:\s*title:\s*(.*?),\s*description:", re.S)


def hex_int(value: Any, default: int | None = None) -> int | None:
    """Parse a JSON-RPC quantity such as ``"0x1a"`` into an int.

    Nodes normally return hex strings, but web3-style clients and some proxies
    return ints. Anything unusable yields ``default`` instead of raising: one
    malformed log must not kill the poll.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            if text[:2].lower() == "0x":
                return int(text[2:], 16) if len(text) > 2 else default
            return int(text, 10)
        except ValueError:
            return default
    return default


def _topic(topics: Sequence[Any], index: int) -> str | None:
    if index < len(topics):
        value = topics[index]
        if isinstance(value, str) and value:
            return value
    return None


def parse_log(
    raw: Any,
    *,
    recv_ts_ns: int | None = None,
    recv_mono_ns: int | None = None,
) -> dict[str, Any] | None:
    """One `eth_getLogs` / `logs` subscription object -> a `polygon_logs` row.

    Returns None when the object carries no block number, because a log that
    cannot be placed on the chain cannot be reasoned about.
    """
    if not isinstance(raw, dict):
        return None
    block_number = hex_int(raw.get("blockNumber"))
    if block_number is None:
        return None
    topics = raw.get("topics")
    if not isinstance(topics, list):
        topics = []
    data = raw.get("data")
    return {
        "recv_ts_ns": recv_ts_ns,
        "recv_mono_ns": recv_mono_ns,
        "block_number": block_number,
        "block_hash": raw.get("blockHash") if isinstance(raw.get("blockHash"), str) else None,
        "tx_hash": (
            raw.get("transactionHash")
            if isinstance(raw.get("transactionHash"), str)
            else None
        ),
        "tx_index": hex_int(raw.get("transactionIndex")),
        "log_index": hex_int(raw.get("logIndex")),
        "address": raw.get("address") if isinstance(raw.get("address"), str) else None,
        "topic0": _topic(topics, 0),
        "topic1": _topic(topics, 1),
        "topic2": _topic(topics, 2),
        "topic3": _topic(topics, 3),
        "data": data if isinstance(data, str) else None,
        "removed": bool(raw.get("removed", False)),
    }


def ancillary_title(text: Any) -> str | None:
    """The market question from Polymarket's UMA ancillary text, if present."""
    if not isinstance(text, str):
        return None
    match = _TITLE_RE.match(text)
    if not match:
        return None
    title = match.group(1).strip()
    return title or None


def decode_log(
    row: Mapping[str, Any], *, source_part: str | None = None
) -> dict[str, Any] | None:
    """One `polygon_logs` row -> one canonical UMA event row, or ``None``.

    ``None`` means "this log is not a UMA/adapter event this module knows", not
    "malformed". A log carrying a known topic0 from an unexpected address is
    also rejected: the ABI is per contract, and guessing would mis-decode a
    same-signature event from a different deployment.
    """
    address = str(row.get("address") or "").strip().lower()
    topic0 = str(row.get("topic0") or "").strip().lower()
    contract = CONTRACT_BY_ADDRESS.get(address)
    if contract is None or not topic0:
        return None
    spec = SPEC_BY_CONTRACT_TOPIC.get((contract, topic0))
    if spec is None:
        return None

    topics = (row.get("topic1"), row.get("topic2"), row.get("topic3"))
    try:
        indexed = decode_indexed([t for _f, t in spec.indexed], topics)
        values = decode_values([t for _f, t in spec.data], str(row.get("data") or "0x"))
    except (ValueError, KeyError):
        return None

    fields = dict(zip([f for f, _t in spec.indexed], indexed))
    fields.update(zip([f for f, _t in spec.data], values))

    out = empty_row()
    out.update(
        source_part=source_part,
        contract=contract,
        event=spec.name,
        address=address,
        block_number=row.get("block_number"),
        block_hash=row.get("block_hash"),
        tx_hash=row.get("tx_hash"),
        tx_index=row.get("tx_index"),
        log_index=row.get("log_index"),
        recv_ts_ns=row.get("recv_ts_ns"),
        removed=row.get("removed"),
        identifier=fields.get("identifier"),
        identifier_text=_bytes32_text(fields.get("identifier")),
        requester=fields.get("requester") or fields.get("requestor") or fields.get("creator"),
        proposer=fields.get("proposer"),
        disputer=fields.get("disputer"),
        currency=fields.get("currency") or fields.get("rewardToken"),
        reward=_price(fields.get("reward")),
        final_fee=_price(fields.get("finalFee")),
        proposal_bond=_price(fields.get("proposalBond")),
        proposed_price=_price(fields.get("proposedPrice")),
        settlement_price=_price(
            fields.get("settlementPrice")
            if fields.get("settlementPrice") is not None
            else fields.get("settledPrice")
        ),
        expiry=fields.get("expirationTimestamp"),
        request_timestamp=fields.get("requestTimestamp")
        if fields.get("requestTimestamp") is not None
        else fields.get("timestamp"),
        resolution_time=fields.get("resolutionTime"),
        payouts=_payouts(fields.get("payouts")),
        early_resolution=fields.get("earlyResolutionEnabled")
        if fields.get("earlyResolutionEnabled") is not None
        else fields.get("earlyResolution"),
        emergency_resolution=fields.get("emergencyReport"),
    )

    ancillary = fields.get("ancillaryData", fields.get("update"))
    if ancillary is not None:
        out["ancillary_data"] = "0x" + bytes(ancillary).hex()
        out["ancillary_text"] = _text(bytes(ancillary))
    if fields.get("questionID") is not None:
        out["question_id"] = fields["questionID"]
    elif ancillary is not None:
        # UmaCtfAdapter: questionID = keccak256(ancillaryData). For OO logs this
        # is the join key back to QuestionInitialized/QuestionResolved.
        out["question_id"] = keccak256_hex(bytes(ancillary))

    if spec.name == "DisputePrice":
        out["disputed"] = True
    elif spec.name == "ProposePrice":
        out["disputed"] = False
    elif spec.name == "Settle":
        disputer = out["disputer"] or ZERO_ADDRESS
        out["disputed"] = disputer.lower() != ZERO_ADDRESS
    # RequestPrice and adapter lifecycle events carry no dispute flag. Leaving
    # it NULL is the honest encoding: settlement can still be disputed later.
    return out
