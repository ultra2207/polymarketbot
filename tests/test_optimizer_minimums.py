"""Venue minimums enter the optimizers as constraints, not post-hoc checks."""

from __future__ import annotations

from polymarketbot.strategy.reward.allocator_candidates import activation_lot_capital
from polymarketbot.strategy.sizing import derive_quote_size


def test_size_floor_is_the_notional_on_the_cheap_leg() -> None:
    size = derive_quote_size(
        initial_cash=1000.0,
        mid_price=0.50,
        min_size=0.0,
        reserve_pct=0.0,
        cycles_budget=100.0,
        order_min_usd=5.0,
    )
    assert size == 10.0  # ceil(5 / 0.50)


def test_skewed_market_floor_follows_the_cheap_leg() -> None:
    size = derive_quote_size(
        initial_cash=1000.0,
        mid_price=0.30,
        min_size=0.0,
        reserve_pct=0.0,
        cycles_budget=100.0,
        order_min_usd=5.0,
    )
    assert size == 17.0  # ceil(5 / 0.30)


def test_rewards_min_size_still_wins_when_larger() -> None:
    size = derive_quote_size(
        initial_cash=1000.0,
        mid_price=0.50,
        min_size=20.0,
        reserve_pct=0.0,
        cycles_budget=100.0,
        order_min_usd=5.0,
    )
    assert size == 20.0


def test_unfundable_pair_returns_zero_not_a_sub_minimum_order() -> None:
    size = derive_quote_size(
        initial_cash=8.0,
        mid_price=0.50,
        min_size=0.0,
        reserve_pct=0.0,
        cycles_budget=100.0,
        order_min_usd=5.0,
    )
    assert size == 0.0


def test_activation_lot_is_the_pair_not_one_leg() -> None:
    # 0.50/0.50, rewards minimum 20 shares: the pair is 20 shares at par,
    # not 20 x 0.49.
    assert (
        activation_lot_capital(
            {"min_size": 20.0, "real_mid": 0.50, "lot_price": 0.49, "order_min_usd": 5.0}
        )
        == 20.0
    )


def test_activation_lot_clears_the_notional_on_the_cheap_leg() -> None:
    assert (
        activation_lot_capital(
            {"min_size": 5.0, "real_mid": 0.30, "lot_price": 0.29, "order_min_usd": 5.0}
        )
        == 18.0
    )
