"""Pure-function tests for scripts/scan_toxicity.py.

The scan itself reads a 45 GB tape, but every judgement it makes -- which side a
fill was on, whether a quote is fresh enough to count, whether a bucket crosses
a threshold -- lives in a helper that takes plain numbers. Those helpers are
tested here, plus one tiny in-memory fixture that walks trades and quote
changes through the aggregation. No tape, no network.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SECOND = 1_000_000_000
MINUTE = 60 * SECOND


@pytest.fixture(scope="module")
def tox():
    path = Path(__file__).resolve().parent.parent / "scripts" / "scan_toxicity.py"
    spec = importlib.util.spec_from_file_location("scan_toxicity_pure", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# -------------------------------------------------------------------- bucketing
def test_bucket_start_floors_to_five_minute_grid(tox):
    assert tox.bucket_start_ns(0) == 0
    assert tox.bucket_start_ns(300 * SECOND - 1) == 0
    assert tox.bucket_start_ns(300 * SECOND) == 300 * SECOND
    assert tox.bucket_start_ns(1_000 * SECOND) == 900 * SECOND


def test_partition_predicate_groups_hours_per_date(tox):
    parts = [("2026-11-04", "23"), ("2026-11-05", "00"), ("2026-11-05", "01")]
    assert tox._partition_predicate(parts) == (
        "((date = '2026-11-04' AND hour IN ('23')) OR "
        "(date = '2026-11-05' AND hour IN ('00', '01')))"
    )
    assert tox._partition_predicate([]) == "(FALSE)"


# ----------------------------------------------------------------- signed move
def test_signed_move_is_taker_side_relative(tox):
    # BUY at 0.40 that moves the mid to 0.42: the taker gained 0.02.
    assert tox.signed_move("BUY", 0.40, 0.42) == pytest.approx(0.02)
    # The same mid move is a loss for a seller.
    assert tox.signed_move("SELL", 0.40, 0.42) == pytest.approx(-0.02)
    assert tox.signed_move("sell", 0.40, 0.38) == pytest.approx(0.02)
    assert tox.signed_move(" BUY ", 0.40, 0.40) == pytest.approx(0.0)


def test_signed_move_never_invents_a_zero(tox):
    assert tox.signed_move("BUY", None, 0.5) is None
    assert tox.signed_move("BUY", 0.5, None) is None
    assert tox.signed_move(None, 0.5, 0.5) is None
    assert tox.signed_move("", 0.5, 0.5) is None


# ------------------------------------------------------------------- imbalance
def test_trade_imbalance_bounds(tox):
    assert tox.trade_imbalance(100.0, 100.0) == pytest.approx(0.0)
    assert tox.trade_imbalance(100.0, 0.0) == pytest.approx(1.0)
    assert tox.trade_imbalance(0.0, 100.0) == pytest.approx(-1.0)
    assert tox.trade_imbalance(300.0, 100.0) == pytest.approx(0.5)


def test_trade_imbalance_without_volume_is_missing_not_zero(tox):
    assert tox.trade_imbalance(0.0, 0.0) is None
    assert tox.trade_imbalance(None, None) is None


# -------------------------------------------------------------------------- OFI
def test_ofi_step_counts_bid_and_ask_separately(tox):
    assert tox.ofi_step(0.40, 0.42, 0.41, 0.42) == 1  # bid up: buyers stepping in
    assert tox.ofi_step(0.40, 0.42, 0.39, 0.42) == -1  # bid down
    assert tox.ofi_step(0.40, 0.42, 0.40, 0.41) == 1  # ask down: offers lifted
    assert tox.ofi_step(0.40, 0.42, 0.40, 0.43) == -1  # ask up
    assert tox.ofi_step(0.40, 0.42, 0.41, 0.43) == 0  # both sides up: mid flat
    assert tox.ofi_step(0.40, 0.42, 0.39, 0.41) == 0  # both sides down
    assert tox.ofi_step(0.40, 0.42, 0.42, 0.41) == 2  # both sides up: +1 and +1


def test_ofi_step_handles_missing_book(tox):
    assert tox.ofi_step(None, 0.42, 0.41, 0.42) is None
    assert tox.ofi_step(0.40, None, 0.41, 0.42) is None
    assert tox.ofi_step(0.40, 0.42, None, 0.42) is None


# ------------------------------------------------------------------ markout agg
def test_weighted_markout_weights_by_size(tox):
    moves = [(0.02, 100.0), (-0.02, 300.0)]
    value, size, n = tox.weighted_markout(moves)
    assert value == pytest.approx(-0.01)
    assert size == pytest.approx(400.0)
    assert n == 2


def test_weighted_markout_empty_stays_empty(tox):
    assert tox.weighted_markout([]) == (None, 0.0, 0)
    assert tox.weighted_markout([(None, 10.0)]) == (None, 0.0, 0)


def test_quantile_interpolates(tox):
    assert tox.quantile([0.0, 1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.0)
    assert tox.quantile([0.0, 10.0], 0.25) == pytest.approx(2.5)
    assert tox.quantile([], 0.95) is None


def test_ofi_tail_needs_a_distribution(tox):
    threshold, n = tox.ofi_tail_threshold([0.1, 0.2, 0.3], q=0.95, min_buckets=20)
    assert threshold is None and n == 3
    values = [(-0.5 if i % 2 else 0.5) for i in range(100)]
    threshold, n = tox.ofi_tail_threshold(values, q=0.5, min_buckets=20)
    assert n == 100
    assert threshold == pytest.approx(0.5)  # magnitudes are pooled, signs are not


# --------------------------------------------------------------------- fixture
def _trade(ts_ns, price, size, side, condition_id="0xabc", m30=None, m300=None):
    """``m30``/``m300`` are ``(quote_ts_ns, mid, spread)`` as the ASOF join emits."""
    return {
        "condition_id": condition_id,
        "asset_id": "yes",
        "recv_ts_ns": ts_ns,
        "price": price,
        "size": size,
        "side": side,
        "mids": {30: m30 or (None, None, None), 300: m300 or (None, None, None)},
    }


def test_fixture_aggregates_trades_per_condition_and_bucket(tox):
    base = 1_700_000_000 * SECOND  # aligned to no particular boundary
    bucket = tox.bucket_start_ns(base)
    inside = base + 10 * SECOND
    rows = [
        # BUY 100 @ 0.40, mid 30 s later 0.42 -> +0.02 per share
        _trade(inside, 0.40, 100.0, "BUY", m30=(inside + 30 * SECOND, 0.42, 0.02),
               m300=(inside + 300 * SECOND, 0.44, 0.02)),
        # SELL 300 @ 0.40, mid 30 s later 0.39 -> +0.01 per share
        _trade(inside + 31 * SECOND, 0.40, 300.0, "SELL", m30=(inside + 61 * SECOND, 0.39, 0.02),
               m300=(inside + 331 * SECOND, 0.41, 0.02)),
        # Second condition, second bucket: nothing to aggregate with the first.
        _trade(bucket + 400 * SECOND, 0.60, 50.0, "BUY", condition_id="0xdef",
               m30=(bucket + 430 * SECOND, 0.59, 0.02)),
    ]
    out = tox.aggregate_trades(rows, horizons=(30, 300))

    first = out[("0xabc", bucket)]
    assert first["trades"] == 2
    assert first["buy_volume"] == pytest.approx(100.0)
    assert first["sell_volume"] == pytest.approx(300.0)
    assert first["matched"][30] == 2
    assert first["matched"][300] == 2
    assert tox.quantile(first["spreads"][30], 0.5) == pytest.approx(0.02)
    markout_30s, size, n = tox.weighted_markout(first["moves"][30])
    assert (markout_30s, size, n) == (pytest.approx(0.0125), pytest.approx(400.0), 2)
    markout_5m, _, _ = tox.weighted_markout(first["moves"][300])
    # +0.04 * 100 shares for the BUY, -0.01 * 300 for the SELL.
    assert markout_5m == pytest.approx(0.0025)

    second = out[("0xdef", tox.bucket_start_ns(bucket + 400 * SECOND))]
    assert second["trades"] == 1
    assert second["matched"][30] == 1
    assert second["matched"][300] == 0  # no 5 m quote supplied: unmatched, not zero


def test_fixture_drops_quotes_outside_the_match_window(tox):
    ts = 1_700_000_000 * SECOND
    stale = ts + 30 * SECOND + 61 * SECOND  # 61 s past the 30 s horizon
    fresh = ts + 30 * SECOND + 59 * SECOND
    rows = [
        _trade(ts, 0.40, 10.0, "BUY", m30=(stale, 0.45, 0.02)),
        _trade(ts, 0.40, 10.0, "SELL", m30=(fresh, 0.45, 0.02)),
    ]
    cell = tox.aggregate_trades(rows, horizons=(30,))[(("0xabc"), tox.bucket_start_ns(ts))]
    assert cell["trades"] == 2
    assert cell["matched"][30] == 1
    markout, size, n = tox.weighted_markout(cell["moves"][30])
    assert n == 1 and size == pytest.approx(10.0)
    # The only fresh quote serves the SELL: the mid rose to 0.45, so the seller
    # paid 0.05 for the information.
    assert markout == pytest.approx(-0.05)


def test_fixture_ofi_from_consecutive_top_of_book_changes(tox):
    base = 1_700_000_000 * SECOND
    rows = [
        # before the window: supplies the predecessor, must not create a bucket
        {"condition_id": "0xabc", "asset_id": "yes", "recv_ts_ns": base - 10 * SECOND,
         "best_bid": 0.40, "best_ask": 0.42},
        {"condition_id": "0xabc", "asset_id": "yes", "recv_ts_ns": base + 1 * SECOND,
         "best_bid": 0.41, "best_ask": 0.42},
        {"condition_id": "0xabc", "asset_id": "yes", "recv_ts_ns": base + 2 * SECOND,
         "best_bid": 0.41, "best_ask": 0.43},
        {"condition_id": "0xabc", "asset_id": "yes", "recv_ts_ns": base + 3 * SECOND,
         "best_bid": 0.41, "best_ask": 0.41},
    ]
    out = tox.aggregate_ofi(rows, start_ns=base)
    cell = out[("0xabc", tox.bucket_start_ns(base))]
    assert cell["ofi_changes"] == 3
    # +1 for the bid tick up, -1 for the ask tick up, +1 for the ask tick back
    # down. The pre-window row supplies the predecessor but is not counted.
    assert cell["ofi_sum"] == 1


def test_fixture_bucket_without_trades_is_not_a_zero(tox):
    ofi_rows = [
        {"condition_id": "0xabc", "asset_id": "yes", "recv_ts_ns": 1_700_000_000 * SECOND,
         "best_bid": 0.40, "best_ask": 0.42},
        {"condition_id": "0xabc", "asset_id": "yes", "recv_ts_ns": 1_700_000_001 * SECOND,
         "best_bid": 0.41, "best_ask": 0.42},
    ]
    verdict = tox.classify_bucket(
        n_trades=0,
        matched={30: 0, 300: 0},
        markouts={30: None, 300: None},
        median_spread=0.02,
        ofi_per_change=1.0,
        ofi_changes=10,
        ofi_threshold=0.5,
    )
    assert verdict["status"] == "no_trades"
    assert verdict["markout_flags"]["markout_30s"] is False
    assert verdict["markout_flags"]["markout_300s"] is False
    # OFI comes from the book, not from trades, so book pressure still flags.
    assert verdict["flag_ofi"] is True
    assert verdict["flag_reason"] == "ofi"
    assert verdict["half_spread"] == pytest.approx(0.01)
    assert tox.aggregate_ofi(ofi_rows)  # the bucket still exists via quotes


# ----------------------------------------------------------------------- flags
def test_markout_flag_fires_only_above_half_the_median_spread(tox):
    common = dict(matched={30: 10, 300: 10}, ofi_per_change=None, ofi_changes=0,
                  ofi_threshold=None, median_spread=0.04)
    above = tox.classify_bucket(n_trades=10, markouts={30: 0.0201, 300: 0.001}, **common)
    equal = tox.classify_bucket(n_trades=10, markouts={30: 0.02, 300: 0.001}, **common)
    below = tox.classify_bucket(n_trades=10, markouts={30: 0.0199, 300: 0.001}, **common)
    assert above["markout_flags"]["markout_30s"] is True
    assert equal["markout_flags"]["markout_30s"] is False  # strict exceedance
    assert below["flagged"] is False
    assert above["flagged"] is True
    assert above["flag_reason"] == "markout_30s"


def test_markout_flag_ignores_the_side_that_helped_the_maker(tox):
    verdict = tox.classify_bucket(
        n_trades=10,
        matched={30: 10, 300: 10},
        markouts={30: -0.05, 300: -0.05},
        median_spread=0.02,
        ofi_per_change=None,
        ofi_changes=0,
        ofi_threshold=None,
    )
    assert verdict["status"] == "ok"
    assert verdict["flagged"] is False


def test_thin_buckets_never_flag(tox):
    verdict = tox.classify_bucket(
        n_trades=5,
        matched={30: 2, 300: 2},
        markouts={30: 0.5, 300: 0.5},
        median_spread=0.02,
        ofi_per_change=None,
        ofi_changes=0,
        ofi_threshold=None,
    )
    assert verdict["status"] == "thin"
    assert verdict["flagged"] is False


def test_unmatched_bucket_is_not_ok(tox):
    verdict = tox.classify_bucket(
        n_trades=4,
        matched={30: 0, 300: 0},
        markouts={30: None, 300: None},
        median_spread=0.02,
        ofi_per_change=None,
        ofi_changes=0,
        ofi_threshold=None,
    )
    assert verdict["status"] == "unmatched"
    assert verdict["flagged"] is False


def test_missing_median_spread_disables_the_markout_flag(tox):
    verdict = tox.classify_bucket(
        n_trades=10,
        matched={30: 10, 300: 10},
        markouts={30: 0.5, 300: 0.5},
        median_spread=None,
        ofi_per_change=None,
        ofi_changes=0,
        ofi_threshold=None,
    )
    assert verdict["half_spread"] is None
    assert verdict["flagged"] is False


def test_ofi_flag_needs_a_threshold_and_enough_changes(tox):
    kwargs = dict(
        n_trades=0,
        matched={30: 0, 300: 0},
        markouts={30: None, 300: None},
        median_spread=0.02,
    )
    no_threshold = tox.classify_bucket(ofi_per_change=1.0, ofi_changes=50, ofi_threshold=None, **kwargs)
    too_few = tox.classify_bucket(ofi_per_change=1.0, ofi_changes=9, ofi_threshold=0.5, **kwargs)
    extreme = tox.classify_bucket(ofi_per_change=-0.75, ofi_changes=50, ofi_threshold=0.5, **kwargs)
    inside = tox.classify_bucket(ofi_per_change=0.5, ofi_changes=50, ofi_threshold=0.5, **kwargs)
    assert no_threshold["flag_ofi"] is False
    assert too_few["flag_ofi"] is False
    assert extreme["flag_ofi"] is True
    assert extreme["flag_reason"] == "ofi"
    assert inside["flag_ofi"] is False  # strict, and the sign does not matter


def test_flags_combine_into_one_reason(tox):
    verdict = tox.classify_bucket(
        n_trades=20,
        matched={30: 20, 300: 20},
        markouts={30: 0.05, 300: 0.05},
        median_spread=0.02,
        ofi_per_change=0.9,
        ofi_changes=40,
        ofi_threshold=0.5,
    )
    assert verdict["flagged"] is True
    assert verdict["flag_reason"] == "markout_30s+markout_300s+ofi"

def test_fixture_drops_a_transiently_wide_book(tox):
    """A one-sided book's mid is not a price: it must not manufacture a markout."""
    ts = 1_700_000_000 * SECOND
    rows = [
        # 18x the condition's median spread of 0.05: dropped.
        _trade(ts, 0.40, 10.0, "BUY", m30=(ts + 30 * SECOND, 0.48, 0.90)),
        # Inside the guard: kept.
        _trade(ts, 0.40, 10.0, "BUY", m30=(ts + 30 * SECOND, 0.41, 0.06)),
    ]
    cell = tox.aggregate_trades(
        rows,
        horizons=(30,),
        median_spreads={"0xabc": 0.05},
        max_spread_multiple=10.0,
    )[("0xabc", tox.bucket_start_ns(ts))]
    assert cell["trades"] == 2
    assert cell["matched"][30] == 1
    assert cell["wide_quotes"] == 1
    markout, size, n = tox.weighted_markout(cell["moves"][30])
    assert n == 1 and size == pytest.approx(10.0)
    assert markout == pytest.approx(0.01)
