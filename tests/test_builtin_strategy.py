"""Built-in strategy modules and the queue-aware orchestrator they wrap."""

from __future__ import annotations

import pytest

from polymarketbot.strategy.base import BookContext, DecisionContext
from polymarketbot.strategy.builtin import BUILTINS, QueueAwareOFI, Reactionary
from polymarketbot.strategy.orchestrator import (
    OrchestratorConfig,
    QueueSnapshot,
    QuoteOrchestrator,
    fill_probability,
)

# One quote per side inside a level of 100 shares. The front has nobody ahead
# (ratio 0.00); the back is a fresh join behind the whole level (ratio 1.00).
FRONT = QueueSnapshot(0.0, 100.0, 0.0, 100.0)
BACK = QueueSnapshot(0.0, 100.0, 1.0, 900.0)
BID_BACK = QueueSnapshot(1.0, 900.0, 0.0, 100.0)


def _orch(**kwargs) -> QuoteOrchestrator:
    return QuoteOrchestrator(OrchestratorConfig(enabled=True, **kwargs))


def _book() -> BookContext:
    return BookContext(
        condition_id="0xctx",
        asset_id="tok_yes",
        outcome="Yes",
        mid=0.50,
        best_bid=0.49,
        best_ask=0.51,
        tick_size=0.01,
        spread_cents=2.0,
    )


def _ctx(*, ofi: float | None = 2.0, bid_ratio: float = 0.0,
         ask_ratio: float = 1.0) -> DecisionContext:
    return DecisionContext(
        book=_book(),
        seconds_elapsed=0.0,
        inventory_shares=0.0,
        max_inventory_shares=100.0,
        bid_queue_ratio=bid_ratio,
        ask_queue_ratio=ask_ratio,
        bid_size_at_level=100.0,
        ask_size_at_level=900.0,
        ofi=ofi,
        quote_size=100.0,
    )


def test_disabled_by_default_quotes_both_sides():
    decision = QuoteOrchestrator().decide(ofi=5.0, queue=BACK)
    assert decision.bid.action == "QUOTE"
    assert decision.ask.action == "QUOTE"
    assert decision.bid.size_scale == pytest.approx(1.0)


def test_queue_aware_keeps_the_front_and_shrinks_the_back():
    orch = _orch(policy="queue_aware")
    front = orch.decide(ofi=+2.0, queue=FRONT)
    assert (front.bid.action, front.ask.action) == ("QUOTE", "QUOTE")

    back = orch.decide(ofi=+2.0, queue=BACK)
    assert (back.bid.action, back.ask.action) == ("QUOTE", "SHRINK")
    assert back.ask.size_scale == pytest.approx(0.25)
    assert back.bid.size_scale == pytest.approx(1.0)


def test_queue_aware_pull_action_and_bid_side():
    orch = _orch(policy="queue_aware", gate_action="pull")
    decision = orch.decide(ofi=-2.0, queue=BID_BACK)
    assert decision.bid.action == "PULL"
    assert decision.bid.size_scale == pytest.approx(0.0)
    assert decision.ask.action == "QUOTE"


def test_side_selection_gates_even_at_the_front():
    orch = _orch(policy="side_selection")
    decision = orch.decide(ofi=+2.0, queue=FRONT)
    assert decision.ask.action == "SHRINK"
    assert decision.bid.action == "QUOTE"


def test_reactionary_and_missing_inputs_keep_both_sides():
    assert _orch(policy="reactionary").decide(ofi=+9.0, queue=BACK).ask.action == "QUOTE"
    assert _orch(policy="queue_aware").decide(ofi=None, queue=BACK).ask.action == "QUOTE"
    assert _orch(policy="queue_aware").decide(ofi=+9.0, queue=None).ask.action == "QUOTE"
    assert _orch(policy="queue_aware", signal="binance").decide(
        ofi=+9.0, queue=BACK, binance_bps=None
    ).ask.action == "QUOTE"


def test_threshold_keeps_both_sides():
    orch = _orch(policy="queue_aware", ofi_threshold=1.0)
    assert orch.decide(ofi=1.0, queue=BACK).ask.action == "QUOTE"
    assert orch.decide(ofi=1.01, queue=BACK).ask.action == "SHRINK"


def test_fill_probability_decreases_with_queue_ratio():
    assert fill_probability(0.0, 100.0) > fill_probability(1.0, 100.0)
    assert 0.0 < fill_probability(0.6, 100.0) < 1.0


def test_config_validation_rejects_unknown_policy():
    with pytest.raises(ValueError):
        QuoteOrchestrator(OrchestratorConfig(enabled=True, policy="momentum"))


def test_kalshi_threshold_boundary():
    orch = QuoteOrchestrator(
        OrchestratorConfig(
            enabled=True,
            signal="kalshi",
            policy="queue_aware",
            kalshi_basis_threshold=2.0,
        )
    )
    assert orch.decide(None, BACK, kalshi_basis=2.0).ask.action == "QUOTE"
    assert orch.decide(None, BACK, kalshi_basis=-2.0).bid.action == "QUOTE"
    assert orch.decide(None, BACK, kalshi_basis=2.001).ask.action == "SHRINK"
    assert orch.decide(None, BID_BACK, kalshi_basis=-2.001).bid.action == "SHRINK"


def test_kalshi_signal_is_off_by_default():
    decision = QuoteOrchestrator().decide(None, BACK, kalshi_basis=9.0)
    assert (decision.bid.action, decision.ask.action) == ("QUOTE", "QUOTE")

    off = QuoteOrchestrator(OrchestratorConfig(signal="kalshi"))
    kept = off.decide(None, BACK, kalshi_basis=9.0)
    assert (kept.bid.action, kept.ask.action) == ("QUOTE", "QUOTE")


def test_reactionary_module_quotes_both_sides():
    decision = Reactionary().decide(_ctx())
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert Reactionary.name == "reactionary"


def test_queue_aware_ofi_shrinks_the_threatened_back_of_queue():
    decision = QueueAwareOFI().decide(_ctx(ofi=+2.0, ask_ratio=1.0))
    assert decision.bid == "QUOTE"
    assert decision.ask == "SHRINK"
    assert decision.ask_scale == pytest.approx(0.25)


def test_queue_aware_ofi_keeps_both_at_the_front():
    decision = QueueAwareOFI().decide(_ctx(ofi=+2.0, ask_ratio=0.0))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert decision.ask_scale == pytest.approx(1.0)


def test_queue_aware_ofi_without_signal_keeps_both_sides():
    decision = QueueAwareOFI().decide(_ctx(ofi=None))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")


def test_builtins_are_registered():
    assert BUILTINS["queue_aware_ofi"] is QueueAwareOFI
    assert BUILTINS["reactionary"] is Reactionary
