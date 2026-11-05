"""Unit tests for the raw CLOB tape stream.

Every test feeds the parser fixture dicts; nothing here opens a socket. The
point is to pin the shape of each event type and the shard bookkeeping, because
both are cheap to change and expensive to notice in a multi-day tape.
"""

from __future__ import annotations

import orjson
import pytest

from polymarketbot.data.streams.clob import (
    assign_shards,
    backoff_delay,
    decode_frames,
    gap_row,
    heartbeat_row,
    is_control_message,
    parse_frame,
    parse_levels,
    plan_universe_diff,
    subscribe_frame,
)

ASSET_A = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
ASSET_B = "52114319501245915516055106046884209969926127482827954674443846427813813222426"
MARKET = "0xabc0000000000000000000000000000000000000000000000000000000000001"
MARKET_B = "0xdef0000000000000000000000000000000000000000000000000000000000002"
A2M = {ASSET_A: MARKET, ASSET_B: MARKET_B}

TS = 1762300000000
RECV_TS = 1762300000123456789
RECV_MONO = 987654321


def _parse(frame, shard=0, asset_to_market=A2M):
    return parse_frame(
        frame,
        shard=shard,
        asset_to_market=asset_to_market,
        recv_ts_ns=RECV_TS,
        recv_mono_ns=RECV_MONO,
    )


# ------------------------------------------------------------------ shard layout
def test_assign_shards_round_robin_and_stable():
    assets = [f"a{i}" for i in range(10)]
    mapping = assign_shards(assets, 3)
    assert mapping == {a: i % 3 for i, a in enumerate(assets)}


def test_assign_shards_dedupes_and_ignores_empty():
    mapping = assign_shards(["a", "", "a", "b"], 2)
    assert mapping == {"a": 0, "b": 1}


def test_assign_shards_clamps_zero_shards():
    assert assign_shards(["a", "b"], 0) == {"a": 0, "b": 0}


def test_plan_universe_diff_routes_added_to_least_loaded():
    counts = [3, 1, 2]
    assignments, messages, new_counts = plan_universe_diff(
        ["new1", "new2"], [], {}, counts, 3
    )
    assert assignments == {"new1": 1, "new2": 1}
    assert new_counts == [3, 3, 2]
    assert messages == [
        (1, {"assets_ids": ["new1"], "operation": "subscribe"}),
        (1, {"assets_ids": ["new2"], "operation": "subscribe"}),
    ]
    # Input list is not mutated.
    assert counts == [3, 1, 2]


def test_plan_universe_diff_unsubscribes_removed_and_ignores_unknown():
    asset_shard = {"a": 0, "b": 2}
    counts = [1, 0, 1]
    assignments, messages, new_counts = plan_universe_diff(
        [], ["a", "untracked"], asset_shard, counts, 3
    )
    assert assignments == {}
    assert messages == [(0, {"assets_ids": ["a"], "operation": "unsubscribe"})]
    assert new_counts == [0, 0, 1]


def test_plan_universe_diff_add_remove_same_diff():
    asset_shard = {"old": 1}
    counts = [2, 1]
    assignments, messages, new_counts = plan_universe_diff(
        ["fresh"], ["old"], asset_shard, counts, 2
    )
    assert assignments == {"fresh": 1}
    assert messages[0] == (1, {"assets_ids": ["old"], "operation": "unsubscribe"})
    assert messages[1] == (1, {"assets_ids": ["fresh"], "operation": "subscribe"})
    assert new_counts == [2, 1]


# ----------------------------------------------------------------- frame decoding
def test_decode_single_dict():
    frames = decode_frames({"event_type": "book", "asset_id": ASSET_A})
    assert frames == [{"event_type": "book", "asset_id": ASSET_A}]


def test_decode_bytes_and_list():
    raw = orjson.dumps([{"event_type": "book"}, {"event_type": "price_change"}])
    assert [f["event_type"] for f in decode_frames(raw)] == ["book", "price_change"]


def test_decode_unwraps_payload_envelope():
    message = {"topic": "market", "payload": [{"event_type": "book"}, {"event_type": "last_trade_price"}]}
    assert [f["event_type"] for f in decode_frames(orjson.dumps(message))] == ["book", "last_trade_price"]


def test_decode_does_not_unwrap_event_with_payload_field():
    frame = {"event_type": "new_market", "payload": {"x": 1}, "assets_ids": [ASSET_A]}
    frames = decode_frames(frame)
    assert len(frames) == 1
    assert frames[0]["event_type"] == "new_market"


def test_decode_rejects_garbage():
    with pytest.raises(Exception):
        decode_frames("not json")


def test_control_messages():
    assert is_control_message("PONG")
    assert is_control_message(" ping\n")
    assert not is_control_message("PINGED")
    assert not is_control_message(b"PONG")


# ------------------------------------------------------------------- book parsing
def test_book_levels_keep_venue_order_and_floats():
    frame = {
        "event_type": "book",
        "asset_id": ASSET_A,
        "market": MARKET,
        "timestamp": str(TS),
        "hash": "0xfeed",
        "bids": [{"price": "0.41", "size": "120"}, {"price": "0.40", "size": "75"}],
        "asks": [{"price": "0.43", "size": "10"}],
    }
    rows = _parse(frame, shard=3)
    assert len(rows) == 1
    table, row = rows[0]
    assert table == "clob_events"
    assert row["event_type"] == "book"
    assert row["shard"] == 3
    assert row["asset_id"] == ASSET_A
    assert row["condition_id"] == MARKET
    assert row["venue_ts_ms"] == TS
    assert row["event_hash"] == "0xfeed"
    assert row["recv_ts_ns"] == RECV_TS
    assert row["recv_mono_ns"] == RECV_MONO
    assert row["bids"] == [
        {"price": 0.41, "size": 120.0},
        {"price": 0.40, "size": 75.0},
    ]
    assert row["asks"] == [{"price": 0.43, "size": 10.0}]


def test_parse_levels_skips_malformed_entries():
    levels = parse_levels(
        [{"price": "0.5", "size": "1"}, {"price": None, "size": "2"}, "junk", ["0.4", "9"]]
    )
    assert levels == [{"price": 0.5, "size": 1.0}, {"price": 0.4, "size": 9.0}]
    assert parse_levels(None) == []


def test_book_condition_falls_back_to_universe():
    frame = {"event_type": "book", "asset_id": ASSET_B, "bids": [], "asks": []}
    _, row = _parse(frame)[0]
    assert row["condition_id"] == MARKET_B


# --------------------------------------------------------------- price changes
def test_price_change_one_row_with_change_list():
    frame = {
        "event_type": "price_change",
        "market": MARKET,
        "timestamp": str(TS),
        "hash": "0xhash",
        "price_changes": [
            {"asset_id": ASSET_A, "price": "0.42", "size": "0", "side": "BUY", "best_bid": "0.41", "best_ask": "0.43"},
            {"asset_id": ASSET_A, "price": "0.44", "size": "250", "side": "SELL", "best_bid": "0.41", "best_ask": "0.44"},
        ],
    }
    rows = _parse(frame)
    assert len(rows) == 1
    table, row = rows[0]
    assert table == "clob_events"
    assert row["event_type"] == "price_change"
    assert row["asset_id"] == ASSET_A
    assert row["condition_id"] == MARKET
    assert row["event_hash"] == "0xhash"
    assert row["price_changes"] == [
        {"asset_id": ASSET_A, "price": 0.42, "size": 0.0, "side": "BUY", "best_bid": 0.41, "best_ask": 0.43},
        {"asset_id": ASSET_A, "price": 0.44, "size": 250.0, "side": "SELL", "best_bid": 0.41, "best_ask": 0.44},
    ]


def test_price_change_mixed_assets_keeps_nested_identity():
    frame = {
        "event_type": "price_change",
        "market": MARKET,
        "timestamp": str(TS),
        "price_changes": [
            {"asset_id": ASSET_A, "price": "0.42", "size": "100", "side": "BUY", "best_bid": "0.41", "best_ask": "0.43"},
            {"asset_id": ASSET_B, "price": "0.58", "size": "100", "side": "SELL", "best_bid": "0.57", "best_ask": "0.59"},
        ],
    }
    _, row = _parse(frame)[0]
    assert row["asset_id"] is None
    assert row["condition_id"] is None
    assert [c["asset_id"] for c in row["price_changes"]] == [ASSET_A, ASSET_B]


# ------------------------------------------------------------------- trade tape
def test_last_trade_price_goes_only_to_trades():
    frame = {
        "event_type": "last_trade_price",
        "asset_id": ASSET_A,
        "market": MARKET,
        "price": "0.47",
        "size": "35.5",
        "side": "BUY",
        "fee_rate_bps": "0",
        "transaction_hash": "0xdead",
        "timestamp": str(TS),
    }
    rows = _parse(frame, shard=5)
    assert [t for t, _ in rows] == ["clob_trades"]
    row = rows[0][1]
    assert row["price"] == 0.47
    assert row["size"] == 35.5
    assert row["side"] == "BUY"
    assert row["fee_rate_bps"] == 0.0
    assert row["transaction_hash"] == "0xdead"
    assert row["condition_id"] == MARKET
    assert row["shard"] == 5
    assert row["venue_ts_ms"] == TS
    assert orjson.loads(row["raw_json"])["asset_id"] == ASSET_A


def test_trade_type_parses_as_trade():
    frame = {
        "event_type": "trade",
        "market": MARKET_B,
        "asset_id": ASSET_B,
        "price": "0.12",
        "size": "10",
        "side": "SELL",
        "timestamp": TS,
    }
    table, row = _parse(frame)[0]
    assert table == "clob_trades"
    assert row["condition_id"] == MARKET_B
    assert row["asset_id"] == ASSET_B


# ------------------------------------------------------------- other event types
def test_tick_size_change():
    frame = {
        "event_type": "tick_size_change",
        "asset_id": ASSET_A,
        "old_tick_size": "0.01",
        "new_tick_size": "0.001",
        "timestamp": str(TS),
    }
    _, row = _parse(frame)[0]
    assert row["event_type"] == "tick_size_change"
    assert row["old_tick_size"] == 0.01
    assert row["new_tick_size"] == 0.001


def test_best_bid_ask_computes_spread():
    frame = {"event_type": "best_bid_ask", "asset_id": ASSET_A, "best_bid": "0.40", "best_ask": "0.44"}
    _, row = _parse(frame)[0]
    assert row["best_bid"] == 0.40
    assert row["best_ask"] == 0.44
    assert row["spread"] == pytest.approx(0.04)


def test_best_bid_ask_keeps_venue_spread():
    frame = {"event_type": "best_bid_ask", "asset_id": ASSET_A, "best_bid": "0.40", "best_ask": "0.44", "spread": "0.039"}
    _, row = _parse(frame)[0]
    assert row["spread"] == 0.039


def test_new_market_keeps_raw_frame_and_asset():
    frame = {
        "event_type": "new_market",
        "assets_ids": [ASSET_A, ASSET_B],
        "question": "Will it rain?",
        "timestamp": str(TS),
    }
    _, row = _parse(frame)[0]
    assert row["event_type"] == "new_market"
    assert row["asset_id"] == ASSET_A
    assert row["condition_id"] == MARKET
    assert orjson.loads(row["raw_json"])["question"] == "Will it rain?"


def test_market_resolved_keeps_raw_frame():
    frame = {"event_type": "market_resolved", "market": MARKET, "assets_ids": [ASSET_B]}
    _, row = _parse(frame)[0]
    assert row["event_type"] == "market_resolved"
    assert row["asset_id"] == ASSET_B
    assert row["condition_id"] == MARKET
    assert orjson.loads(row["raw_json"])["market"] == MARKET


def test_unknown_event_type_is_taped_not_dropped():
    frame = {"event_type": "brand_new_thing", "asset_id": ASSET_A, "value": 7}
    _, row = _parse(frame)[0]
    assert row["event_type"] == "brand_new_thing"
    assert orjson.loads(row["raw_json"])["value"] == 7


def test_missing_event_type_is_labelled_unknown():
    _, row = _parse({"asset_id": ASSET_A})[0]
    assert row["event_type"] == "unknown"


# --------------------------------------------------------------- tape landmarks
def test_gap_row_shape():
    table, row = gap_row(
        shard=2,
        gap_start_ns=100,
        gap_end_ns=250,
        reason="ConnectionClosed: 1006",
        recv_ts_ns=RECV_TS,
        recv_mono_ns=RECV_MONO,
    )
    assert table == "clob_events"
    assert row["event_type"] == "gap"
    assert row["shard"] == 2
    assert orjson.loads(row["raw_json"]) == {
        "gap_start_ns": 100,
        "gap_end_ns": 250,
        "reason": "ConnectionClosed: 1006",
    }


def test_heartbeat_row_shape():
    table, row = heartbeat_row(
        shard=1, last_msg_age_s=61.5, assets=42, recv_ts_ns=RECV_TS, recv_mono_ns=RECV_MONO
    )
    assert table == "clob_events"
    assert row["event_type"] == "heartbeat"
    assert row["asset_id"] is None
    assert orjson.loads(row["raw_json"]) == {"last_msg_age_s": 61.5, "assets": 42}


def test_subscribe_frame():
    frame = subscribe_frame([ASSET_B, ASSET_A], initial_dump=True)
    assert frame["assets_ids"] == sorted([ASSET_A, ASSET_B])
    assert frame["type"] == "market"
    assert frame["initial_dump"] is True
    assert frame["custom_feature_enabled"] is True


def test_backoff_is_bounded_and_jittered():
    assert backoff_delay(0, cap=30.0, rng=lambda: 0.0) == 0.5
    assert backoff_delay(0, cap=30.0, rng=lambda: 1.0) == 1.0
    assert backoff_delay(3, cap=30.0, rng=lambda: 0.0) == 4.0
    assert backoff_delay(3, cap=30.0, rng=lambda: 1.0) == 8.0
    assert backoff_delay(6, cap=30.0, rng=lambda: 0.0) == 30.0
    assert backoff_delay(10, cap=30.0, rng=lambda: 1.0) == 30.0
    assert backoff_delay(-3, cap=30.0, rng=lambda: 1.0) == 1.0


# ----------------------------------------------------------------- integration
def test_full_message_round_trip():
    """A realistic mixed message decodes and splits across both tables."""
    payload = [
        {"event_type": "book", "asset_id": ASSET_A, "bids": [{"price": "0.4", "size": "5"}], "asks": []},
        {"event_type": "last_trade_price", "asset_id": ASSET_B, "price": "0.6", "size": "3", "side": "SELL"},
        {"event_type": "price_change", "market": MARKET, "price_changes": [{"asset_id": ASSET_A, "price": "0.4", "size": "7", "side": "BUY"}]},
    ]
    frames = decode_frames(orjson.dumps({"payload": payload}))
    rows = [r for frame in frames for r in _parse(frame)]
    tables = [t for t, _ in rows]
    assert tables == ["clob_events", "clob_trades", "clob_events"]
    assert rows[0][1]["event_type"] == "book"
    assert rows[2][1]["event_type"] == "price_change"
