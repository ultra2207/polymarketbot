"""Queue-aware orchestrator: measured front/back split and paper-engine wiring."""

from pathlib import Path

import pytest

from polymarketbot.paper_trading import PaperTradingEngine
from polymarketbot.paper_trading.models import MarketConfig, TokenConfig
from polymarketbot.paper_trading.orchestrator import (
    OrchestratorConfig,
    QuoteOrchestrator,
    QueueSnapshot,
    fill_probability,
)

# One quote per side inside a level of 100 shares. The front has nobody ahead
# (ratio 0.00); the back is a fresh join behind the whole level (ratio 1.00).
FRONT = QueueSnapshot(0.0, 100.0, 0.0, 100.0)
BACK = QueueSnapshot(0.0, 100.0, 1.0, 900.0)
BID_BACK = QueueSnapshot(1.0, 900.0, 0.0, 100.0)


def _orch(**kwargs) -> QuoteOrchestrator:
    return QuoteOrchestrator(OrchestratorConfig(enabled=True, **kwargs))


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


def _engine(tmp_path: Path, orchestrator: QuoteOrchestrator | None) -> PaperTradingEngine:
    engine = PaperTradingEngine(
        initial_cash_usd=10000.0,
        base_spread_offset_cents=0.75,
        default_quote_size=100.0,
        ledger_path=tmp_path / "ledger.json",
        orchestrator=orchestrator,
    )
    engine.register_market(
        MarketConfig(
            condition_id="0xqueue",
            question="Queue test",
            rate_per_day=2000.0,
            rewards_min_size=50.0,
            rewards_max_spread=3.0,
            tokens=[TokenConfig(token_id="tok_yes", outcome="Yes", price=0.50)],
        )
    )
    return engine


def test_engine_shrinks_threatened_back_of_queue(tmp_path: Path):
    engine = _engine(tmp_path, _orch(policy="queue_aware"))
    # Quote sits at 0.51 with 900 shares resting there: a fresh join is last in
    # line (ratio 1.0). The rising bid makes the ask the threatened side.
    engine.update_orderbook(
        "tok_yes",
        [{"price": "0.48", "size": "100"}],
        [{"price": "0.51", "size": "900"}],
        0.01,
    )
    engine.update_orderbook(
        "tok_yes",
        [{"price": "0.49", "size": "100"}],
        [{"price": "0.51", "size": "900"}],
        0.01,
    )
    quote = engine.virtual_quotes["tok_yes"]
    assert quote["bid"] is not None and quote["bid"].size == pytest.approx(100.0)
    assert quote["ask"] is not None
    assert quote["ask"].size == pytest.approx(50.0)  # max(rewards_min_size, 25)
    assert engine.last_orchestrator_decision["tok_yes"].ask.action == "SHRINK"


def test_engine_keeps_both_at_queue_front(tmp_path: Path):
    engine = _engine(tmp_path, _orch(policy="queue_aware"))
    # The level starts thin and is topped up behind our quote, which moves it to
    # the front (10 / 100 = 0.10 < 0.60): both sides stay at full size.
    engine.update_orderbook(
        "tok_yes",
        [{"price": "0.48", "size": "100"}],
        [{"price": "0.51", "size": "10"}],
        0.01,
    )
    engine.update_orderbook(
        "tok_yes",
        [{"price": "0.49", "size": "100"}],
        [{"price": "0.51", "size": "100"}],
        0.01,
    )
    quote = engine.virtual_quotes["tok_yes"]
    assert quote["bid"].size == pytest.approx(100.0)
    assert quote["ask"].size == pytest.approx(100.0)
    decision = engine.last_orchestrator_decision["tok_yes"]
    assert decision.ask.action == "QUOTE"
    assert decision.ask_queue_ratio == pytest.approx(0.1)


def test_engine_pull_action_removes_threatened_side(tmp_path: Path):
    engine = _engine(tmp_path, _orch(policy="queue_aware", gate_action="pull"))
    engine.update_orderbook(
        "tok_yes",
        [{"price": "0.48", "size": "100"}],
        [{"price": "0.51", "size": "900"}],
        0.01,
    )
    engine.update_orderbook(
        "tok_yes",
        [{"price": "0.49", "size": "100"}],
        [{"price": "0.51", "size": "900"}],
        0.01,
    )
    quote = engine.virtual_quotes["tok_yes"]
    assert quote["ask"] is None
    assert quote["bid"] is not None and quote["bid"].size == pytest.approx(100.0)


class FakeKalshiMonitor:
    """A monitor stand-in: returns one fixed basis and records its scope."""

    def __init__(self, basis: float) -> None:
        self.basis_value = basis
        self.conditions: set[str] | None = None
        self.asked: list[tuple[str, float | None]] = []

    def set_conditions(self, conditions) -> None:
        self.conditions = set(conditions)

    def basis(self, condition_id: str, pm_yes_mid: float | None) -> float:
        self.asked.append((condition_id, pm_yes_mid))
        return self.basis_value


def _engine_with_kalshi(
    tmp_path: Path, monitor: FakeKalshiMonitor | None, *, gate_action: str = "shrink"
) -> PaperTradingEngine:
    engine = PaperTradingEngine(
        initial_cash_usd=10000.0,
        base_spread_offset_cents=0.75,
        default_quote_size=100.0,
        ledger_path=tmp_path / "ledger.json",
        orchestrator=_orch(
            policy="queue_aware", signal="kalshi", gate_action=gate_action
        ),
    )
    engine.register_market(
        MarketConfig(
            condition_id="0xkalshi",
            question="Kalshi basis test",
            rate_per_day=2000.0,
            rewards_min_size=50.0,
            rewards_max_spread=3.0,
            tokens=[TokenConfig(token_id="tok_yes", outcome="Yes", price=0.50)],
        )
    )
    if monitor is not None:
        engine.attach_kalshi_monitor(monitor)
    return engine


def test_engine_shrinks_ask_from_positive_kalshi_basis(tmp_path: Path):
    monitor = FakeKalshiMonitor(5.0)
    engine = _engine_with_kalshi(tmp_path, monitor)
    assert monitor.conditions == {"0xkalshi"}
    engine.update_orderbook(
        "tok_yes",
        [{"price": "0.48", "size": "100"}],
        [{"price": "0.51", "size": "900"}],
        0.01,
    )
    quote = engine.virtual_quotes["tok_yes"]
    assert quote["ask"].size == pytest.approx(50.0)  # max(rewards_min_size, 25)
    assert quote["bid"].size == pytest.approx(100.0)
    assert engine.last_orchestrator_decision["tok_yes"].ask.action == "SHRINK"
    assert monitor.asked and monitor.asked[0][0] == "0xkalshi"
    assert monitor.asked[0][1] == pytest.approx(0.495)


def test_engine_pulls_ask_from_positive_kalshi_basis(tmp_path: Path):
    engine = _engine_with_kalshi(
        tmp_path, FakeKalshiMonitor(5.0), gate_action="pull"
    )
    engine.update_orderbook(
        "tok_yes",
        [{"price": "0.48", "size": "100"}],
        [{"price": "0.51", "size": "900"}],
        0.01,
    )
    quote = engine.virtual_quotes["tok_yes"]
    assert quote["ask"] is None
    assert quote["bid"] is not None


def test_engine_without_monitor_keeps_both_sides_for_kalshi_signal(tmp_path: Path):
    engine = _engine_with_kalshi(tmp_path, None)
    engine.update_orderbook(
        "tok_yes",
        [{"price": "0.48", "size": "100"}],
        [{"price": "0.51", "size": "900"}],
        0.01,
    )
    quote = engine.virtual_quotes["tok_yes"]
    assert quote["bid"].size == pytest.approx(100.0)
    assert quote["ask"].size == pytest.approx(100.0)
    assert engine.last_orchestrator_decision == {}
