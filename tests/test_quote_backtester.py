"""Pure-function tests for scripts/backtest_quotes.py.

Every case is a hand-built quote/fill sequence, so the PnL is arithmetic rather
than a re-derivation of the code. No tape, no network: the module is loaded from
the script path and only its pure helpers are called.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SEC = 1_000_000_000
T0 = 1_700_000_000 * SEC
HORIZON = 300 * SEC


@pytest.fixture(scope="module")
def bt():
    path = Path(__file__).resolve().parent.parent / "scripts" / "backtest_quotes.py"
    spec = importlib.util.spec_from_file_location("backtest_quotes_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclass field resolution looks the class's module up in sys.modules, so
    # the module has to be registered before it is executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def episode(bt, start: int, mid: float, length: int = 10 * SEC):
    return bt.Episode(start_ns=start, end_ns=start + length, mid=mid)


def cfg(bt, **kw):
    base = dict(
        sides="bid",
        distance=0.01,
        tick=0.01,
        order_size=100.0,
        horizon_ns=HORIZON,
        markout_tolerance_ns=60 * SEC,
        merge_usd=0.0,
    )
    base.update(kw)
    return bt.QuoteConfig(**base)


# ------------------------------------------------------------------ geometry
def test_quote_prices_round_away_from_the_mid(bt):
    one_tick = bt.quote_prices(0.155, 0.01, 0.01)
    assert one_tick["bid"] == pytest.approx(0.14)
    assert one_tick["ask"] == pytest.approx(0.17)
    half_tick = bt.quote_prices(0.155, 0.005, 0.01)
    assert half_tick["bid"] == pytest.approx(0.15)
    assert half_tick["ask"] == pytest.approx(0.16)


def test_quote_prices_drop_a_side_outside_the_probability_range(bt):
    prices = bt.quote_prices(0.995, 0.01, 0.01)
    assert prices["bid"] == pytest.approx(0.98)
    assert prices["ask"] is None
    prices = bt.quote_prices(0.005, 0.01, 0.01)
    assert prices["bid"] is None


def test_quote_prices_survive_float_dust(bt):
    assert bt.quote_prices(0.5900000000000001, 0.01, 0.01)["bid"] == pytest.approx(0.58)
    assert bt.quote_prices(0.59, 0.01, 0.001)["bid"] == pytest.approx(0.58)
    assert bt.quote_prices(0.59, 0.01, 0.001)["ask"] == pytest.approx(0.60)


def test_infer_tick_reads_the_level_grid(bt):
    assert bt.infer_tick([0.01, 0.02, 0.03, 0.05]) == pytest.approx(0.01)
    assert bt.infer_tick([0.041, 0.042, 0.043, 0.0442]) == pytest.approx(0.001)
    assert bt.infer_tick([0.1998, 0.1999, 0.2]) == pytest.approx(0.0001)


def test_infer_tick_snaps_a_missing_level_down_never_up(bt):
    # 0.02 gaps in a 0.01 market: the tick is 0.01, not 0.02.
    assert bt.infer_tick([0.10, 0.12, 0.14, 0.16]) == pytest.approx(0.01)
    # A single price says nothing about the grid.
    assert bt.infer_tick([0.5]) is None


def test_infer_tick_lands_a_quote_on_an_existing_level(bt):
    levels = [0.04 + i * 0.001 for i in range(40)]
    tick = bt.infer_tick(levels)
    prices = bt.quote_prices(0.0615, tick, tick)
    assert prices["bid"] == pytest.approx(0.060)
    assert prices["bid"] in levels


# ------------------------------------------------------------------- episodes
def test_collapse_mid_states_keeps_one_episode_per_state(bt):
    rows = [(1000, 0.15, 0.16), (2000, 0.15, 0.16), (3000, 0.14, 0.16), (5000, None, 0.16)]
    episodes = bt.collapse_mid_states(rows, end_ns=9000)
    assert [(e.start_ns, e.end_ns) for e in episodes] == [(1000, 3000), (3000, 9000)]
    assert [round(e.mid, 4) for e in episodes] == [0.155, 0.15]


def test_collapse_mid_states_skips_a_frame_with_no_side(bt):
    assert bt.collapse_mid_states([(1000, None, 0.16)], end_ns=9000) == []


# ---------------------------------------------------------------------- PnL
def test_one_bid_fill_marked_out_at_five_minutes(bt):
    quote = episode(bt, T0, 0.50)
    levels = {("bid", 0.49): bt.make_level("bid", 0.49, [(T0 + 1 * SEC, 30.0)])}
    mid_ts = [T0, T0 + 1 * SEC + HORIZON]
    mid_val = [0.50, 0.48]

    result = bt.simulate([quote], levels, mid_ts, mid_val, cfg(bt))
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["quote_price"] == pytest.approx(0.49)
    assert row["shares"] == pytest.approx(30.0)
    assert row["mid_at_fill"] == pytest.approx(0.50)
    assert row["gross_spread"] == pytest.approx(0.01)
    assert row["markout"] == pytest.approx(-0.01)  # 0.48 - 0.49
    assert row["fee_per_share"] == 0.0
    assert row["gas_per_share"] == 0.0
    assert row["net_per_share"] == pytest.approx(-0.01)
    assert row["net_usd"] == pytest.approx(-0.30)

    stats = bt.summarise(result, min_fills=1)
    assert stats["quotes"] == 1
    assert stats["fills"] == 1
    assert stats["fill_rate"] == pytest.approx(1.0)
    assert stats["shares"] == pytest.approx(30.0)
    assert stats["net_per_share"] == pytest.approx(-0.01)
    assert stats["net_usd"] == pytest.approx(-0.30)


def test_ask_fill_pnl_is_the_mirror_leg(bt):
    quote = episode(bt, T0, 0.50)
    levels = {("ask", 0.51): bt.make_level("ask", 0.51, [(T0 + 2 * SEC, 40.0)])}
    mid_ts = [T0, T0 + 2 * SEC + HORIZON]
    mid_val = [0.50, 0.52]

    result = bt.simulate([quote], levels, mid_ts, mid_val, cfg(bt, sides="ask"))
    row = result["rows"][0]
    assert row["quote_price"] == pytest.approx(0.51)
    assert row["gross_spread"] == pytest.approx(0.01)
    assert row["markout"] == pytest.approx(-0.01)  # 0.51 - 0.52
    assert row["net_usd"] == pytest.approx(-0.40)


def test_fill_is_capped_at_the_order_size(bt):
    quote = episode(bt, T0, 0.50)
    levels = {("bid", 0.49): bt.make_level("bid", 0.49, [(T0 + 1 * SEC, 500.0)])}
    mid_ts = [T0, T0 + 1 * SEC + HORIZON]
    mid_val = [0.50, 0.48]

    result = bt.simulate([quote], levels, mid_ts, mid_val, cfg(bt, order_size=100.0))
    assert result["rows"][0]["shares"] == pytest.approx(100.0)
    assert result["rows"][0]["net_usd"] == pytest.approx(-1.0)


def test_repeated_fills_in_one_episode_are_summed_then_capped(bt):
    quote = episode(bt, T0, 0.50)
    fills = [(T0 + 1 * SEC, 20.0), (T0 + 3 * SEC, 20.0), (T0 + 5 * SEC, 20.0)]
    levels = {("bid", 0.49): bt.make_level("bid", 0.49, fills)}
    mid_ts = [T0, T0 + 1 * SEC + HORIZON]
    mid_val = [0.50, 0.48]

    result = bt.simulate([quote], levels, mid_ts, mid_val, cfg(bt, order_size=50.0))
    row = result["rows"][0]
    assert row["shares"] == pytest.approx(50.0)
    assert row["fill_ts_ns"] == T0 + 1 * SEC
    assert result["rows"].__len__() == 1


def test_a_fill_at_the_instant_the_quote_is_placed_does_not_count(bt):
    """The trade that changed the top of book was already in flight."""
    quote = episode(bt, T0, 0.50)
    levels = {
        ("bid", 0.49): bt.make_level(
            "bid", 0.49, [(T0, 30.0)], last_ts=T0 + 5 * SEC
        )
    }
    result = bt.simulate([quote], levels, [T0], [0.50], cfg(bt))
    assert result["rows"] == []


def test_a_fill_at_the_instant_the_quote_is_cancelled_does_not_count(bt):
    quote = episode(bt, T0, 0.50, length=10 * SEC)
    levels = {
        ("bid", 0.49): bt.make_level(
            "bid", 0.49, [(T0 + 10 * SEC, 30.0)], last_ts=T0 + 20 * SEC
        )
    }
    result = bt.simulate([quote], levels, [T0], [0.50], cfg(bt))
    assert result["rows"] == []


def test_a_fill_before_the_quote_is_placed_does_not_count(bt):
    quote = episode(bt, T0, 0.50)
    levels = {
        ("bid", 0.49): bt.make_level(
            "bid", 0.49, [(T0 - 1 * SEC, 30.0)], last_ts=T0 + 5 * SEC
        )
    }
    mid_ts = [T0]
    mid_val = [0.50]

    result = bt.simulate([quote], levels, mid_ts, mid_val, cfg(bt))
    assert result["rows"] == []
    stats = bt.summarise(result, min_fills=1)
    assert stats["quotes"] == 1
    assert stats["quotes_observed"] == 1
    assert stats["fill_rate"] == pytest.approx(0.0)


def test_a_level_with_no_observation_after_the_quote_is_excluded(bt):
    quote = episode(bt, T0, 0.50)
    levels = {
        ("bid", 0.49): bt.make_level(
            "bid", 0.49, [(T0 - 5 * SEC, 10.0)], last_ts=T0 - 1 * SEC
        )
    }
    result = bt.simulate([quote], levels, [T0], [0.50], cfg(bt))
    stats = bt.summarise(result, min_fills=1)
    assert stats["quotes"] == 1
    assert stats["quotes_no_level"] == 1
    assert stats["quotes_observed"] == 0
    assert stats["fill_rate"] is None


def test_a_level_observed_without_any_fill_is_still_a_quote_that_missed(bt):
    """The fill rate denominator is every level the telemetry saw, not just filled ones."""
    quote = episode(bt, T0, 0.50)
    levels = {("bid", 0.49): bt.make_level("bid", 0.49, [], last_ts=T0 + 5 * SEC)}
    result = bt.simulate([quote], levels, [T0], [0.50], cfg(bt))
    stats = bt.summarise(result, min_fills=1)
    assert stats["fills"] == 0
    assert stats["quotes_no_level"] == 0
    assert stats["quotes_observed"] == 1
    assert stats["fill_rate"] == pytest.approx(0.0)


def test_a_level_absent_from_the_file_is_excluded(bt):
    quote = episode(bt, T0, 0.50)
    result = bt.simulate([quote], {}, [T0], [0.50], cfg(bt))
    stats = bt.summarise(result, min_fills=1)
    assert stats["quotes_no_level"] == 1
    assert stats["fills"] == 0


def test_markout_outside_the_tolerance_is_missing_not_stale(bt):
    quote = episode(bt, T0, 0.50)
    levels = {("bid", 0.49): bt.make_level("bid", 0.49, [(T0 + 1 * SEC, 30.0)])}
    # The next mid is 120 s late for a 60 s tolerance.
    mid_ts = [T0, T0 + 1 * SEC + HORIZON + 120 * SEC]
    mid_val = [0.50, 0.48]

    result = bt.simulate([quote], levels, mid_ts, mid_val, cfg(bt))
    row = result["rows"][0]
    assert row["markout"] is None
    assert row["net_per_share"] is None
    assert row["net_usd"] is None

    stats = bt.summarise(result, min_fills=0)
    assert stats["fills"] == 1
    assert stats["marked_fills"] == 0
    assert stats["fills_missing_markout"] == 1
    assert stats["net_per_share"] is None
    assert stats["net_usd"] is None  # never a fabricated zero


def test_gas_is_amortised_over_the_filled_shares(bt):
    quote = episode(bt, T0, 0.50)
    levels = {("bid", 0.49): bt.make_level("bid", 0.49, [(T0 + 1 * SEC, 10.0)])}
    mid_ts = [T0, T0 + 1 * SEC + HORIZON]
    mid_val = [0.50, 0.48]

    result = bt.simulate([quote], levels, mid_ts, mid_val, cfg(bt, merge_usd=0.05))
    row = result["rows"][0]
    assert row["gas_per_share"] == pytest.approx(0.005)
    assert row["net_per_share"] == pytest.approx(-0.015)  # -0.01 markout - 0.005 gas
    assert row["net_usd"] == pytest.approx(-0.15)


def test_the_ask_leg_pays_no_merge_gas(bt):
    quote = episode(bt, T0, 0.50)
    levels = {("ask", 0.51): bt.make_level("ask", 0.51, [(T0 + 1 * SEC, 10.0)])}
    mid_ts = [T0, T0 + 1 * SEC + HORIZON]
    mid_val = [0.50, 0.49]

    result = bt.simulate([quote], levels, mid_ts, mid_val, cfg(bt, sides="ask", merge_usd=0.05))
    row = result["rows"][0]
    assert row["gas_per_share"] == 0.0
    assert row["markout"] == pytest.approx(0.02)  # 0.51 - 0.49
    assert row["net_per_share"] == pytest.approx(0.02)


def test_both_sides_quote_independently_in_one_episode(bt):
    quote = episode(bt, T0, 0.50)
    levels = {
        ("bid", 0.49): bt.make_level("bid", 0.49, [(T0 + 1 * SEC, 20.0)]),
        # The ask level trades but after the quote expired: no ask fill.
        ("ask", 0.51): bt.make_level("ask", 0.51, [(T0 + 11 * SEC, 20.0)]),
    }
    mid_ts = [T0, T0 + 1 * SEC + HORIZON]
    mid_val = [0.50, 0.48]

    result = bt.simulate([quote], levels, mid_ts, mid_val, cfg(bt, sides="both"))
    assert [r["side"] for r in result["rows"]] == ["bid"]
    stats = bt.summarise(result, min_fills=1)
    assert stats["quotes"] == 2  # both sides placed
    assert stats["fill_rate"] == pytest.approx(0.5)


def test_summary_weights_the_money_by_shares(bt):
    episodes = [episode(bt, T0, 0.50), episode(bt, T0 + 20 * SEC, 0.60)]
    levels = {
        ("bid", 0.49): bt.make_level("bid", 0.49, [(T0 + 1 * SEC, 90.0)]),
        ("bid", 0.59): bt.make_level("bid", 0.59, [(T0 + 21 * SEC, 10.0)]),
    }
    mid_ts = [T0, T0 + 20 * SEC, T0 + 1 * SEC + HORIZON, T0 + 21 * SEC + HORIZON]
    mid_val = [0.50, 0.60, 0.40, 0.52]

    result = bt.simulate(episodes, levels, mid_ts, mid_val, cfg(bt, order_size=100.0))
    stats = bt.summarise(result, min_fills=1)
    assert stats["fills"] == 2
    assert stats["net_per_share"] == pytest.approx(-0.08)  # (-0.09 + -0.07) / 2
    assert stats["net_per_share_weighted"] == pytest.approx(-0.088)  # -8.8 / 100
    assert stats["net_usd"] == pytest.approx(-8.8)
