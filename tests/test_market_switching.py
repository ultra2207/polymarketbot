"""Focused tests for the opportunistic rewards-market switching decision.

A stub scanner replaces the rewards universe: the engine must migrate only when
the measured yield improvement clears the module's own switching cost
(gas + merge gas + crossing friction, amortized over the holding horizon).
"""

from __future__ import annotations

import pytest

from polymarketbot.rewards.scanner import MarketOpportunity
from polymarketbot.rewards.switching import (
    OpportunisticSwitchingEngine,
    SwitchingConfig,
)

# 0.5 * $1,000 = $500 allocated; 0.25% crossing friction = $1.25 one-off.
# Amortized over the default 4h horizon that is $7.50/day.
ALLOCATED_CAPITAL = 500.0
SWITCH_COST_USD = 1.25
AMORTIZED_COST_DAILY = 7.50


def _opportunity(condition_id: str, question: str, net_yield: float) -> MarketOpportunity:
    return MarketOpportunity(
        condition_id=condition_id,
        question=question,
        rate_per_day=10.0,
        rewards_min_size=50.0,
        rewards_max_spread=3.0,
        net_expected_yield=net_yield,
        passed_payout_floor=True,
    )


class StubScanner:
    """Returns a fixed ranked universe and records each scan's sizing inputs."""

    def __init__(self, ranked: list[MarketOpportunity] | None = None) -> None:
        self.ranked = list(ranked or [])
        self.calls: list[tuple[float, float]] = []

    def scan_and_rank_markets(
        self,
        *,
        allocated_capital_usd: float,
        quote_horizon_s: float,
        **_: object,
    ) -> list[MarketOpportunity]:
        self.calls.append((allocated_capital_usd, quote_horizon_s))
        return list(self.ranked)


def _engine(scanner: StubScanner) -> OpportunisticSwitchingEngine:
    return OpportunisticSwitchingEngine(
        scanner=scanner,
        config=SwitchingConfig(
            switch_hurdle_pct=0.0,
            merge_gas_usd=0.0,
            crossing_spread_friction_pct=0.0025,
        ),
        bankroll_usd=1000.0,
        concentration_pct=0.5,
    )


def test_better_net_payout_triggers_a_switch() -> None:
    current = _opportunity("0xcurrent", "Will the current market pay?", 10.0)
    better = _opportunity("0xbetter", "Will the better market pay?", 20.0)
    scanner = StubScanner([better, current])
    engine = _engine(scanner)
    engine.set_active_market(current)

    decision = engine.rescan_and_evaluate()

    assert decision.should_switch is True
    assert decision.candidate is better
    assert decision.to_condition_id == "0xbetter"
    assert decision.expected_gain_per_day == pytest.approx(10.0)
    assert decision.switching_cost_usd == pytest.approx(SWITCH_COST_USD)
    assert decision.amortized_cost_per_day == pytest.approx(AMORTIZED_COST_DAILY)
    assert engine.bankroll_usd * engine.concentration_pct == pytest.approx(ALLOCATED_CAPITAL)
    # The scanner is told the dollar amount the caller allocated and the quote
    # lifetime is the engine's own scan interval - no scanner-side default.
    assert scanner.calls == [
        (ALLOCATED_CAPITAL, engine.config.scan_interval_seconds)
    ]


def test_improvement_below_switching_cost_does_not_switch() -> None:
    current = _opportunity("0xcurrent", "Will the current market pay?", 10.0)
    marginal = _opportunity("0xmarginal", "Will the marginal market pay?", 12.0)
    scanner = StubScanner([marginal, current])
    engine = _engine(scanner)
    engine.set_active_market(current)

    decision = engine.rescan_and_evaluate()

    assert decision.should_switch is False
    assert decision.expected_gain_per_day == pytest.approx(2.0)
    assert decision.expected_gain_per_day < decision.amortized_cost_per_day
    assert decision.hurdle_threshold > decision.y_net_to
    assert "fails hurdle" in decision.reason
    assert engine.active_opportunity is current


def test_no_candidate_keeps_the_current_market() -> None:
    current = _opportunity("0xcurrent", "Will the current market pay?", 10.0)
    scanner = StubScanner([])
    engine = _engine(scanner)
    engine.set_active_market(current)

    decision = engine.rescan_and_evaluate()

    assert decision.should_switch is False
    assert decision.candidate is None
    assert decision.to_condition_id is None
    assert decision.y_net_from == pytest.approx(10.0)
    assert engine.active_opportunity is current
    assert engine.state.value == "QUOTING"
