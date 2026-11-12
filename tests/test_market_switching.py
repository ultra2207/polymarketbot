"""Focused tests for the opportunistic rewards-market switching decision.

A stub scanner replaces the rewards universe: the engine must migrate only when
the measured yield improvement clears the module's own switching cost
(gas + merge gas + crossing friction, amortized over the holding horizon).
"""

from __future__ import annotations

import pytest

from polymarketbot.strategy.reward.scanner import MarketOpportunity
from polymarketbot.strategy.reward.switching import (
    OpportunisticSwitchingEngine,
    SwitchingConfig,
    evaluate_switching_hurdle,
    solve_switching_hurdle_inputs,
)

# 0.5 * $1,000 = $500 allocated; 0.25% crossing friction = $1.25 one-off.
# The round trip also forgoes the current yield while unquoted: two measured
# CLOB flights (2 x 0.3 s) plus the venue's 3.5 s eligibility age, at $10/day
# = $0.000475. Round-trip cost = $1.250475.
ALLOCATED_CAPITAL = 500.0
SWITCH_COST_USD = 1.25
ROUND_TRIP_COST_USD = 1.25 + (2 * 0.3 + 3.5) * 10.0 / 86_400.0
HOLDING_HORIZON_HOURS = 4.0
HURDLE_PCT = ROUND_TRIP_COST_USD / ALLOCATED_CAPITAL
AMORTIZED_COST_DAILY = ROUND_TRIP_COST_USD * 24.0 / HOLDING_HORIZON_HOURS


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
            # Measured inputs: the crossing friction the module prices, the
            # persisted CLOB round trip, and the caller's holding horizon.
            round_trip_latency_seconds=0.3,
            amortization_horizon_hours=HOLDING_HORIZON_HOURS,
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
    assert decision.switching_cost_usd == pytest.approx(ROUND_TRIP_COST_USD)
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


def test_hurdle_and_amortisation_horizon_are_solved_from_measurements() -> None:
    """No 20 % premium and no 4 h window: both are measured break-evens."""
    hurdle, horizon, cost = solve_switching_hurdle_inputs(
        switching_cost=SWITCH_COST_USD,
        capital_moved=ALLOCATED_CAPITAL,
        yield_now_daily=10.0,
        yield_candidate_daily=20.0,
        holding_horizon_hours=None,
        unquoted_seconds=2 * 0.3 + 3.5,
    )

    assert cost == pytest.approx(ROUND_TRIP_COST_USD)
    assert hurdle == pytest.approx(HURDLE_PCT)
    # Payback = round-trip cost / ($10/day / 24 h).
    assert horizon == pytest.approx(ROUND_TRIP_COST_USD / (10.0 / 24.0))


def test_unmeasured_round_trip_refuses_to_price_a_switch() -> None:
    """A missing flight time is missing data, not a free migration."""
    hurdle, horizon, cost = solve_switching_hurdle_inputs(
        switching_cost=SWITCH_COST_USD,
        capital_moved=ALLOCATED_CAPITAL,
        yield_now_daily=10.0,
        yield_candidate_daily=20.0,
        holding_horizon_hours=None,
        unquoted_seconds=None,
    )

    assert cost == float("inf")
    assert hurdle == float("inf")
    assert horizon == float("inf")


def test_payback_horizon_rejects_a_switch_that_only_breaks_even() -> None:
    """At the payback horizon the amortised cost *is* the differential.

    The model therefore rejects the migration: the premium makes a break-even
    switch a loss. A caller that measured a longer hold passes that horizon.
    """
    current = _opportunity("0xcurrent", "Will the current market pay?", 10.0)
    better = _opportunity("0xbetter", "Will the better market pay?", 20.0)
    scanner = StubScanner([better, current])
    engine = OpportunisticSwitchingEngine(
        scanner=scanner,
        config=SwitchingConfig(
            round_trip_latency_seconds=0.3,
            merge_gas_usd=0.0,
            crossing_spread_friction_pct=0.0025,
        ),
        bankroll_usd=1000.0,
        concentration_pct=0.5,
    )
    engine.set_active_market(current)

    decision = engine.rescan_and_evaluate()

    assert decision.should_switch is False
    assert decision.switching_cost_usd > SWITCH_COST_USD
    assert "fails hurdle" in decision.reason


def test_evaluate_switching_hurdle_requires_its_inputs() -> None:
    """The two knobs have no defaults to fall back on."""
    with pytest.raises(TypeError):
        evaluate_switching_hurdle(  # type: ignore[call-arg]
            y_net_current=10.0,
            y_net_candidate=20.0,
            switching_cost=1.0,
        )
