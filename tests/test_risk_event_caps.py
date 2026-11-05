from __future__ import annotations

import pytest

from polymarketbot.paper_trading.engine import PaperTradingEngine
from polymarketbot.paper_trading.models import MarketConfig, TokenConfig
from polymarketbot.risk.manager import RiskManager
from polymarketbot.risk.models import RiskConfig


def _manager(**overrides) -> RiskManager:
    config = RiskConfig(
        total_bankroll_usd=1000.0,
        max_quote_size_shares=1000.0,
        max_position_shares_per_token=10000.0,
        max_position_cost_per_market=1000.0,
        max_portfolio_inventory_usd=1000.0,
        max_event_inventory_usd=100.0,
        max_event_position_cost_usd=100.0,
        allow_short=False,
        **overrides,
    )
    return RiskManager(config)


def _check(manager: RiskManager, **kwargs):
    return manager.validate_quote(
        asset_id="token",
        condition_id="0xcid",
        side="BUY",
        price=0.5,
        size=1000.0,
        current_pos_shares=0.0,
        current_pos_cost=0.0,
        current_portfolio_inventory_usd=0.0,
        current_cash_usd=1000.0,
        mid_price=0.5,
        tick_size=0.01,
        **kwargs,
    )


def test_event_inventory_cap_limits_order() -> None:
    allowed, _, size, _ = _check(
        _manager(),
        event_id="event-1",
        current_event_inventory_usd=0.0,
        current_event_cost=0.0,
    )
    assert allowed
    # $100 event cap at $0.50 -> 200 shares, below the 1,000 quote cap.
    assert size == pytest.approx(200.0)


def test_event_cost_headroom_binds_before_inventory() -> None:
    allowed, _, size, _ = _check(
        _manager(),
        event_id="event-1",
        current_event_inventory_usd=90.0,
        current_event_cost=95.0,
    )
    assert allowed
    # Cost headroom $5 / $0.50 = 10 shares; inventory headroom would allow 20.
    assert size == pytest.approx(10.0)


def test_no_event_id_keeps_per_market_cap() -> None:
    allowed, _, size, _ = _check(_manager())
    assert allowed
    assert size == pytest.approx(1000.0)


def test_event_at_cap_rejects_order() -> None:
    allowed, reason, size, _ = _check(
        _manager(),
        event_id="event-1",
        current_event_inventory_usd=100.0,
        current_event_cost=100.0,
    )
    assert not allowed
    assert "event" in reason
    assert size == 0.0


def test_paper_engine_event_exposure_sums_markets(tmp_path) -> None:
    engine = PaperTradingEngine(ledger_path=tmp_path / "ledger.json")
    for index, event_id in enumerate(("event-1", "event-1", "event-2")):
        token_id = f"yes-{index}"
        engine.register_market(
            MarketConfig(
                condition_id=f"0xcid{index}",
                question=f"q{index}",
                rate_per_day=10.0,
                rewards_min_size=5.0,
                rewards_max_spread=3.0,
                event_id=event_id,
                tokens=[
                    TokenConfig(token_id=token_id, outcome="YES", price=0.5),
                    TokenConfig(token_id=f"no-{index}", outcome="NO", price=0.5),
                ],
            )
        )
        position = engine.positions[token_id]
        position.shares = 60.0
        position.avg_cost = 0.5
        position.market_value = 30.0

    inventory, cost = engine._event_exposure("event-1")
    assert inventory == pytest.approx(60.0)
    assert cost == pytest.approx(60.0)
    other_inventory, other_cost = engine._event_exposure("event-2")
    assert other_inventory == pytest.approx(30.0)
    assert other_cost == pytest.approx(30.0)
    assert engine._event_exposure(None) == (0.0, 0.0)
