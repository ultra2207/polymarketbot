"""The venue minimum is a notional, so both legs quote at one share count."""

from __future__ import annotations

from polymarketbot.live.supervisor.sizing import matched_pair_quote_size


def test_fifty_fifty_raises_five_shares_to_ten() -> None:
    size, cost = matched_pair_quote_size(
        configured_size=5.0,
        mid_price=0.50,
        spread_offset_cents=0.5,
        tick_size=0.01,
        min_order_usd=5.0,
    )
    assert size == 11.0
    assert cost == 11.0


def test_skewed_market_sizes_the_cheap_leg_not_equal_dollars() -> None:
    # 0.30 / 0.70: the engineered prices land on 0.29 / 0.70 at a 1c tick, so
    # the cheap leg needs 18 shares to clear $5, and BOTH legs carry 18
    # shares. Equal dollars would be 16.67 YES vs 7.35 NO.
    size, cost = matched_pair_quote_size(
        configured_size=5.0,
        mid_price=0.30,
        spread_offset_cents=0.5,
        tick_size=0.01,
        min_order_usd=5.0,
    )
    assert size == 18.0
    assert cost == 17.82


def test_configured_size_above_the_floor_is_kept() -> None:
    size, cost = matched_pair_quote_size(
        configured_size=20.0,
        mid_price=0.50,
        spread_offset_cents=0.5,
        tick_size=0.01,
        min_order_usd=5.0,
    )
    assert size == 20.0
    assert cost == 20.0


def test_missing_venue_minimum_does_not_invent_one() -> None:
    size, _ = matched_pair_quote_size(
        configured_size=5.0,
        mid_price=0.50,
        spread_offset_cents=0.5,
        tick_size=0.01,
        min_order_usd=0.0,
    )
    assert size == 5.0
