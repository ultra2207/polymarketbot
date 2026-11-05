"""Queue reconstruction tests on synthetic book / delta / trade sequences.

Every case is a hand-built sequence, so the expected ``q_ahead`` / ``q_behind``
and the cancel-vs-fill split are arithmetic, not a re-derivation of the code.
"""

from __future__ import annotations

import pyarrow.parquet as pq

from polymarketbot.data.l3 import (
    BOOK_SIDE_ASK,
    BOOK_SIDE_BID,
    L3_QUEUE_SCHEMA,
    L3Replayer,
    L3Session,
    book_side,
    norm_price,
    write_rows,
)

ASSET = "11111111111111111111111111111111111111111111111111111111111111111111111111"
MIRROR = "22222222222222222222222222222222222222222222222222222222222222222222222222"

T0 = 1_700_000_000_000_000_000
MS = 1_000_000


def levels(*pairs: tuple[float, float]) -> list[dict[str, float]]:
    return [{"price": price, "size": size} for price, size in pairs]


def book(replayer: L3Replayer, *, bids, asks, ts: int = T0) -> list[dict]:
    return replayer.on_book(asset_id=ASSET, recv_ts_ns=ts, bids=bids, asks=asks, shard=0)


def change(
    replayer: L3Replayer, *, side: str, price: float, size: float, ts: int
) -> list[dict]:
    return replayer.on_price_change(
        asset_id=ASSET, recv_ts_ns=ts, side=side, price=price, size=size, shard=0
    )


# ------------------------------------------------------------------ the anchor
def test_anchor_takes_queue_size_from_the_book_snapshot():
    r = L3Replayer()
    rows = book(r, bids=levels((0.40, 100.0), (0.39, 250.0)), asks=levels((0.41, 60.0)))

    assert [row["side"] for row in rows] == [BOOK_SIDE_BID, BOOK_SIDE_BID, BOOK_SIDE_ASK]
    assert [row["price"] for row in rows] == [0.40, 0.39, 0.41]
    assert [row["size_at_level"] for row in rows] == [100.0, 250.0, 60.0]
    # Everybody resting at the level is ahead of an order joining at the back.
    assert [row["q_ahead"] for row in rows] == [100.0, 250.0, 60.0]
    assert all(row["q_behind"] == 0.0 for row in rows)
    assert all(row["queue_ratio"] == 1.0 for row in rows)
    assert all(row["survival"] == 1 for row in rows)
    assert all(row["attribution"] == "anchor" for row in rows)
    assert all(row["discontinuity"] is False for row in rows)
    assert r.counters.books == 1
    assert r.counters.book_levels == 3


def test_zero_size_levels_in_a_snapshot_are_not_levels():
    r = L3Replayer()
    rows = book(r, bids=levels((0.40, 100.0), (0.39, 0.0)), asks=levels((0.41, 60.0)))
    assert [row["price"] for row in rows] == [0.40, 0.41]


# -------------------------------------------------------------- queue movement
def test_increase_lands_behind_and_decrease_eats_ahead_first():
    r = L3Replayer()
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)))

    # Someone joins at our level: they are behind us.
    rows = change(r, side="BUY", price=0.40, size=130.0, ts=T0 + 10 * MS)
    assert len(rows) == 1
    row = rows[0]
    assert row["size_at_level"] == 130.0
    assert row["q_ahead"] == 100.0
    assert row["q_behind"] == 30.0
    assert row["queue_ratio"] == 100.0 / 130.0
    assert row["attribution"] == "increase"
    assert row["survival"] == 1

    # 70 leaves the level with no print to explain it: 100 ahead -> 30, the
    # 30 behind is untouched because FIFO eats the front first.
    rows = change(r, side="BUY", price=0.40, size=60.0, ts=T0 + 20 * MS)
    row = rows[0]
    assert row["cancel_size"] == 70.0
    assert row["fill_size"] == 0.0
    assert row["attribution"] == "cancel"
    assert row["q_ahead"] == 30.0
    assert row["q_behind"] == 30.0
    assert row["queue_ratio"] == 0.5
    assert row["survival"] == 1
    # The shadow order joined at the anchor, so it is 20 ms old here.
    assert row["queue_age_ns"] == 20 * MS


def test_duplicate_frame_with_unchanged_size_is_a_noop():
    r = L3Replayer()
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)))
    rows = change(r, side="BUY", price=0.40, size=100.0, ts=T0 + 5 * MS)
    assert rows == []
    assert r.counters.noop_changes == 1


def test_replenishment_latency_is_measured_from_the_depletion():
    r = L3Replayer()
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)))
    change(r, side="BUY", price=0.40, size=80.0, ts=T0 + 10 * MS)
    rows = change(r, side="BUY", price=0.40, size=95.0, ts=T0 + 35 * MS)
    row = rows[0]
    assert row["replenishment_latency"] == 25 * MS
    assert row["attribution"] == "replenish"
    assert row["q_ahead"] == 80.0
    assert row["q_behind"] == 15.0


# ------------------------------------------------------- cancel/fill attribution
def test_decrease_without_a_print_is_a_cancel():
    r = L3Replayer()
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)))
    rows = change(r, side="BUY", price=0.40, size=40.0, ts=T0 + 10 * MS)
    row = rows[0]
    assert row["attribution"] == "cancel"
    assert row["cancel_size"] == 60.0
    assert row["fill_size"] == 0.0
    assert row["attribution_ambiguous"] is False
    assert r.counters.cancels == 1


def test_print_at_the_level_makes_the_decrease_a_fill():
    r = L3Replayer()
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)))
    # Taker SELL hits the bid at 0.40.
    r.on_trade(asset_id=ASSET, recv_ts_ns=T0 + 9 * MS, side="SELL", price=0.40, size=45.0)
    row = change(r, side="BUY", price=0.40, size=55.0, ts=T0 + 10 * MS)[0]
    assert row["attribution"] == "fill"
    assert row["fill_size"] == 45.0
    assert row["cancel_size"] == 0.0
    assert row["attribution_ambiguous"] is False
    assert r.counters.fills == 1
    assert r.counters.fill_volume == 45.0


def test_print_on_the_other_book_side_does_not_explain_a_bid_decrease():
    r = L3Replayer()
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)))
    # A taker BUY consumes asks; it cannot explain a bid level shrinking.
    r.on_trade(asset_id=ASSET, recv_ts_ns=T0 + 9 * MS, side="BUY", price=0.40, size=45.0)
    row = change(r, side="BUY", price=0.40, size=55.0, ts=T0 + 10 * MS)[0]
    assert row["attribution"] == "cancel"
    assert row["cancel_size"] == 45.0


def test_partial_print_coverage_is_flagged_ambiguous():
    r = L3Replayer()
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)))
    r.on_trade(asset_id=ASSET, recv_ts_ns=T0 + 9 * MS, side="SELL", price=0.40, size=20.0)
    row = change(r, side="BUY", price=0.40, size=50.0, ts=T0 + 10 * MS)[0]
    # 50 left the level, a 20 print explains part of it: the other 30 could be a
    # cancel or an unrecorded fill, so the row says so instead of guessing.
    assert row["attribution"] == "ambiguous"
    assert row["attribution_ambiguous"] is True
    assert row["fill_size"] == 20.0
    assert row["cancel_size"] == 30.0
    assert r.counters.ambiguous == 1


def test_print_outside_the_match_window_is_not_claimed():
    r = L3Replayer(match_window_ns=50 * MS)
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)))
    r.on_trade(asset_id=ASSET, recv_ts_ns=T0 + 1 * MS, side="SELL", price=0.40, size=45.0)
    row = change(r, side="BUY", price=0.40, size=55.0, ts=T0 + 500 * MS)[0]
    assert row["attribution"] == "cancel"


def test_a_print_cannot_explain_a_level_at_another_price():
    r = L3Replayer()
    book(r, bids=levels((0.40, 100.0), (0.39, 80.0)), asks=levels((0.41, 60.0)))
    r.on_trade(asset_id=ASSET, recv_ts_ns=T0 + 9 * MS, side="SELL", price=0.39, size=80.0)
    row = change(r, side="BUY", price=0.40, size=90.0, ts=T0 + 10 * MS)[0]
    assert row["attribution"] == "cancel"
    assert row["cancel_size"] == 10.0


def test_trade_volume_is_consumed_once():
    r = L3Replayer()
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)))
    r.on_trade(asset_id=ASSET, recv_ts_ns=T0 + 9 * MS, side="SELL", price=0.40, size=30.0)
    first = change(r, side="BUY", price=0.40, size=70.0, ts=T0 + 10 * MS)[0]
    second = change(r, side="BUY", price=0.40, size=40.0, ts=T0 + 11 * MS)[0]
    assert (first["fill_size"], first["cancel_size"]) == (30.0, 0.0)
    assert (second["fill_size"], second["cancel_size"]) == (0.0, 30.0)


# ----------------------------------------------------------- survival / rejoin
def test_filling_past_the_queue_ahead_kills_then_rejoins_the_order():
    r = L3Replayer()
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)))
    # 50 joins behind us: ahead 100, behind 50.
    change(r, side="BUY", price=0.40, size=150.0, ts=T0 + 5 * MS)

    # 120 leaves the level with a 120 print behind it: our 100-ahead and our own
    # order are consumed, 30 of the 50 behind survives.
    r.on_trade(asset_id=ASSET, recv_ts_ns=T0 + 9 * MS, side="SELL", price=0.40, size=120.0)
    row = change(r, side="BUY", price=0.40, size=30.0, ts=T0 + 10 * MS)[0]
    assert row["fill_size"] == 120.0
    assert row["cancel_size"] == 0.0
    assert row["attribution"] == "fill"
    assert row["survival"] == 0
    assert row["q_ahead"] == 0.0
    assert row["q_behind"] == 30.0

    # The maker is quoting again: the remaining 30 is now ahead of it.
    rows = change(r, side="BUY", price=0.40, size=35.0, ts=T0 + 20 * MS)
    assert rows[0]["q_ahead"] == 30.0
    assert rows[0]["q_behind"] == 5.0
    assert rows[0]["queue_age_ns"] == 10 * MS
    assert rows[0]["survival"] == 1


def test_emptied_level_drops_the_order_and_forgets_the_level():
    r = L3Replayer()
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)))
    row = change(r, side="BUY", price=0.40, size=0.0, ts=T0 + 10 * MS)[0]
    assert row["size_at_level"] == 0.0
    assert row["survival"] == 0
    assert row["cancel_size"] == 100.0
    state = r.state(ASSET)
    assert (BOOK_SIDE_BID, 0.4) not in state.levels

    # A level that comes back is a fresh join at the back.
    rows = change(r, side="BUY", price=0.40, size=12.0, ts=T0 + 30 * MS)
    assert rows[0]["q_ahead"] == 12.0
    assert rows[0]["q_behind"] == 0.0
    assert rows[0]["queue_age_ns"] == 0


# ---------------------------------------------------------------- discontinuities
def test_gap_unanchors_until_the_next_book_and_flags_it():
    r = L3Replayer()
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)), ts=T0)
    r.add_gap(shard=0, recv_ts_ns=T0 + 5 * MS)

    assert change(r, side="BUY", price=0.40, size=90.0, ts=T0 + 10 * MS) == []
    assert r.counters.dropped_changes == 1
    assert r.counters.gap_resets == 1

    rows = book(r, bids=levels((0.40, 70.0)), asks=levels((0.41, 60.0)), ts=T0 + 20 * MS)
    assert rows and all(row["discontinuity"] is True for row in rows)
    assert rows[0]["q_ahead"] == 70.0
    assert change(r, side="BUY", price=0.40, size=65.0, ts=T0 + 21 * MS)[0]["discontinuity"] is False


def test_a_fresh_book_reanchors_and_restarts_the_queue():
    r = L3Replayer()
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)), ts=T0)
    rows = book(r, bids=levels((0.40, 55.0)), asks=levels((0.41, 60.0)), ts=T0 + 10 * MS)
    assert len(rows) == 1  # the unchanged ask level is not restated
    row = rows[0]
    assert row["discontinuity"] is True
    assert row["price"] == 0.4
    assert row["q_ahead"] == 55.0
    assert row["q_behind"] == 0.0
    assert row["queue_age_ns"] == 0
    assert r.counters.book_unchanged == 1


def test_an_unchanged_reanchor_emits_nothing_and_is_not_a_discontinuity():
    r = L3Replayer()
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)), ts=T0)
    assert book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)), ts=T0 + 10 * MS) == []
    assert r.counters.anchor_mismatches == 0
    # The next delta is still continuous.
    row = change(r, side="BUY", price=0.40, size=90.0, ts=T0 + 20 * MS)[0]
    assert row["discontinuity"] is False


def test_a_level_missing_from_the_new_snapshot_is_reported_as_removed():
    r = L3Replayer()
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)), ts=T0)
    rows = book(r, bids=levels((0.40, 100.0)), asks=None, ts=T0 + 10 * MS)
    assert len(rows) == 1
    row = rows[0]
    assert (row["side"], row["price"]) == (BOOK_SIDE_ASK, 0.41)
    assert row["size_at_level"] == 0.0
    assert row["survival"] == 0
    assert row["discontinuity"] is True


def test_reanchoring_counts_levels_that_disagree_with_the_snapshot():
    r = L3Replayer()
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)), ts=T0)
    # 20 leaves the bid without any print; the next book should still say 80.
    change(r, side="BUY", price=0.40, size=80.0, ts=T0 + 10 * MS)
    book(r, bids=levels((0.40, 80.0)), asks=levels((0.41, 60.0)), ts=T0 + 20 * MS)
    assert r.counters.anchor_checks == 2
    assert r.counters.anchor_mismatches == 0

    book(r, bids=levels((0.40, 70.0)), asks=levels((0.41, 60.0)), ts=T0 + 30 * MS)
    assert r.counters.anchor_mismatches == 1


def test_a_gap_does_not_touch_another_shards_assets():
    r = L3Replayer()
    r.on_book(asset_id=MIRROR, recv_ts_ns=T0, bids=levels((0.5, 10.0)), asks=None, shard=1)
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)), ts=T0)
    r.add_gap(shard=1, recv_ts_ns=T0 + 5 * MS)

    row = change(r, side="BUY", price=0.40, size=90.0, ts=T0 + 10 * MS)[0]
    assert row["cancel_size"] == 10.0
    assert r.counters.gap_resets == 0

    # The gap belongs to shard 1 only, and MIRROR is the asset that notices.
    assert r.on_price_change(
        asset_id=MIRROR, recv_ts_ns=T0 + 11 * MS, side="BUY", price=0.5, size=5.0, shard=1
    ) == []
    assert r.counters.gap_resets == 1
    assert r.counters.dropped_changes == 1


# ---------------------------------------------------------------------- parsing
def test_sides_and_prices_are_normalised():
    assert book_side("BUY") == BOOK_SIDE_BID
    assert book_side("sell") == BOOK_SIDE_ASK
    assert book_side("weird") is None
    assert norm_price(0.59000000001) == 0.59
    assert norm_price(None) is None


def test_changes_for_the_mirror_token_are_ignored():
    r = L3Replayer()
    book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)))
    rows = r.on_changes(
        asset_id=ASSET,
        recv_ts_ns=T0 + 10 * MS,
        changes=[
            {"asset_id": ASSET, "side": "BUY", "price": 0.40, "size": 90.0},
            {"asset_id": MIRROR, "side": "SELL", "price": 0.60, "size": 90.0},
        ],
        shard=0,
    )
    assert len(rows) == 1
    assert rows[0]["asset_id"] == ASSET
    assert rows[0]["cancel_size"] == 10.0


# ---------------------------------------------------------------------- session
def test_session_matches_a_print_pushed_after_the_change():
    session = L3Session()
    rows = list(
        session.feed(
            [
                {"kind": "book", "asset_id": ASSET, "recv_ts_ns": T0,
                 "bids": levels((0.40, 100.0)), "asks": levels((0.41, 60.0)), "shard": 0},
                {"kind": "price_change", "asset_id": ASSET, "recv_ts_ns": T0 + 10 * MS,
                 "changes": [{"asset_id": ASSET, "side": "BUY", "price": 0.40, "size": 55.0}],
                 "shard": 0},
                {"kind": "trade", "asset_id": ASSET, "recv_ts_ns": T0 + 12 * MS,
                 "side": "SELL", "price": 0.40, "size": 45.0},
            ]
        )
    )
    fills = [row for row in rows if row["attribution"] in ("fill", "ambiguous")]
    assert len(fills) == 1
    assert fills[0]["attribution"] == "fill"
    assert fills[0]["fill_size"] == 45.0
    assert fills[0]["cancel_size"] == 0.0


def test_session_processes_rows_in_time_order_per_asset():
    session = L3Session(lookahead_ns=0)
    rows = list(
        session.feed(
            [
                {"kind": "book", "asset_id": ASSET, "recv_ts_ns": T0,
                 "bids": levels((0.40, 100.0)), "asks": None, "shard": 0},
                {"kind": "trade", "asset_id": ASSET, "recv_ts_ns": T0 + 5 * MS,
                 "side": "SELL", "price": 0.40, "size": 40.0},
                {"kind": "price_change", "asset_id": ASSET, "recv_ts_ns": T0 + 10 * MS,
                 "changes": [{"asset_id": ASSET, "side": "BUY", "price": 0.40, "size": 60.0}],
                 "shard": 0},
            ]
        )
    )
    timestamps = [row["recv_ts_ns"] for row in rows]
    assert timestamps == sorted(timestamps)
    assert rows[-1]["fill_size"] == 40.0


# ----------------------------------------------------------------------- output
def test_rows_round_trip_through_parquet(tmp_path):
    r = L3Replayer()
    rows = book(r, bids=levels((0.40, 100.0)), asks=levels((0.41, 60.0)))
    rows += change(r, side="SELL", price=0.41, size=45.0, ts=T0 + 10 * MS)

    path = tmp_path / "asset-hour.parquet"
    assert write_rows(path, rows) == len(rows)

    table = pq.read_table(path)
    assert table.schema.equals(L3_QUEUE_SCHEMA)
    stored = table.to_pylist()
    assert stored[0]["asset_id"] == ASSET
    assert stored[-1]["attribution"] == "cancel"
    assert stored[-1]["cancel_size"] == 15.0
