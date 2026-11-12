"""Paper microstructure gate wiring and refusal semantics.

The gate is the shared ``UnifiedRiskEngine`` built from the measured OFI fit;
these tests drive the paper adapter against a fake calibration and the real
``RiskManager`` so the adapter, not the model, is under test. No network and no
persisted fit are read: the calibration, fill table and markout table are
injected fixtures.
"""

from __future__ import annotations

import types

import pytest

from polymarketbot.paper import micro
from polymarketbot.risk.contracts import RiskAction, RiskDecision
from polymarketbot.risk.manager import RiskManager
from polymarketbot.risk.models import RiskConfig
from polymarketbot.risk.unified import UnifiedRiskEngine
from polymarketbot.strategy.reward.microstructure import MicrostructureEngine
from polymarketbot.strategy.venue.reads import Print

YES = "yes-token"
NO = "no-token"
OTHER = "other-token"
TICK_MAIN = 0.01
TICK_FINE = 0.001


class _Tables:
    """Measured-table stand-in; the ingestion tests never price a fill."""

    def p_fill(self, horizon_s: float, tick: float, min_flow_shares: float):
        return None

    def markout(self, horizon_s: float, tick: float):
        return None


def _calibration() -> types.SimpleNamespace:
    """A fake fitted calibration carrying the shape of ``RiskCalibration``.

    Production always loads the real ``load_risk_calibration()`` artifact; the
    coefficients here only exist to exercise the wiring and engine construction.
    """
    return types.SimpleNamespace(
        consecutive_fills_threshold=3,
        # Mirrors ``RiskCalibration.flow_factory``: the method returns a
        # factory, not a constructed engine (``UnifiedRiskEngine`` calls it per
        # token).
        flow_factory=lambda: (
            lambda: MicrostructureEngine(
                c_depth=1.5,
                z_score=3.0,
                v_ref=50_000.0,
                beta_vol=0.15,
                pull_multiplier=1.5,
                widen_offset_multiplier=0.0,
            )
        ),
    )


def _engine(*markets: types.SimpleNamespace) -> types.SimpleNamespace:
    manager = RiskManager(
        RiskConfig(
            total_bankroll_usd=300.0,
            allow_short=False,
            max_quote_size_shares=100.0,
        )
    )
    return types.SimpleNamespace(
        interval=10.0,
        risk_manager=manager,
        markets=list(markets),
        by_condition={
            market.condition_id: market
            for market in markets
            if getattr(market, "condition_id", None)
        },
    )


def _market(condition_id: str, tokens: tuple[str, ...], tick: float) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        condition_id=condition_id,
        token_ids=tokens,
        tick=tick,
    )


def _built_engine() -> tuple[types.SimpleNamespace, micro.PaperMicroRisk]:
    engine = _engine(
        _market("cond-main", (YES, NO), TICK_MAIN),
        _market("cond-fine", (OTHER,), TICK_FINE),
    )
    adapter = micro.build(
        engine,
        calibration=_calibration(),
        fill_horizon=_Tables(),
        markout=_Tables(),
    )
    micro.attach(engine, adapter)
    return engine, adapter


def test_absent_calibration_leaves_the_risk_manager_unwired() -> None:
    engine = _engine(_market("cond-main", (YES, NO), TICK_MAIN))

    adapter = micro.build(engine, calibration=None)
    micro.attach(engine, adapter)

    assert not adapter.built
    assert adapter.engines == {}
    assert "no fitted risk calibration" in adapter.reason
    assert engine.risk_manager._micro is None

    stats = micro.feed(
        engine,
        {YES: {"bids": [{"price": "0.49", "size": "10"}],
               "asks": [{"price": "0.51", "size": "10"}]}},
        [],
        now=1.0,
    )
    assert stats.books == 0
    assert "no fitted risk calibration" in stats.reason
    assert engine.risk_manager._micro is None


def test_built_calibration_attaches_a_gate_and_validate_quote_calls_it() -> None:
    engine, adapter = _built_engine()

    assert adapter.built
    assert engine.risk_manager._micro is adapter
    gate = adapter.gate_for_token(YES)
    assert isinstance(gate, UnifiedRiskEngine)
    assert gate.horizon_seconds == 10.0
    assert gate.consecutive_fills_threshold == 3
    assert gate.config is engine.risk_manager.config
    assert adapter.tick_sizes == (TICK_FINE, TICK_MAIN)

    calls: list[tuple] = []

    class _RecordingGate:
        def evaluate(self, *args):
            calls.append(args)
            return RiskDecision(
                action=RiskAction.MAINTAIN,
                side=str(args[1]).upper(),
                reason="recording gate",
                measured=True,
                ts=float(args[4]),
            )

    adapter.engines[TICK_MAIN] = _RecordingGate()
    allowed, reason, _size, price = engine.risk_manager.validate_quote(
        asset_id=YES,
        condition_id="cond-main",
        side="BUY",
        price=0.49,
        size=10.0,
        current_pos_shares=0.0,
        current_pos_cost=0.0,
        current_portfolio_inventory_usd=0.0,
        current_cash_usd=1_000.0,
        mid_price=0.50,
        tick_size=TICK_MAIN,
        timestamp=1.0,
    )

    assert allowed, reason
    assert price == 0.49
    assert calls == [(YES, "BUY", 0.49, 0.50, 1.0)]


def test_two_sided_book_and_dated_print_reach_the_shared_gate() -> None:
    engine, adapter = _built_engine()
    gate = adapter.gate_for_token(YES)
    gate.on_entry(
        "order-1",
        YES,
        "BUY",
        0.49,
        20.0,
        1.5,
        bids=[(0.49, 100.0)],
        asks=[(0.51, 80.0)],
    )

    stats = micro.feed(
        engine,
        {
            YES: {
                "bids": [{"price": "0.49", "size": "100"}],
                "asks": [{"price": "0.51", "size": "80"}],
            }
        },
        [
            Print(
                token_id=YES,
                condition_id="cond-main",
                side="SELL",
                price=0.49,
                size=10.0,
                ts=2.0,
            )
        ],
        now=2.0,
    )

    assert stats.books == 1
    assert stats.prints == 1
    assert gate.flow(YES) is not None
    progress = gate._queue.progress("order-1")
    assert progress is not None
    # The book refresh saw 100 resting against our 20 -> 80 ahead, then the
    # 10-share print at our price consumed 10 more.
    assert progress.ahead == 70.0
    assert progress.traded_at_level == 10.0


def test_one_sided_and_untimestamped_inputs_are_skipped() -> None:
    engine, adapter = _built_engine()
    gate = adapter.gate_for_token(YES)

    stats = micro.feed(
        engine,
        {
            YES: {"bids": [{"price": "0.49", "size": "100"}], "asks": []},
            NO: {"bids": [], "asks": [{"price": "0.51", "size": "80"}]},
            "unpriced-token": {},
        },
        [
            Print(
                token_id=YES,
                condition_id="cond-main",
                side="SELL",
                price=0.49,
                size=10.0,
                ts=0.0,
            ),
            types.SimpleNamespace(
                token_id=YES,
                side="SELL",
                price=0.49,
                size=10.0,
                ts=None,
            ),
            types.SimpleNamespace(
                token_id="",
                side="SELL",
                price=0.49,
                size=10.0,
                ts=2.0,
            ),
        ],
        now=2.0,
    )

    assert stats.books == 0
    assert stats.prints == 0
    assert stats.skipped_books == 3
    assert stats.skipped_prints == 3
    assert gate.flow(YES) is None
    assert gate.flow(NO) is None


def test_gate_waits_for_the_first_measured_market_tick() -> None:
    engine = _engine()  # the universe walker has not published a market yet

    adapter = micro.build(
        engine,
        calibration=_calibration(),
        fill_horizon=_Tables(),
        markout=_Tables(),
    )
    micro.attach(engine, adapter)

    assert not adapter.built
    assert engine.risk_manager._micro is None
    assert "no measured per-market tick" in adapter.reason

    engine.markets = [_market("cond-main", (YES, NO), TICK_MAIN)]
    engine.by_condition = {market.condition_id: market for market in engine.markets}
    stats = micro.feed(engine, {}, [], now=1.0)

    assert adapter.built
    assert engine.risk_manager._micro is adapter
    assert stats.reason == ""
    assert adapter.gate_for_token(YES) is not None


@pytest.mark.parametrize(
    "action", [RiskAction.PULL, RiskAction.SUPPRESS, RiskAction.BLOCK]
)
def test_pull_suppress_and_block_propagate_as_refusal(action: RiskAction) -> None:
    engine, adapter = _built_engine()
    reason = f"{action.value} from the test gate"

    class _RefusingGate:
        def evaluate(self, token_id, side, price, mid, ts):
            return RiskDecision(
                action=action,
                side=str(side).upper(),
                reason=reason,
                measured=True,
                ts=float(ts),
            )

    adapter.engines[TICK_MAIN] = _RefusingGate()
    allowed, returned_reason, size, price = engine.risk_manager.validate_quote(
        asset_id=YES,
        condition_id="cond-main",
        side="BUY",
        price=0.49,
        size=10.0,
        current_pos_shares=0.0,
        current_pos_cost=0.0,
        current_portfolio_inventory_usd=0.0,
        current_cash_usd=1_000.0,
        mid_price=0.50,
        tick_size=TICK_MAIN,
        timestamp=1.0,
    )

    assert not allowed
    assert returned_reason == reason
    assert size == 0.0
    assert price == 0.49
