"""Unified microstructure risk engine: queue position + flow + adverse fills."""

from __future__ import annotations

import json

import pytest

from polymarketbot.risk.contracts import RiskAction
from polymarketbot.risk.models import RiskConfig
from polymarketbot.risk.unified import UnifiedRiskEngine
from polymarketbot.strategy.reward.measurement import MakerMarkout, QuoteFillHorizon
from polymarketbot.strategy.reward.microstructure import (
    MicrostructureEngine,
    compute_dynamic_ofi_threshold,
)

TICK = 0.01
HORIZON = 10.0
FILLS = QuoteFillHorizon(cells=((HORIZON, TICK, 20.0, 0.5),), placements=100)
MARKOUT = MakerMarkout(cells=((HORIZON, TICK, 0.02, 100),))

# Tape-fitted flow coefficients. In production the caller supplies these from
# the fit artifact; MicrostructureEngine has no defaults.
OFI_FIT = {
    "c_depth": 1.5,
    "z_score": 3.0,
    "v_ref": 50_000.0,
    "beta_vol": 0.15,
}
FLOW_FIT = {**OFI_FIT, "pull_multiplier": 1.5, "widen_offset_multiplier": 1.5}


def _dynamic_flow():
    return MicrostructureEngine(**FLOW_FIT)


def _flow(ofi_threshold: float = 1.0, drift_threshold_cents: float = 0.01):
    return lambda: MicrostructureEngine(
        **FLOW_FIT,
        ofi_threshold=ofi_threshold,
        drift_threshold_cents=drift_threshold_cents,
    )


def _engine(**kwargs) -> UnifiedRiskEngine:
    options = {
        "fill_horizon": FILLS,
        "markout": MARKOUT,
        "horizon_seconds": HORIZON,
        "consecutive_fills_threshold": 3,
        "flow_factory": _dynamic_flow,
    }
    options.update(kwargs)
    return UnifiedRiskEngine(tick_size=TICK, **options)


def _buy_pressure(engine: UnifiedRiskEngine, token: str = "YES") -> None:
    """Two book updates whose bid growth makes the flow toxic toward asks."""
    engine.on_book(token, [(0.49, 100.0)], [(0.51, 100.0)], 1.0)
    engine.on_book(token, [(0.49, 1000.0)], [(0.51, 100.0)], 2.0)


def test_unmeasured_flow_refuses_to_act() -> None:
    engine = _engine()
    engine.on_book("YES", [(0.49, 100.0)], [(0.51, 100.0)], 1.0)

    decision = engine.evaluate("YES", "BUY", 0.49, 0.50, 1.0)

    assert decision.measured is False
    assert decision.action is RiskAction.MAINTAIN
    assert "unmeasured" in decision.reason


def test_unmeasured_flow_stays_unmeasured_even_with_a_streak() -> None:
    engine = _engine(consecutive_fills_threshold=2)
    engine.on_book("YES", [(0.49, 100.0)], [(0.51, 100.0)], 1.0)
    engine.on_fill("YES", "BUY", 0.49, 20.0, 1.0)
    engine.on_fill("YES", "BUY", 0.49, 20.0, 1.1)

    decision = engine.evaluate("YES", "BUY", 0.49, 0.50, 1.2)

    assert decision.measured is False
    assert decision.action is RiskAction.MAINTAIN


def test_thresholds_are_dynamic_and_use_observed_dispersion() -> None:
    engine = _engine()
    for step in range(10):
        mid = 0.50 + 0.001 * step
        engine.on_book(
            "YES",
            [(mid - 0.005, 500.0), (mid - 0.01, 800.0)],
            [(mid + 0.005, 500.0), (mid + 0.01, 800.0)],
            1000.0 + step * 10.0,
        )

    snap = engine.snapshot()["tokens"]["YES"]
    assert snap["measured"] is True

    # The depth-only structural value: 1.5 x the harmonic mean of the two sides,
    # which is what the threshold collapsed to when the measured dispersion was
    # never passed in.
    structural_only = compute_dynamic_ofi_threshold(
        1300.0, 1300.0, rolling_ofi_std=0.0, **OFI_FIT
    )
    assert snap["ofi_threshold"] > structural_only
    assert snap["drift_threshold_cents"] > 0.0
    assert snap["drift_threshold_cents"] != pytest.approx(1.5, abs=1e-9)


def test_two_book_shapes_produce_two_thresholds() -> None:
    thin = _engine()
    thin.on_book("YES", [(0.49, 10.0)], [(0.51, 10.0)], 1.0)
    deep = _engine()
    deep.on_book("YES", [(0.49, 100_000.0)], [(0.51, 100_000.0)], 1.0)

    thin_flow = thin.flow("YES")
    deep_flow = deep.flow("YES")
    assert thin_flow is not None and deep_flow is not None
    assert thin_flow.ofi_threshold != deep_flow.ofi_threshold


def test_toxic_buying_pressure_pulls_the_ask_when_we_are_first_in_line() -> None:
    engine = _engine(flow_factory=_flow())
    _buy_pressure(engine)
    engine.on_entry("a1", "YES", "SELL", 0.51, 20.0, 3.0, bids=[], asks=[])

    decision = engine.evaluate("YES", "SELL", 0.51, 0.50, 4.0)

    assert decision.action is RiskAction.PULL
    assert decision.queue is not None and decision.queue.at_front


def test_deep_queue_without_an_economic_argument_does_not_pull() -> None:
    engine = _engine(
        flow_factory=_flow(ofi_threshold=1000.0, drift_threshold_cents=1.0),
        markout=MakerMarkout(cells=((10.0, TICK, 0.001, 100),)),
    )
    engine.on_book("YES", [(0.49, 1000.0)], [(0.51, 1000.0)], 1.0)
    engine.on_book("YES", [(0.49, 2200.0)], [(0.51, 1000.0)], 2.0)
    engine.on_entry(
        "a2", "YES", "SELL", 0.51, 20.0, 3.0, bids=[], asks=[(0.51, 5000.0)]
    )

    decision = engine.evaluate("YES", "SELL", 0.51, 0.50, 4.0)

    assert decision.action is not RiskAction.PULL
    assert decision.queue is not None and not decision.queue.at_front


def test_measured_adverse_drift_above_capture_pulls_even_from_deep_in_the_queue() -> None:
    engine = _engine(flow_factory=_flow())
    _buy_pressure(engine)
    engine.on_entry(
        "a3", "YES", "SELL", 0.51, 20.0, 3.0, bids=[], asks=[(0.51, 120.0)]
    )
    # The level shrank from 120 to 70 shares: half the queue ahead is gone,
    # so the measured cost is no longer cancelled out by zero consumption.
    engine.on_book("YES", [(0.49, 1000.0)], [(0.51, 70.0)], 3.5)

    decision = engine.evaluate("YES", "SELL", 0.51, 0.50, 4.0)

    assert decision.action is RiskAction.PULL
    assert "expected hold cost" in decision.reason
    assert decision.queue is not None and not decision.queue.at_front


def test_consecutive_fills_suppress_the_side() -> None:
    engine = _engine(
        consecutive_fills_threshold=2,
        config=RiskConfig(toxicity_action="suppress"),
        flow_factory=_flow(),
    )
    _buy_pressure(engine)
    engine.on_fill("YES", "SELL", 0.51, 20.0, 3.0)
    engine.on_fill("YES", "SELL", 0.51, 20.0, 3.1)

    decision = engine.evaluate("YES", "SELL", 0.51, 0.50, 4.0)

    assert decision.action is RiskAction.SUPPRESS
    assert decision.adverse_streak == 2


def test_an_opposing_fill_resets_the_streak() -> None:
    engine = _engine(consecutive_fills_threshold=2, flow_factory=_flow())
    _buy_pressure(engine)
    engine.on_fill("YES", "SELL", 0.51, 20.0, 3.0)
    engine.on_fill("YES", "BUY", 0.49, 20.0, 3.1)

    decision = engine.evaluate("YES", "SELL", 0.51, 0.50, 4.0)

    assert decision.adverse_streak == 0


def test_a_full_fill_retires_the_order() -> None:
    engine = _engine(flow_factory=_flow())
    _buy_pressure(engine)
    engine.on_entry(
        "a4", "YES", "SELL", 0.51, 20.0, 3.0, bids=[], asks=[(0.51, 500.0)]
    )
    assert engine.snapshot()["orders"] == 1

    engine.on_fill("YES", "SELL", 0.51, 20.0, 3.1)

    assert engine.snapshot()["orders"] == 0


def test_new_size_at_our_level_never_moves_us_forward() -> None:
    engine = _engine(flow_factory=_flow())
    engine.on_book("YES", [(0.49, 100.0)], [(0.51, 100.0)], 1.0)
    entry = engine.on_entry(
        "a5", "YES", "SELL", 0.51, 20.0, 2.0, bids=[], asks=[(0.51, 500.0)]
    )
    assert entry.ahead_at_entry == 500.0

    engine.on_book("YES", [(0.49, 100.0)], [(0.51, 900.0)], 3.0)

    # The public decision carries the same queue progress the tracker holds;
    # no private entry/queue access is needed to observe it.
    decision = engine.evaluate("YES", "SELL", 0.51, 0.50, 3.0)
    assert decision.queue is not None
    assert decision.queue.ahead == 500.0


def test_snapshot_is_json_safe_and_carries_the_last_decision() -> None:
    engine = _engine(flow_factory=_flow())
    _buy_pressure(engine)
    engine.on_entry("a6", "YES", "SELL", 0.51, 20.0, 3.0, bids=[], asks=[])
    engine.evaluate("YES", "SELL", 0.51, 0.50, 4.0)

    payload = json.dumps(engine.snapshot())

    assert '"action": "pull"' in payload
    assert '"ofi_threshold"' in payload
    assert '"queue_consumed_fraction"' in payload


def test_decision_record_is_flat_for_the_parquet_writer() -> None:
    engine = _engine(flow_factory=_flow())
    _buy_pressure(engine)
    engine.on_entry("a7", "YES", "SELL", 0.51, 20.0, 3.0, bids=[], asks=[])
    decision = engine.evaluate("YES", "SELL", 0.51, 0.50, 4.0)

    record = decision.as_record()

    assert all(
        not isinstance(value, (dict, list, tuple)) for value in record.values()
    ), record
    assert record["queue_position_inferred"] is True


def test_repeated_evaluate_is_memoised_until_state_changes() -> None:
    """The quoting leg and RiskManager ask twice per order; the second is cached."""
    engine = _engine(flow_factory=_flow())
    _buy_pressure(engine)
    engine.on_entry("a8", "YES", "SELL", 0.51, 20.0, 3.0, bids=[], asks=[])

    first = engine.evaluate("YES", "SELL", 0.51, 0.50, 4.0)
    second = engine.evaluate("YES", "SELL", 0.51, 0.50, 4.0)
    assert second is first

    engine.on_fill("YES", "SELL", 0.51, 20.0, 4.0)
    third = engine.evaluate("YES", "SELL", 0.51, 0.50, 4.0)
    assert third is not first


def test_decision_prices_the_supplied_quote_horizon_not_the_longest_cell() -> None:
    """An unmeasured horizon makes the economics unmeasured, never defaulted."""
    engine = _engine(
        flow_factory=_flow(),
        horizon_seconds=5.0,  # FILLS/MARKOUT only measured 10.0 s
    )
    _buy_pressure(engine)
    engine.on_entry(
        "a9", "YES", "SELL", 0.51, 20.0, 3.0, bids=[], asks=[(0.51, 500.0)]
    )

    decision = engine.evaluate("YES", "SELL", 0.51, 0.50, 4.0)

    assert decision.queue is not None and not decision.queue.at_front
    assert decision.measured is False
    assert "economics unmeasured" in decision.reason


def test_horizon_and_streak_threshold_are_required_constructor_inputs() -> None:
    with pytest.raises(TypeError):
        UnifiedRiskEngine(  # type: ignore[call-arg]
            tick_size=TICK,
            flow_factory=_dynamic_flow,
            consecutive_fills_threshold=3,
        )
    with pytest.raises(TypeError):
        UnifiedRiskEngine(  # type: ignore[call-arg]
            tick_size=TICK,
            flow_factory=_dynamic_flow,
            horizon_seconds=HORIZON,
        )
